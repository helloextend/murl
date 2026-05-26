"""Tests for the CLI module."""

import json
import pytest
import subprocess
import time
import sys
import requests
from pathlib import Path
from click.testing import CliRunner

# Python 3.10 compatibility: ExceptionGroup was added in 3.11
try:
    ExceptionGroup
except NameError:
    from exceptiongroup import ExceptionGroup

from murl.cli import (
    main,
    parse_url,
    parse_data_value,
    parse_data_flags,
    map_virtual_path_to_method,
    parse_headers,
)
from murl import __version__


# Test server configuration
TEST_SERVER_PORT = 8765
TEST_SERVER_URL = f"http://localhost:{TEST_SERVER_PORT}"


def parse_ndjson(output):
    """Parse NDJSON (one JSON object per line) output."""
    return [json.loads(line) for line in output.strip().split('\n') if line.strip()]


@pytest.fixture(scope="module")
def mcp_server():
    """Start the real MCP test server for integration tests."""
    test_dir = Path(__file__).parent
    server_script = test_dir / "mcp_test_server.py"

    process = subprocess.Popen(
        [sys.executable, str(server_script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    import requests
    max_retries = 10
    retry_delay = 0.2

    for attempt in range(max_retries):
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"Server failed to start:\nSTDOUT: {stdout}\nSTDERR: {stderr}")

        try:
            response = requests.post(
                TEST_SERVER_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                timeout=1
            )
            if response.status_code == 200:
                break
        except (requests.ConnectionError, requests.Timeout):
            time.sleep(retry_delay)
    else:
        process.terminate()
        pytest.fail(f"Server failed to start after {max_retries} attempts")

    yield TEST_SERVER_URL

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


# Test helper functions

def test_parse_url_tools():
    base, path = parse_url("http://localhost:3000/tools")
    assert base == "http://localhost:3000"
    assert path == "/tools"


def test_parse_url_tools_with_name():
    base, path = parse_url("http://localhost:3000/tools/weather")
    assert base == "http://localhost:3000"
    assert path == "/tools/weather"


def test_parse_url_resources():
    base, path = parse_url("https://api.example.com/mcp/resources")
    assert base == "https://api.example.com/mcp"
    assert path == "/resources"


def test_parse_url_prompts():
    base, path = parse_url("http://localhost:3000/prompts/greeting")
    assert base == "http://localhost:3000"
    assert path == "/prompts/greeting"


def test_parse_url_invalid():
    with pytest.raises(ValueError, match="Invalid MCP URL"):
        parse_url("http://localhost:3000/invalid")


def test_parse_data_value_boolean_true():
    assert parse_data_value("true") is True
    assert parse_data_value("True") is True


def test_parse_data_value_boolean_false():
    assert parse_data_value("false") is False
    assert parse_data_value("False") is False


def test_parse_data_value_integer():
    assert parse_data_value("123") == 123
    assert parse_data_value("-456") == -456


def test_parse_data_value_float():
    assert parse_data_value("3.14") == 3.14
    assert parse_data_value("-2.5") == -2.5


def test_parse_data_value_string():
    assert parse_data_value("hello") == "hello"
    assert parse_data_value("world123") == "world123"


def test_parse_data_flags_key_value():
    result = parse_data_flags(("name=John", "age=30", "active=true"))
    assert result == {"name": "John", "age": 30, "active": True}


def test_parse_data_flags_json():
    result = parse_data_flags(('{"city": "Paris", "metric": true}',))
    assert result == {"city": "Paris", "metric": True}


def test_parse_data_flags_json_array_error():
    with pytest.raises(ValueError, match="JSON arrays are not supported"):
        parse_data_flags(('[1, 2, 3]',))


def test_parse_data_flags_mixed():
    result = parse_data_flags(("name=Alice", '{"age": 25}'))
    assert result == {"name": "Alice", "age": 25}


def test_parse_data_flags_invalid_format():
    with pytest.raises(ValueError, match="Invalid data format"):
        parse_data_flags(("invalid",))


def test_parse_data_flags_invalid_json():
    with pytest.raises(ValueError, match="Invalid JSON"):
        parse_data_flags(('{"invalid": json}',))


def test_parse_data_flags_stdin(monkeypatch):
    """@- reads a JSON object from stdin."""
    monkeypatch.setattr('sys.stdin', __import__('io').StringIO('{"key": "from_stdin"}'))
    result = parse_data_flags(('@-',))
    assert result == {"key": "from_stdin"}


def test_parse_data_flags_stdin_merged_with_flags(monkeypatch):
    """Explicit -d key=value overrides stdin values (later flags win)."""
    monkeypatch.setattr('sys.stdin', __import__('io').StringIO('{"a": 1, "b": 2}'))
    result = parse_data_flags(('@-', 'b=99'))
    assert result == {"a": 1, "b": 99}


def test_parse_data_flags_file(tmp_path):
    """@path reads a JSON object from a file."""
    f = tmp_path / "args.json"
    f.write_text('{"name": "from_file", "count": 3}')
    result = parse_data_flags((f'@{f}',))
    assert result == {"name": "from_file", "count": 3}


def test_parse_data_flags_file_not_found():
    """@nonexistent gives a clear error."""
    with pytest.raises(ValueError, match="Cannot read @/no/such/file"):
        parse_data_flags(('@/no/such/file',))


def test_parse_data_flags_stdin_non_object(monkeypatch):
    """@- with a JSON array (not object) raises ValueError."""
    monkeypatch.setattr('sys.stdin', __import__('io').StringIO('[1, 2, 3]'))
    with pytest.raises(ValueError, match="must be an object, not list"):
        parse_data_flags(('@-',))


def test_parse_data_flags_stdin_empty(monkeypatch):
    """@- with empty stdin raises ValueError."""
    monkeypatch.setattr('sys.stdin', __import__('io').StringIO(''))
    with pytest.raises(ValueError, match="Empty input"):
        parse_data_flags(('@-',))


def test_map_tools_list():
    method, params = map_virtual_path_to_method("/tools", {})
    assert method == "tools/list"
    assert params == {}


def test_map_tools_call():
    data = {"message": "hello"}
    method, params = map_virtual_path_to_method("/tools/echo", data)
    assert method == "tools/call"
    assert params == {"name": "echo", "arguments": {"message": "hello"}}


def test_map_resources_list():
    method, params = map_virtual_path_to_method("/resources", {})
    assert method == "resources/list"
    assert params == {}


def test_map_resources_read():
    method, params = map_virtual_path_to_method("/resources/path/to/file", {})
    assert method == "resources/read"
    assert params == {"uri": "file:///path/to/file"}


def test_map_resources_read_with_additional_params():
    data = {"format": "json", "encoding": "utf-8"}
    method, params = map_virtual_path_to_method("/resources/path/to/file", data)
    assert method == "resources/read"
    assert params == {"uri": "file:///path/to/file", "format": "json", "encoding": "utf-8"}


def test_map_resources_read_empty_path():
    with pytest.raises(ValueError, match="path cannot be empty"):
        map_virtual_path_to_method("/resources/", {})


def test_map_resources_read_with_special_characters():
    method, params = map_virtual_path_to_method("/resources/path/to/my%20file.txt", {})
    assert method == "resources/read"
    assert params == {"uri": "file:///path/to/my%20file.txt"}


def test_map_resources_read_with_multiple_slashes():
    method, params = map_virtual_path_to_method("/resources/path//to///file", {})
    assert method == "resources/read"
    assert params == {"uri": "file:///path//to///file"}


def test_map_resources_read_relative_path():
    method, params = map_virtual_path_to_method("/resources/relative/path", {})
    assert method == "resources/read"
    assert params == {"uri": "file:///relative/path"}


def test_map_resources_read_custom_uri_scheme():
    """Path containing :// is treated as a full URI, not a file path."""
    method, params = map_virtual_path_to_method("/resources/https://example.com/data", {})
    assert method == "resources/read"
    assert params == {"uri": "https://example.com/data"}


def test_map_resources_read_uri_override():
    """The -d uri=... flag overrides the path-derived URI."""
    data = {"uri": "git://repo/file.txt"}
    method, params = map_virtual_path_to_method("/resources/placeholder", data)
    assert method == "resources/read"
    assert params["uri"] == "git://repo/file.txt"


def test_map_prompts_list():
    method, params = map_virtual_path_to_method("/prompts", {})
    assert method == "prompts/list"
    assert params == {}


def test_map_prompts_get():
    data = {"variable": "value"}
    method, params = map_virtual_path_to_method("/prompts/greeting", data)
    assert method == "prompts/get"
    assert params == {"name": "greeting", "arguments": {"variable": "value"}}


def test_parse_headers():
    headers = parse_headers(("Authorization: Bearer token123", "X-Custom: value"))
    assert headers == {
        "Authorization": "Bearer token123",
        "X-Custom": "value"
    }


def test_parse_headers_invalid():
    with pytest.raises(ValueError, match="Invalid header format"):
        parse_headers(("InvalidHeader",))


# Integration tests with real MCP server
# Default output: compact NDJSON (one JSON object per line)

def test_cli_list_tools(mcp_server):
    """Test listing tools outputs NDJSON by default."""
    runner = CliRunner()
    result = runner.invoke(main, [f"{mcp_server}/tools", "--no-auth"])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) == 2
    assert output[0]["name"] == "echo"
    assert output[1]["name"] == "weather"


def test_cli_call_tool_with_data(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/tools/echo",
        "-d", "message=hello",
        "--no-auth"
    ])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) > 0
    assert output[0]["type"] == "text"
    assert output[0]["text"] == "hello"


