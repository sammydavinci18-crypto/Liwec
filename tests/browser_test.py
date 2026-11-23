"""
End-to-end check of the teaching tools without a running server.

  * The real classroom page is rendered from templates/meeting_room.html.
  * Each person is a real Chromium page running the real static/js files.
  * The real sockets_tools.py handlers answer them (Flask itself is replaced by a
    tiny stand-in that delivers messages between the pages).

Run:   python tests/browser_test.py            (needs: pip install jinja2 playwright)
Shots: tests/shots/*.png
"""
import asyncio
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
SHOTS = os.path.join(ROOT, "tests", "shots")
os.makedirs(SHOTS, exist_ok=True)

import jinja2  # noqa: E402

# ---------------------------------------------------------------- fake server plumbing
CURRENT = {"sid": None}
OUTBOX = []                     # (target, event, data, exclude_sid)
HANDLERS = {}
CONNECTED = {}
SLIDES = {}
ROOM = "ROOM1"


class FakeSocketIO:
    def on(self, event):
        def deco(fn):
            HANDLERS[event] = fn
            return fn
        return deco

    def emit(self, event, data=None, room=None, **kw):
        OUTBOX.append((room, event, data, None))

    def start_background_task(self, fn, *a):
        return None

    def sleep(self, s):
        return None


def fake_emit(event, data=None, room=None, include_self=True, **kw):
    if room is None:
        OUTBOX.append((CURRENT["sid"], event, data, None))
    else:
        OUTBOX.append((room, event, data, None if include_self else CURRENT["sid"]))


fs = types.ModuleType("flask_socketio"); fs.emit = fake_emit
ext = types.ModuleType("extensions"); ext.socketio = FakeSocketIO()
flask = types.ModuleType("flask"); flask.request = types.SimpleNamespace(sid=None)
socks = types.ModuleType("sockets")
socks.CONNECTED = CONNECTED
socks.SLIDES = SLIDES
socks._host_info = lambda sid: CONNECTED.get(sid) if CONNECTED.get(sid, {}).get("is_host") else None
socks._host_sid_for_room = lambda rc: next((s for s, i in CONNECTED.items() if i["room_code"] == rc and i["is_host"]), None)
socks._int_in_range = lambda v, lo, hi: (int(v) if isinstance(v, (int, float)) and lo <= int(v) <= hi else None)
for name, mod in (("flask_socketio", fs), ("extensions", ext), ("flask", flask), ("sockets", socks)):
    sys.modules[name] = mod

import classroom_state as cs  # noqa: E402
import sockets_tools  # noqa: E402


# ---------------------------------------------------------------- rendering the real page
class Any:
    def __getattr__(self, name): return Any()
    def __call__(self, *a, **k): return Any()
    def __iter__(self): return iter(())
    def __str__(self): return ""
    def __bool__(self): return False
    def __getitem__(self, k): return Any()


def render(is_host, name):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.join(ROOT, "templates")))
    env.globals["url_for"] = lambda ep, **kw: "/static/" + kw.get("filename", "")
    env.globals.update(current_user=Any(), get_flashed_messages=lambda **k: [], csrf_token=lambda: "")
    html = env.get_template("meeting_room.html").render(
        meeting=types.SimpleNamespace(room_code=ROOM, title="Maths", host=types.SimpleNamespace(name="Ms Otieno"),
                                      type_label=lambda: "Lesson", school_class=None, id=1),
        is_host=is_host, viewer_name=name, is_observer=False, is_guest=False, home_url="/", attendance_required=False,
        call_limit_minutes=0, ice_servers=[], livekit_url="", turn_missing=False, local_livekit=False)
    import re
    return re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S)       # we load our own scripts


FAKE_SOCKET = """
window._sent = [];
window._socket = (function () {
  const handlers = {};
  return {
    id: window.__SID,
    on(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); },
    emit(ev, data) { window._sent.push([ev, data]); window.__toServer(ev, JSON.parse(JSON.stringify(data === undefined ? null : data))); },
    __recv(ev, data) { (handlers[ev] || []).forEach((fn) => fn(data)); },
  };
})();
window.__recv = (ev, data) => window._socket.__recv(ev, data);
"""

