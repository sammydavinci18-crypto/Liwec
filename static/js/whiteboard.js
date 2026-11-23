/*
  Shared whiteboard.

  The host (teacher) opens it and it covers the whole screen for everyone in the
  session. The teacher has pen, highlighter, eraser, text, shapes (line, arrow,
  box, oval), a ruler, pictures, several pages, and a choice of paper (plain,
  graph, graph with axes, lined). The teacher can also let ONE student write on
  the board; everyone else only watches. Closing the board returns everyone to
  the session, and it keeps what was drawn, so reopening continues where it was.

  Everything is a small operation in 0..1 board coordinates (see ink.js), so any
  screen size shows the same picture. The server validates every operation, keeps
  the pages for people who join late, and relays them (sockets_tools.py). The
  ruler is drawn onto the canvas itself, so the recording shows it too.
*/
(function () {
  const socket = window._socket;
  const overlay = document.getElementById("wb-overlay");
  if (!socket || !overlay) return;

  const isHost = !!window.ROOM_CONFIG.isHost;
  const isObserver = !!window.ROOM_CONFIG.isObserver;
  const canvas = document.getElementById("wb-canvas");
  const stage = document.getElementById("wb-stage");
  const W = canvas.width, H = canvas.height;
  const engine = new window.InkEngine(canvas);

  let isOpen = false;
  let page = 0, pageCount = 1;
  let writer = null;              // { sid, name }: the student allowed to write
  let ruler = null;               // { on, x, y, a, l }
  let preview = null;             // a shape being dragged (only on the screen of whoever is drawing)
  let stroke = null;              // { id, k, pending: [], snap } while a line is being drawn
  let textPos = null;             // where typed text will go
  let placing = null;             // a picture waiting for the board to be tapped
  const textBox = document.getElementById("wb-text-input");

  const iAmWriter = () => !!(writer && writer.sid === socket.id);
  const canDraw = () => isHost || iAmWriter();

  /* ---------- ruler (drawn into the canvas, so the recording shows it) ---------- */
  const RULER_T = 74;             // thickness, in board pixels
  const UNIT = W / 40;            // one "cm" = one graph-paper square

  function frame() {
    if (!ruler || !ruler.on) return null;
    const a = ruler.a;
    return { cx: ruler.x * W, cy: ruler.y * H, half: ruler.l * W / 2, a, cos: Math.cos(a), sin: Math.sin(a) };
  }
  const toLocal = (f, px, py) => { const dx = px - f.cx, dy = py - f.cy; return [dx * f.cos + dy * f.sin, -dx * f.sin + dy * f.cos]; };
  const fromLocal = (f, lx, ly) => [f.cx + lx * f.cos - ly * f.sin, f.cy + lx * f.sin + ly * f.cos];

  function drawRuler(ctx) {
    const f = frame();
    if (!f) return;
    ctx.save();
    ctx.translate(f.cx, f.cy);
    ctx.rotate(f.a);
    const T = RULER_T;
    ctx.fillStyle = "rgba(255, 233, 160, 0.78)";
    ctx.strokeStyle = "rgba(125, 92, 0, 0.95)";
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    ctx.rect(-f.half, -T / 2, f.half * 2, T);
    ctx.fill();
    ctx.stroke();
    // darker ends: the parts you grab to turn it
    ctx.fillStyle = "rgba(125, 92, 0, 0.22)";
    ctx.fillRect(-f.half, -T / 2, T * 0.8, T);
    ctx.fillRect(f.half - T * 0.8, -T / 2, T * 0.8, T);
    ctx.strokeStyle = "rgba(90, 65, 0, 0.9)";
    ctx.fillStyle = "rgba(60, 42, 0, 0.95)";
    ctx.lineWidth = 1.5;
    ctx.font = `${Math.round(T * 0.26)}px "Segoe UI", Arial, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (let i = 0; i * UNIT <= f.half * 2 + 0.5; i++) {
      const x = -f.half + i * UNIT;
      const len = i % 5 === 0 ? T * 0.34 : T * 0.2;
      ctx.beginPath(); ctx.moveTo(x, -T / 2); ctx.lineTo(x, -T / 2 + len); ctx.stroke();
      if (i % 5 === 0 && i > 0) ctx.fillText(String(i), x, -T / 2 + len + 2);
    }
    ctx.restore();
  }

  engine.overlay = (ctx) => {
    drawRuler(ctx);
    if (preview) window.InkEngine.drawShape(ctx, W, H, preview);
  };

  /* ---------- state from the server (everyone) ---------- */
  function setOpen(open) {
    isOpen = open;
    overlay.classList.toggle("show", open);
    const btn = document.getElementById("btn-whiteboard");
    if (btn) btn.classList.toggle("active-on", open);
    if (!open) { cancelText(); cancelPlacing(); }
  }

  function updateWriterUi() {
    canvas.classList.toggle("wb-readonly", !canDraw());
    overlay.classList.toggle("is-writer", iAmWriter());
    const note = document.getElementById("wb-writer-note");
    if (note) note.textContent = writer ? (iAmWriter() ? "Your teacher has let you write." : writer.name + " is writing") : "";
    const sel = document.getElementById("wb-writer");
    if (sel && writer && ![...sel.options].some((o) => o.value === writer.sid)) refreshWriterChoices();
    if (sel) sel.value = writer ? writer.sid : "";
    if (!canDraw()) { cancelText(); stroke = null; preview = null; }
    engine.present();
  }

  function updatePageUi() {
    const label = document.getElementById("wb-page-label");
    if (label) label.textContent = "Page " + (page + 1) + " / " + pageCount;
    const prev = document.getElementById("wb-page-prev"), next = document.getElementById("wb-page-next");
    if (prev) prev.disabled = page <= 0;
    if (next) next.disabled = page >= pageCount - 1;
    const paper = document.getElementById("wb-paper");
    if (paper) paper.value = engine.bg;
  }

  function applyState(st) {
    page = st.page || 0;
    pageCount = st.pages || 1;
    writer = st.writer || null;
    ruler = st.ruler || null;
    engine.setBg(st.bg || "plain");
    engine.setOps(st.ops || []);
    updateWriterUi();
    updatePageUi();
    setOpen(!!st.open);
  }

  socket.on("wb_open", () => setOpen(true));
  socket.on("wb_close", () => setOpen(false));
  socket.on("wb_op", ({ op }) => engine.apply(op));
  socket.on("wb_state", applyState);
  socket.on("wb_ruler", (m) => { ruler = m.ruler; engine.present(); });
  socket.on("wb_writer", (m) => { writer = m.writer; updateWriterUi(); });

  window.Whiteboard = { isOpen: () => isOpen, canvas };

  if (isObserver) return;     // an observing admin only watches

  /* ---------- tools (the teacher, and a student the teacher let write) ---------- */
  let tool = "pen";           // pen | hi | eraser | text | line | arrow | rect | ellipse | ruler
  let colour = "#111111";
  let sizeIdx = 1;
  const PEN = [0.003, 0.006, 0.012];
  const HI = [0.016, 0.028, 0.045];
  const SHAPE = [0.003, 0.005, 0.009];
  const TEXT = [0.04, 0.06, 0.09];
  const ERASER = [0.02, 0.04, 0.08];
  const SHAPES = ["line", "arrow", "rect", "ellipse"];
  let flushTimer = null;

  function send(op) {
    engine.apply(op);
    socket.emit("wb_op", { op });
  }

  const newId = () => Math.random().toString(36).slice(2, 12);
  const clamp01 = (v) => Math.min(1, Math.max(0, v));

  function rawPoint(e) {                                   // board pixels
    const r = canvas.getBoundingClientRect();
    return [clamp01((e.clientX - r.left) / r.width) * W, clamp01((e.clientY - r.top) / r.height) * H];
  }
  const norm = (px) => [Math.round(px[0] / W * 10000) / 10000, Math.round(px[1] / H * 10000) / 10000];

  // The ruler: starting a line close to its edge draws it straight along the edge.
  function snapSide(px) {
    const f = frame();
    if (!f) return 0;
    const [lx, ly] = toLocal(f, px[0], px[1]);
    if (Math.abs(lx) <= f.half + 24 && Math.abs(ly) <= RULER_T / 2 + 28) return ly >= 0 ? 1 : -1;
    return 0;
  }
  function snapTo(px, side) {
    const f = frame();
    if (!f || !side) return px;
    const [lx] = toLocal(f, px[0], px[1]);
    return fromLocal(f, Math.max(-f.half, Math.min(f.half, lx)), side * RULER_T / 2);
  }

  function flush() {
    flushTimer = null;
    if (!stroke || !stroke.pending.length) return;
    const pts = stroke.pending.splice(0, stroke.pending.length);
    send({ t: "more", id: stroke.id, p: pts });
  }

  /* ----- ruler dragging ----- */
  let rulerDrag = null;
  let rulerSendTimer = null;
  function sendRuler() {
    rulerSendTimer = null;
    socket.emit("wb_ruler", ruler || { on: false });
  }
  function moveRuler() {
    engine.present();
    if (!rulerSendTimer) rulerSendTimer = setTimeout(sendRuler, 50);
  }
  function startRulerDrag(px) {
    const f = frame();
    if (!f) return false;
    const [lx, ly] = toLocal(f, px[0], px[1]);
    if (Math.abs(lx) > f.half + 20 || Math.abs(ly) > RULER_T / 2 + 14) return false;
    if (Math.abs(lx) > f.half - RULER_T * 0.8) rulerDrag = { mode: "turn", side: lx >= 0 ? 1 : -1 };
    else rulerDrag = { mode: "move", dx: f.cx - px[0], dy: f.cy - px[1] };
    return true;
  }
  function dragRuler(px) {
    if (!rulerDrag || !ruler) return;
    if (rulerDrag.mode === "move") {
      ruler.x = clamp01((px[0] + rulerDrag.dx) / W);
      ruler.y = clamp01((px[1] + rulerDrag.dy) / H);
    } else {
      const f = frame();
      let a = Math.atan2(px[1] - f.cy, px[0] - f.cx);
      if (rulerDrag.side < 0) a += Math.PI;
      // Near flat or upright: click into place, so a level ruler is easy.
      const snaps = [0, Math.PI / 2, Math.PI, -Math.PI / 2, -Math.PI];
      for (const s of snaps) if (Math.abs(a - s) < 0.05) a = s;
      ruler.a = Math.max(-7, Math.min(7, a));
    }
    moveRuler();
  }

  /* ----- pointer ----- */
  canvas.addEventListener("pointerdown", (e) => {
    if (!canDraw()) return;
    e.preventDefault();
    const px = rawPoint(e);

    if (placing) { placePicture(px); return; }
    if (tool === "text") { startText(e); return; }
    if (tool === "ruler") {
      if (startRulerDrag(px)) canvas.setPointerCapture(e.pointerId);
      return;
    }

    canvas.setPointerCapture(e.pointerId);
    const side = (tool === "pen" || tool === "hi" || tool === "line" || tool === "arrow") ? snapSide(px) : 0;
    const start = snapTo(px, side);

    if (SHAPES.includes(tool)) {
      stroke = { id: newId(), shape: tool, a: start, snap: side };
      preview = { t: "shape", k: tool, c: colour, w: SHAPE[sizeIdx], a: norm(start), b: norm(start) };
      return;
    }
    const k = tool === "eraser" ? "erase" : tool === "hi" ? "hi" : "pen";
    stroke = { id: newId(), k, pending: [], snap: side };
    send({
      t: "line", id: stroke.id, k, c: k === "erase" ? "#ffffff" : colour,
      w: (k === "erase" ? ERASER : k === "hi" ? HI : PEN)[sizeIdx],
      p: [norm(start)],
    });
  });

  canvas.addEventListener("pointermove", (e) => {
    if (rulerDrag) { e.preventDefault(); dragRuler(rawPoint(e)); return; }
    if (!stroke) return;
    e.preventDefault();
    const px = snapTo(rawPoint(e), stroke.snap);
    if (stroke.shape) {
      preview.b = norm(px);
      engine.present();
      return;
    }
    stroke.pending.push(norm(px));
    if (!flushTimer) flushTimer = setTimeout(flush, 40);
  });

  function endStroke() {
    if (rulerDrag) { rulerDrag = null; clearTimeout(rulerSendTimer); sendRuler(); }
    if (!stroke) return;
    if (stroke.shape) {
      const p = preview;
      stroke = null; preview = null;
      const far = p && (Math.abs(p.a[0] - p.b[0]) * W > 4 || Math.abs(p.a[1] - p.b[1]) * H > 4);
      if (far) send({ t: "shape", id: newId(), k: p.k, c: p.c, w: p.w, a: p.a, b: p.b });
      else engine.present();
      return;
    }
    flush();
    stroke = null;
  }
  canvas.addEventListener("pointerup", endStroke);
  canvas.addEventListener("pointercancel", endStroke);

  /* ---------- typing ---------- */
  function startText(e) {
    commitText();
    const r = canvas.getBoundingClientRect();
    const px = rawPoint(e);
    textPos = { x: px[0] / W, y: px[1] / H };
    textBox.style.display = "block";
    textBox.style.left = (e.clientX - stage.getBoundingClientRect().left) + "px";
    textBox.style.top = (e.clientY - stage.getBoundingClientRect().top) + "px";
    textBox.style.fontSize = (TEXT[sizeIdx] * r.height) + "px";
    textBox.style.color = colour;
    textBox.value = "";
    setTimeout(() => textBox.focus(), 0);
  }

  function commitText() {
    if (!textPos) return;
    const value = textBox.value;
    const pos = textPos;
    textPos = null;
    textBox.style.display = "none";
    textBox.value = "";
    if (value.trim() && canDraw()) {
      send({ t: "text", s: value, c: colour, x: Math.round(pos.x * 10000) / 10000, y: Math.round(pos.y * 10000) / 10000, size: TEXT[sizeIdx] });
    }
  }
  function cancelText() {
    textPos = null;
    textBox.style.display = "none";
    textBox.value = "";
  }
  textBox.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); commitText(); }
    if (e.key === "Escape") { e.preventDefault(); cancelText(); }
    e.stopPropagation();
  });
  textBox.addEventListener("blur", commitText);

  /* ---------- toolbar ---------- */
  function highlight(selector, activeEl) {
    document.querySelectorAll(selector).forEach((b) => b.classList.toggle("is-on", b === activeEl));
  }
  function highlightByValue(selector, attr, value) {
    document.querySelectorAll(selector).forEach((b) => b.classList.toggle("is-on", b.dataset[attr] === value));
  }

  function chooseTool(name) {
    commitText();
    tool = name;
    highlightByValue("[data-wb-tool]", "wbTool", name);
    canvas.classList.toggle("tool-text", name === "text");
    canvas.classList.toggle("tool-ruler", name === "ruler");
    // A black highlighter would hide the writing: start from yellow.
    if (name === "hi" && colour === "#111111") chooseColour("#f5c400");
    if (name !== "hi" && colour === "#f5c400") chooseColour("#111111");
  }
  function chooseColour(c) {
    colour = c;
    highlightByValue("[data-wb-colour]", "wbColour", c);
    if (tool === "eraser") chooseTool("pen");
  }

  document.querySelectorAll("[data-wb-tool]").forEach((b) => b.addEventListener("click", () => {
    cancelPlacing();
    if (b.dataset.wbTool === "ruler" && (!ruler || !ruler.on)) toggleRuler(true);
    chooseTool(b.dataset.wbTool);
  }));
  document.querySelectorAll("[data-wb-colour]").forEach((b) => {
    b.style.background = b.dataset.wbColour;
    b.addEventListener("click", () => chooseColour(b.dataset.wbColour));
  });
  document.querySelectorAll("[data-wb-size]").forEach((b) => {
    b.addEventListener("click", () => { sizeIdx = +b.dataset.wbSize; highlight("[data-wb-size]", b); });
  });

  // Teacher-only controls below. (A student who may write gets the same pen,
  // highlighter, eraser, colours and sizes, wired above.)
  const bind = (id, fn) => { const el = document.getElementById(id); if (el) el.addEventListener("click", fn); return el; };

  const doneBtn = bind("wb-writer-done", () => socket.emit("wb_done", {}));

  if (!isHost) return;

  bind("wb-undo", () => { commitText(); send({ t: "undo" }); });
  bind("wb-clear", () => { if (confirm("Clear this page?")) send({ t: "clear" }); });

  function toggleRuler(on) {
    ruler = on ? { on: true, x: 0.5, y: 0.5, a: 0, l: 0.6 } : null;
    const btn = document.getElementById("wb-ruler");
    if (btn) btn.classList.toggle("is-on", !!on);
    engine.present();
    socket.emit("wb_ruler", ruler || { on: false });
  }
  bind("wb-ruler", () => {
    if (ruler && ruler.on) { toggleRuler(false); if (tool === "ruler") chooseTool("pen"); }
    else { toggleRuler(true); chooseTool("ruler"); }
  });
  socket.on("wb_state", () => {
    const btn = document.getElementById("wb-ruler");
    if (btn) btn.classList.toggle("is-on", !!(ruler && ruler.on));
  });

  const paper = document.getElementById("wb-paper");
  if (paper) paper.addEventListener("change", () => socket.emit("wb_bg", { bg: paper.value }));

  bind("wb-page-prev", () => socket.emit("wb_page", { page: page - 1 }));
  bind("wb-page-next", () => socket.emit("wb_page", { page: page + 1 }));
  bind("wb-page-add", () => socket.emit("wb_page", { add: true }));

  /* ----- pictures ----- */
  const picInput = document.getElementById("wb-image-input");
  const hint = document.getElementById("wb-hint");
  function say(msg) {
    if (!hint) return;
    hint.textContent = msg || "";
    hint.hidden = !msg;
    if (msg) { clearTimeout(say.t); say.t = setTimeout(() => say(""), 6000); }
  }

  // Pictures are made smaller before they are sent, so a phone photo doesn't flood the lesson.
  function shrink(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        URL.revokeObjectURL(url);
        let side = 1280, quality = 0.82, out = null;
        for (let tries = 0; tries < 6; tries++) {
          const scale = Math.min(1, side / Math.max(img.naturalWidth, img.naturalHeight));
          const c = document.createElement("canvas");
          c.width = Math.max(1, Math.round(img.naturalWidth * scale));
          c.height = Math.max(1, Math.round(img.naturalHeight * scale));
          const g = c.getContext("2d");
          g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);
          g.drawImage(img, 0, 0, c.width, c.height);
          out = { src: c.toDataURL("image/jpeg", quality), w: c.width, h: c.height };
          if (out.src.length <= 360000) return resolve(out);
          side = Math.round(side * 0.75); quality = Math.max(0.55, quality - 0.08);
        }
        reject(new Error("too big"));
      };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("unreadable")); };
      img.src = url;
    });
  }

  bind("wb-image", () => picInput && picInput.click());
  if (picInput) picInput.addEventListener("change", async () => {
    const f = picInput.files && picInput.files[0];
    picInput.value = "";
    if (!f) return;
    try {
      const pic = await shrink(f);
      placing = pic;
      canvas.classList.add("tool-place");
      say("Tap the board where the picture should go.");
    } catch (err) {
      say("That picture could not be used. Try a smaller photo.");
    }
  });
  function cancelPlacing() { placing = null; canvas.classList.remove("tool-place"); }
  function placePicture(px) {
    const pic = placing;
    cancelPlacing();
    say("");
    if (!pic) return;
    let wf = 0.42;
    let hf = wf * W * (pic.h / pic.w) / H;
    if (hf > 0.8) { hf = 0.8; wf = hf * H * (pic.w / pic.h) / W; }
    const x = Math.min(1 - wf, Math.max(0, px[0] / W - wf / 2));
    const y = Math.min(1 - hf, Math.max(0, px[1] / H - hf / 2));
    send({ t: "image", id: newId(), src: pic.src, x: Math.round(x * 1e4) / 1e4, y: Math.round(y * 1e4) / 1e4, w: Math.round(wf * 1e4) / 1e4, h: Math.round(hf * 1e4) / 1e4 });
  }

  /* ----- letting a student write ----- */
  const writerSel = document.getElementById("wb-writer");
  function refreshWriterChoices() {
    if (!writerSel) return;
    const meta = window.peerMeta || {};
    const keep = writer ? writer.sid : writerSel.value;
    writerSel.textContent = "";
    const none = document.createElement("option");
    none.value = ""; none.textContent = "Only me";
    writerSel.appendChild(none);
    Object.keys(meta).filter((sid) => !meta[sid].isHost).sort((a, b) => (meta[a].name || "").localeCompare(meta[b].name || "")).forEach((sid) => {
      const o = document.createElement("option");
      o.value = sid; o.textContent = "Let " + (meta[sid].name || "student") + " write";
      writerSel.appendChild(o);
    });
    if (writer && ![...writerSel.options].some((o) => o.value === writer.sid)) {
      const o = document.createElement("option");
      o.value = writer.sid; o.textContent = "Let " + writer.name + " write";
      writerSel.appendChild(o);
    }
    writerSel.value = keep && [...writerSel.options].some((o) => o.value === keep) ? keep : "";
  }
  if (writerSel) {
    refreshWriterChoices();
    window.addEventListener("participants-changed", refreshWriterChoices);
    writerSel.addEventListener("focus", refreshWriterChoices);
    writerSel.addEventListener("change", () => socket.emit("wb_allow", { sid: writerSel.value || null }));
  }

  /* ----- open / close ----- */
  function openBoard() { setOpen(true); socket.emit("wb_open", {}); }
  function closeBoard() { commitText(); setOpen(false); socket.emit("wb_close", {}); }
  bind("wb-close", closeBoard);
  bind("btn-whiteboard", () => (isOpen ? closeBoard() : openBoard()));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && isOpen && !textPos && !placing) closeBoard(); });

  updatePageUi();
})();