def test_cli_call_weather_tool(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/tools/weather",
        "-d", "city=Paris",
        "-d", "metric=true",
        "--no-auth"
    ])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) > 0
    assert output[0]["type"] == "text"
    assert "Paris" in output[0]["text"]


def test_cli_list_resources(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [f"{mcp_server}/resources", "--no-auth"])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) == 2
    assert output[0]["uri"] == "file:///path/to/file1.txt"


def test_cli_read_resource(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [f"{mcp_server}/resources/test.txt", "--no-auth"])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) > 0
    assert output[0]["uri"] == "file:///test.txt"
    assert output[0]["text"] == "Mock file content"


def test_cli_list_prompts(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [f"{mcp_server}/prompts", "--no-auth"])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) == 2
    assert output[0]["name"] == "greeting"


def test_cli_get_prompt(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/prompts/greeting",
        "-d", "name=Alice",
        "--no-auth"
    ])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) > 0
    assert output[0]["role"] == "user"
    assert "Alice" in output[0]["content"]["text"]


def test_cli_with_headers(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/prompts",
        "-H", "Authorization: Bearer token123"
    ])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) == 2


def test_cli_verbose_mode(mcp_server):
    """Test -v outputs pretty-printed JSON and debug info."""
    runner = CliRunner()
    result = runner.invoke(main, [f"{mcp_server}/tools", "-v", "--no-auth"])

    assert result.exit_code == 0
    # Verbose mixes debug info (stderr) and pretty JSON (stdout) in CliRunner
    assert "=== MCP Request ===" in result.output or len(result.output) > 0
    # Output should contain indentation (pretty-printed)
    assert '  ' in result.output


