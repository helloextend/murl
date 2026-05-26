"""CLI entry point for murl."""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import urllib.parse
from typing import Dict, Any, Tuple, Optional

# The MCP SDK logs a noisy warning when the server returns 404 on session
# DELETE (valid per MCP 2025-11-25 §Session Management — 404 means "session
# already gone").  Suppress it so users don't see spurious errors.
logging.getLogger("mcp.client.streamable_http").setLevel(logging.ERROR)

# Python 3.10 compatibility: ExceptionGroup was added in 3.11
try:
    ExceptionGroup
except NameError:
    from exceptiongroup import ExceptionGroup

import click
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from murl import __version__
import httpx as _httpx
from murl.token_store import get_credentials, save_credentials, clear_credentials, is_expired
from murl.auth import authorize, refresh_token, OAuthError, parse_www_authenticate

# Optional TOON format support (pip install mcp-curl[toon])
try:
    from toon import encode as toon_encode
except ImportError:
    toon_encode = None


# Error patterns for connection failures
# Note: These patterns are based on httpx/httpcore error messages and may need
# updates if the underlying library changes its error message formats
DNS_ERROR_PATTERNS = [
    "No address associated with hostname",
    "Name or service not known",
    "nodename nor servname provided",
]

CONNECTION_REFUSED_PATTERNS = [
    "Connection refused",
    "All connection attempts failed",
]


# Error code constants
class ErrorCode:
    SUCCESS = 0
    GENERAL_ERROR = 1
    INVALID_ARGUMENT = 2
    MCP_SERVER_ERROR = 100  # Used in JSON 'code' field, not as exit code


def output_error(error_type: str, message: str, exit_code: int,
                 suggestion: Optional[str] = None) -> None:
    """Output a structured JSON error to stderr and exit."""
    error_obj = {
        "error": error_type,
        "message": message,
        "code": exit_code
    }
    if suggestion:
        error_obj["suggestion"] = suggestion
    click.echo(json.dumps(error_obj), err=True)
    sys.exit(exit_code)


def parse_url(full_url: str) -> Tuple[str, str]:
    """Parse the full URL into base URL and virtual path.

    Returns:
        Tuple of (base_url, virtual_path)

    Raises:
        ValueError: If the URL doesn't contain a valid MCP path
    """
    pattern = r'/(tools|resources|prompts)(\/.*)?$'
    match = re.search(pattern, full_url)

    if not match:
        raise ValueError(
            "Invalid MCP URL. Must contain /tools, /resources, or /prompts"
        )

    virtual_path = match.group(0)
    base_url = full_url[:match.start()]

    return base_url, virtual_path


def parse_data_value(value: str) -> Any:
    """Parse a data value and coerce types."""
    if value.lower() == 'true':
        return True
    if value.lower() == 'false':
        return False

    try:
        if '.' not in value:
            return int(value)
        return float(value)
    except ValueError:
        pass

    return value


_MAX_FILE_READ_BYTES = 10 * 1024 * 1024  # 10 MB


def _read_json_source(source: str) -> dict:
    """Read a JSON object from stdin (@-) or a file (@path).

    Follows the curl convention: -d @- reads stdin, -d @file reads a file.
    The content must be a JSON object (dict), not an array or scalar.
    """
    path = source[1:]  # strip leading @
    try:
        if path == '-':
            content = sys.stdin.read(_MAX_FILE_READ_BYTES + 1)
        else:
            with open(path) as f:
                content = f.read(_MAX_FILE_READ_BYTES + 1)
    except OSError as e:
        raise ValueError(f"Cannot read {source}: {e}") from e

    if len(content) > _MAX_FILE_READ_BYTES:
        raise ValueError(
            f"Input from {source} exceeds 10 MB limit"
        )

    if not content.strip():
        raise ValueError(f"Empty input from {source}")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON from {source}: {e}") from e

    if not isinstance(parsed, dict):
        raise ValueError(
            f"JSON from {source} must be an object, not {type(parsed).__name__}"
        )
    return parsed


