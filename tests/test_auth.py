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
)


# ---------------------------------------------------------------------------
# token_store tests
# ---------------------------------------------------------------------------

class TestTokenStore:
    """Tests for credential persistence."""

    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        url = "https://example.com/mcp"
        creds = {"access_token": "tok123", "expires_at": time.time() + 3600}

        assert get_credentials(url) is None
        save_credentials(url, creds)
        loaded = get_credentials(url)
        assert loaded["access_token"] == "tok123"
        assert loaded["server_url"] == url

    def test_clear(self, tmp_path, monkeypatch):
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        url = "https://example.com/mcp"
        save_credentials(url, {"access_token": "tok"})
        clear_credentials(url)
        assert get_credentials(url) is None

    def test_clear_nonexistent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("murl.token_store.CREDENTIALS_DIR", tmp_path)
        clear_credentials("https://nope.example.com")  # should not raise

    def test_is_expired_true(self):
        assert is_expired({"expires_at": time.time() - 10})

    def test_is_expired_false(self):
        assert not is_expired({"expires_at": time.time() + 3600})

    def test_is_expired_within_buffer(self):
        # Expires in 30s — within the 60s buffer
        assert is_expired({"expires_at": time.time() + 30})

    def test_is_expired_missing(self):
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

    def test_success(self):
        creds = {
            "client_id": "cid",
            "client_secret": None,
            "refresh_token": "rt_old",
            "token_endpoint": "https://auth.example.com/token",
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
# canonical_resource_uri
# ---------------------------------------------------------------------------

class TestCanonicalResourceUri:

    def test_simple(self):
        assert _canonical_resource_uri("https://mcp.example.com/mcp") == "https://mcp.example.com/mcp"

    def test_strips_fragment(self):
        assert _canonical_resource_uri("https://mcp.example.com/mcp#frag") == "https://mcp.example.com/mcp"

    def test_strips_query(self):
        assert _canonical_resource_uri("https://mcp.example.com/mcp?foo=bar") == "https://mcp.example.com/mcp"

    def test_no_path(self):
        assert _canonical_resource_uri("https://mcp.example.com") == "https://mcp.example.com/"


# ---------------------------------------------------------------------------
# Full authorize flow (mocked)
# ---------------------------------------------------------------------------

class TestAuthorize:

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