def test_cli_json_data(mcp_server):
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/tools/echo",
        "-d", '{"message": "complex json"}',
        "--no-auth"
    ])

    assert result.exit_code == 0
    output = parse_ndjson(result.output)
    assert len(output) > 0
    assert output[0]["type"] == "text"
    assert output[0]["text"] == "complex json"


# Error tests — all errors are structured JSON by default

def test_cli_connection_error():
    """Test connection error outputs structured JSON."""
    runner = CliRunner()
    result = runner.invoke(main, ["http://localhost:9999/tools", "--no-auth"])

    assert result.exit_code == 1
    error_obj = json.loads(result.output.strip())
    assert error_obj["error"] == "CONNECTION_REFUSED"
    assert "Connection refused" in error_obj["message"]


def test_cli_dns_resolution_error():
    """Test DNS error outputs structured JSON."""
    runner = CliRunner()
    result = runner.invoke(main, ["https://invalid-server.test/tools", "--no-auth"])

    assert result.exit_code == 1
    error_obj = json.loads(result.output.strip())
    assert error_obj["error"] == "DNS_RESOLUTION_FAILED"
    assert "DNS resolution failed" in error_obj["message"]


def test_cli_timeout_error():
    """Test timeout error outputs structured JSON."""
    from unittest.mock import patch

    runner = CliRunner()

    with patch("murl.cli.make_mcp_request") as mock_request:
        timeout_exc = TimeoutError("Request timed out")
        mock_request.side_effect = ExceptionGroup("unhandled errors in a TaskGroup", [timeout_exc])

        result = runner.invoke(main, ["http://localhost:8765/tools", "--no-auth"])

    assert result.exit_code == 1
    error_obj = json.loads(result.output.strip())
    assert error_obj["error"] == "TIMEOUT"
    assert "timeout" in error_obj["message"].lower()


def test_cli_generic_connect_error():
    """Test generic ConnectError outputs structured JSON."""
    from unittest.mock import patch

    runner = CliRunner()

    with patch("murl.cli.make_mcp_request") as mock_request:
        class ConnectError(Exception):
            pass

        connect_exc = ConnectError("Some other network error")
        mock_request.side_effect = ExceptionGroup("unhandled errors in a TaskGroup", [connect_exc])

        result = runner.invoke(main, ["http://localhost:8765/tools", "--no-auth"])

    assert result.exit_code == 1
    error_obj = json.loads(result.output.strip())
    assert "Some other network error" in error_obj["message"]


