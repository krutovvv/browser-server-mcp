# The Camoufox browser's MCP with a live view for Bench, in one process.
#
#   python3 /opt/live.py [camoufox-playwright-mcp's own flags]   the MCP on :PORT/mcp, the view on :PORT/live
#   python3 /opt/live.py add <name> | remove <name> | list        the people's keys
#
# Why in the MCP's process: camoufox-playwright-mcp drives Firefox over a pipe (Juggler) that only
# its own process holds; there is no DevTools port another process could attach to, as live.mjs
# does with Chrome. So this starts the same MCP (its own `main`, its own flags) and adds a WebSocket
# route to its HTTP app that works on the very BrowserContext the agent's tools use: the person sees
# the agent's tabs and the agent sees a tab the person opens.
#
# The protocol is live.mjs's (Bench's `remote_browser.rs` speaks it to both):
#   → {op:"tabs"} | {op:"watch",tab} | {op:"wake",tab} | {op:"unwatch"} | {op:"open",url} | {op:"close",tab}
#     {op:"mouse",kind:"mousePressed"|"mouseReleased"|"mouseMoved",x,y,button,clicks}
#     {op:"wheel",x,y,dx,dy} | {op:"text",text} | {op:"key",key,code,vk,modifiers}
#   ← {tabs:[{id,url,title}]} (and on every change) | {opened:id} | {woke:id, as:id}
#     {frame:base64 jpeg,w,h,tab} | {error:"…"}
# Firefox has no screencast: a frame is page.screenshot, at most one every FRAME_GAP_MS and only
# while someone watches that tab; an unchanged picture is not sent again. No tab ever sleeps here
# (`wake` answers with the same id).
#
# Access: ?token=… — a person's own key; only its sha256 is kept (/data/live-users.json). Every
# operation goes to /data/live-audit.log (time, person, operation, detail).

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

USERS = Path(os.environ.get("LIVE_USERS", "/data/live-users.json"))
AUDIT = Path(os.environ.get("LIVE_AUDIT", "/data/live-audit.log"))
FRAME_GAP_MS = int(os.environ.get("LIVE_FRAME_GAP_MS", "500"))
MAX_CLIENTS = int(os.environ.get("LIVE_MAX_CLIENTS", "50"))
HARD_TABS = int(os.environ.get("LIVE_HARD_TABS", "8"))


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def users() -> dict:
    return json.loads(USERS.read_text()) if USERS.exists() else {}


def log(*a) -> None:
    print(datetime.now(timezone.utc).isoformat(), "live:", *a, flush=True)


def audit(user: str, op: str, detail: str = "") -> None:
    with suppress(Exception), AUDIT.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}\t{user}\t{op}\t{str(detail)[:300]}\n")


# ---- keys ---------------------------------------------------------------------------------------
if len(sys.argv) > 1 and sys.argv[1] in ("add", "remove", "list"):
    u = users()
    name = sys.argv[2] if len(sys.argv) > 2 else None
    if sys.argv[1] == "list":
        print("\n".join(u) or "(none)")
    elif not name:
        print("a name?", file=sys.stderr)
    elif sys.argv[1] == "remove":
        u.pop(name, None)
        USERS.write_text(json.dumps(u, indent=2))
        print(f"key {name} revoked")
    else:
        token = secrets.token_urlsafe(24)
        u[name] = sha(token)
        USERS.write_text(json.dumps(u, indent=2))
        USERS.chmod(0o600)
        print(token)
    sys.exit(0)

from starlette.requests import Request  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import WebSocketRoute  # noqa: E402
from starlette.websockets import WebSocket, WebSocketDisconnect  # noqa: E402

from camoufox_playwright_mcp import cli  # noqa: E402
from camoufox_playwright_mcp.backend import browser_backend  # noqa: E402
from camoufox_playwright_mcp.mcp import server as mcp_server  # noqa: E402