def parse_data_flags(data_flags: Tuple[str, ...]) -> Dict[str, Any]:
    """Parse -d/--data flags into a dictionary.

    Supports three formats (processed in order, later values override earlier):
        - key=value       — simple key-value pair with type coercion
        - {"key": "val"}  — inline JSON object, merged into result
        - @-              — read JSON object from stdin (curl convention)
        - @path           — read JSON object from a file
    """
    # Detect multiple @- (stdin) references before reading anything.
    # Stdin is a one-shot resource; the second read would always be empty.
    stdin_count = sum(1 for d in data_flags if d.strip() == '@-')
    if stdin_count > 1:
        raise ValueError(
            "stdin (@-) can only be used once (it is consumed on first read)"
        )

    result = {}

    for data in data_flags:
        stripped = data.strip()
        # Check for key=value first so that e.g. "email=@user" is not
        # mistaken for a @-source read.
        if '=' in stripped and not stripped.startswith('{') and not stripped.startswith('['):
            key, value = data.split('=', 1)
            result[key] = parse_data_value(value)
        elif stripped.startswith('@'):
            result.update(_read_json_source(stripped))
        elif stripped.startswith('{'):
            try:
                parsed = json.loads(data)
                if not isinstance(parsed, dict):
                    raise ValueError(f"JSON in -d flag must be an object, not {type(parsed).__name__}")
                result.update(parsed)
            except json.JSONDecodeError:
                raise ValueError(f"Invalid JSON in -d flag: {data}")
        elif stripped.startswith('['):
            raise ValueError(f"JSON arrays are not supported in -d flag. Use key=value or JSON objects.")
        else:
            raise ValueError(f"Invalid data format: {data}. Expected key=value or JSON")

    return result