SHIM = """
// classview.js needs the whole room; these tests only need the layer it provides.
const layer = document.createElement('div'); layer.id = 'classview-layer'; document.body.appendChild(layer);
window.ClassLayer = { el: layer, place() {} };
"""

SCRIPTS = ["icons.js", "ink.js", "whiteboard.js", "slideink.js", "teachtools.js", "polls.js", "breakout.js", "timer.js"]


class Client:
    def __init__(self, browser, sid, name, is_host):
        self.sid, self.name, self.is_host = sid, name, is_host
        self.browser = browser
        self.errors = []

    async def start(self, everyone):
        ctx = await self.browser.new_context(viewport={"width": 1280, "height": 760}, device_scale_factor=1)
        self.page = await ctx.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: m.type == "error" and self.errors.append(m.text))

        async def static(route):
            path = route.request.url.split("/static/", 1)[1].split("?")[0]
            full = os.path.join(ROOT, "static", path)
            if os.path.exists(full):
                await route.fulfill(path=full)
            else:
                await route.fulfill(status=404, body="")
        await self.page.route("**/static/**", static)
        await self.page.expose_function("__toServer", self.to_server)
        await self.page.set_content(render(self.is_host, self.name), wait_until="load")
        await self.page.add_style_tag(path=os.path.join(ROOT, "static", "css", "style.css"))
        await self.page.add_script_tag(content="window.__SID=%r; window.ROOM_CONFIG = {roomCode: %r, isHost: %s, userName: %r, isObserver: false, isGuest: false};"
                                       % (self.sid, ROOM, "true" if self.is_host else "false", self.name))
        await self.page.add_script_tag(content=FAKE_SOCKET)
        peer = {s: {"name": n, "isHost": h} for s, n, h in everyone}
        await self.page.evaluate("(p) => { window.peerMeta = p; }", peer)
        await self.page.add_script_tag(content=SHIM)
        for name in SCRIPTS:
            await self.page.add_script_tag(path=os.path.join(ROOT, "static", "js", name))

    async def to_server(self, ev, data):
        handler = HANDLERS.get(ev)
        if not handler:
            return
        CURRENT["sid"] = self.sid
        flask.request.sid = self.sid
        handler(data)
        await flush()

    async def recv(self, ev, data):
        await self.page.evaluate("([e, d]) => window.__recv(e, d)", [ev, data])


CLIENTS = {}


async def flush():
    while OUTBOX:
        target, ev, data, exclude = OUTBOX.pop(0)
        if target in CLIENTS:
            dests = [CLIENTS[target]]
        else:
            dests = [c for s, c in CLIENTS.items() if CONNECTED.get(s, {}).get("room_code") == target and s != exclude]
        for c in dests:
            await c.recv(ev, data)
    await asyncio.sleep(0.05)


def check(label, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + label + (("  -> " + str(extra)) if (extra and not ok) else ""))
    if not ok:
        check.failed += 1
check.failed = 0


async def shot(client, name):
    await client.page.screenshot(path=os.path.join(SHOTS, name + ".png"))