class Live:
    """The browser as the people see it: one BrowserContext (the MCP's), its pages by id."""

    def __init__(self) -> None:
        self.server = None  # the MCP server (its backend pool opens the browser when needed)
        self.context = None  # the shared Playwright BrowserContext
        self.ids: dict = {}  # page -> id
        self.pages: dict[str, object] = {}  # id -> page
        self.titles: dict[str, str] = {}
        self.clients: set[WebSocket] = set()
        self.watchers: dict[str, set[WebSocket]] = {}  # tab id -> clients
        self.streams: dict[str, asyncio.Task] = {}
        self.watching: dict[WebSocket, str] = {}
        self.users: dict[WebSocket, str] = {}
        self.fresh: set[str] = set()  # tabs whose next picture goes out even if unchanged
        self.sent = ""  # the tab list last broadcast (a page's events repeat it)

    # ---- the browser ------------------------------------------------------------------------
    def adopt(self, context) -> None:
        """The MCP opened (or reopened) its BrowserContext: follow its pages."""
        if context is self.context:
            return
        self.context = context
        self.ids.clear(), self.pages.clear(), self.titles.clear()
        for page in context.pages:
            self.add_page(page)
        context.on("page", self.add_page)
        context.on("close", lambda *_: self.lost(context))
        log("browser context adopted")
        self.broadcast_tabs()

    def lost(self, context) -> None:
        if context is not self.context:
            return
        self.context = None
        self.ids.clear(), self.pages.clear(), self.titles.clear()
        self.broadcast_tabs()

    def add_page(self, page) -> None:
        if page in self.ids:
            return
        tid = uuid.uuid4().hex[:16]
        self.ids[page] = tid
        self.pages[tid] = page

        def changed(*_):
            asyncio.ensure_future(self.retitle(tid))

        def closed(*_):
            self.ids.pop(page, None)
            self.pages.pop(tid, None)
            self.titles.pop(tid, None)
            task = self.streams.pop(tid, None)
            if task:
                task.cancel()
            self.watchers.pop(tid, None)
            self.broadcast_tabs()

        page.on("framenavigated", lambda f: f == page.main_frame and changed())
        page.on("domcontentloaded", changed)
        page.on("load", changed)
        page.on("close", closed)
        changed()

    async def retitle(self, tid: str) -> None:
        page = self.pages.get(tid)
        if page is None:
            return
        with suppress(Exception):
            self.titles[tid] = await asyncio.wait_for(page.title(), 5)
        self.broadcast_tabs()

    async def ensure_context(self):
        """The browser for a person who opens a tab before the agent ever used it."""
        if self.context is not None:
            return self.context
        pool = self.server.backend_pool
        await pool.close("__live__")
        backend = await pool.backend_for("__live__")
        await backend._ensure_context(cwd=Path.cwd(), roots=None)
        if self.context is None:
            raise RuntimeError("the browser did not start")
        return self.context

    def tab_list(self) -> list[dict]:
        return [{"id": tid, "url": p.url, "title": self.titles.get(tid, "")} for tid, p in self.pages.items()]

    def broadcast_tabs(self) -> None:
        msg = json.dumps({"tabs": self.tab_list()})
        if msg == self.sent:
            return
        self.sent = msg
        for c in list(self.clients):
            asyncio.ensure_future(self.send(c, msg))

    async def send(self, c: WebSocket, text: str) -> None:
        with suppress(Exception):
            await c.send_text(text)

    # ---- watching ---------------------------------------------------------------------------
    def watch(self, c: WebSocket, tid: str) -> None:
        self.unwatch(c)
        self.watching[c] = tid
        self.watchers.setdefault(tid, set()).add(c)
        task = self.streams.get(tid)
        if task is None or task.done():
            self.streams[tid] = asyncio.ensure_future(self.stream(tid))
        else:
            # A new watcher gets the current picture even if it has not changed.
            self.fresh.add(tid)

    def unwatch(self, c: WebSocket) -> None:
        tid = self.watching.pop(c, None)
        if tid and tid in self.watchers:
            self.watchers[tid].discard(c)

    async def stream(self, tid: str) -> None:
        """Screenshots of one tab, at most one per FRAME_GAP_MS, while someone watches it."""
        last = b""
        while self.watchers.get(tid):
            start = time.monotonic()
            page = self.pages.get(tid)
            if page is None:
                break
            try:
                shot = await page.screenshot(type="jpeg", quality=55, scale="css", timeout=5000, animations="allow", caret="initial")
                size = page.viewport_size or await page.evaluate("() => ({width: innerWidth, height: innerHeight})")
                if shot != last or tid in self.fresh:
                    self.fresh.discard(tid)
                    last = shot
                    msg = json.dumps({"frame": base64.b64encode(shot).decode(), "w": size["width"], "h": size["height"], "tab": tid})
                    for c in list(self.watchers.get(tid, ())):
                        await self.send(c, msg)
            except Exception as e:  # a navigation in progress, a dialog: try the next time
                if page.is_closed():
                    break
                log(f"frame {tid}: {str(e).splitlines()[0][:200]}")
            await asyncio.sleep(max(FRAME_GAP_MS / 1000 - (time.monotonic() - start), 0.05))
        self.streams.pop(tid, None)

    # ---- input ------------------------------------------------------------------------------
    async def handle(self, c: WebSocket, m: dict) -> None:
        user = self.users[c]
        op = m.get("op")
        page = self.pages.get(self.watching.get(c, ""))
        if op == "tabs":
            await c.send_text(json.dumps({"tabs": self.tab_list()}))
        elif op == "watch":
            tid = str(m.get("tab"))
            if tid in self.pages:
                self.watch(c, tid)
                audit(user, "watch", self.pages[tid].url)
        elif op == "wake":
            # Nothing sleeps here: the tab is as it was.
            await c.send_text(json.dumps({"woke": m.get("tab"), "as": m.get("tab")}))
        elif op == "unwatch":
            self.unwatch(c)
        elif op == "open":
            url = str(m.get("url", ""))
            if not url.startswith(("http://", "https://")):
                raise ValueError("only http(s) pages")
            if len(self.pages) >= HARD_TABS:
                raise ValueError("browser busy: all tabs in use, try again shortly")
            context = await self.ensure_context()
            page = await context.new_page()
            self.add_page(page)
            audit(user, "open", url)
            await c.send_text(json.dumps({"opened": self.ids[page]}))
            asyncio.ensure_future(self.goto(page, url))
        elif op == "close":
            target = self.pages.get(str(m.get("tab")))
            if target is not None:
                audit(user, "close", target.url)
                await target.close()
        elif page is None:
            return
        elif op == "mouse":
            kind, x, y = m.get("kind"), float(m.get("x", 0)), float(m.get("y", 0))
            button = m.get("button") if m.get("button") in ("left", "right", "middle") else "left"
            if kind == "mousePressed":
                await page.mouse.move(x, y)
                await page.mouse.down(button=button, click_count=max(int(m.get("clicks") or 1), 1))
                audit(user, "click", f"{round(x)},{round(y)}")
            elif kind == "mouseReleased":
                await page.mouse.move(x, y)
                await page.mouse.up(button=button, click_count=max(int(m.get("clicks") or 1), 1))
            elif kind == "mouseMoved" and m.get("button") in ("left", "right", "middle"):
                # Only drags: a plain hover move per pixel would queue up behind Camoufox's
                # humanized cursor and lag every click after it.
                await page.mouse.move(x, y)
        elif op == "wheel":
            await page.mouse.move(float(m.get("x", 0)), float(m.get("y", 0)))
            await page.mouse.wheel(float(m.get("dx", 0)), float(m.get("dy", 0)))
        elif op == "text":
            text = str(m.get("text", ""))[:100000]
            await page.keyboard.insert_text(text)
            audit(user, "type", text)
        elif op == "key":
            mods = int(m.get("modifiers") or 0)
            names = [n for bit, n in ((1, "Alt"), (2, "Control"), (4, "Meta"), (8, "Shift")) if mods & bit]
            await page.keyboard.press("+".join([*names, str(m.get("key"))]))

    async def goto(self, page, url: str) -> None:
        with suppress(Exception):
            await page.goto(url, wait_until="commit", timeout=30000)

    # ---- the WebSocket ----------------------------------------------------------------------
    async def endpoint(self, ws: WebSocket) -> None:
        token = ws.query_params.get("token", "")
        user = next((name for name, h in users().items() if h == sha(token)), None)
        if not user or len(self.clients) >= MAX_CLIENTS:
            log("refused: wrong key" if not user else "refused: too many")
            await ws.close(code=4401)
            return
        await ws.accept()
        self.clients.add(ws)
        self.users[ws] = user
        audit(user, "connect")
        try:
            await ws.send_text(json.dumps({"tabs": self.tab_list()}))
            while True:
                raw = await ws.receive_text()
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                try:
                    await self.handle(ws, m)
                except Exception as e:
                    await self.send(ws, json.dumps({"error": str(e).splitlines()[0][:300]}))
        except WebSocketDisconnect:
            pass
        finally:
            self.unwatch(ws)
            self.clients.discard(ws)
            self.users.pop(ws, None)
            audit(user, "disconnect")


LIVE = Live()

# The MCP's backends all register their BrowserContext here (`_ensure_context`); with
# --shared-browser-context it is the one persistent Camoufox context everyone shares.
_register = browser_backend.BrowserBackend._register_disconnect_listeners


def _register_and_adopt(self, browser_context) -> None:
    _register(self, browser_context)
    LIVE.adopt(browser_context)


browser_backend.BrowserBackend._register_disconnect_listeners = _register_and_adopt

# The MCP's HTTP app gets /live next to /mcp: `_create_server` calls this for every server it makes.
_kill_endpoint = mcp_server._register_kill_endpoint


def _register_with_live(app, server) -> None:
    _kill_endpoint(app, server)
    LIVE.server = server
    app._additional_http_routes.append(WebSocketRoute("/live", LIVE.endpoint))

    @app.custom_route("/live/health", methods=["GET"])
    async def _health(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")


mcp_server._register_kill_endpoint = _register_with_live

if __name__ == "__main__":
    sys.exit(cli.main())
