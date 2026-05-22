# Changelog

## 0.5.0

**Breaking — `tools/call` output shape changed by default.**

Four protocol-level quirks that every shell consumer used to hand-roll
are now handled transparently. All zero-config; `--raw` opts out.

### Added

- **Auto-unwrap** the canonical `[{type:"text", text:"<json>"}]` envelope.
  When a `tools/call` response is a single text block containing valid
  JSON, murl emits the inner JSON directly.
- **Auto-follow `nextCursor`** on `tools/call` responses. When the
  unwrapped body carries `{hasMore: true, nextCursor}`, murl follows the
  cursor and emits aggregated items as NDJSON. Strips `perPage` /
  `pageSize` / `status` on cursor calls (many MCP tools reject the
  combination since the cursor already encodes that context). Cap with
  `--max-pages` (default 1000); cursor-cycle protection built in.
- **`.mcp.json` discovery.** Walks up from CWD, exact-matches the base
  URL against `mcpServers[*].url` (URL-decoded, trailing-slash normalized),
  and fills `--client-id` / `--callback-port` from `oauth.clientId` /
  `oauth.callbackPort`. Mirrors Claude Code's discovery semantics.
- **Tool-level `AUTH_FAILED` detection.** When the response body carries
  `{error: {code: "invalid_token"}}` or an `AUTH_FAILED` marker (HTTP 200
  but the downstream provider rejected the token), murl exits with a
  structured `AUTH_FAILED` error to stderr instead of forcing callers to
  substring-match on stdout.
- **`--raw`** — opt out of auto-unwrap and cursor follow; return the
  protocol response verbatim.
- **`--max-pages <N>`** — cap auto-pagination.
- **`--no-mcp-config`** — skip `.mcp.json` discovery.

### Breaking

- **`tools/call` output is now the unwrapped inner JSON by default.** A
  call that returned `[{"type":"text","text":"{...}"}]` in 0.4.0 now
  returns the parsed `{...}` object. Pass `--raw` to preserve the old
  shape.
- **Paginated `tools/call` responses now emit NDJSON of aggregated
  items.** A call that returned a single envelope with a `nextCursor` in
  0.4.0 now emits one JSON object per line across all pages. Pass
  `--raw` to disable cursor-follow.

### Migration

```bash
# 0.4.0 — manual unwrap + cursor loop
RAW=$(murl "$URL/tools/list_things" -d filter=active)
INNER=$(jq -r '.[0].text' <<<"$RAW")
jq -c '.things[]' <<<"$INNER" > all.ndjson
# ...followed by hand-rolled nextCursor follow loop

# 0.5.0 — same result
murl "$URL/tools/list_things" -d filter=active > all.ndjson

# 0.5.0 — preserve 0.4.0 behavior
murl "$URL/tools/list_things" -d filter=active --raw
```

## 0.4.0

- `--client-id` skips origin check (#6)
- Host-level fallback for OAuth metadata discovery (#5)
- MCP 2025-11-25 spec compliance + real-world OAuth (#2)
- System keychain credential storage with filesystem fallback (#4)
- `-d @-` and `-d @file` for Unix pipeline composability (#3)
- TOON output format support (#1)
