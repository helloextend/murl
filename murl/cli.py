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


def parse_data_flags(data_flags: Tuple[str, ...]) -> Dict[str, Any]:
    """Parse -d/--data flags into a dictionary.

    Note:
        JSON objects (starting with '{') are merged into the result.
        JSON arrays (starting with '[') are not supported as they don't
        represent key-value pairs needed for MCP arguments.
    """
    result = {}

    for data in data_flags:
        stripped = data.strip()
        if stripped.startswith('{'):
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
            if '=' not in data:
                raise ValueError(f"Invalid data format: {data}. Expected key=value or JSON")

            key, value = data.split('=', 1)
            result[key] = parse_data_value(value)

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

                if method == 'tools/list':
                    all_items = []
                    cursor = None
                    seen_cursors: set = set()
                    for _ in range(MAX_PAGES):
                        result = await session.list_tools(cursor=cursor)
                        all_items.extend(result.tools)
                        if not result.nextCursor or result.nextCursor in seen_cursors:
                            break
                        seen_cursors.add(result.nextCursor)
                        cursor = result.nextCursor
                    return [t.model_dump(mode='json', exclude_none=True) for t in all_items]
                elif method == 'tools/call':
                    tool_name = params.get('name')
                    arguments = params.get('arguments', {})
                    result = await session.call_tool(tool_name, arguments)
                    return [content.model_dump(mode='json', exclude_none=True) for content in result.content]
                elif method == 'resources/list':
                    all_items = []
                    cursor = None
                    seen_cursors = set()
                    for _ in range(MAX_PAGES):
                        result = await session.list_resources(cursor=cursor)
                        all_items.extend(result.resources)
                        if not result.nextCursor or result.nextCursor in seen_cursors:
                            break
                        seen_cursors.add(result.nextCursor)
                        cursor = result.nextCursor
                    return [r.model_dump(mode='json', exclude_none=True) for r in all_items]
                elif method == 'resources/read':
                    uri = params.get('uri')
                    result = await session.read_resource(uri)
                    return [content.model_dump(mode='json', exclude_none=True) for content in result.contents]
                elif method == 'prompts/list':
                    all_items = []
                    cursor = None
                    seen_cursors = set()
                    for _ in range(MAX_PAGES):
                        result = await session.list_prompts(cursor=cursor)
                        all_items.extend(result.prompts)
                        if not result.nextCursor or result.nextCursor in seen_cursors:
                            break
                        seen_cursors.add(result.nextCursor)
                        cursor = result.nextCursor
                    return [p.model_dump(mode='json', exclude_none=True) for p in all_items]
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


def run_upgrade(ctx, param, value):
    """Run the upgrade process."""
    if not value or ctx.resilient_parsing:
        return

    click.echo("Upgrading murl...")

    def show_error_and_exit(error_msg: str):
        click.echo(f"Error: {error_msg}", err=True)
        click.echo("Please try upgrading manually with: pip install --upgrade mcp-curl", err=True)
        ctx.exit(1)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "mcp-curl"],
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
  murl https://server.com/mcp/resources/path/to/file         # Read resource
  murl https://server.com/mcp/prompts/greeting -d name=Alice # Get prompt

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
  -d, --data <key=value|JSON>  Request data (repeatable)
  -H, --header <Key: Value>    HTTP header (repeatable)
  -v, --verbose                Pretty-print output, show request debug info
  --format <json|toon>          Output format (default: json, toon for LLMs)
  --login                      Force OAuth re-authentication
  --no-auth                    Skip all authentication
  --client-id <id>             Pre-registered OAuth client ID (skip DCR)
  --client-secret <secret>     Pre-registered OAuth client secret
  --callback-port <port>       Fixed port for OAuth callback redirect URI
  --scope <scopes>             OAuth scope to request (e.g. "openid profile")
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
@click.option('--client-secret', default=None, help='Pre-registered OAuth client secret')
@click.option('--callback-port', default=None, type=int,
              help='Fixed port for OAuth callback (must match registered redirect URI)')
@click.option('--scope', default=None,
              help='OAuth scope to request (e.g. "openid profile")')
def main(url: Optional[str], data_flags: Tuple[str, ...], header_flags: Tuple[str, ...],
         verbose: bool, output_format: Optional[str], login: bool, no_auth: bool,
         client_id: Optional[str], client_secret: Optional[str],
         callback_port: Optional[int], scope: Optional[str]):
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