def map_virtual_path_to_method(virtual_path: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Map a virtual path to an MCP JSON-RPC method and params."""
    parts = virtual_path.lstrip('/').split('/')

    if not parts or parts == ['']:
        raise ValueError("Invalid virtual path: empty path")

    category = parts[0]

    if category == 'tools':
        if len(parts) == 1:
            return 'tools/list', {}
        else:
            tool_name = parts[1]
            return 'tools/call', {
                'name': tool_name,
                'arguments': data
            }

    elif category == 'resources':
        if len(parts) == 1:
            return 'resources/list', {}
        else:
            resource_path = '/'.join(parts[1:])
            if not resource_path or resource_path == '':
                raise ValueError("Invalid resources path: path cannot be empty after /resources/")
            # If the path looks like a URI scheme (contains ://), use it as-is.
            # Otherwise, construct a file:// URI for backwards compatibility.
            if '://' in resource_path:
                uri = resource_path
            else:
                if not resource_path.startswith('/'):
                    resource_path = '/' + resource_path
                uri = f'file://{resource_path}'
            # Allow -d uri=... to override the path-derived URI
            if 'uri' in data:
                uri = data.pop('uri')
            return 'resources/read', {'uri': uri, **data}

    elif category == 'prompts':
        if len(parts) == 1:
            return 'prompts/list', {}
        else:
            prompt_name = parts[1]
            return 'prompts/get', {
                'name': prompt_name,
                'arguments': data
            }

    else:
        raise ValueError(f"Invalid MCP category: {category}")


def parse_headers(header_flags: Tuple[str, ...]) -> Dict[str, str]:
    """Parse -H/--header flags into a dictionary."""
    headers = {}

    for header in header_flags:
        if ':' not in header:
            raise ValueError(f"Invalid header format: {header}. Expected 'Key: Value'")

        key, value = header.split(':', 1)
        headers[key.strip()] = value.strip()

    return headers


# --- Text-envelope unwrap, paginate, and config discovery ---

def unwrap_text_envelope(result: Any) -> Tuple[Any, bool]:
    """Unwrap an MCP {type:"text", text:"<json>"} response when possible.

    Many MCP tool servers stringify their response payload and wrap it in a
    single text content block. The caller almost always wants the parsed inner
    value. Returns (unwrapped, did_unwrap). Idempotent: a result that is not a
    single-text envelope (or whose text isn't JSON) is returned unchanged.
    """
    if not isinstance(result, list) or len(result) != 1:
        return result, False
    item = result[0]
    if not isinstance(item, dict) or item.get("type") != "text":
        return result, False
    text = item.get("text")
    if not isinstance(text, str):
        return result, False
    try:
        return json.loads(text), True
    except (json.JSONDecodeError, ValueError):
        return result, False


# Auth-failure signals that appear inside an MCP tool response body (HTTP 200
# but the downstream provider rejected the credential). Matching these lets us
# exit with a structured AUTH_FAILED instead of forcing callers to substring-
# match on stdout.
_TOOL_AUTH_FAILURE_MARKERS = (
    "invalid_token",
    "AUTH_FAILED",
    "Unauthorized",
    "authentication failed",
)


def detect_tool_auth_failure(unwrapped: Any) -> Optional[str]:
    """Return a short failure description if the response body signals an auth
    failure, otherwise None.

    Inspects common shapes: a top-level {error: {...}} object, or any string
    field containing a known auth-failure marker. Bounded recursion keeps this
    cheap on large bodies.
    """
    def _walk(node: Any, depth: int = 0) -> Optional[str]:
        if depth > 6:
            return None
        if isinstance(node, dict):
            err = node.get("error")
            if isinstance(err, dict):
                code = str(err.get("code", ""))
                msg = str(err.get("message", ""))
                if any(m.lower() in (code + " " + msg).lower() for m in _TOOL_AUTH_FAILURE_MARKERS):
                    return msg or code or "auth failure"
            for v in node.values():
                hit = _walk(v, depth + 1)
                if hit:
                    return hit
        elif isinstance(node, list):
            for v in node[:50]:
                hit = _walk(v, depth + 1)
                if hit:
                    return hit
        elif isinstance(node, str):
            low = node.lower()
            for marker in _TOOL_AUTH_FAILURE_MARKERS:
                if marker.lower() in low:
                    return marker
        return None

    return _walk(unwrapped)


def find_mcp_config(start_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Walk up from start_dir (default: CWD) looking for a .mcp.json file.

    Returns the parsed config dict, or None if no file is found or it fails to
    parse. Stops at the filesystem root or the user's home directory, whichever
    comes first.
    """
    cwd = os.path.abspath(start_dir or os.getcwd())
    home = os.path.expanduser("~")
    while True:
        candidate = os.path.join(cwd, ".mcp.json")
        if os.path.isfile(candidate):
            try:
                with open(candidate) as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                return None
        parent = os.path.dirname(cwd)
        if parent == cwd or cwd == home:
            return None
        cwd = parent


def mcp_config_defaults(base_url: str, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract OAuth defaults for base_url from a parsed .mcp.json.

    Matches the server entry whose `url` exactly equals base_url (after URL
    decoding both, since AgentCore ARNs are percent-encoded in practice).
    Trailing slashes are normalized; query strings are not — the caller's URL
    must include the same query as the config entry, otherwise no match.
    Returns at most {client_id, callback_port} for filling missing flags.
    """
    if not config:
        return {}
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    target = urllib.parse.unquote(base_url).rstrip("/")
    for entry in servers.values():
        if not isinstance(entry, dict):
            continue
        entry_url = entry.get("url")
        if not isinstance(entry_url, str):
            continue
        decoded = urllib.parse.unquote(entry_url).rstrip("/")
        if decoded != target:
            continue
        oauth = entry.get("oauth") or {}
        out: Dict[str, Any] = {}
        if isinstance(oauth.get("clientId"), str):
            out["client_id"] = oauth["clientId"]
        cb = oauth.get("callbackPort")
        if isinstance(cb, int):
            out["callback_port"] = cb
        return out
    return {}


async def make_mcp_request(
    base_url: str,
    method: str,
    params: Dict[str, Any],
    headers: Dict[str, str],
    verbose: bool
) -> Any:
    """Make an MCP request using the official SDK."""
    # Validate required parameters before making connection
    if method == 'tools/call':
        if params.get('name') is None:
            raise ValueError("Missing required 'name' parameter for tools/call method")
    elif method == 'resources/read':
        if params.get('uri') is None:
            raise ValueError("Missing required 'uri' parameter for resources/read method")
    elif method == 'prompts/get':
        if params.get('name') is None:
            raise ValueError("Missing required 'name' parameter for prompts/get request")

    if verbose:
        click.echo("=== MCP Request ===", err=True)
        click.echo(f"Method: {method}", err=True)
        click.echo(f"Params: {json.dumps(params, indent=2)}", err=True)
        click.echo(f"URL: {base_url}", err=True)
        if headers:
            click.echo(f"Headers: {json.dumps(headers, indent=2)}", err=True)
        click.echo("", err=True)

    # Create httpx client with custom headers and reasonable timeout
    import httpx
    http_client = httpx.AsyncClient(
        headers=headers or {},
        timeout=httpx.Timeout(30.0, connect=10.0),
    )

    try:
        async with streamable_http_client(base_url, http_client=http_client) as (read, write, get_session_id):
            async with ClientSession(read, write) as session:
                init_result = await session.initialize()

                if verbose:
                    click.echo("=== MCP Initialization ===", err=True)
                    click.echo(f"Protocol Version: {init_result.protocolVersion}", err=True)
                    click.echo(f"Server: {init_result.serverInfo.name} {init_result.serverInfo.version}", err=True)
                    click.echo("", err=True)

                # MCP 2025-11-25 §Lifecycle/Operation: both parties MUST only use
                # capabilities that were successfully negotiated.
                category = method.split('/')[0]
                if category in ("tools", "resources", "prompts"):
                    if getattr(init_result.capabilities, category, None) is None:
                        available = [
                            cap for cap in ["tools", "resources", "prompts", "logging", "completions"]
                            if getattr(init_result.capabilities, cap, None) is not None
                        ]
                        raise ValueError(
                            f"Server does not support '{category}'. Supported: {', '.join(available)}"
                        )

                # List operations use cursor-based pagination (MCP 2025-11-25).
                # We collect all pages so the caller gets the complete result set.
                # Guard against buggy servers that repeat or cycle cursors.
                MAX_PAGES = 1000

                async def _collect_all(fetch_page, extract_items):
                    """Paginate a list endpoint, returning all items across pages."""
                    all_items = []
                    cursor = None
                    seen_cursors: set = set()
                    for _ in range(MAX_PAGES):
                        result = await fetch_page(cursor=cursor)
                        all_items.extend(extract_items(result))
                        if not result.nextCursor or result.nextCursor in seen_cursors:
                            break
                        seen_cursors.add(result.nextCursor)
                        cursor = result.nextCursor
                    return [item.model_dump(mode='json', exclude_none=True) for item in all_items]

                if method == 'tools/list':
                    return await _collect_all(session.list_tools, lambda r: r.tools)
                elif method == 'tools/call':
                    tool_name = params.get('name')
                    arguments = params.get('arguments', {})
                    result = await session.call_tool(tool_name, arguments)
                    return [content.model_dump(mode='json', exclude_none=True) for content in result.content]
                elif method == 'resources/list':
                    return await _collect_all(session.list_resources, lambda r: r.resources)
                elif method == 'resources/read':
                    uri = params.get('uri')
                    result = await session.read_resource(uri)
                    return [content.model_dump(mode='json', exclude_none=True) for content in result.contents]
                elif method == 'prompts/list':
                    return await _collect_all(session.list_prompts, lambda r: r.prompts)
                elif method == 'prompts/get':
                    prompt_name = params.get('name')
                    arguments = params.get('arguments', {})
                    result = await session.get_prompt(prompt_name, arguments)
                    return [message.model_dump(mode='json', exclude_none=True) for message in result.messages]
                else:
                    raise ValueError(f"Unsupported method: {method}")
    finally:
        if http_client is not None:
            await http_client.aclose()


def print_version(ctx, param, value):
    """Print detailed version information."""
    if not value or ctx.resilient_parsing:
        return

    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    try:
        import murl
        install_path = os.path.dirname(os.path.abspath(murl.__file__))
    except Exception:
        install_path = "unknown"

    click.echo(f"murl version {__version__}")
    click.echo(f"Python {python_version}")
    click.echo(f"Installation path: {install_path}")
    ctx.exit()


# The internal fork is the source of truth for this CLI. PyPI publishes a
# `mcp-curl` package that lags well behind the fork's master, so upgrading
# against PyPI silently downgrades fork users. Pin --upgrade to the fork.
UPGRADE_SOURCE = "mcp-curl[keychain,toon] @ git+https://github.com/helloextend/murl.git"


def run_upgrade(ctx, param, value):
    """Run the upgrade process."""
    if not value or ctx.resilient_parsing:
        return

    click.echo("Upgrading murl from helloextend/murl...")

    def show_error_and_exit(error_msg: str):
        click.echo(f"Error: {error_msg}", err=True)
        click.echo(f'Please try upgrading manually with: pip install --upgrade "{UPGRADE_SOURCE}"', err=True)
        ctx.exit(1)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", UPGRADE_SOURCE],
            capture_output=True,
            text=True,
            check=False,
            timeout=300
        )

        if result.returncode == 0:
            click.echo(result.stdout)
            click.echo("Upgrade complete!")
            ctx.exit(0)
        else:
            show_error_and_exit(f"Upgrade failed:\n{result.stderr}")
    except subprocess.TimeoutExpired:
        show_error_and_exit("Upgrade timed out after 5 minutes.")


def show_help(ctx, param, value):
    """Show help output."""
    if not value or ctx.resilient_parsing:
        return

    help_text = """USAGE:
  murl <url> [OPTIONS]

DESCRIPTION:
  MCP Curl - CLI for querying Model Context Protocol (MCP) servers.
  Outputs compact JSON to stdout, structured errors to stderr.

EXAMPLES:
  murl https://server.com/mcp/tools                         # List tools
  murl https://server.com/mcp/tools/echo -d message=hello   # Call tool
  murl https://server.com/mcp/tools/echo -d @params.json    # Args from file
  echo '{"message":"hi"}' | murl $S/tools/echo -d @-        # Args from stdin
  murl https://server.com/mcp/resources/path/to/file         # Read resource
  murl https://server.com/mcp/prompts/greeting -d name=Alice # Get prompt

PIPELINES:
  murl $S/tools/search -d q=foo | jq '{id:.[0].text}' | murl $S/tools/get -d @-

AUTHENTICATION:
  OAuth 2.0 with PKCE is built in (Dynamic Client Registration or pre-configured).
  Credentials auto-refresh. On 401, re-authenticates and retries once.

  murl --login https://server.com/mcp/tools    # First-time auth (opens browser)
  murl https://server.com/mcp/tools            # Uses stored token
  murl --no-auth https://server.com/mcp/tools  # Skip auth
  murl -H "Authorization: Bearer <tok>" <url>  # Manual token

  Pre-configured OAuth:
  murl --client-id ID --client-secret SECRET --callback-port 8080 --scope openid <url>

  Credentials: ~/.murl/credentials/<hash>.json

OPTIONS:
  -d, --data <val>             Request data: key=value, JSON, @file, @- (repeatable)
  -H, --header <Key: Value>    HTTP header (repeatable)
  -v, --verbose                Pretty-print output, show request debug info
  --format <json|toon>          Output format (default: json, toon for LLMs)
  --login                      Force OAuth re-authentication
  --no-auth                    Skip all authentication
  --client-id <id>             Pre-registered OAuth client ID (skip DCR)
  --client-secret <secret>     Pre-registered OAuth client secret (or MURL_CLIENT_SECRET env)
  --callback-port <port>       Fixed port for OAuth callback redirect URI
  --scope <scopes>             OAuth scope to request (e.g. "openid profile")
  --raw                        Skip auto-unwrap and cursor follow; emit the protocol response verbatim
  --max-pages <N>              Cap auto-paginated tools/call follow at N pages (default 1000)
  --no-mcp-config              Skip .mcp.json discovery for OAuth client defaults
  --version                    Version info
  --upgrade                    Self-upgrade via pip
  -h, --help                   This help

URL PATHS:
  /tools            List tools         /tools/<name>       Call tool
  /resources        List resources     /resources/<path>   Read resource
  /prompts          List prompts       /prompts/<name>     Get prompt

OUTPUT:
  stdout  Compact JSON (NDJSON for lists). Pretty-printed with -v. TOON with --format toon.
  stderr  Errors as {"error":"CODE","message":"...","code":N}
  exit    0=success  1=error  2=invalid args"""
    click.echo(help_text)
    ctx.exit()


def _probe_www_authenticate(base_url: str) -> Optional[str]:
    """Send an unauthenticated POST to the MCP endpoint to elicit a 401.

    Per MCP 2025-11-25 §Protected Resource Metadata Discovery Requirements,
    the server MUST include the resource_metadata URL in the WWW-Authenticate
    header on 401 responses.  For servers with complex URLs (encoded ARNs,
    query parameters), well-known URI construction from the URL alone may fail,
    so this probe ensures we get the server-provided discovery URL.

    Returns the WWW-Authenticate header value, or None if the probe fails.
    """
    try:
        resp = _httpx.post(
            base_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            timeout=10,
        )
        if resp.status_code == 401:
            return resp.headers.get("www-authenticate")
    except _httpx.HTTPError:
        pass
    return None


@click.command()
@click.argument('url', required=False)
@click.option('-d', '--data', 'data_flags', multiple=True,
              help='Request data. Format: key=value or JSON object')
@click.option('-H', '--header', 'header_flags', multiple=True,
              help='HTTP header. Format: "Key: Value"')
@click.option('-v', '--verbose', is_flag=True,
              help='Pretty-print output and show request debug info')
@click.option('--help', '-h', 'help_', is_flag=True, callback=show_help, expose_value=False, is_eager=True,
              help='Show help')
@click.option('--version', is_flag=True, callback=print_version, expose_value=False, is_eager=True,
              help='Show version')
@click.option('--upgrade', is_flag=True, callback=run_upgrade, expose_value=False, is_eager=True,
              help='Upgrade murl')
@click.option('--format', 'output_format', type=click.Choice(['json', 'toon']), default='json',
              help='Output format (default: json, toon = token-efficient for LLMs)')
@click.option('--login', is_flag=True, help='Force OAuth re-authentication')
@click.option('--no-auth', is_flag=True, help='Skip all authentication')
@click.option('--client-id', default=None, help='Pre-registered OAuth client ID (skips dynamic registration)')
@click.option('--client-secret', default=None, envvar='MURL_CLIENT_SECRET',
              help='Pre-registered OAuth client secret (or set MURL_CLIENT_SECRET)')
@click.option('--callback-port', default=None, type=int,
              help='Fixed port for OAuth callback (must match registered redirect URI)')
@click.option('--scope', default=None,
              help='OAuth scope to request (e.g. "openid profile")')
@click.option('--raw', is_flag=True,
              help='Skip auto-unwrap and auto-paginate; emit the protocol response verbatim')
@click.option('--max-pages', default=1000, type=int, show_default=True,
              help='Maximum pages to follow when a tools/call response carries nextCursor')
@click.option('--no-mcp-config', is_flag=True,
              help='Skip .mcp.json discovery for OAuth client defaults')
def main(url: Optional[str], data_flags: Tuple[str, ...], header_flags: Tuple[str, ...],
         verbose: bool, output_format: Optional[str], login: bool, no_auth: bool,
         client_id: Optional[str], client_secret: Optional[str],
         callback_port: Optional[int], scope: Optional[str],
         raw: bool, max_pages: int, no_mcp_config: bool):
    """murl - MCP Curl"""
    if url is None:
        output_error(
            error_type="MISSING_ARGUMENT",
            message="URL argument is required",
            exit_code=ErrorCode.INVALID_ARGUMENT,
            suggestion="Run: murl --help"
        )

    if output_format != 'json' and verbose:
        output_error(
            error_type="INVALID_ARGUMENT",
            message="--format and --verbose are mutually exclusive",
            exit_code=ErrorCode.INVALID_ARGUMENT,
            suggestion="Use --format for alternative output or -v for verbose JSON, not both"
        )

    try:
        base_url, virtual_path = parse_url(url)
        data = parse_data_flags(data_flags) if data_flags else {}
        method, params = map_virtual_path_to_method(virtual_path, data)
        headers = parse_headers(header_flags) if header_flags else {}

        # --- .mcp.json defaults ---
        # When the caller didn't pass --client-id / --callback-port, look for a
        # sibling .mcp.json (walking up from CWD) and pick them up from the
        # matching server entry. Mirrors Claude Code's discovery semantics.
        if not no_mcp_config:
            cfg_defaults = mcp_config_defaults(base_url, find_mcp_config())
            if client_id is None and "client_id" in cfg_defaults:
                client_id = cfg_defaults["client_id"]
                if verbose:
                    click.echo(f"Using client_id from .mcp.json: {client_id}", err=True)
            if callback_port is None and "callback_port" in cfg_defaults:
                callback_port = cfg_defaults["callback_port"]
                if verbose:
                    click.echo(f"Using callback_port from .mcp.json: {callback_port}", err=True)

        # --- Auth ---
        # Common kwargs for all authorize() calls
        auth_kwargs = {}
        if client_id:
            auth_kwargs["client_id"] = client_id
        if client_secret:
            auth_kwargs["client_secret"] = client_secret
        if callback_port:
            auth_kwargs["callback_port"] = callback_port
        if scope:
            auth_kwargs["scope"] = scope

        has_auth_header = any(k.lower() == 'authorization' for k in headers)
        if not no_auth and not has_auth_header:
            if login:
                clear_credentials(base_url)

            creds = get_credentials(base_url)

            if creds and not login:
                if is_expired(creds):
                    try:
                        creds = refresh_token(creds)
                        save_credentials(base_url, creds)
                    except OAuthError:
                        # Probe for WWW-Authenticate so discovery has the
                        # resource_metadata URL (needed for complex server URLs).
                        www_auth = _probe_www_authenticate(base_url)
                        creds = authorize(base_url, www_authenticate=www_auth, **auth_kwargs)
                        save_credentials(base_url, creds)
                headers["Authorization"] = f"Bearer {creds['access_token']}"
            elif login:
                # Probe for WWW-Authenticate so discovery has the
                # resource_metadata URL (needed for complex server URLs).
                www_auth = _probe_www_authenticate(base_url)
                creds = authorize(base_url, www_authenticate=www_auth, **auth_kwargs)
                save_credentials(base_url, creds)
                headers["Authorization"] = f"Bearer {creds['access_token']}"

        # --- Request with 401 retry ---
        try:
            result = asyncio.run(make_mcp_request(base_url, method, params, headers, verbose))
        except (Exception, ExceptionGroup) as req_err:
            # Extract WWW-Authenticate header and 401 status from the exception chain.
            # The MCP SDK raises httpx.HTTPStatusError (possibly wrapped in ExceptionGroup)
            # which carries the full HTTP response including headers.
            www_auth_header = None
            is_401 = False
            is_403 = False

            exceptions = req_err.exceptions if isinstance(req_err, ExceptionGroup) else [req_err]
            for exc in exceptions:
                # httpx.HTTPStatusError has a .response attribute
                if hasattr(exc, 'response') and hasattr(exc.response, 'status_code'):
                    if exc.response.status_code == 401:
                        is_401 = True
                        www_auth_header = exc.response.headers.get("www-authenticate")
                        break
                    if exc.response.status_code == 403:
                        is_403 = True
                        www_auth_header = exc.response.headers.get("www-authenticate")
                        break
                # Fallback: string matching for non-httpx exceptions
                exc_str = str(exc)
                if "401" in exc_str or "Unauthorized" in exc_str:
                    is_401 = True
                    break

            # MCP 2025-11-25 §Authorization Flow Steps: on 401, discover
            # metadata via WWW-Authenticate, then run full OAuth flow.
            if not no_auth and is_401:
                if verbose:
                    click.echo("Received 401 — initiating OAuth flow...", err=True)
                    if www_auth_header:
                        click.echo(f"WWW-Authenticate: {www_auth_header}", err=True)
                creds = authorize(base_url, www_authenticate=www_auth_header, **auth_kwargs)
                save_credentials(base_url, creds)
                headers["Authorization"] = f"Bearer {creds['access_token']}"
                result = asyncio.run(make_mcp_request(base_url, method, params, headers, verbose))
            # MCP 2025-11-25 §Scope Challenge Handling: on 403 with explicit
            # insufficient_scope error, parse required scopes and re-authorize.
            elif not no_auth and is_403:
                www_params = parse_www_authenticate(www_auth_header) if www_auth_header else {}
                if www_params.get("error") != "insufficient_scope" or not www_params.get("scope"):
                    raise
                if verbose:
                    click.echo("Received 403 insufficient_scope — re-authorizing...", err=True)
                    click.echo(f"WWW-Authenticate: {www_auth_header}", err=True)
                # Merge scope override into auth_kwargs to avoid passing scope twice.
                reauth_kwargs = {**auth_kwargs, "scope": www_params["scope"]}
                creds = authorize(base_url, www_authenticate=www_auth_header, **reauth_kwargs)
                save_credentials(base_url, creds)
                headers["Authorization"] = f"Bearer {creds['access_token']}"
                result = asyncio.run(make_mcp_request(base_url, method, params, headers, verbose))
            else:
                raise

        # --- Post-process: unwrap, follow cursors, detect tool-level auth failure ---
        # By default, tools/call responses are unwrapped from their {type:"text"}
        # envelope and any nextCursor is followed transparently. --raw opts out
        # of both, returning the raw protocol response. List endpoints and
        # resources/read have their own handling upstream.
        if not raw and method == 'tools/call':
            unwrapped, did_unwrap = unwrap_text_envelope(result)

            if did_unwrap and isinstance(unwrapped, dict) and unwrapped.get("hasMore") and unwrapped.get("nextCursor"):
                # Locate the single array field carrying page items. The MCP
                # convention is {<items_field>: [...], hasMore, nextCursor}.
                array_keys = [k for k, v in unwrapped.items() if isinstance(v, list)]
                if len(array_keys) == 1:
                    items_key = array_keys[0]
                    all_items = list(unwrapped[items_key])
                    seen_cursors: set = set()
                    cursor = unwrapped.get("nextCursor")
                    pages = 1
                    while (unwrapped.get("hasMore") and cursor
                           and cursor not in seen_cursors and pages < max_pages):
                        seen_cursors.add(cursor)
                        next_params = {
                            'name': params['name'],
                            'arguments': {**params.get('arguments', {}), 'cursor': cursor},
                        }
                        # Strip page-size hints on cursor calls — many MCP
                        # tools reject them when a cursor is supplied because
                        # the cursor already encodes that context.
                        for k in ('pageSize', 'perPage', 'status'):
                            next_params['arguments'].pop(k, None)
                        next_raw = asyncio.run(make_mcp_request(base_url, method, next_params, headers, verbose))
                        next_unwrapped, _ = unwrap_text_envelope(next_raw)
                        # Detect a mid-pagination auth failure before deciding
                        # whether to break: an error body looks structurally
                        # similar to an "end of pagination" body (no items_key),
                        # and silently breaking would return partial results
                        # with no signal that auth expired mid-loop.
                        page_failure = detect_tool_auth_failure(next_unwrapped)
                        if page_failure:
                            output_error(
                                error_type="AUTH_FAILED",
                                message=f"Tool reported authentication failure during pagination at page {pages + 1}: {page_failure}",
                                exit_code=ErrorCode.GENERAL_ERROR,
                                suggestion="Re-authenticate with `murl --login <url>` and retry."
                            )
                            return
                        if not isinstance(next_unwrapped, dict) or items_key not in next_unwrapped:
                            break
                        all_items.extend(next_unwrapped.get(items_key, []))
                        cursor = next_unwrapped.get("nextCursor")
                        unwrapped = next_unwrapped
                        pages += 1
                    # Emit the flat items list; the NDJSON output branch below
                    # will write one item per line.
                    result = all_items
                else:
                    # Ambiguous page shape — surface the unwrapped envelope as-is.
                    result = unwrapped
            elif did_unwrap:
                result = unwrapped

            failure = detect_tool_auth_failure(result)
            if failure:
                output_error(
                    error_type="AUTH_FAILED",
                    message=f"Tool reported authentication failure: {failure}",
                    exit_code=ErrorCode.GENERAL_ERROR,
                    suggestion="Re-authenticate with `murl --login <url>` and retry."
                )
                return

        # --- Output ---
        if verbose:
            click.echo(json.dumps(result, indent=2))
        elif output_format == 'toon':
            if toon_encode is None:
                output_error(
                    error_type="MISSING_DEPENDENCY",
                    message="python-toon package is required for --format toon",
                    exit_code=ErrorCode.GENERAL_ERROR,
                    suggestion="Install it with: pip install mcp-curl[toon]"
                )
                return
            click.echo(toon_encode(result))
        elif isinstance(result, list):
            for item in result:
                click.echo(json.dumps(item, separators=(',', ':')))
        else:
            click.echo(json.dumps(result, separators=(',', ':')))

    except ValueError as e:
        output_error(
            error_type="INVALID_ARGUMENT",
            message=str(e),
            exit_code=ErrorCode.INVALID_ARGUMENT
        )
    except ConnectionError as e:
        output_error(
            error_type="CONNECTION_ERROR",
            message=f"Failed to connect: {e}",
            exit_code=ErrorCode.GENERAL_ERROR
        )
    except TimeoutError as e:
        output_error(
            error_type="TIMEOUT",
            message=f"Request timeout: {e}",
            exit_code=ErrorCode.GENERAL_ERROR
        )
    except ExceptionGroup as eg:
        if eg.exceptions:
            exc = eg.exceptions[0]
            exc_type = type(exc).__name__
            exc_msg = str(exc)

            parsed_url = urllib.parse.urlparse(base_url)
            hostname = parsed_url.hostname
            if not hostname:
                netloc = parsed_url.netloc
                if netloc:
                    host_port = netloc.rsplit("@", 1)[-1]
                    hostname = host_port.split(":", 1)[0] or "unknown host"
                else:
                    hostname = "unknown host"

            if exc_type == "ConnectError":
                if any(p in exc_msg for p in DNS_ERROR_PATTERNS):
                    error_type = "DNS_RESOLUTION_FAILED"
                    msg = f"DNS resolution failed for host: {hostname}"
                elif any(p in exc_msg for p in CONNECTION_REFUSED_PATTERNS):
                    error_type = "CONNECTION_REFUSED"
                    msg = f"Connection refused by host: {hostname}"
                else:
                    error_type = "CONNECTION_ERROR"
                    msg = exc_msg
            elif exc_type == "TimeoutError" or "Timeout" in exc_msg:
                error_type = "TIMEOUT"
                msg = "Request timeout"
            else:
                error_type = exc_type.upper()
                msg = exc_msg

            error_obj = {
                "error": error_type,
                "message": msg,
                "code": ErrorCode.GENERAL_ERROR
            }
            click.echo(json.dumps(error_obj), err=True)
            sys.exit(ErrorCode.GENERAL_ERROR)
        else:
            output_error(
                error_type="EXCEPTION_GROUP",
                message=str(eg),
                exit_code=ErrorCode.GENERAL_ERROR
            )
    except OAuthError as e:
        # The OAuth flow failed (redirect-uri mismatch, port conflict, expired
        # token with no refresh, discovery failure, etc.). Surface a structured
        # AUTH_FAILED rather than the generic ERROR fall-through, and prefer the
        # failure-specific suggestion the raise site attached; otherwise fall
        # back to the most common remedy — re-authenticating.
        output_error(
            error_type="AUTH_FAILED",
            message=str(e),
            exit_code=ErrorCode.GENERAL_ERROR,
            suggestion=e.suggestion or "Re-authenticate with `murl --login <url>`.",
        )
    except Exception as e:
        error_msg = str(e)

        if "ValidationError" in error_msg:
            error_obj = {
                "error": "VALIDATION_ERROR",
                "message": f"Invalid response from server: {error_msg}",
                "code": ErrorCode.MCP_SERVER_ERROR
            }
            click.echo(json.dumps(error_obj), err=True)
            sys.exit(ErrorCode.GENERAL_ERROR)
        elif "ConnectError" in error_msg or "Connection" in error_msg:
            output_error(
                error_type="CONNECTION_ERROR",
                message="Failed to connect",
                exit_code=ErrorCode.GENERAL_ERROR
            )
        elif "Timeout" in error_msg:
            output_error(
                error_type="TIMEOUT",
                message="Request timeout",
                exit_code=ErrorCode.GENERAL_ERROR
            )
        else:
            output_error(
                error_type="ERROR",
                message=error_msg,
                exit_code=ErrorCode.GENERAL_ERROR
            )


if __name__ == "__main__":
    main()
