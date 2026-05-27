# murl v1 — Redesign Proposal

**Status:** Proposal — not implemented. Filed to capture the design before details fade.
**Audience:** murl contributors and downstream consumers.
**Author:** Mike Darmousseh (turlockmike).

## Problem statement

`murl` shipped fast and has grown a flag surface that doesn't match what an
agent-first CLI for MCP wants to be. Three problems stand out — none of
them are correctness bugs in 0.5.x, but each will cost more to unwind the
longer it ships.

### 1. URL-as-method has reserved path segments

`parse_url` regex-matches `/(tools|resources|prompts)(/.*)?$` against the
full URL and splits on the match. This means:

- A server whose endpoint is `https://example.com/my/tools/production/mcp`
  cannot be addressed — the regex grabs `/tools/production/mcp` and treats
  `production` as the tool name.
- `/tools`, `/resources`, `/prompts` are now reserved path segments that
  any operator deploying an MCP server has to avoid.

The URL is no longer a URL. It is a syntax tree encoded as a URL — and
because URL-shape is what callers reach for first, the encoding is
invisible until it fails.

### 2. Output post-processing lives behind opt-in flags

`tools/call` responses come back as a `[{type:"text", text:"<json>"}]`
envelope. Every shell consumer ends up writing
`jq -r '.[0].text' | jq '...'`. 0.5.0 made auto-unwrap and auto-paginate
the default, which fixed the common case — but the *category* of
problem remains. Callers shouldn't need to know about the envelope
existing, the cursor protocol, or which flag toggles which behavior.
The shipped surface (`--raw`, `--max-pages`, `--no-mcp-config`,
`--format toon`) is still a flag pile that grew because each new
behavior got its own flag.

### 3. Three identical operations, three encoding styles

Tools, resources, and prompts all support `list` and one invocation
verb. Today they look like this:

```
murl https://server/mcp/tools/echo -d message=hi      # tool: -d args
murl https://server/mcp/resources/file:///etc/hosts   # resource: URI in path
murl https://server/mcp/prompts/greet -d name=Alice   # prompt: -d args
```

Three different ways to identify a target. An LLM constructing a
command has to learn the URL-rewrite rules for each category separately.

## Proposal: positional arguments

Drop URL-as-method. Make the operation tree positional, like every
modern CLI an LLM has been trained on:

```bash
murl <server-url> tools                          # tools/list
murl <server-url> tools <name> [-d key=value]    # tools/call <name>
murl <server-url> resources                      # resources/list
murl <server-url> resources <uri>                # resources/read <uri>
murl <server-url> prompts                        # prompts/list
murl <server-url> prompts <name> [-d key=value]  # prompts/get <name>
```

Server URL is position 1. Category is position 2. Name/URI (if any) is
position 3. The method is derivable: two positional args = list, three
= invoke. No regex required. No reserved path segments. The server URL
is opaque text that gets POSTed as-is.

### Why this is better

- **Consistent across categories.** Same shape for tools, resources,
  prompts. The model learns one pattern.
- **No URL-rewriting rules to remember.** The URL goes in the URL slot;
  the operation goes in the operation slot.
- **Easier to extend.** If MCP adds a fourth category (or a new
  per-category verb), it's a new positional value, not a new regex
  branch.
- **Matches training priors.** `git <category> <name>`,
  `kubectl get <kind> <name>`, `aws <service> <verb>` — every CLI
  the model has seen does this.

## Flag set after the redesign

The full v1 surface, in alphabetical order, after taking everything
shipped in 0.5.x and removing what becomes obsolete:

```
-d, --data <key=value|@file|@->        Request data (repeatable)
-H, --header <Key: Value>              HTTP header (repeatable)
--login                                Force OAuth re-auth
--no-auth                              Skip authentication
--client-id <id>                       Pre-registered OAuth client ID
--client-secret <secret>               Pre-registered OAuth client secret
--callback-port <n>                    Fixed OAuth callback port
--scope <scopes>                       OAuth scopes
--no-mcp-config                        Skip .mcp.json discovery
--raw                                  Disable auto-unwrap and cursor-follow
--max-pages <N>                        Cap auto-pagination (default 1000)
-v, --verbose                          Pretty-print, debug info
--version
--upgrade
```

Removed vs 0.5.x:
- **`--format toon`** — TOON belongs as a separate pipe filter
  (`murl ... | toon`), not as an output-mode toggle inside murl. One
  fewer code path, one fewer dependency, one fewer thing to document.

That's it. Everything else stays. The flag surface doesn't grow.

## Migration path

The new shape and the old shape can coexist for one or two releases by
detecting whether the second positional arg is a category keyword.

```python
# Pseudocode for the dispatch
def main(arg1, arg2=None, arg3=None, ...):
    if arg2 in ("tools", "resources", "prompts"):
        # New positional shape
        category, name = arg2, arg3
        server_url = arg1
    else:
        # Legacy URL-as-method shape — emit a deprecation warning
        server_url, virtual_path = parse_url(arg1)
        ...
```

Each invocation in legacy shape would print a one-line stderr
deprecation pointing at this doc. v2 (the next breaking release) drops
the legacy path entirely.

## What I'm asking for

Two decisions:

1. **Is the positional shape the right v1 target?** If yes, this becomes
   the work plan for v1.x. If a different shape is better — say it now,
   not after we've burned a release on this one.

2. **Is the `--format toon` removal too aggressive?** TOON has real
   users at Extend. Moving it to a pipe filter means those users need
   to install one more thing. If that cost is too high, leave it. The
   rest of the proposal stands regardless.

Anything else (timing, who implements, what version number it lands
under) is downstream of those two.

## Appendix — what 0.5.x got right

So the redesign doesn't sound like a full rewrite request:

- **NDJSON-by-default for lists.** Right.
- **Compact JSON to stdout, structured errors to stderr.** Right.
- **`-d key=value | JSON | @file | @-`.** Right. Don't touch.
- **OAuth in-band (handles 401, refreshes, retries).** Right. Agents
  cannot orchestrate side-channel auth flows; murl owning this is
  correct.
- **Auto-unwrap envelope, auto-follow cursors.** Right (0.5.0).
- **Tool-level `AUTH_FAILED` detection.** Right (0.5.0).
- **`.mcp.json` discovery with exact URL match.** Right (0.5.0,
  hardened in #7 review).

The 0.5.x defaults are correct. The surface shape isn't.