def test_cli_invalid_url():
    """Test invalid URL outputs structured JSON error."""
    runner = CliRunner()
    result = runner.invoke(main, ["http://localhost:3000/invalid"])

    assert result.exit_code == 2
    error_obj = json.loads(result.output.strip())
    assert error_obj["error"] == "INVALID_ARGUMENT"
    assert "Invalid MCP URL" in error_obj["message"]


# Flag tests

def test_version_option():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help():
    """Test --help shows concise help."""
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "USAGE:" in result.output
    assert "EXAMPLES:" in result.output
    assert "AUTHENTICATION:" in result.output
    assert "--login" in result.output
    assert "--no-auth" in result.output


def test_upgrade_option():
    from unittest.mock import patch, MagicMock

    runner = CliRunner()

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Successfully installed mcp-curl-0.2.1"
    mock_result.stderr = ""

    with patch('subprocess.run', return_value=mock_result) as mock_run:
        result = runner.invoke(main, ["--upgrade"])

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        from murl.cli import UPGRADE_SOURCE
        assert call_args[0][0] == [sys.executable, "-m", "pip", "install", "--upgrade", UPGRADE_SOURCE]
        assert "helloextend/murl" in UPGRADE_SOURCE
        assert call_args[1]['timeout'] == 300

    assert result.exit_code == 0
    assert "Upgrading murl" in result.output
    assert "Upgrade complete" in result.output


# Default output format tests

def test_default_list_output_is_ndjson(mcp_server):
    """Default output for lists is compact NDJSON (one JSON object per line)."""
    runner = CliRunner()
    result = runner.invoke(main, [f"{TEST_SERVER_URL}/tools", "--no-auth"])

    assert result.exit_code == 0
    lines = result.output.strip().split('\n')
    assert len(lines) > 0

    for line in lines:
        obj = json.loads(line)
        assert isinstance(obj, dict)
        # Compact: no whitespace after separators
        assert ', ' not in line and '": ' not in line


def test_default_single_output_is_compact(mcp_server):
    """Default output for single results is compact JSON."""
    runner = CliRunner()
    result = runner.invoke(main, [f"{TEST_SERVER_URL}/tools/echo", "-d", "message=test", "--no-auth"])

    assert result.exit_code == 0
    assert '  ' not in result.output  # No indentation
    lines = result.output.strip().split('\n')
    for line in lines:
        obj = json.loads(line)
        assert isinstance(obj, dict)


def test_default_error_is_structured_json():
    """Default error output is structured JSON on stderr."""
    runner = CliRunner()
    result = runner.invoke(main, ["http://localhost:3000/invalid"])

    assert result.exit_code == 2
    error_obj = json.loads(result.output.strip())
    assert "error" in error_obj
    assert "message" in error_obj
    assert "code" in error_obj
    assert error_obj["code"] == 2


def test_default_connection_error_is_structured():
    """Default connection error is structured JSON."""
    runner = CliRunner()
    result = runner.invoke(main, ["http://localhost:19999/tools", "--no-auth"])

    assert result.exit_code == 1
    error_obj = json.loads(result.output.strip())
    assert "error" in error_obj
    assert error_obj["error"] in ["CONNECTION_REFUSED", "CONNECTION_ERROR"]


def test_default_missing_url_is_structured():
    """Missing URL produces structured JSON error."""
    runner = CliRunner()
    result = runner.invoke(main, [])

    assert result.exit_code == 2
    error_obj = json.loads(result.output.strip())
    assert error_obj["error"] == "MISSING_ARGUMENT"
    assert "URL argument is required" in error_obj["message"]


def test_verbose_output_is_pretty_printed(mcp_server):
    """Verbose mode outputs pretty-printed JSON (with indentation)."""
    runner = CliRunner()
    result = runner.invoke(main, [f"{TEST_SERVER_URL}/tools", "-v", "--no-auth"])

    assert result.exit_code == 0
    assert '  ' in result.output  # Has indentation


# OAuth CLI integration tests

def test_cli_login_triggers_oauth(mcp_server):
    """--login clears stored creds, runs OAuth, stores new creds, and makes request."""
    from unittest.mock import patch, MagicMock
    import time as _time

    fake_creds = {
        "client_id": "cid",
        "access_token": "tok_new",
        "refresh_token": "rt",
        "expires_at": _time.time() + 3600,
        "token_endpoint": "https://auth.example.com/token",
        "registration_endpoint": "https://auth.example.com/register",
        "server_url": "http://localhost",
    }

    runner = CliRunner()
    with patch("murl.cli.clear_credentials") as mock_clear, \
         patch("murl.cli.get_credentials", return_value=None), \
         patch("murl.cli.authorize", return_value=fake_creds) as mock_auth, \
         patch("murl.cli.save_credentials") as mock_save:
        result = runner.invoke(main, [f"{TEST_SERVER_URL}/tools", "--login"])

    assert mock_clear.called, "clear_credentials should be called with --login"
    assert mock_auth.called, "authorize should be called with --login"
    assert mock_save.called, "save_credentials should be called after OAuth"
    assert result.exit_code == 0


