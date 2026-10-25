# The Camoufox browser

A second kind of server browser for Bench's `/browser` insert: [Camoufox](https://camoufox.com)
(an anti-detect Firefox), driven by the agent through
[camoufox-playwright-mcp](https://github.com/chandu-cpz/camoufox-playwright-mcp) (the Playwright
MCP's tools, in Python). Use it where sites turn a Chrome away; the shared Chrome is `../`.

One container: Xvfb, Camoufox (headed), and one Python process, `live.py`, that is the MCP on
`:8931/mcp` and the live view on `:8931/live`. The view is in the MCP's own process because the MCP
holds Firefox over a pipe (Juggler) that no other process can attach to. It speaks the protocol of
`../live.mjs`, so Bench watches either browser the same way. Firefox has no screencast: a frame is
a screenshot, at most two a second, only while someone watches that tab, and not sent again while
the page doesn't change. noVNC on `:6080` stays for the rare case of the whole window.

## Set it up

```bash
docker build -t browser:latest .
docker compose -f compose.yml up -d
docker exec browser python3 /opt/live.py add anna     # a person's key for the live view (prints once)
docker exec browser python3 /opt/live.py list
docker exec browser python3 /opt/live.py remove anna
```

## Connect Bench

**The agent**: the project's `mcp.json` names the browser's watch address in `live`; that server is
the one the project's `/browser` shows (its tools are `mcp__<name>__*`):

```json
{ "mcpServers": { "camoufox": { "url": "http://<host>:10150/mcp", "transport": "http", "live": "ws://<host>:10150/live" } } }
```

**The person's key**, in `~/.bench/ui/browser.json` on their Mac, by that address:

```json
{ "url": "wss://browser.example.com/live", "token": "<the Chrome's key>",
  "keys": { "ws://<host>:10150/live": "<their key here>" } }
```

The MCP port has no authentication of its own: keep it on a private network (or behind a proxy
with basic auth for `/mcp`; `/live` checks its own keys).
