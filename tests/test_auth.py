"""Tests for the OAuth 2.0 auth module."""

import time
import urllib.parse
from unittest.mock import patch, MagicMock

import pytest
import httpx

from murl.auth import (
    _auth_base_url,
    _canonical_resource_uri,
    _generate_pkce,
    _validate_auth_server_origin,
    discover_metadata,
    discover_resource_metadata,
    discover_auth_server_metadata,
    parse_www_authenticate,
    register_client,
    authorize,
    refresh_token,
    OAuthError,
)
from murl.token_store import (
    get_credentials,
    save_credentials,
    clear_credentials,
    is_expired,
    _keyring_available,
    _KEYRING_SERVICE,
    _key_for_url,
)

import json


# ---------------------------------------------------------------------------
# token_store tests — helpers
# ---------------------------------------------------------------------------

def _mock_keyring_get(store, key):
    """In-memory keyring get."""
    data = store.get((_KEYRING_SERVICE, key))
    if data is None:
        return None
    try:
        return json.loads(data)
    except Exception:
        return None


def _mock_keyring_set(store, key, creds):
    """In-memory keyring set."""
    store[(_KEYRING_SERVICE, key)] = json.dumps(creds)
    return True


def _mock_keyring_delete(store, key):
    """In-memory keyring delete."""
    store.pop((_KEYRING_SERVICE, key), None)


def _disable_keyring(monkeypatch):
    """Force the filesystem backend by making keyring appear unavailable."""
    monkeypatch.setattr("murl.token_store._keyring_available", lambda: False)


class TestTokenStoreFilesystem:
    """Tests for credential persistence via filesystem (no keyring)."""

    def test_roundtrip(self, tmp_path, monkeypatch):
        _disable_keyring(monkeypatch)
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        url = "https://example.com/mcp"
        creds = {"access_token": "tok123", "expires_at": time.time() + 3600}

        assert get_credentials(url) is None
        save_credentials(url, creds)
        loaded = get_credentials(url)
        assert loaded["access_token"] == "tok123"
        assert loaded["server_url"] == url
        # Verify it's on disk
        assert len(list(tmp_path.glob("*.json"))) == 1

    def test_clear(self, tmp_path, monkeypatch):
        _disable_keyring(monkeypatch)
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        url = "https://example.com/mcp"
        save_credentials(url, {"access_token": "tok"})
        clear_credentials(url)
        assert get_credentials(url) is None

    def test_clear_nonexistent(self, tmp_path, monkeypatch):
        _disable_keyring(monkeypatch)
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        clear_credentials("https://nope.example.com")  # should not raise