def test_cli_stored_creds_used_without_login(mcp_server):
    """Valid stored credentials are injected as Authorization header without prompting OAuth."""
    from unittest.mock import patch
    import time as _time

    fake_creds = {
        "access_token": "tok_stored",
        "expires_at": _time.time() + 3600,
    }

    runner = CliRunner()
    with patch("murl.cli.get_credentials", return_value=fake_creds), \
         patch("murl.cli.is_expired", return_value=False), \
         patch("murl.cli.authorize") as mock_auth:
        result = runner.invoke(main, [f"{TEST_SERVER_URL}/tools"])

    assert not mock_auth.called, "authorize should NOT be called when valid creds exist"
    assert result.exit_code == 0


def test_cli_401_retry_triggers_oauth(mcp_server):
    """A 401 error on first request triggers OAuth and retries."""
    from unittest.mock import patch
    import time as _time

    fake_creds = {
        "client_id": "cid",
        "access_token": "tok_retry",
        "refresh_token": "rt",
        "expires_at": _time.time() + 3600,
        "token_endpoint": "https://auth.example.com/token",
        "registration_endpoint": "https://auth.example.com/register",
        "server_url": "http://localhost",
    }

    call_count = [0]
    original_make_mcp_request = None

    async def mock_make_mcp_request(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise Exception("HTTP 401 Unauthorized")
        # Import the real function and call it for the retry
        from murl.cli import make_mcp_request as real_fn
        return await real_fn(*args, **kwargs)

    runner = CliRunner()
    with patch("murl.cli.make_mcp_request", side_effect=mock_make_mcp_request), \
         patch("murl.cli.authorize", return_value=fake_creds) as mock_auth, \
         patch("murl.cli.save_credentials"):
        result = runner.invoke(main, [f"{TEST_SERVER_URL}/tools"])

    assert mock_auth.called, "authorize should be called after 401"
    assert call_count[0] >= 2, "Should retry after 401"


def test_cli_403_step_up_triggers_reauth(mcp_server):
    """A 403 with insufficient_scope triggers re-authorization with required scopes."""
    from unittest.mock import patch, MagicMock
    import time as _time
    import httpx

    fake_creds = {
        "client_id": "cid",
        "access_token": "tok_stepup",
        "refresh_token": "rt",
        "expires_at": _time.time() + 3600,
        "token_endpoint": "https://auth.example.com/token",
        "registration_endpoint": "https://auth.example.com/register",
        "server_url": "http://localhost",
    }

    call_count = [0]

    async def mock_make_mcp_request(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            # Simulate a 403 with WWW-Authenticate header
            mock_response = MagicMock(spec=httpx.Response)
            mock_response.status_code = 403
            mock_response.headers = {
                "www-authenticate": 'Bearer error="insufficient_scope", scope="files:write"'
            }
            mock_response.text = "Forbidden"
            mock_response.stream = MagicMock()
            raise httpx.HTTPStatusError(
                "403 Forbidden",
                request=MagicMock(spec=httpx.Request),
                response=mock_response,
            )
        # Return a successful result on retry
        return [{"name": "test_tool"}]

    runner = CliRunner()
    with patch("murl.cli.make_mcp_request", side_effect=mock_make_mcp_request), \
         patch("murl.cli.authorize", return_value=fake_creds) as mock_auth, \
         patch("murl.cli.save_credentials"):
        result = runner.invoke(main, [f"{TEST_SERVER_URL}/tools"])

    assert mock_auth.called, "authorize should be called after 403 insufficient_scope"
    # Verify scope was passed to authorize
    _, auth_kwargs = mock_auth.call_args
    assert auth_kwargs.get("scope") == "files:write", "authorize should be called with the required scope"
    assert call_count[0] >= 2, "Should retry after 403"
    assert result.exit_code == 0


def test_cli_no_auth_skips_all_auth(mcp_server):
    """--no-auth skips credential loading and OAuth entirely."""
    from unittest.mock import patch

    runner = CliRunner()
    with patch("murl.cli.get_credentials") as mock_get, \
         patch("murl.cli.authorize") as mock_auth:
        result = runner.invoke(main, [f"{TEST_SERVER_URL}/tools", "--no-auth"])

    assert not mock_get.called, "get_credentials should NOT be called with --no-auth"
    assert not mock_auth.called, "authorize should NOT be called with --no-auth"
    assert result.exit_code == 0


# --- TOON format tests ---


def test_toon_output_tools_list(mcp_server):
    """--format toon outputs TOON format for tools/list."""
    runner = CliRunner()
    result = runner.invoke(main, [f"{mcp_server}/tools", "--format", "toon", "--no-auth"])

    assert result.exit_code == 0
    output = result.output.strip()
    # Should contain tool names from test server
    assert 'echo' in output
    # Should NOT be valid JSON (it's TOON)
    with pytest.raises(json.JSONDecodeError):
        json.loads(output)


def test_toon_output_tool_call(mcp_server):
    """--format toon outputs TOON format for tool call results."""
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/tools/echo",
        "-d", "message=hello",
        "--format", "toon", "--no-auth"
    ])

    assert result.exit_code == 0
    output = result.output.strip()
    assert 'hello' in output
    # Verify output is TOON, not passthrough JSON
    with pytest.raises(json.JSONDecodeError):
        json.loads(output)


