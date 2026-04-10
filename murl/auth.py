"""OAuth 2.0 authorization for MCP (2025-11-25 spec).

Implements:
- Protected Resource Metadata discovery (RFC 9728)
- Authorization Server Metadata with OIDC fallback (RFC 8414)
- Client ID Metadata Documents (draft-ietf-oauth-client-id-metadata-document-00)
- Dynamic Client Registration (RFC 7591) as fallback
- PKCE with S256 (OAuth 2.1)
- Resource Indicators (RFC 8707)
"""

import base64
import hashlib
import html
import json
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

import httpx


CALLBACK_TIMEOUT = 60  # seconds to wait for browser callback

# Client ID Metadata Document (draft-ietf-oauth-client-id-metadata-document-00)
# This URL serves as both the client_id and the location of the metadata document.
# The document must be hosted at this exact URL (e.g., via GitHub Pages).
CLIENT_ID_METADATA_URL = "https://turlockmike.github.io/murl/oauth/client-metadata.json"

# Fixed callback port for CIMD — the metadata document's redirect_uris must match.
CIMD_CALLBACK_PORT = 19362


class OAuthError(Exception):
    """Raised when an OAuth operation fails."""


# ---------------------------------------------------------------------------
# WWW-Authenticate header parsing
# ---------------------------------------------------------------------------

def parse_www_authenticate(header_value: str) -> dict:
    """Parse a WWW-Authenticate Bearer header into a dict of parameters.

    Handles: Bearer resource_metadata="...", scope="...", error="..."
    Returns dict with keys like resource_metadata, scope, error, error_description.
    """
    result = {}
    # Strip the "Bearer" scheme prefix if present
    value = header_value.strip()
    if value.lower().startswith("bearer"):
        value = value[len("bearer"):].strip()
        # Handle case where there's nothing after Bearer (just "Bearer")
        if not value:
            return result

    # Parse key="value" pairs (values may or may not be quoted)
    for match in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', value):
        result[match.group(1)] = match.group(2)
    # Also handle unquoted values
    for match in re.finditer(r'(\w+)\s*=\s*([^",\s]+)', value):
        key = match.group(1)
        if key not in result:  # quoted takes precedence
            result[key] = match.group(2)

    return result


# ---------------------------------------------------------------------------
# Discovery: Protected Resource Metadata (RFC 9728)
# ---------------------------------------------------------------------------