class TestTokenStoreKeyring:
    """Tests for credential persistence via system keychain."""

    def _mock_keyring(self, monkeypatch):
        """Set up an in-memory keyring mock and enable it."""
        store = {}

        def get_password(service, key):
            return store.get((service, key))

        def set_password(service, key, value):
            store[(service, key)] = value

        def delete_password(service, key):
            store.pop((service, key), None)

        mock_keyring = MagicMock()
        mock_keyring.get_password = get_password
        mock_keyring.set_password = set_password
        mock_keyring.delete_password = delete_password

        monkeypatch.setattr("murl.token_store._keyring_available", lambda: True)
        import murl.token_store as ts
        monkeypatch.setattr(ts, "_keyring_get", lambda key: _mock_keyring_get(store, key))
        monkeypatch.setattr(ts, "_keyring_set", lambda key, creds: _mock_keyring_set(store, key, creds))
        monkeypatch.setattr(ts, "_keyring_delete", lambda key: _mock_keyring_delete(store, key))
        return store

    def test_roundtrip(self, tmp_path, monkeypatch):
        self._mock_keyring(monkeypatch)
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        url = "https://example.com/mcp"
        creds = {"access_token": "tok_keyring", "expires_at": time.time() + 3600}

        assert get_credentials(url) is None
        save_credentials(url, creds)
        loaded = get_credentials(url)
        assert loaded["access_token"] == "tok_keyring"
        assert loaded["server_url"] == url
        # No file should exist — creds are in the keychain.
        assert len(list(tmp_path.glob("*.json"))) == 0

    def test_clear(self, tmp_path, monkeypatch):
        self._mock_keyring(monkeypatch)
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        url = "https://example.com/mcp"
        save_credentials(url, {"access_token": "tok"})
        clear_credentials(url)
        assert get_credentials(url) is None

    def test_migration_from_file(self, tmp_path, monkeypatch):
        """Credentials saved to file are readable once keyring is enabled."""
        # First, save to filesystem.
        _disable_keyring(monkeypatch)
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        url = "https://example.com/mcp"
        save_credentials(url, {"access_token": "old_file_tok"})
        assert len(list(tmp_path.glob("*.json"))) == 1

        # Now enable keyring — get should find the file credential.
        self._mock_keyring(monkeypatch)
        loaded = get_credentials(url)
        assert loaded["access_token"] == "old_file_tok"

        # Re-saving moves it to keychain and cleans up the file.
        save_credentials(url, {"access_token": "new_keyring_tok"})
        loaded = get_credentials(url)
        assert loaded["access_token"] == "new_keyring_tok"
        assert len(list(tmp_path.glob("*.json"))) == 0

    def test_keyring_failure_falls_back_to_file(self, tmp_path, monkeypatch):
        """If keyring save fails, credentials go to a file instead."""
        monkeypatch.setattr("murl.token_store._keyring_available", lambda: True)
        import murl.token_store as ts
        monkeypatch.setattr(ts, "_keyring_set", lambda key, creds: False)
        monkeypatch.setattr(ts, "_keyring_get", lambda key: None)
        monkeypatch.setattr(ts, "_keyring_delete", lambda key: None)
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)

        url = "https://example.com/mcp"
        save_credentials(url, {"access_token": "fallback_tok"})
        # Should have fallen back to file.
        assert len(list(tmp_path.glob("*.json"))) == 1


class TestIsExpired:
    """Tests for token expiry checks."""

    def test_expired(self):
        assert is_expired({"expires_at": time.time() - 10})

    def test_not_expired(self):
        assert not is_expired({"expires_at": time.time() + 3600})

    def test_within_buffer(self):
        # Expires in 30s — within the 60s buffer
        assert is_expired({"expires_at": time.time() + 30})

    def test_missing(self):
        assert is_expired({})


# ---------------------------------------------------------------------------
# auth helpers
# ---------------------------------------------------------------------------

class TestAuthHelpers:

    def test_auth_base_url(self):
        assert _auth_base_url("https://foo.com/mcp/default") == "https://foo.com"
        assert _auth_base_url("http://localhost:3000/mcp") == "http://localhost:3000"

    def test_pkce_verifier_and_challenge_differ(self):
        v, c = _generate_pkce()
        assert v != c
        assert len(v) > 40
        assert len(c) > 20
        # Challenge must be base64url without padding
        assert "=" not in c
        assert "+" not in c
        assert "/" not in c


# ---------------------------------------------------------------------------
# discover_metadata
# ---------------------------------------------------------------------------

class TestDiscoverMetadata:

    def test_success(self):
        meta = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "registration_endpoint": "https://auth.example.com/register",
        }

        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = meta
            mock_get.return_value = mock_resp

            result = discover_metadata("https://example.com/mcp")
            assert result == meta
            # First URL tried should be path-aware RFC 8414
            first_url = mock_get.call_args_list[0][0][0]
            assert "/.well-known/oauth-authorization-server/mcp" in first_url

    def test_fallback_on_404(self):
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_get.return_value = mock_resp

            result = discover_metadata("https://example.com/mcp")
            assert "/authorize" in result["authorization_endpoint"]
            assert "/token" in result["token_endpoint"]
            assert "/register" in result["registration_endpoint"]

    def test_fallback_on_network_error(self):
        with patch("murl.auth.httpx.get", side_effect=httpx.ConnectError("fail")):
            result = discover_metadata("https://example.com/mcp")
            assert "authorization_endpoint" in result

    def test_fallback_on_500(self):
        """All endpoints returning 500 falls back to defaults (tries multiple URLs)."""
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_resp.text = "Internal Server Error"
            mock_get.return_value = mock_resp

            result = discover_metadata("https://example.com/mcp")
            assert "/authorize" in result["authorization_endpoint"]
            assert "/token" in result["token_endpoint"]
            # Should have tried multiple URLs (RFC 8414 + OIDC)
            assert mock_get.call_count >= 2