def test_toon_format_verbose_mutually_exclusive(mcp_server):
    """--format and --verbose together should error."""
    runner = CliRunner()
    result = runner.invoke(main, [
        f"{mcp_server}/tools", "--format", "toon", "-v", "--no-auth"
    ])

    assert result.exit_code == 2
    error_obj = json.loads(result.output.strip())
    assert error_obj["error"] == "INVALID_ARGUMENT"
    assert "mutually exclusive" in error_obj["message"]


def test_toon_roundtrip_tools_list(mcp_server):
    """TOON output can be decoded back to the original data."""
    from toon import decode as toon_decode

    runner = CliRunner()

    # Get JSON output
    json_result = runner.invoke(main, [f"{mcp_server}/tools", "--no-auth"])
    json_data = parse_ndjson(json_result.output)

    # Get TOON output
    toon_result = runner.invoke(main, [f"{mcp_server}/tools", "--format", "toon", "--no-auth"])
    toon_data = toon_decode(toon_result.output.strip())

    assert toon_data == json_data


def test_toon_missing_dependency(mcp_server):
    """--format toon with missing python-toon shows helpful error."""
    from unittest.mock import patch

    runner = CliRunner()
    with patch("murl.cli.toon_encode", None):
        result = runner.invoke(main, [f"{mcp_server}/tools", "--format", "toon", "--no-auth"])

    assert result.exit_code == 1
    error_obj = json.loads(result.output.strip())
    assert error_obj["error"] == "MISSING_DEPENDENCY"
    assert "mcp-curl[toon]" in error_obj["suggestion"]


def test_toon_nested_objects(mcp_server):
    """TOON handles tools with nested inputSchema correctly."""
    from toon import decode as toon_decode

    runner = CliRunner()
    toon_result = runner.invoke(main, [f"{mcp_server}/tools", "--format", "toon", "--no-auth"])

    assert toon_result.exit_code == 0
    decoded = toon_decode(toon_result.output.strip())

    # Verify nested inputSchema survived the roundtrip
    tools_with_schema = [t for t in decoded if 'inputSchema' in t]
    assert len(tools_with_schema) > 0
    for tool in tools_with_schema:
        assert isinstance(tool['inputSchema'], dict)


def test_toon_empty_result(mcp_server):
    """TOON handles empty list results without error."""
    from unittest.mock import patch, AsyncMock

    runner = CliRunner()
    with patch("murl.cli.make_mcp_request", new_callable=AsyncMock, return_value=[]):
        result = runner.invoke(main, [f"{mcp_server}/tools", "--format", "toon", "--no-auth"])

    assert result.exit_code == 0
    assert '[0]:' in result.output


def test_cli_unsupported_capability_error():
    """CLI outputs structured INVALID_ARGUMENT error when server lacks the requested capability."""
    from unittest.mock import patch, AsyncMock

    runner = CliRunner()
    with patch(
        "murl.cli.make_mcp_request",
        new_callable=AsyncMock,
        side_effect=ValueError("Server does not support 'resources'. Supported: tools, logging"),
    ):
        result = runner.invoke(main, ["http://localhost:8080/resources", "--no-auth"])

    assert result.exit_code == 2
    error_obj = json.loads(result.output.strip())
    assert error_obj["error"] == "INVALID_ARGUMENT"
    assert "Server does not support 'resources'" in error_obj["message"]
    assert "tools, logging" in error_obj["message"]


# ---------------------------------------------------------------------------
# Pagination tests — MCP 2025-11-25 cursor-based pagination
# ---------------------------------------------------------------------------