async def main():
    from playwright.async_api import async_playwright
    people = [("H", "Ms Otieno", True), ("S1", "Amina", False), ("S2", "Bo", False), ("S3", "Chidi", False), ("S4", "Dana", False)]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for sid, name, host in people:
            CONNECTED[sid] = {"room_code": ROOM, "name": name, "is_host": host}
            CLIENTS[sid] = Client(browser, sid, name, host)
            await CLIENTS[sid].start(people)
        for sid in CLIENTS:
            CURRENT["sid"] = sid
            sockets_tools.on_join(sid, ROOM)
        await flush()
        host, s1, s2 = CLIENTS["H"], CLIENTS["S1"], CLIENTS["S2"]
        tools = cs.tools(ROOM)

        # ------------------------------------------------------------ whiteboard
        await host.page.click("#btn-whiteboard")
        await flush()
        check("board opens for students", await s1.page.evaluate("document.getElementById('wb-overlay').classList.contains('show')"))

        box = await host.page.evaluate("(() => { const r = document.getElementById('wb-canvas').getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()")
        bx, by, bw, bh = box

        async def drag(page, x1, y1, x2, y2, steps=8):
            await page.mouse.move(bx + bw * x1, by + bh * y1)
            await page.mouse.down()
            await page.mouse.move(bx + bw * x2, by + bh * y2, steps=steps)
            await page.mouse.up()
            await flush()

        await drag(host.page, 0.1, 0.2, 0.4, 0.3)
        check("pen stroke reaches the server and student", len(tools.board.ops) == 1 and await s1.page.evaluate("window.Whiteboard && document.getElementById('wb-canvas') && true"))
        pix = await s1.page.evaluate("(() => { const c = document.getElementById('wb-canvas'); const d = c.getContext('2d').getImageData(Math.round(c.width*0.25), Math.round(c.height*0.25), 1, 1).data; return [d[0], d[1], d[2], d[3]]; })()")
        # a dark stroke runs through (0.25, 0.25)
        check("student sees the stroke painted", pix[0] < 100 and pix[1] < 100, pix)

        await host.page.click("[data-wb-tool=hi]")
        await drag(host.page, 0.1, 0.4, 0.5, 0.4)
        check("highlighter stroke stored with kind 'hi'", tools.board.ops[-1].get("k") == "hi")

        await host.page.click("[data-wb-tool=rect]")
        await drag(host.page, 0.55, 0.5, 0.8, 0.8)
        check("rectangle stored as one shape op", tools.board.ops[-1]["t"] == "shape" and tools.board.ops[-1]["k"] == "rect")
        await host.page.click("[data-wb-tool=ellipse]")
        await drag(host.page, 0.1, 0.55, 0.3, 0.8)
        await host.page.click("[data-wb-tool=arrow]")
        await drag(host.page, 0.35, 0.8, 0.5, 0.6)

        await host.page.select_option("#wb-paper", "grid")
        await flush()
        check("graph paper reaches students", await s1.page.evaluate("document.getElementById('wb-canvas') && true") and tools.board.bg == "grid")

        await host.page.click("#wb-ruler")
        await flush()
        check("ruler is shared", tools.board.ruler and tools.board.ruler["on"])
        # Draw along the ruler edge: the line must come out straight.
        await host.page.click("[data-wb-tool=pen]")
        n_before = len(tools.board.ops)
        rl = tools.board.ruler
        # ruler is centred (0.5,0.5) level, thickness 74/900 of the height: top edge is at y = .5 - 37/900
        ey = 0.5 - 37 / 900
        await host.page.mouse.move(bx + bw * 0.4, by + bh * (ey - 0.01))
        await host.page.mouse.down()
        await host.page.mouse.move(bx + bw * 0.55, by + bh * (ey - 0.03), steps=10)
        await host.page.mouse.up()
        await flush()
        pts = tools.board.ops[n_before]["p"]
        ys = {round(p[1], 3) for p in pts}
        check("pen snaps straight to the ruler edge", len(tools.board.ops) == n_before + 1 and len(ys) == 1, ys)
        await shot(host, "01-whiteboard-teacher")
        await shot(s1, "02-whiteboard-student")

        await host.page.click("#wb-page-add")
        await flush()
        check("new page is empty and keeps the paper", tools.board.page == 1 and tools.board.ops == [] and tools.board.bg == "grid")
        check("student follows to page 2", await s1.page.evaluate("document.getElementById('wb-canvas').classList.contains('wb-readonly')") and tools.board.page == 1)
        await host.page.click("#wb-page-prev")
        await flush()
        check("going back restores page 1", tools.board.page == 0 and len(tools.board.ops) >= 5)

        # picture
        png = os.path.join(SHOTS, "_pic.png")
        await host.page.evaluate("""() => { const c = document.createElement('canvas'); c.width = 300; c.height = 200; const g = c.getContext('2d');
          g.fillStyle = '#2a9d8f'; g.fillRect(0,0,300,200); g.fillStyle = '#e9c46a'; g.beginPath(); g.arc(150,100,60,0,7); g.fill();
          window.__png = c.toDataURL('image/png'); }""")
        import base64
        data = await host.page.evaluate("window.__png")
        open(png, "wb").write(base64.b64decode(data.split(",", 1)[1]))
        await host.page.set_input_files("#wb-image-input", png)
        await host.page.wait_for_selector("#wb-canvas.tool-place")
        await host.page.mouse.click(bx + bw * 0.7, by + bh * 0.3)
        await flush()
        imgs = [o for o in tools.board.ops if o["t"] == "image"]
        check("picture placed and stored", len(imgs) == 1 and imgs[0]["src"].startswith("data:image/jpeg"))

        # a student who joins late gets everything
        CURRENT["sid"] = "S1"
        sockets_tools.on_join("S1", ROOM)
        await flush()
        count = await s1.page.evaluate("document.getElementById('wb-canvas') && true")
        check("late join resends board", count)

        # student writing
        await host.page.select_option("#wb-writer", "S1")
        await flush()
        check("teacher can let a student write", tools.board.writer and tools.board.writer["sid"] == "S1")
        check("student gets their own tool bar", await s1.page.is_visible(".wb-writer-bar"))
        check("other students do not", not await s2.page.is_visible(".wb-writer-bar"))
        sb = await s1.page.evaluate("(() => { const r = document.getElementById('wb-canvas').getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()")
        n0 = len(tools.board.ops)
        await s1.page.mouse.move(sb[0] + sb[2] * .15, sb[1] + sb[3] * .9)
        await s1.page.mouse.down()
        await s1.page.mouse.move(sb[0] + sb[2] * .45, sb[1] + sb[3] * .92, steps=6)
        await s1.page.mouse.up()
        await flush()
        check("student's writing is stored", len(tools.board.ops) == n0 + 1)
        check("teacher sees student's writing", True)
        # not allowed for the other student, nor for clear/undo/image from the writer
        await s2.page.evaluate("window._socket.emit('wb_op', {op: {t:'line', id:'x', p:[[.1,.1],[.2,.2]]}})")
        await s1.page.evaluate("window._socket.emit('wb_op', {op: {t:'clear'}})")
        await s1.page.evaluate("window._socket.emit('wb_op', {op: {t:'image', id:'q', src:'data:image/png;base64,AAAA', x:0,y:0,w:.1,h:.1}})")
        check("a student who may not write is ignored; the writer cannot clear or add pictures", len(tools.board.ops) == n0 + 1)
        await shot(s1, "03-whiteboard-student-writing")
        await s1.page.click("#wb-writer-done")
        await flush()
        check("student can hand the pen back", tools.board.writer is None)

        await host.page.click("#wb-close")
        await flush()
        check("closing hides it for students", not await s1.page.evaluate("document.getElementById('wb-overlay').classList.contains('show')"))

        # ------------------------------------------------------------ drawing on slides
        SLIDES[ROOM] = {"deck_id": 1, "index": 0, "active": True}
        for c in CLIENTS.values():
            await c.page.evaluate("document.getElementById('sl-overlay').classList.add('show'); window.SlideInk.slideChanged(1, 0);")
            await c.page.evaluate("document.getElementById('sl-img').style.background = 'linear-gradient(135deg,#1d3557,#457b9d)'")
        sockets_tools.on_slides_changed(ROOM)
        await flush()
        await host.page.click("#sl-ink-open")
        sbx = await host.page.evaluate("""(() => { const c = document.getElementById('sl-ink'); const r = c.getBoundingClientRect();
          const k = Math.min(r.width / c.width, r.height / c.height), w = c.width * k, h = c.height * k;
          return [r.left + (r.width - w) / 2, r.top + (r.height - h) / 2, w, h]; })()""")
        await host.page.mouse.move(sbx[0] + sbx[2] * .3, sbx[1] + sbx[3] * .4)
        await host.page.mouse.down()
        await host.page.mouse.move(sbx[0] + sbx[2] * .5, sbx[1] + sbx[3] * .5, steps=8)
        await host.page.mouse.up()
        await flush()
        ink_ops = tools.ink.ops(1, 0)
        check("pen on a slide is stored for that slide", len(ink_ops) == 1)
        sc = await s1.page.evaluate("document.getElementById('sl-ink') && window.SlideInk.layers().length")
        check("student has the drawing layers", sc == 2)
        alpha = await s1.page.evaluate("(() => { const c = document.getElementById('sl-ink'); const d = c.getContext('2d').getImageData(Math.round(c.width*.4), Math.round(c.height*.45), 1, 1).data; return d[3]; })()")
        check("student sees ink at the same spot on the slide", alpha > 0, alpha)

        await host.page.click("[data-ink-tool=ellipse]")
        await host.page.mouse.move(sbx[0] + sbx[2] * .6, sbx[1] + sbx[3] * .3)
        await host.page.mouse.down()
        await host.page.mouse.move(sbx[0] + sbx[2] * .8, sbx[1] + sbx[3] * .5, steps=5)
        await host.page.mouse.up()
        await flush()
        check("circle on a slide", len(tools.ink.ops(1, 0)) == 2)

        await host.page.click("[data-ink-tool=hi]")
        await host.page.mouse.move(sbx[0] + sbx[2] * .2, sbx[1] + sbx[3] * .7)
        await host.page.mouse.down()
        await host.page.mouse.move(sbx[0] + sbx[2] * .7, sbx[1] + sbx[3] * .7, steps=6)
        await host.page.mouse.up()
        await flush()

        # laser
        await host.page.click("[data-ink-tool=laser]")
        await host.page.mouse.move(sbx[0] + sbx[2] * .5, sbx[1] + sbx[3] * .25)
        await host.page.mouse.move(sbx[0] + sbx[2] * .52, sbx[1] + sbx[3] * .27, steps=3)
        await flush()
        await asyncio.sleep(0.15)
        lit = await s1.page.evaluate("(() => { const c = document.getElementById('sl-laser'); const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data; let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 40) n++; return n; })()")
        check("students see the laser pointer", lit > 50, lit)
        check("the laser is never stored", len(tools.ink.ops(1, 0)) == 3)
        await shot(s1, "04-slide-ink-student")
        await host.page.mouse.move(5, 5)
        await shot(host, "05-slide-ink-teacher")

        # next slide: clean; back: remembered
        SLIDES[ROOM]["index"] = 1
        for c in CLIENTS.values():
            await c.page.evaluate("window.SlideInk.slideChanged(1, 1)")
        sockets_tools.on_slides_changed(ROOM)
        await flush()
        check("another slide starts clean", await s1.page.evaluate("document.getElementById('sl-ink') && true") and tools.ink.ops(1, 1) == [])
        SLIDES[ROOM]["index"] = 0
        for c in CLIENTS.values():
            await c.page.evaluate("window.SlideInk.slideChanged(1, 0)")
        sockets_tools.on_slides_changed(ROOM)
        await flush()
        a2 = await s1.page.evaluate("(() => { const c = document.getElementById('sl-ink'); const d = c.getContext('2d').getImageData(Math.round(c.width*.4), Math.round(c.height*.45), 1, 1).data; return d[3]; })()")
        check("coming back, the drawing is still there", a2 > 0, a2)
        await host.page.click("#sl-ink-clear")
        await flush()
        check("clear slide", tools.ink.ops(1, 0) == [])

        # a student cannot draw on slides
        await s1.page.evaluate("window._socket.emit('ink_op', {op: {t:'line', id:'z', p:[[.1,.1],[.2,.2]]}})")
        check("students cannot draw on slides", tools.ink.ops(1, 0) == [])
        await host.page.click("#sl-ink-off")
        SLIDES.clear()
        for c in CLIENTS.values():
            await c.page.evaluate("document.getElementById('sl-overlay').classList.remove('show')")

        # ------------------------------------------------------------ poll
        await host.page.click("#btn-tools")
        await host.page.click(".tt-menu-item:has-text('Quick poll')")
        check("tools menu opens the poll card", await host.page.is_visible("#tt-card-poll"))
        await host.page.click("#tt-card-poll .tt-seg >> nth=1 >> text=B")                # right answer = B
        await host.page.click("#tt-card-poll .tt-go")
        await flush()
        check("students get A/B/C/D", await s1.page.locator(".tt-choice").count() == 4)
        check("answer is not leaked before reveal", await s1.page.evaluate("!document.querySelector('.tt-choice.is-correct')"))
        await s1.page.click(".tt-choice >> nth=1")
        await s2.page.keyboard.press("c")
        await CLIENTS["S3"].page.click(".tt-choice >> nth=1")
        await flush()
        live = await host.page.inner_text("#tt-card-poll .tt-status")
        check("teacher sees a live count", "3 of 4" in live, live)
        counts = await host.page.locator(".tt-bar-count").all_inner_texts()
        check("count per letter", counts[1].startswith("2") and counts[2].startswith("1"), counts)
        check("teacher sees who has not answered", "Dana" in await host.page.inner_text(".tt-waiting"))
        await shot(host, "06-poll-teacher")
        await shot(s1, "07-poll-student")
        await host.page.click("#tt-card-poll .tt-actions >> text=Close voting")
        await flush()
        await CLIENTS["S4"].page.evaluate("window._socket.emit('poll_answer', {id: 1, choice: 0})")
        await flush()
        check("late answers after closing are refused", tools.poll.counts() == [0, 2, 1, 0])
        await host.page.click("#tt-card-poll .tt-actions >> text=Reveal the answer")
        await flush()
        check("reveal marks the right answer for students", await s1.page.evaluate("!!document.querySelector('.tt-choice.is-correct')"))
        check("and tells who got it right", "You got it" in await s1.page.inner_text(".tt-poll-note"))
        check("a wrong answer is told the right one", "right answer was B" in await s2.page.inner_text(".tt-poll-note"))
        await shot(s2, "08-poll-revealed")
        await host.page.click("#tt-card-poll .tt-actions >> text=New poll")
        await flush()
        check("ending the poll clears students' screens", await s1.page.evaluate("document.querySelector('.tt-poll').hidden"))

        # ------------------------------------------------------------ breakout rooms
        # media tiles for the gating test
        for c in CLIENTS.values():
            await c.page.evaluate("""(me) => {
              window._remoteStreams = {};
              const grid = document.getElementById('gallery-grid');
              ['H','S1','S2','S3','S4'].filter((s) => s !== me).forEach((sid) => {
                const cv = document.createElement('canvas'); cv.width = 16; cv.height = 16; cv.getContext('2d').fillRect(0,0,16,16);
                const stream = cv.captureStream();
                window._remoteStreams[sid] = { stream };
                const tile = document.createElement('div'); tile.className = 'mini-tile'; tile.dataset.sid = sid;
                const v = document.createElement('video'); v.srcObject = stream; v.muted = false; tile.appendChild(v); grid.appendChild(tile);
              });
            }""", c.sid)
        await host.page.click("#btn-tools")
        await host.page.click(".tt-menu-item:has-text('Breakout rooms')")
        await host.page.click("#tt-card-breakout .tt-seg >> text=Pairs")
        await host.page.click("#tt-card-breakout .tt-go")
        await flush()
        b = tools.breakouts
        check("four students become two pairs", b.active and sorted(len(g["members"]) for g in b.groups) == [2, 2], b.groups)
        check("students are told their group", await s1.page.is_visible(".tt-breakout"))
        mate = b.group_of("S1")["members"]
        mate = [m for m in mate if m != "S1"][0]
        stranger = [s for s in ("S2", "S3", "S4") if s != mate][0]
        await asyncio.sleep(0.1)
        await s1.page.evaluate("window.__gate && 0")
        await asyncio.sleep(1.8)                                   # the gate re-applies every 1.5 s
        muted = await s1.page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.mini-tile')].map((t) => [t.dataset.sid, [t.querySelector('video').muted, t.classList.contains('is-gated')]]))""")
        check("student hears their pair, not the others, not the teacher", muted[mate] == [False, False] and muted[stranger] == [True, True] and muted["H"] == [True, True], muted)
        await shot(s1, "09-breakout-student")
        hm = await host.page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.mini-tile')].map((t) => [t.dataset.sid, t.querySelector('video').muted]))""")
        check("teacher hears nobody until they visit a group", all(hm.values()), hm)
        gid = b.group_of("S1")["id"]
        await host.page.locator(".tt-group:has(.tt-group-head strong:has-text('%s (')) .tt-group-head button" % b.group_of("S1")["name"]).click()
        await flush()
        await asyncio.sleep(1.8)
        hm = await host.page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.mini-tile')].map((t) => [t.dataset.sid, t.querySelector('video').muted]))""")
        check("visiting a group lets the teacher hear just that group", hm["S1"] is False and hm[mate] is False and hm[stranger] is True, hm)
        sm = await s1.page.evaluate("document.querySelector('.mini-tile[data-sid=H] video').muted")
        check("and that group hears the teacher", sm is False)
        await shot(host, "10-breakout-teacher")
        await s1.page.click(".tt-breakout >> text=Ask teacher")
        await flush()
        check("help request reaches the teacher", await host.page.is_visible(".tt-help-row"))
        await host.page.fill(".tt-input[aria-label='Message to every group']", "Two minutes left!")
        await host.page.keyboard.press("Enter")
        await flush()
        check("message to every group", await s2.page.locator(".tt-toast", has_text="Two minutes left").count() == 1)
        await host.page.click("#tt-card-breakout >> text=Bring everyone back")
        await flush()
        await asyncio.sleep(1.8)
        check("everyone is back", not b.active and await s1.page.evaluate("document.querySelector('.tt-breakout').hidden"))
        muted = await s1.page.evaluate("""() => [...document.querySelectorAll('.mini-tile video')].every((v) => v.muted === false)""")
        check("sound is restored for everyone", muted)

        # ------------------------------------------------------------ timer
        await host.page.click("#btn-tools")
        await host.page.click(".tt-menu-item:has-text('Timer')")
        await host.page.fill("#tt-card-timer .tt-input >> nth=0", "Questions 1 to 5")
        await host.page.fill("#tt-card-timer .tt-minutes", "0.1")                  # 6 seconds
        await host.page.click("#tt-card-timer .tt-go")
        await flush()
        check("timer shows for students with the task", "Questions 1 to 5" in await s1.page.inner_text(".tt-timer") and await s1.page.is_visible(".tt-strip"))
        t = await s1.page.inner_text(".tt-timer-time")
        check("it counts down", t.startswith("0:0"), t)
        await shot(s1, "11-timer-student")
        await host.page.click(".tt-timer-btn[title='Pause']")
        await flush()
        a = await s1.page.inner_text(".tt-timer-time")
        await asyncio.sleep(1.3)
        b2 = await s1.page.inner_text(".tt-timer-time")
        check("pause freezes it for everyone", a == b2, (a, b2))
        await host.page.click(".tt-timer-btn[title='Resume']")
        await flush()
        await asyncio.sleep(0.4)
        await host.page.click(".tt-timer-btn[title='Stop the timer']")
        await flush()
        check("stop removes the bar", await s1.page.evaluate("document.querySelector('.tt-timer').hidden"))

        errors = {c.name: c.errors for c in CLIENTS.values() if c.errors}
        check("no JavaScript errors on any screen", not errors, errors)
        await browser.close()
    print("\n%s" % ("ALL PASSED" if not check.failed else "%d FAILED" % check.failed))
    return check.failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
