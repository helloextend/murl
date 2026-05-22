# murl - MCP Curl

[![Tests](https://github.com/turlockmike/murl/actions/workflows/test.yml/badge.svg)](https://github.com/turlockmike/murl/actions/workflows/test.yml)

A curl-like CLI for [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers. Query tools, resources, and prompts using simple REST-like URLs.

**LLM-friendly:** compact JSON output, NDJSON streaming, structured errors to stderr, semantic exit codes. Built for agents to call from shell.

<p align="center">
  <img src="images/logo.png" alt="murl logo" width="400">
</p>

## Installation

### Homebrew (Recommended)

```bash
brew install turlockmike/murl/murl
```

### pip

```bash
pip install mcp-curl
```

### Shell script

```bash
curl -sSL https://raw.githubusercontent.com/turlockmike/murl/master/install.sh | bash
```

To upgrade: `brew upgrade turlockmike/murl/murl` or `murl --upgrade`

## Quick Start

```bash
# List tools on a server (NDJSON output — one JSON object per line)
murl https://mcp.deepwiki.com/mcp/tools | jq -r '.name'

# Call a tool — response is auto-unwrapped from the MCP text envelope
murl https://remote.mcpservers.org/fetch/mcp/tools/fetch -d url=https://example.com

# Paginated tools auto-follow nextCursor and emit NDJSON
murl https://your-server/mcp/tools/list_things -d filter=active > all.ndjson
```

**Public demo servers:**
- `https://mcp.deepwiki.com/mcp` — GitHub repository docs
- `https://remote.mcpservers.org/fetch/mcp` — fetch web content

## Usage

```bash
murl <url> [options]
```

### URL Mapping

| URL Path | MCP Method |
|---|---|
| `/tools` | `tools/list` |
| `/tools/<name>` | `tools/call` |
| `/resources` | `resources/list` |
| `/resources/<path>` | `resources/read` |
| `/prompts` | `prompts/list` |
| `/prompts/<name>` | `prompts/get` |

### Default Behavior

`tools/call` responses are post-processed by default so the common case
needs zero flags:

1. **Auto-unwrap the text envelope.** MCP tool responses are returned as
   `[{type:"text", text:"<stringified JSON>"}]`. murl detects this shape
   and emits the inner JSON directly.
2. **Auto-follow `nextCursor`.** When the unwrapped body carries
   `{hasMore: true, nextCursor}`, murl follows the cursor and emits the
   aggregated items as NDJSON to stdout. Capped at `--max-pages` (default
   1000) with cursor-cycle protection.
3. **Detect tool-level auth failures.** When the response body carries
   `{error: {code: "invalid_token"}}` or an `AUTH_FAILED` marker (HTTP
   200 but the downstream provider rejected the token), murl exits with
   a structured `AUTH_FAILED` error to stderr.

Use `--raw` to disable all three and return the protocol response verbatim.

### Examples

```bash
# List tools
murl http://localhost:3000/tools

# Call a tool (response is auto-unwrapped to inner JSON)
murl http://localhost:3000/tools/echo -d message=hello

# Paginated tool — get every page as NDJSON, no manual cursor loop
murl http://localhost:3000/tools/list_things -d filter=active > all.ndjson

# Cap pagination
murl http://localhost:3000/tools/list_things --max-pages 5

# Multiple arguments (auto type-coerced)
murl http://localhost:3000/tools/weather -d city=Paris -d metric=true

# JSON data
murl http://localhost:3000/tools/config -d '{"settings": {"theme": "dark"}}'

# Custom headers
murl http://localhost:3000/tools -H "Authorization: Bearer token123"

# Raw protocol response (no unwrap, no cursor follow)
murl http://localhost:3000/tools/echo -d message=hi --raw

# Verbose mode (pretty-prints output, shows request debug info)
murl http://localhost:3000/tools -v
```

### Options

| Flag | Description |
|---|---|
| `-d, --data` | Add `key=value`, JSON, `@file`, or `@-` (stdin). Repeatable. |
| `-H, --header` | Add HTTP header. Repeatable. |
| `-v, --verbose` | Pretty-print output, show request debug info. |
| `--raw` | Skip auto-unwrap and cursor follow; emit the protocol response verbatim. |
| `--max-pages <N>` | Cap auto-pagination on `tools/call` responses (default 1000). |
| `--format toon` | Output in [TOON](https://github.com/toon-format/spec) format. |
| `--login` | Force OAuth re-authentication. |
| `--no-auth` | Skip all authentication. |
| `--client-id <id>` | Pre-registered OAuth client ID (skips dynamic registration). |
| `--client-secret <s>` | Pre-registered OAuth client secret (or `MURL_CLIENT_SECRET`). |
| `--callback-port <n>` | Fixed port for the OAuth callback redirect URI. |
| `--scope <scopes>` | OAuth scope to request. |
| `--no-mcp-config` | Skip `.mcp.json` discovery for OAuth client defaults. |
| `--version` | Show version info. |
| `--upgrade` | Upgrade to latest version. |

### TOON Output

Use `--format toon` for [TOON](https://github.com/toon-format/spec) output, which uses fewer tokens than JSON when feeding results into LLM contexts. Requires the optional `python-toon` package:

```bash
pip install mcp-curl[toon]

# List tools in TOON format — produces fewer tokens than JSON for structured data
murl https://mcp.deepwiki.com/mcp/tools --format toon
```

### OAuth

murl supports OAuth 2.0 with Dynamic Client Registration (RFC 7591) and PKCE. Tokens are cached automatically.

```bash
# First call triggers browser-based OAuth flow
murl https://example.com/mcp/tools

# Skip auth for public servers
murl https://example.com/mcp/tools --no-auth

# Force re-authentication
murl https://example.com/mcp/tools --login

# Pre-registered OAuth client (skip Dynamic Client Registration)
murl --client-id 0oax4xo --callback-port 8080 https://example.com/mcp/tools
```

### `.mcp.json` Discovery

murl walks up from the current directory looking for a sibling `.mcp.json`
and uses it to fill in OAuth defaults — same convention Claude Code uses.
When the requested base URL exactly matches an entry's `url` field (after
URL decoding and trailing-slash normalization), murl picks up:

- `oauth.clientId` → fills `--client-id` if not passed
- `oauth.callbackPort` → fills `--callback-port` if not passed

```json
{
  "mcpServers": {
    "myserver": {
      "type": "http",
      "url": "https://example.com/mcp",
      "oauth": {
        "clientId": "0oax4xo",
        "callbackPort": 8080
      }
    }
  }
}
```

Matching is **exact** (not prefix): a caller URL with extra path segments
or a different query string is treated as a different runtime and falls
through to dynamic client registration. Use `--no-mcp-config` to disable
discovery entirely.

## Documentation

- [Output & Exit Codes](docs/agent-mode.md) — NDJSON format, structured errors, exit codes
- [MCP Server Setup](docs/mcp-servers.md) — mcp-proxy, Streamable HTTP, local servers
- [Contributing](docs/contributing.md) — development setup, testing, releasing
- [CHANGELOG](CHANGELOG.md) — release history and breaking changes

## Requirements

- Python 3.10+

## License

MIT