def test_pagination_tools_list():
    """tools/list must follow nextCursor and collect all pages."""
    from unittest.mock import patch, AsyncMock, MagicMock
    from murl.cli import make_mcp_request

    # Build two pages of tools
    tool_a = MagicMock()
    tool_a.model_dump.return_value = {"name": "tool_a"}
    tool_b = MagicMock()
    tool_b.model_dump.return_value = {"name": "tool_b"}
    tool_c = MagicMock()
    tool_c.model_dump.return_value = {"name": "tool_c"}

    page1 = MagicMock()
    page1.tools = [tool_a, tool_b]
    page1.nextCursor = "cursor_page2"

    page2 = MagicMock()
    page2.tools = [tool_c]
    page2.nextCursor = None

    mock_session = AsyncMock()
    mock_session.list_tools = AsyncMock(side_effect=[page1, page2])
    mock_session.initialize = AsyncMock()

    init_result = MagicMock()
    init_result.capabilities.tools = MagicMock()
    init_result.capabilities.resources = None
    init_result.capabilities.prompts = None
    init_result.protocolVersion = "2025-11-25"
    init_result.serverInfo.name = "test"
    init_result.serverInfo.version = "1.0"
    mock_session.initialize.return_value = init_result

    import contextlib

    @contextlib.asynccontextmanager
    async def mock_http_client(*args, **kwargs):
        yield (AsyncMock(), AsyncMock(), MagicMock())

    @contextlib.asynccontextmanager
    async def mock_client_session(read, write):
        yield mock_session

    with patch("murl.cli.streamable_http_client", mock_http_client), \
         patch("murl.cli.ClientSession", mock_client_session):
        import asyncio
        result = asyncio.run(make_mcp_request(
            "http://localhost:8080", "tools/list", {}, {}, False
        ))

    assert len(result) == 3
    assert result[0]["name"] == "tool_a"
    assert result[2]["name"] == "tool_c"
    # Verify cursor was passed on second call
    assert mock_session.list_tools.call_count == 2
    second_call_kwargs = mock_session.list_tools.call_args_list[1]
    assert second_call_kwargs[1]["cursor"] == "cursor_page2"


def test_pagination_resources_list():
    """resources/list must follow nextCursor and collect all pages."""
    from unittest.mock import patch, AsyncMock, MagicMock
    from murl.cli import make_mcp_request

    res_a = MagicMock()
    res_a.model_dump.return_value = {"uri": "file:///a.txt", "name": "a"}
    res_b = MagicMock()
    res_b.model_dump.return_value = {"uri": "file:///b.txt", "name": "b"}

    page1 = MagicMock()
    page1.resources = [res_a]
    page1.nextCursor = "page2"

    page2 = MagicMock()
    page2.resources = [res_b]
    page2.nextCursor = None

    mock_session = AsyncMock()
    mock_session.list_resources = AsyncMock(side_effect=[page1, page2])
    mock_session.initialize = AsyncMock()

    init_result = MagicMock()
    init_result.capabilities.tools = None
    init_result.capabilities.resources = MagicMock()
    init_result.capabilities.prompts = None
    init_result.protocolVersion = "2025-11-25"
    init_result.serverInfo.name = "test"
    init_result.serverInfo.version = "1.0"
    mock_session.initialize.return_value = init_result

    import contextlib

    @contextlib.asynccontextmanager
    async def mock_http_client(*args, **kwargs):
        yield (AsyncMock(), AsyncMock(), MagicMock())

    @contextlib.asynccontextmanager
    async def mock_client_session(read, write):
        yield mock_session

    with patch("murl.cli.streamable_http_client", mock_http_client), \
         patch("murl.cli.ClientSession", mock_client_session):
        import asyncio
        result = asyncio.run(make_mcp_request(
            "http://localhost:8080", "resources/list", {}, {}, False
        ))

    assert len(result) == 2
    assert result[0]["name"] == "a"
    assert result[1]["name"] == "b"
    assert mock_session.list_resources.call_count == 2