# ---------------------------------------------------------------------------
# register_client
# ---------------------------------------------------------------------------

class TestRegisterClient:

    def test_success(self):
        with patch("murl.auth.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_resp.json.return_value = {
                "client_id": "cid_123",
                "client_secret": "csec_456",
            }
            mock_post.return_value = mock_resp

            result = register_client(
                "https://auth.example.com/register",
                "http://127.0.0.1:9999/callback",
            )
            assert result["client_id"] == "cid_123"

    def test_failure(self):
        with patch("murl.auth.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 400
            mock_resp.text = "bad request"
            mock_post.return_value = mock_resp

            with pytest.raises(OAuthError, match="registration failed"):
                register_client(
                    "https://auth.example.com/register",
                    "http://127.0.0.1:9999/callback",
                )


# ---------------------------------------------------------------------------
# refresh_token
# ---------------------------------------------------------------------------

class TestRefreshToken:
    """MCP 2025-11-25 §Resource Parameter Implementation / RFC 8707.

    refresh_token() MUST include the ``resource`` parameter in the token
    request so that the refreshed access token is audience-bound.
    """

    def test_success(self):
        creds = {
            "client_id": "cid",
            "client_secret": None,
            "refresh_token": "rt_old",
            "token_endpoint": "https://auth.example.com/token",
            "resource_uri": "https://mcp.example.com/mcp",
        }

        with patch("murl.auth.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "access_token": "new_at",
                "refresh_token": "new_rt",
                "expires_in": 7200,
            }
            mock_post.return_value = mock_resp

            updated = refresh_token(creds)
            assert updated["access_token"] == "new_at"
            assert updated["refresh_token"] == "new_rt"
            assert updated["expires_at"] > time.time()

    def test_resource_included_in_refresh_request(self):
        """RFC 8707: resource MUST be sent in token requests including refresh."""
        creds = {
            "client_id": "cid",
            "client_secret": None,
            "refresh_token": "rt_old",
            "token_endpoint": "https://auth.example.com/token",
            "resource_uri": "https://mcp.example.com/mcp",
        }

        with patch("murl.auth.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "access_token": "new_at",
                "expires_in": 3600,
            }
            mock_post.return_value = mock_resp

            refresh_token(creds)

            post_data = mock_post.call_args[1].get("data", {})
            assert post_data["resource"] == "https://mcp.example.com/mcp"

    def test_resource_omitted_when_missing_from_creds(self):
        """Legacy creds without resource_uri should not break refresh."""
        creds = {
            "client_id": "cid",
            "client_secret": None,
            "refresh_token": "rt_old",
            "token_endpoint": "https://auth.example.com/token",
            # No resource_uri — pre-existing creds from before spec update
        }

        with patch("murl.auth.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "access_token": "new_at",
                "expires_in": 3600,
            }
            mock_post.return_value = mock_resp

            refresh_token(creds)

            post_data = mock_post.call_args[1].get("data", {})
            assert "resource" not in post_data

    def test_client_secret_included_in_refresh(self):
        """Confidential clients (pre-configured) MUST send client_secret on refresh."""
        creds = {
            "client_id": "cid",
            "client_secret": "sec123",
            "refresh_token": "rt_old",
            "token_endpoint": "https://auth.example.com/token",
            "resource_uri": "https://mcp.example.com/mcp",
        }

        with patch("murl.auth.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "access_token": "new_at",
                "expires_in": 3600,
            }
            mock_post.return_value = mock_resp

            refresh_token(creds)

            post_data = mock_post.call_args[1].get("data", {})
            assert post_data["client_secret"] == "sec123"
            assert post_data["resource"] == "https://mcp.example.com/mcp"

    def test_no_refresh_token(self):
        with pytest.raises(OAuthError, match="No refresh token"):
            refresh_token({"client_id": "cid", "token_endpoint": "https://x"})

    def test_failure(self):
        creds = {
            "client_id": "cid",
            "client_secret": None,
            "refresh_token": "rt",
            "token_endpoint": "https://auth.example.com/token",
        }

        with patch("murl.auth.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 400
            mock_resp.text = "invalid_grant"
            mock_post.return_value = mock_resp

            with pytest.raises(OAuthError, match="refresh failed"):
                refresh_token(creds)


# ---------------------------------------------------------------------------
# parse_www_authenticate
# ---------------------------------------------------------------------------

class TestParseWwwAuthenticate:

    def test_bearer_with_resource_metadata(self):
        header = 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource", scope="files:read"'
        result = parse_www_authenticate(header)
        assert result["resource_metadata"] == "https://mcp.example.com/.well-known/oauth-protected-resource"
        assert result["scope"] == "files:read"

    def test_bearer_with_error(self):
        header = 'Bearer error="insufficient_scope", scope="files:read files:write", error_description="Need write access"'
        result = parse_www_authenticate(header)
        assert result["error"] == "insufficient_scope"
        assert result["scope"] == "files:read files:write"
        assert result["error_description"] == "Need write access"

    def test_bare_bearer(self):
        result = parse_www_authenticate("Bearer")
        assert result == {}

    def test_empty(self):
        result = parse_www_authenticate("")
        assert result == {}


# ---------------------------------------------------------------------------
# discover_resource_metadata (RFC 9728)
# ---------------------------------------------------------------------------

class TestDiscoverResourceMetadata:

    def test_from_explicit_url(self):
        meta = {
            "resource": "https://mcp.example.com/mcp",
            "authorization_servers": ["https://auth.example.com"],
        }
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = meta
            mock_get.return_value = mock_resp

            result = discover_resource_metadata(
                "https://mcp.example.com/mcp",
                resource_metadata_url="https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
            )
            assert result["authorization_servers"] == ["https://auth.example.com"]
            # Should use the explicit URL first
            mock_get.assert_called_once_with(
                "https://mcp.example.com/.well-known/oauth-protected-resource/mcp",
                follow_redirects=True, timeout=10
            )

    def test_well_known_path_aware(self):
        meta = {
            "resource": "https://mcp.example.com/mcp",
            "authorization_servers": ["https://auth.example.com"],
        }
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = meta
            mock_get.return_value = mock_resp

            result = discover_resource_metadata("https://mcp.example.com/mcp")
            assert result["authorization_servers"] == ["https://auth.example.com"]
            first_url = mock_get.call_args_list[0][0][0]
            assert first_url == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"

    def test_fallback_to_root(self):
        """If path-aware URL fails, try root well-known."""
        with patch("murl.auth.httpx.get") as mock_get:
            not_found = MagicMock()
            not_found.status_code = 404
            root_meta = MagicMock()
            root_meta.status_code = 200
            root_meta.json.return_value = {
                "authorization_servers": ["https://auth.example.com"],
            }
            mock_get.side_effect = [not_found, root_meta]

            result = discover_resource_metadata("https://mcp.example.com/mcp")
            assert result["authorization_servers"] == ["https://auth.example.com"]
            assert mock_get.call_count == 2

    def test_all_fail_raises(self):
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_get.return_value = mock_resp

            with pytest.raises(OAuthError, match="Protected Resource Metadata"):
                discover_resource_metadata("https://mcp.example.com/mcp")


# ---------------------------------------------------------------------------
# discover_auth_server_metadata (RFC 8414 + OIDC)
# ---------------------------------------------------------------------------

class TestDiscoverAuthServerMetadata:

    def test_rfc8414_success(self):
        meta = {
            "issuer": "https://auth.example.com",
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
        }
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = meta
            mock_get.return_value = mock_resp

            result = discover_auth_server_metadata("https://auth.example.com")
            assert result["token_endpoint"] == "https://auth.example.com/token"
            assert result["_discovery_source"] == "rfc8414"

    def test_oidc_fallback(self):
        """RFC 8414 fails, OIDC discovery succeeds."""
        meta = {
            "issuer": "https://auth.example.com",
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
        }
        with patch("murl.auth.httpx.get") as mock_get:
            not_found = MagicMock()
            not_found.status_code = 404
            oidc_resp = MagicMock()
            oidc_resp.status_code = 200
            oidc_resp.json.return_value = meta
            mock_get.side_effect = [not_found, oidc_resp]

            result = discover_auth_server_metadata("https://auth.example.com")
            assert result["token_endpoint"] == "https://auth.example.com/token"
            assert result["_discovery_source"] == "oidc"
            assert mock_get.call_count == 2

    def test_with_path_component(self):
        """Issuer URL with path uses path-insertion for both RFC 8414 and OIDC."""
        meta = {
            "issuer": "https://auth.example.com/tenant1",
            "authorization_endpoint": "https://auth.example.com/tenant1/authorize",
            "token_endpoint": "https://auth.example.com/tenant1/token",
        }
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = meta
            mock_get.return_value = mock_resp

            result = discover_auth_server_metadata("https://auth.example.com/tenant1")
            first_url = mock_get.call_args_list[0][0][0]
            assert first_url == "https://auth.example.com/.well-known/oauth-authorization-server/tenant1"

    def test_all_fail_raises(self):
        with patch("murl.auth.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_get.return_value = mock_resp

            with pytest.raises(OAuthError, match="Could not discover Authorization Server"):
                discover_auth_server_metadata("https://auth.example.com")


# ---------------------------------------------------------------------------
# _validate_auth_server_origin (RFC 9728 §3)
# ---------------------------------------------------------------------------

class TestValidateAuthServerOrigin:
    """Prevent redirect attacks via crafted authorization_servers values."""

    def test_same_host_passes(self):
        _validate_auth_server_origin(
            "https://example.com/oauth2", "https://example.com/mcp"
        )

    def test_same_host_different_port_passes(self):
        """Port differs but hostname matches — allowed."""
        _validate_auth_server_origin(
            "https://example.com:8443/oauth", "https://example.com/mcp"
        )

    def test_case_insensitive(self):
        _validate_auth_server_origin(
            "https://Example.COM/oauth", "https://example.com/mcp"
        )

    def test_different_host_raises(self):
        with pytest.raises(OAuthError, match="does not match"):
            _validate_auth_server_origin(
                "https://evil.example.com/oauth", "https://mcp.example.com/mcp"
            )

    def test_attacker_redirect_raises(self):
        with pytest.raises(OAuthError, match="does not match"):
            _validate_auth_server_origin(
                "https://attacker.com", "https://legit-server.com/mcp"
            )

    def test_invalid_url_raises(self):
        with pytest.raises(OAuthError, match="Invalid authorization server"):
            _validate_auth_server_origin("not-a-url", "https://example.com/mcp")


# ---------------------------------------------------------------------------
# canonical_resource_uri
# ---------------------------------------------------------------------------

class TestCanonicalResourceUri:
    """MCP 2025-11-25 §Resource Parameter Implementation / RFC 8707 §2.

    The canonical URI MUST NOT include fragments, SHOULD omit trailing slash
    unless semantically significant, and SHOULD be the most specific URI.
    """

    def test_simple(self):
        assert _canonical_resource_uri("https://mcp.example.com/mcp") == "https://mcp.example.com/mcp"

    def test_strips_fragment(self):
        assert _canonical_resource_uri("https://mcp.example.com/mcp#frag") == "https://mcp.example.com/mcp"

    def test_strips_query(self):
        assert _canonical_resource_uri("https://mcp.example.com/mcp?foo=bar") == "https://mcp.example.com/mcp"

    def test_no_path_no_trailing_slash(self):
        """Spec SHOULD: use form without trailing slash (MCP 2025-11-25 §Canonical Server URI)."""
        assert _canonical_resource_uri("https://mcp.example.com") == "https://mcp.example.com"

    def test_preserves_explicit_path_slash(self):
        """A trailing slash that was explicitly part of the URL is preserved."""
        assert _canonical_resource_uri("https://mcp.example.com/") == "https://mcp.example.com/"

    def test_with_port(self):
        """Port is part of the canonical URI (spec example: https://mcp.example.com:8443)."""
        assert _canonical_resource_uri("https://mcp.example.com:8443/mcp") == "https://mcp.example.com:8443/mcp"


# ---------------------------------------------------------------------------
# Full authorize flow (mocked)
# ---------------------------------------------------------------------------

class TestAuthorize:
    """MCP 2025-11-25 §Authorization Flow Steps — end-to-end flow tests.

    Validates: discovery chain, PKCE enforcement, resource parameter (RFC 8707),
    client registration priority, and scope selection strategy.
    """

    def _mock_full_flow(self, mock_httpx_get, mock_httpx_post, mock_webbrowser, mock_server):
        """Set up mocks for a successful full OAuth flow."""
        # Metadata discovery
        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "registration_endpoint": "https://auth.example.com/register",
            "code_challenge_methods_supported": ["S256"],
        }
        mock_httpx_get.return_value = meta_resp

        # Registration + token exchange (two POST calls)
        reg_resp = MagicMock()
        reg_resp.status_code = 201
        reg_resp.json.return_value = {"client_id": "cid_test"}

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {
            "access_token": "at_final",
            "refresh_token": "rt_final",
            "expires_in": 3600,
        }
        mock_httpx_post.side_effect = [reg_resp, token_resp]

        # Browser open — no-op
        mock_webbrowser.return_value = True

        # Callback server returns a code
        mock_server.return_value = "test_auth_code"

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_full_flow(self, mock_get, mock_post, mock_browser, mock_server):
        self._mock_full_flow(mock_get, mock_post, mock_browser, mock_server)

        creds = authorize("https://example.com/mcp")

        assert creds["access_token"] == "at_final"
        assert creds["refresh_token"] == "rt_final"
        assert creds["client_id"] == "cid_test"
        assert creds["expires_at"] > time.time()
        assert creds["server_url"] == "https://example.com/mcp"
        assert creds["resource_uri"] == "https://example.com/mcp"

        # Verify browser was opened with correct params
        mock_browser.assert_called_once()
        auth_url = mock_browser.call_args[0][0]
        parsed = urllib.parse.urlparse(auth_url)
        params = urllib.parse.parse_qs(parsed.query)
        assert params["client_id"] == ["cid_test"]
        assert params["response_type"] == ["code"]
        assert params["code_challenge_method"] == ["S256"]
        assert "state" in params
        assert "code_challenge" in params
        # RFC 8707: resource parameter must be present
        assert params["resource"] == ["https://example.com/mcp"]

        # Verify token exchange also included resource parameter
        token_call = mock_post.call_args_list[1]  # second POST is token exchange
        token_data = token_call[1].get("data", {})
        assert token_data["resource"] == "https://example.com/mcp"

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_no_registration_endpoint(self, mock_get, mock_post, mock_browser, mock_server):
        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "code_challenge_methods_supported": ["S256"],
            # No registration_endpoint
        }
        mock_get.return_value = meta_resp

        with pytest.raises(OAuthError, match="registration endpoint"):
            authorize("https://example.com/mcp")

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_pkce_missing_from_metadata(self, mock_get, mock_post, mock_browser, mock_server):
        """authorize() must refuse if code_challenge_methods_supported is absent."""
        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "registration_endpoint": "https://auth.example.com/register",
            # No code_challenge_methods_supported
        }
        mock_get.return_value = meta_resp

        with pytest.raises(OAuthError, match="does not advertise PKCE support"):
            authorize("https://example.com/mcp")

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_pkce_oidc_missing_proceeds_with_warning(self, mock_get, mock_post, mock_browser, mock_server):
        """OIDC metadata without code_challenge_methods_supported should warn but proceed.

        OIDC Provider Metadata does not define this field (it's an OAuth 2.0 AS
        Metadata field).  Major IDPs like Okta fully support S256 PKCE but don't
        include the field in their OIDC discovery document.
        """
        # Resource metadata -> points to auth server (same origin as MCP server)
        resource_meta_resp = MagicMock()
        resource_meta_resp.status_code = 200
        resource_meta_resp.json.return_value = {
            "authorization_servers": ["https://example.com/oauth2/default"],
        }

        # Auth server metadata via OIDC (no code_challenge_methods_supported)
        oidc_resp = MagicMock()
        oidc_resp.status_code = 200
        oidc_resp.json.return_value = {
            "issuer": "https://example.com/oauth2/default",
            "authorization_endpoint": "https://example.com/oauth2/default/v1/authorize",
            "token_endpoint": "https://example.com/oauth2/default/v1/token",
            "registration_endpoint": "https://example.com/oauth2/v1/clients",
            # No code_challenge_methods_supported — typical for Okta OIDC
        }

        # RFC 8414 fails (404/405), OIDC path-insert fails, OIDC path-append succeeds
        not_found = MagicMock()
        not_found.status_code = 405

        # Order: resource_meta path-aware (succeeds), AS rfc8414, OIDC insert, OIDC append
        mock_get.side_effect = [resource_meta_resp, not_found, not_found, oidc_resp]

        # Registration + token exchange
        reg_resp = MagicMock()
        reg_resp.status_code = 201
        reg_resp.json.return_value = {"client_id": "cid_oidc"}

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {
            "access_token": "at_oidc",
            "refresh_token": "rt_oidc",
            "expires_in": 3600,
        }
        mock_post.side_effect = [reg_resp, token_resp]
        mock_browser.return_value = True
        mock_server.return_value = "test_auth_code"

        # Should succeed despite missing code_challenge_methods_supported
        creds = authorize("https://example.com/mcp")
        assert creds["access_token"] == "at_oidc"

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_pkce_s256_not_supported(self, mock_get, mock_post, mock_browser, mock_server):
        """authorize() must refuse if S256 is not in code_challenge_methods_supported."""
        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "registration_endpoint": "https://auth.example.com/register",
            "code_challenge_methods_supported": ["plain"],
        }
        mock_get.return_value = meta_resp

        with pytest.raises(OAuthError, match="does not support S256"):
            authorize("https://example.com/mcp")

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_preconfigured_credentials_skip_registration(self, mock_get, mock_post, mock_browser, mock_server):
        """Pre-configured client_id skips Dynamic Client Registration."""
        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "code_challenge_methods_supported": ["S256"],
            # No registration_endpoint — should still work with pre-configured creds
        }
        mock_get.return_value = meta_resp

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {
            "access_token": "at_preconfig",
            "refresh_token": "rt_preconfig",
            "expires_in": 3600,
        }
        mock_post.return_value = token_resp
        mock_browser.return_value = True
        mock_server.return_value = "test_auth_code"

        creds = authorize(
            "https://example.com/mcp",
            client_id="my-app-id",
            client_secret="my-app-secret",
            callback_port=8080,
        )

        assert creds["access_token"] == "at_preconfig"
        assert creds["client_id"] == "my-app-id"

        # Only one POST (token exchange) — no registration POST
        assert mock_post.call_count == 1

        # Verify the auth URL uses the pre-configured client_id and fixed port
        auth_url = mock_browser.call_args[0][0]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
        assert params["client_id"] == ["my-app-id"]
        assert params["redirect_uri"] == ["http://localhost:8080/callback"]

        # Verify token exchange includes client_secret
        token_data = mock_post.call_args[1].get("data", {})
        assert token_data["client_secret"] == "my-app-secret"

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_no_registration_and_no_client_id_raises(self, mock_get, mock_post, mock_browser, mock_server):
        """Without pre-configured creds or a registration endpoint, authorize fails."""
        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "authorization_endpoint": "https://auth.example.com/authorize",
            "token_endpoint": "https://auth.example.com/token",
            "code_challenge_methods_supported": ["S256"],
            # No registration_endpoint
        }
        mock_get.return_value = meta_resp

        with pytest.raises(OAuthError, match="--client-id"):
            authorize("https://example.com/mcp")

    @patch("murl.auth._run_callback_server")
    @patch("murl.auth.webbrowser.open")
    @patch("murl.auth.httpx.post")
    @patch("murl.auth.httpx.get")
    def test_cross_origin_auth_server_rejected(self, mock_get, mock_post, mock_browser, mock_server):
        """RFC 9728 §3: reject authorization_servers with a different hostname."""
        resource_meta_resp = MagicMock()
        resource_meta_resp.status_code = 200
        resource_meta_resp.json.return_value = {
            "authorization_servers": ["https://evil.attacker.com/oauth"],
        }
        mock_get.return_value = resource_meta_resp

        with pytest.raises(OAuthError, match="does not match"):
            authorize("https://legit-server.com/mcp")