def discover_resource_metadata(server_url: str, resource_metadata_url: Optional[str] = None) -> dict:
    """Fetch Protected Resource Metadata per RFC 9728.

    If resource_metadata_url is provided (from WWW-Authenticate header), use it directly.
    Otherwise, try well-known URIs:
      1. /.well-known/oauth-protected-resource/<path> (path-aware)
      2. /.well-known/oauth-protected-resource (root)

    Returns the metadata dict, which must contain 'authorization_servers'.
    Raises OAuthError if discovery fails.
    """
    urls_to_try = []

    if resource_metadata_url:
        urls_to_try.append(resource_metadata_url)
    else:
        parsed = urllib.parse.urlparse(server_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")

        if path:
            urls_to_try.append(f"{base}/.well-known/oauth-protected-resource{path}")
        urls_to_try.append(f"{base}/.well-known/oauth-protected-resource")

    for url in urls_to_try:
        try:
            resp = httpx.get(url, follow_redirects=True, timeout=10)
        except httpx.HTTPError:
            continue

        if resp.status_code == 200:
            try:
                meta = resp.json()
            except json.JSONDecodeError:
                continue
            if isinstance(meta, dict) and "authorization_servers" in meta:
                return meta
            # Valid JSON but missing required field — keep trying
            continue

    raise OAuthError(
        "Could not discover Protected Resource Metadata (RFC 9728). "
        "The server may not support OAuth, or the well-known endpoint is unreachable."
    )


# ---------------------------------------------------------------------------
# Discovery: Authorization Server Metadata (RFC 8414 + OIDC)
# ---------------------------------------------------------------------------

def discover_auth_server_metadata(issuer_url: str) -> dict:
    """Fetch Authorization Server Metadata with OIDC Discovery fallback.

    Per MCP 2025-11-25 spec, clients MUST try:
      For issuer with path (e.g. https://auth.example.com/tenant1):
        1. /.well-known/oauth-authorization-server/tenant1  (RFC 8414)
        2. /.well-known/openid-configuration/tenant1        (OIDC path insertion)
        3. /tenant1/.well-known/openid-configuration        (OIDC path append)

      For issuer without path (e.g. https://auth.example.com):
        1. /.well-known/oauth-authorization-server           (RFC 8414)
        2. /.well-known/openid-configuration                 (OIDC)
    """
    parsed = urllib.parse.urlparse(issuer_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")

    urls_to_try = []
    if path:
        urls_to_try.append(f"{base}/.well-known/oauth-authorization-server{path}")
        urls_to_try.append(f"{base}/.well-known/openid-configuration{path}")
        urls_to_try.append(f"{base}{path}/.well-known/openid-configuration")
    else:
        urls_to_try.append(f"{base}/.well-known/oauth-authorization-server")
        urls_to_try.append(f"{base}/.well-known/openid-configuration")

    for url in urls_to_try:
        try:
            resp = httpx.get(url, follow_redirects=True, timeout=10)
        except httpx.HTTPError:
            continue

        if resp.status_code == 200:
            try:
                meta = resp.json()
            except json.JSONDecodeError:
                continue
            if isinstance(meta, dict) and "token_endpoint" in meta:
                return meta

    raise OAuthError(
        f"Could not discover Authorization Server Metadata for {issuer_url}. "
        "Tried OAuth 2.0 (RFC 8414) and OpenID Connect discovery endpoints."
    )


# ---------------------------------------------------------------------------
# Legacy discovery (kept for backwards compat with servers that don't
# implement RFC 9728 yet)
# ---------------------------------------------------------------------------

def _auth_base_url(server_url: str) -> str:
    """Extract scheme + host from server URL."""
    parsed = urllib.parse.urlparse(server_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def discover_metadata(server_url: str) -> dict:
    """Legacy: fetch OAuth 2.0 Authorization Server Metadata directly.

    Tries /.well-known/oauth-authorization-server first, with OIDC fallback.
    Falls back to sensible defaults if both 404.
    """
    parsed = urllib.parse.urlparse(server_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")

    # Try RFC 8414 then OIDC, path-aware
    urls_to_try = []
    if path:
        urls_to_try.append(f"{base}/.well-known/oauth-authorization-server{path}")
        urls_to_try.append(f"{base}/.well-known/openid-configuration{path}")
        urls_to_try.append(f"{base}{path}/.well-known/openid-configuration")
    else:
        urls_to_try.append(f"{base}/.well-known/oauth-authorization-server")
        urls_to_try.append(f"{base}/.well-known/openid-configuration")

    for url in urls_to_try:
        try:
            resp = httpx.get(url, follow_redirects=True, timeout=10)
        except httpx.HTTPError:
            continue

        if resp.status_code == 200:
            try:
                return resp.json()
            except json.JSONDecodeError as exc:
                raise OAuthError("Invalid JSON in OAuth metadata response") from exc

    # All attempts failed — fall back to defaults
    return {
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
    }


def register_client(registration_endpoint: str, redirect_uri: str) -> dict:
    """Dynamic Client Registration (RFC 7591).

    Returns dict with at least ``client_id`` and optionally ``client_secret``.
    """
    payload = {
        "client_name": "murl",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    resp = httpx.post(
        registration_endpoint,
        json=payload,
        timeout=10,
    )
    if resp.status_code not in (200, 201):
        raise OAuthError(
            f"Client registration failed ({resp.status_code}): {resp.text}"
        )
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise OAuthError(
            f"Client registration returned invalid JSON "
            f"({resp.status_code}): {resp.text}"
        ) from exc


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    """Tiny HTTP handler that captures the OAuth callback."""

    # Shared across instances via class attrs set before serve_forever()
    auth_code: Optional[str] = None
    auth_error: Optional[str] = None
    expected_state: Optional[str] = None

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        # Validate state
        state = params.get("state", [None])[0]
        if state != self.expected_state:
            _CallbackHandler.auth_error = "State mismatch"
            self._respond("Authorization failed: state mismatch.")
            return

        error = params.get("error", [None])[0]
        if error:
            desc = params.get("error_description", [error])[0]
            _CallbackHandler.auth_error = desc
            self._respond(f"Authorization failed: {desc}")
            return

        code = params.get("code", [None])[0]
        if not code:
            _CallbackHandler.auth_error = "No authorization code received"
            self._respond("Authorization failed: no code received.")
            return

        _CallbackHandler.auth_code = code
        self._respond("Authorization successful! You can close this tab.")

    def _respond(self, body: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        safe_body = html.escape(body)
        page = (
            "<html><body style='font-family:system-ui;text-align:center;"
            f"padding:3em'><h2>{safe_body}</h2></body></html>"
        )
        self.wfile.write(page.encode())

    def log_message(self, format, *args):
        """Suppress default stderr logging."""
        pass


def _run_callback_server(port: int, state: str, timeout: float) -> str:
    """Start a local server, wait for the callback, return the auth code."""
    _CallbackHandler.auth_code = None
    _CallbackHandler.auth_error = None
    _CallbackHandler.expected_state = state

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = timeout

    # Handle a single request (the callback)
    server.handle_request()
    server.server_close()

    if _CallbackHandler.auth_error:
        raise OAuthError(_CallbackHandler.auth_error)
    if not _CallbackHandler.auth_code:
        raise OAuthError("Timed out waiting for authorization callback")
    return _CallbackHandler.auth_code


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _generate_pkce() -> tuple:
    """Return (code_verifier, code_challenge)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _canonical_resource_uri(server_url: str) -> str:
    """Build the canonical resource URI for RFC 8707 resource parameter.

    Per the spec: scheme + authority + path, no fragment, no trailing slash ambiguity.
    """
    parsed = urllib.parse.urlparse(server_url)
    # Reconstruct without fragment or query
    return urllib.parse.urlunparse((
        parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""
    ))


def authorize(server_url: str, www_authenticate: Optional[str] = None,
              scope: Optional[str] = None) -> dict:
    """Run the full OAuth flow and return credential dict.

    Follows MCP 2025-11-25 authorization spec:
      1. Protected Resource Metadata discovery (RFC 9728)
      2. Authorization Server Metadata discovery (RFC 8414 + OIDC)
      3. Dynamic Client Registration (RFC 7591)
      4. PKCE browser auth with resource parameter (RFC 8707)
      5. Token exchange with resource parameter

    Args:
        server_url: The MCP server base URL.
        www_authenticate: Optional WWW-Authenticate header value from a 401 response.
        scope: Optional scope string to request (from WWW-Authenticate or resource metadata).

    Returns dict with: client_id, client_secret, access_token, refresh_token,
    expires_at, token_endpoint, registration_endpoint, server_url, resource_uri.
    """
    import click

    resource_uri = _canonical_resource_uri(server_url)

    # --- Step 1: Discovery ---
    # Try the full RFC 9728 discovery chain first. If that fails (server doesn't
    # implement Protected Resource Metadata), fall back to legacy direct discovery.
    click.echo("Discovering OAuth metadata...", err=True)

    www_auth_params = parse_www_authenticate(www_authenticate) if www_authenticate else {}
    resource_metadata_url = www_auth_params.get("resource_metadata")

    # Scope priority: explicit param > WWW-Authenticate > resource metadata
    if not scope and www_auth_params.get("scope"):
        scope = www_auth_params["scope"]

    auth_server_meta = None
    try:
        # RFC 9728: fetch Protected Resource Metadata
        resource_meta = discover_resource_metadata(server_url, resource_metadata_url)

        # Extract scope from resource metadata if not already set
        if not scope and resource_meta.get("scopes_supported"):
            scope = " ".join(resource_meta["scopes_supported"])

        # Get the first authorization server
        auth_servers = resource_meta.get("authorization_servers", [])
        if not auth_servers:
            raise OAuthError("Protected Resource Metadata has empty authorization_servers")

        issuer_url = auth_servers[0]

        # Fetch auth server metadata (RFC 8414 + OIDC fallback)
        auth_server_meta = discover_auth_server_metadata(issuer_url)
    except OAuthError:
        # Fallback: legacy direct discovery (for servers not yet on 2025-11-25)
        auth_server_meta = discover_metadata(server_url)

    meta = auth_server_meta
    auth_endpoint = meta["authorization_endpoint"]
    token_endpoint = meta["token_endpoint"]
    reg_endpoint = meta.get("registration_endpoint")

    # Verify PKCE support (MCP 2025-11-25 spec requirement)
    challenge_methods = meta.get("code_challenge_methods_supported")
    if challenge_methods is None:
        raise OAuthError(
            "Authorization server does not advertise PKCE support "
            "(code_challenge_methods_supported). Cannot proceed securely."
        )
    if "S256" not in challenge_methods:
        raise OAuthError(
            "Authorization server does not support S256 PKCE code challenge method"
        )

    # --- Step 2: Client registration ---
    # Priority per MCP spec:
    #   1. Client ID Metadata Documents (if auth server supports it)
    #   2. Dynamic Client Registration (fallback)
    import socket

    use_cimd = meta.get("client_id_metadata_document_supported", False)

    if use_cimd:
        # CIMD: use the hosted metadata document URL as client_id with a fixed port
        click.echo("Using Client ID Metadata Document...", err=True)
        client_id = CLIENT_ID_METADATA_URL
        client_secret = None
        port = CIMD_CALLBACK_PORT
        redirect_uri = f"http://127.0.0.1:{port}/callback"
    else:
        # Dynamic Client Registration (RFC 7591)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        redirect_uri = f"http://127.0.0.1:{port}/callback"

        if not reg_endpoint:
            raise OAuthError(
                "Server does not support Client ID Metadata Documents or "
                "Dynamic Client Registration. Manual client registration may be required."
            )

        click.echo("Registering client...", err=True)
        reg = register_client(reg_endpoint, redirect_uri)
        client_id = reg["client_id"]
        client_secret = reg.get("client_secret")

    # --- Step 4: PKCE + authorization URL with resource parameter (RFC 8707) ---
    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)

    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": resource_uri,
    }
    if scope:
        auth_params["scope"] = scope
    auth_url = f"{auth_endpoint}?{urllib.parse.urlencode(auth_params)}"

    click.echo("Opening browser for authorization...", err=True)
    webbrowser.open(auth_url)

    # --- Step 5: Wait for callback ---
    code_result = [None]
    error_result = [None]

    def _wait():
        try:
            code_result[0] = _run_callback_server(port, state, CALLBACK_TIMEOUT)
        except OAuthError as e:
            error_result[0] = e

    t = threading.Thread(target=_wait, daemon=True)
    t.start()
    click.echo("Waiting for authorization (press Ctrl+C to cancel)...", err=True)
    t.join(timeout=CALLBACK_TIMEOUT + 5)

    if error_result[0]:
        raise error_result[0]
    if not code_result[0]:
        raise OAuthError("Timed out waiting for authorization callback")

    auth_code = code_result[0]

    # --- Step 6: Token exchange with resource parameter (RFC 8707) ---
    click.echo("Exchanging authorization code for token...", err=True)
    token_data = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "resource": resource_uri,
    }
    if client_secret:
        token_data["client_secret"] = client_secret

    resp = httpx.post(token_endpoint, data=token_data, timeout=10)
    if resp.status_code != 200:
        raise OAuthError(f"Token exchange failed ({resp.status_code}): {resp.text}")

    try:
        token = resp.json()
    except json.JSONDecodeError as exc:
        raise OAuthError(
            f"Token exchange returned invalid JSON ({resp.status_code}): {resp.text}"
        ) from exc
    expires_in = token.get("expires_in", 3600)

    creds = {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "expires_at": time.time() + expires_in,
        "token_endpoint": token_endpoint,
        "registration_endpoint": reg_endpoint,
        "server_url": server_url,
        "resource_uri": resource_uri,
    }
    click.echo("Authorization successful!", err=True)
    return creds


def refresh_token(creds: dict) -> dict:
    """Use a refresh token to get a new access token.

    Returns updated credential dict.
    Raises OAuthError if refresh fails.
    """
    rt = creds.get("refresh_token")
    if not rt:
        raise OAuthError("No refresh token available")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": creds["client_id"],
    }
    if creds.get("client_secret"):
        data["client_secret"] = creds["client_secret"]

    resp = httpx.post(creds["token_endpoint"], data=data, timeout=10)
    if resp.status_code != 200:
        raise OAuthError(f"Token refresh failed ({resp.status_code}): {resp.text}")

    try:
        token = resp.json()
    except json.JSONDecodeError as exc:
        raise OAuthError(
            f"Token refresh returned invalid JSON: {resp.text}"
        ) from exc
    expires_in = token.get("expires_in", 3600)

    creds["access_token"] = token["access_token"]
    if "refresh_token" in token:
        creds["refresh_token"] = token["refresh_token"]
    creds["expires_at"] = time.time() + expires_in
    return creds