def test_pagination_prompts_list():
    """prompts/list must follow nextCursor and collect all pages."""
    from unittest.mock import patch, AsyncMock, MagicMock
    from murl.cli import make_mcp_request

    prompt_a = MagicMock()
    prompt_a.model_dump.return_value = {"name": "greeting"}
    prompt_b = MagicMock()
    prompt_b.model_dump.return_value = {"name": "farewell"}

    page1 = MagicMock()
    page1.prompts = [prompt_a]
    page1.nextCursor = "next"

    page2 = MagicMock()
    page2.prompts = [prompt_b]
    page2.nextCursor = None

    mock_session = AsyncMock()
    mock_session.list_prompts = AsyncMock(side_effect=[page1, page2])
    mock_session.initialize = AsyncMock()

    init_result = MagicMock()
    init_result.capabilities.tools = None
    init_result.capabilities.resources = None
    init_result.capabilities.prompts = MagicMock()
    init_result.protocolVersion = "2025-11-25"
    init_result.serverInfo.name = "test"
    init_result.serverInfo.version = "1.0"
    mock_session.initialize.return_value = init_result

    import contextlib

    @contextlib.asynccontextmanager
    async def mock_http_client(*args, **kwargs):
        yield (AsyncMock(), AsyncMock(), MagicMock())

    @contextlib.asynccontextmanager
    async def mock_client_session(read, write):
        yield mock_session

    with patch("murl.cli.streamable_http_client", mock_http_client), \
         patch("murl.cli.ClientSession", mock_client_session):
        import asyncio
        result = asyncio.run(make_mcp_request(
            "http://localhost:8080", "prompts/list", {}, {}, False
        ))

    assert len(result) == 2
    assert result[0]["name"] == "greeting"
    assert result[1]["name"] == "farewell"
    assert mock_session.list_prompts.call_count == 2


def test_pagination_single_page_no_extra_calls():
    """When nextCursor is None on first page, no additional requests are made."""
    from unittest.mock import patch, AsyncMock, MagicMock
    from murl.cli import make_mcp_request

    tool = MagicMock()
    tool.model_dump.return_value = {"name": "only_tool"}

    page = MagicMock()
    page.tools = [tool]
    page.nextCursor = None

    mock_session = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=page)
    mock_session.initialize = AsyncMock()

    init_result = MagicMock()
    init_result.capabilities.tools = MagicMock()
    init_result.capabilities.resources = None
    init_result.capabilities.prompts = None
    init_result.protocolVersion = "2025-11-25"
    init_result.serverInfo.name = "test"
    init_result.serverInfo.version = "1.0"
    mock_session.initialize.return_value = init_result

    import contextlib

    @contextlib.asynccontextmanager
    async def mock_http_client(*args, **kwargs):
        yield (AsyncMock(), AsyncMock(), MagicMock())

    @contextlib.asynccontextmanager
    async def mock_client_session(read, write):
        yield mock_session

    with patch("murl.cli.streamable_http_client", mock_http_client), \
         patch("murl.cli.ClientSession", mock_client_session):
        import asyncio
        result = asyncio.run(make_mcp_request(
            "http://localhost:8080", "tools/list", {}, {}, False
        ))

    assert len(result) == 1
    assert result[0]["name"] == "only_tool"
    # Only one call — no extra pagination requests
    assert mock_session.list_tools.call_count == 1


def test_cli_oautherror_surfaces_as_auth_failed():
    """An OAuthError from the auth flow surfaces as structured AUTH_FAILED.

    Drives the `except OAuthError` handler in cli.py: a port conflict (or any
    auth failure) must produce {"error":"AUTH_FAILED", ...} with the raise
    site's failure-specific suggestion — not the generic ERROR fall-through.
    No live server is needed; authorize() raises before any request is made.
    """
    from unittest.mock import patch
    from murl.auth import OAuthError

    runner = CliRunner()
    with patch("murl.cli.clear_credentials"), \
         patch("murl.cli.get_credentials", return_value=None), \
         patch("murl.cli.save_credentials"), \
         patch(
             "murl.cli.authorize",
             side_effect=OAuthError(
                 "Callback port 8080 is already in use.",
                 suggestion="Pass --callback-port <free-port> ...",
             ),
         ):
        result = runner.invoke(main, [f"{TEST_SERVER_URL}/tools", "--login"])

    assert result.exit_code != 0
    payload = json.loads([l for l in result.output.splitlines() if l.strip().startswith("{")][-1])
    assert payload["error"] == "AUTH_FAILED"
    assert "8080" in payload["message"]
    # The failure-specific suggestion is preserved, not replaced by the --login default.
    assert payload["suggestion"] == "Pass --callback-port <free-port> ..."


def test_cli_oautherror_without_suggestion_falls_back_to_login_hint():
    """An OAuthError carrying no suggestion falls back to the --login remedy."""
    from unittest.mock import patch
    from murl.auth import OAuthError

    runner = CliRunner()
    with patch("murl.cli.clear_credentials"), \
         patch("murl.cli.get_credentials", return_value=None), \
         patch("murl.cli.save_credentials"), \
         patch("murl.cli.authorize", side_effect=OAuthError("token exchange failed")):
        result = runner.invoke(main, [f"{TEST_SERVER_URL}/tools", "--login"])

    assert result.exit_code != 0
    payload = json.loads([l for l in result.output.splitlines() if l.strip().startswith("{")][-1])
    assert payload["error"] == "AUTH_FAILED"
    assert "--login" in payload["suggestion"]
