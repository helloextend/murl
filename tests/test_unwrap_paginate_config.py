"""Unit tests for unwrap, paginate, auth-failure detection, and .mcp.json defaults."""

import json
import os
from pathlib import Path

import pytest

from murl.cli import (
    UPGRADE_SOURCE,
    detect_tool_auth_failure,
    find_mcp_config,
    mcp_config_defaults,
    unwrap_text_envelope,
)


def test_upgrade_source_points_at_helloextend_fork():
    # --upgrade against PyPI silently downgrades fork users to the stale
    # public mcp-curl release. Pin the install spec to helloextend.
    assert "helloextend/murl" in UPGRADE_SOURCE
    assert UPGRADE_SOURCE.startswith("mcp-curl[")
    assert "git+https://" in UPGRADE_SOURCE


# --- unwrap_text_envelope ---


def test_unwrap_text_envelope_with_json_text():
    inner = {"hello": "world", "items": [1, 2]}
    envelope = [{"type": "text", "text": json.dumps(inner)}]
    result, did = unwrap_text_envelope(envelope)
    assert did is True
    assert result == inner


def test_unwrap_text_envelope_passes_through_non_envelope():
    # Not a single-item list
    result, did = unwrap_text_envelope([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    assert did is False
    assert result == [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]


def test_unwrap_text_envelope_passes_through_non_json_text():
    envelope = [{"type": "text", "text": "this is plain text, not JSON"}]
    result, did = unwrap_text_envelope(envelope)
    assert did is False
    assert result == envelope


def test_unwrap_text_envelope_ignores_non_text_blocks():
    envelope = [{"type": "image", "data": "..."}]
    result, did = unwrap_text_envelope(envelope)
    assert did is False
    assert result == envelope


def test_unwrap_text_envelope_handles_non_list():
    result, did = unwrap_text_envelope({"already": "unwrapped"})
    assert did is False
    assert result == {"already": "unwrapped"}


# --- detect_tool_auth_failure ---


def test_detect_tool_auth_failure_invalid_token_in_error():
    body = {"error": {"code": "invalid_token", "message": "JWT expired"}}
    assert detect_tool_auth_failure(body) is not None


def test_detect_tool_auth_failure_auth_failed_string():
    body = {"status": "AUTH_FAILED", "reason": "OBO exchange failed"}
    assert detect_tool_auth_failure(body) is not None


def test_detect_tool_auth_failure_clean_response():
    body = {"applications": [{"id": 1, "name": "Alice"}], "hasMore": False}
    assert detect_tool_auth_failure(body) is None


def test_detect_tool_auth_failure_catches_error_inside_items_envelope():
    # Regression for CodeRabbit finding on PR #7: an error response received
    # mid-pagination must be detectable so the loop surfaces it rather than
    # returning partial results. The body has no items_key (would otherwise
    # break the loop silently) — only an error object.
    body = {"error": {"code": "invalid_token", "message": "expired mid-loop"}}
    assert detect_tool_auth_failure(body) is not None


def test_detect_tool_auth_failure_bounded_recursion():
    # Build a deeply nested object — should not blow up and should not match.
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": "ok"}}}}}}}
    assert detect_tool_auth_failure(deep) is None


# --- find_mcp_config + mcp_config_defaults ---


def test_find_mcp_config_walks_up(tmp_path, monkeypatch):
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"foo": {"url": "https://example/mcp", "oauth": {"clientId": "abc"}}}}))
    sub = tmp_path / "a" / "b" / "c"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    result = find_mcp_config()
    assert result is not None
    assert "mcpServers" in result


def test_find_mcp_config_missing_returns_none(tmp_path, monkeypatch):
    sub = tmp_path / "nested"
    sub.mkdir()
    monkeypatch.chdir(sub)
    monkeypatch.setenv("HOME", str(tmp_path))  # stop walk at tmp root quickly
    assert find_mcp_config() is None


def test_find_mcp_config_malformed_returns_none(tmp_path, monkeypatch):
    (tmp_path / ".mcp.json").write_text("{ not valid json")
    monkeypatch.chdir(tmp_path)
    assert find_mcp_config() is None


def test_mcp_config_defaults_exact_match():
    config = {
        "mcpServers": {
            "greenhouse": {
                "url": "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3A.../invocations?qualifier=DEFAULT",
                "oauth": {"clientId": "0oax4xo2t8czxrESw4x7", "callbackPort": 8080},
            }
        }
    }
    # The base URL passed in must decode to the same string as the config entry.
    base_url = "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn:.../invocations?qualifier=DEFAULT"
    defaults = mcp_config_defaults(base_url, config)
    assert defaults["client_id"] == "0oax4xo2t8czxrESw4x7"
    assert defaults["callback_port"] == 8080


def test_mcp_config_defaults_trailing_slash_normalized():
    config = {"mcpServers": {"s": {"url": "https://x.example/mcp/", "oauth": {"clientId": "abc"}}}}
    assert mcp_config_defaults("https://x.example/mcp", config) == {"client_id": "abc"}


def test_mcp_config_defaults_partial_match_rejected():
    # A prefix match must NOT silently use the wrong config — caller URL has
    # an extra path segment the config entry does not.
    config = {"mcpServers": {"s": {"url": "https://x.example/mcp", "oauth": {"clientId": "abc"}}}}
    assert mcp_config_defaults("https://x.example/mcp/extra", config) == {}


def test_mcp_config_defaults_query_mismatch_rejected():
    # Different qualifier query strings refer to different runtimes; do not
    # silently fall through to the wrong entry.
    config = {"mcpServers": {"s": {"url": "https://x.example/mcp?qualifier=PROD", "oauth": {"clientId": "abc"}}}}
    assert mcp_config_defaults("https://x.example/mcp?qualifier=STAGING", config) == {}


def test_mcp_config_defaults_no_match():
    config = {"mcpServers": {"other": {"url": "https://other.example/mcp", "oauth": {"clientId": "x"}}}}
    defaults = mcp_config_defaults("https://nope.example/mcp", config)
    assert defaults == {}


def test_mcp_config_defaults_none_config():
    assert mcp_config_defaults("https://anywhere/mcp", None) == {}


def test_mcp_config_defaults_missing_oauth_block():
    config = {"mcpServers": {"x": {"url": "https://x.example/mcp"}}}
    assert mcp_config_defaults("https://x.example/mcp", config) == {}
