/*
  Drawing on top of slides: pen, highlighter, laser pointer, eraser, and a few
  shapes (underline, arrow, box, circle).

  Only the teacher draws. Everything appears on every student's slide as it is
  drawn. The drawing belongs to the slide it was made on: go to another slide and
  it clears, come back and it is still there (the server remembers it for the
  session: sockets_tools.py). The laser pointer is never stored: it is only
  there while the teacher moves it.

  Two canvases sit exactly over the slide picture (same size, same "contain"
  fit, same rotation on a phone), so a circle drawn round a word stays round that
  word on every screen. recorder.js paints both into the lesson recording.
*/
(function () {
  const socket = window._socket;
  const inkCanvas = document.getElementById("sl-ink");
  const laserCanvas = document.getElementById("sl-laser");
  if (!socket || !inkCanvas || !laserCanvas || !window.InkEngine) return;

  const isHost = !!window.ROOM_CONFIG.isHost;
  const BASE_W = 1600;

  let engine = null;
  let cur = null;                 // { deckId, index } the slide on screen
  let preview = null;             // a shape being dragged (only on the teacher's screen)

  /* ---------- an engine that matches the slide's shape ---------- */
  function ensureEngine(ratio) {
    ratio = ratio || 9 / 16;
    const h = Math.round(BASE_W * ratio);
    if (engine && engine.height === h) return engine;
    const keep = engine ? engine.ops.map((o) => Object.assign({}, o)) : [];
    inkCanvas.width = BASE_W; inkCanvas.height = h;
    laserCanvas.width = BASE_W; laserCanvas.height = h;
    engine = new window.InkEngine(inkCanvas, { transparent: true });
    engine.overlay = (ctx, W, H) => { if (preview) window.InkEngine.drawShape(ctx, W, H, preview); };
    if (keep.length) engine.setOps(keep);
    return engine;
  }
  ensureEngine(9 / 16);

  const same = (deckId, index) => cur && cur.deckId === deckId && cur.index === index;

  // Called by slides.js
  function slideChanged(deckId, index) {
    if (same(deckId, index)) return;                 // the same slide shown again ("Try again", reconnect)
    cur = { deckId, index };
    engine.setOps([]);
    clearLaser();
  }
  function imageShown(im) {
    if (!im || !im.naturalWidth) return;
    ensureEngine(im.naturalHeight / im.naturalWidth);
  }
  function closed() {
    cur = null;
    engine.setOps([]);
    clearLaser();
    if (isHost) setTool("none");
  }

  socket.on("ink_state", (st) => {
    if (!st || !same(st.deck_id, st.index)) {
      if (st && !cur) { cur = { deckId: st.deck_id, index: st.index }; }       // arrived before the slide
      else return;
    }
    engine.setOps(st.ops || []);
  });
  socket.on("ink_op", ({ deck_id, index, op }) => { if (same(deck_id, index)) engine.apply(op); });
  socket.on("slides_state", (st) => { if (!st || !st.active) closed(); });

  window.SlideInk = {
    slideChanged, imageShown,
    layers: () => (cur ? [inkCanvas, laserCanvas] : []),
  };

  /* ---------- laser pointer (everyone sees it; nothing is stored) ---------- */
  const lctx = laserCanvas.getContext("2d");
  let trail = [];                 // [{ x, y, t }] normalised
  let laserOn = false;
  let laserSeen = 0;
  let raf = 0;

  function clearLaser() {
    trail = []; laserOn = false;
    lctx.clearRect(0, 0, laserCanvas.width, laserCanvas.height);
  }
  function paintLaser() {
    raf = 0;
    const W = laserCanvas.width, H = laserCanvas.height, now = performance.now();
    lctx.clearRect(0, 0, W, H);
    if (laserOn && now - laserSeen > 2500) laserOn = false;            // the teacher's connection dropped
    trail = trail.filter((p) => now - p.t < 450);
    trail.forEach((p, i) => {
      const age = (now - p.t) / 450;
      lctx.fillStyle = "rgba(255, 40, 40, " + (0.35 * (1 - age)).toFixed(3) + ")";
      lctx.beginPath(); lctx.arc(p.x * W, p.y * H, W * 0.007 * (1 - age * 0.5), 0, Math.PI * 2); lctx.fill();
    });
    if (laserOn && trail.length) {
      const p = trail[trail.length - 1];
      const r = W * 0.016;
      const g = lctx.createRadialGradient(p.x * W, p.y * H, 0, p.x * W, p.y * H, r * 2.4);
      g.addColorStop(0, "rgba(255,255,255,0.95)");
      g.addColorStop(0.25, "rgba(255,40,40,0.95)");
      g.addColorStop(1, "rgba(255,40,40,0)");
      lctx.fillStyle = g;
      lctx.beginPath(); lctx.arc(p.x * W, p.y * H, r * 2.4, 0, Math.PI * 2); lctx.fill();
    }
    if (laserOn || trail.length) raf = requestAnimationFrame(paintLaser);
  }
  function laserAt(x, y, on) {
    laserSeen = performance.now();
    laserOn = on;
    if (on) trail.push({ x, y, t: laserSeen });
    if (!raf) raf = requestAnimationFrame(paintLaser);
  }
  socket.on("laser", ({ x, y, on }) => { if (cur) laserAt(x, y, on); });

  if (!isHost) return;      // ---------------- the teacher's tools below ----------------

  /* ---------- tools ---------- */
  const bar = document.getElementById("sl-ink-bar");
  let tool = "none";        // none | pen | hi | laser | eraser | line | arrow | rect | ellipse
  let colour = "#e5262b";
  let sizeIdx = 1;
  const PEN = [0.003, 0.006, 0.011];
  const HI = [0.016, 0.028, 0.045];
  const SHAPE = [0.003, 0.005, 0.008];
  const ERASER = [0.02, 0.04, 0.08];
  const SHAPES = ["line", "arrow", "rect", "ellipse"];

  let stroke = null;        // { id, pending, shape?, a? }
  let flushTimer = null;
  let lastLaser = 0;

  const newId = () => Math.random().toString(36).slice(2, 12);
  const clamp01 = (v) => Math.min(1, Math.max(0, v));
  const round4 = (v) => Math.round(v * 10000) / 10000;

  // The picture is letterboxed inside the canvas box ("contain"): find where it really is.
  function pointOnSlide(e) {
    const r = inkCanvas.getBoundingClientRect();
    const scale = Math.min(r.width / inkCanvas.width, r.height / inkCanvas.height);
    const w = inkCanvas.width * scale, h = inkCanvas.height * scale;
    const ox = r.left + (r.width - w) / 2, oy = r.top + (r.height - h) / 2;
    return [round4(clamp01((e.clientX - ox) / w)), round4(clamp01((e.clientY - oy) / h))];
  }

  function send(op) {
    engine.apply(op);
    socket.emit("ink_op", { op });
  }

  function flush() {
    flushTimer = null;
    if (!stroke || !stroke.pending || !stroke.pending.length) return;
    send({ t: "more", id: stroke.id, p: stroke.pending.splice(0) });
  }

  const stop = (e) => e.stopPropagation();           // don't let a drawing stroke also swipe the slide

  inkCanvas.addEventListener("pointerdown", (e) => {
    if (tool === "none") return;
    stop(e); e.preventDefault();
    inkCanvas.setPointerCapture(e.pointerId);
    const p = pointOnSlide(e);
    if (tool === "laser") { sendLaser(p, true, true); stroke = { laser: true }; return; }
    if (SHAPES.includes(tool)) {
      stroke = { id: newId(), shape: tool, a: p };
      preview = { t: "shape", k: tool, c: colour, w: SHAPE[sizeIdx], a: p, b: p };
      return;
    }
    const k = tool === "eraser" ? "erase" : tool === "hi" ? "hi" : "pen";
    stroke = { id: newId(), pending: [] };
    send({ t: "line", id: stroke.id, k, c: colour, w: (k === "erase" ? ERASER : k === "hi" ? HI : PEN)[sizeIdx], p: [p] });
  });

  inkCanvas.addEventListener("pointermove", (e) => {
    if (tool === "none") return;
    const p = pointOnSlide(e);
    if (tool === "laser") {                           // a mouse shows the laser just by hovering; a finger by touching
      if (e.pointerType === "mouse" || stroke) { stop(e); sendLaser(p, true, false); }
      return;
    }
    if (!stroke) return;
    stop(e); e.preventDefault();
    if (stroke.shape) { preview.b = p; engine.present(); return; }
    stroke.pending.push(p);
    if (!flushTimer) flushTimer = setTimeout(flush, 40);
  });

  function endStroke(e) {
    if (tool === "none") return;
    stop(e);
    if (tool === "laser") {
      // A mouse keeps the laser on while it hovers; a finger or pen only while touching.
      if (e.pointerType !== "mouse" || e.type === "pointerleave") sendLaser([0, 0], false, true);
      stroke = null;
      return;
    }
    if (!stroke) return;
    if (stroke.shape) {
      const s = preview;
      stroke = null; preview = null;
      const far = s && (Math.abs(s.a[0] - s.b[0]) * inkCanvas.width > 5 || Math.abs(s.a[1] - s.b[1]) * inkCanvas.height > 5);
      if (far) send({ t: "shape", id: newId(), k: s.k, c: s.c, w: s.w, a: s.a, b: s.b });
      else engine.present();
      return;
    }
    flush();
    stroke = null;
  }
  inkCanvas.addEventListener("pointerup", endStroke);
  inkCanvas.addEventListener("pointercancel", endStroke);
  inkCanvas.addEventListener("pointerleave", (e) => { if (tool === "laser" && !stroke) endStroke(e); });

  function sendLaser(p, on, force) {
    laserAt(p[0], p[1], on);                          // my own screen at once
    const now = performance.now();
    if (!force && now - lastLaser < 30) return;
    lastLaser = now;
    socket.emit("laser", { x: p[0], y: p[1], on });
  }

  /* ---------- the toolbar ---------- */
  function setTool(name) {
    if (stroke) { endStroke({ type: "x", stopPropagation() {} }); }
    tool = name;
    inkCanvas.classList.toggle("drawing", name !== "none");
    inkCanvas.dataset.tool = name;
    if (bar) {
      bar.classList.toggle("is-open", name !== "none");
      bar.querySelectorAll("[data-ink-tool]").forEach((b) => b.classList.toggle("is-on", b.dataset.inkTool === name));
    }
    if (name !== "laser") socket.emit("laser", { x: 0, y: 0, on: false });
    // Yellow for the highlighter; back to red for everything else.
    if (name === "hi" && colour === "#e5262b") setColour("#f5c400");
    else if (name !== "hi" && name !== "none" && colour === "#f5c400") setColour("#e5262b");
  }
  function setColour(c) {
    colour = c;
    if (bar) bar.querySelectorAll("[data-ink-colour]").forEach((b) => b.classList.toggle("is-on", b.dataset.inkColour === c));
  }

  if (bar) {
    bar.querySelectorAll("[data-ink-tool]").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.inkTool === tool ? "none" : b.dataset.inkTool)));
    bar.querySelectorAll("[data-ink-colour]").forEach((b) => {
      b.style.background = b.dataset.inkColour;
      b.addEventListener("click", () => { setColour(b.dataset.inkColour); if (tool === "eraser" || tool === "laser" || tool === "none") setTool("pen"); });
    });
    bar.querySelectorAll("[data-ink-size]").forEach((b) => b.addEventListener("click", () => {
      sizeIdx = +b.dataset.inkSize;
      bar.querySelectorAll("[data-ink-size]").forEach((x) => x.classList.toggle("is-on", x === b));
    }));
    const on = (id, fn) => { const el = document.getElementById(id); if (el) el.addEventListener("click", fn); };
    on("sl-ink-undo", () => send({ t: "undo" }));
    on("sl-ink-clear", () => send({ t: "clear" }));
    on("sl-ink-off", () => setTool("none"));
    on("sl-ink-open", () => setTool("pen"));
    setColour(colour);
  }
})();
