/*
  Shared whiteboard.

  The host (teacher) opens it and it covers the whole screen for everyone in the
  session. The host can write with a finger, pen or mouse, or type text; guests
  (and an observing admin) only watch. Closing it returns everyone to the
  session. The board keeps what was drawn, so reopening continues where it was.

  Everything is stored as a list of small operations in 0..1 board coordinates
  (so any screen size shows the same picture). The server validates them, keeps
  them for people who join late, and relays them. recorder.js also paints the
  board into the session recording.
*/
(function () {
  const socket = window._socket;
  const overlay = document.getElementById("wb-overlay");
  if (!socket || !overlay) return;

  const isHost = !!window.ROOM_CONFIG.isHost;
  const canvas = document.getElementById("wb-canvas");
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  const stage = document.getElementById("wb-stage");

  let ops = [];          // drawable operations: lines and text
  const lines = {};      // id -> line op (so "more" points can be appended)
  let isOpen = false;

  /* ---------- drawing ---------- */
  function strokePath(op, fromIndex) {
    const pts = op.p;
    ctx.strokeStyle = op.c;
    ctx.fillStyle = op.c;
    ctx.lineWidth = op.w * W;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    const start = Math.max(0, fromIndex - 1);
    ctx.moveTo(pts[start][0] * W, pts[start][1] * H);
    if (pts.length - start === 1) ctx.lineTo(pts[start][0] * W + 0.01, pts[start][1] * H); // a dot
    for (let i = start + 1; i < pts.length; i++) ctx.lineTo(pts[i][0] * W, pts[i][1] * H);
    ctx.stroke();
  }

  function drawText(op) {
    const px = op.size * H;
    ctx.fillStyle = op.c;
    ctx.font = `${px}px "Segoe UI", Arial, sans-serif`;
    ctx.textBaseline = "top";
    op.s.split("\n").forEach((line, i) => ctx.fillText(line, op.x * W, op.y * H + i * px * 1.25));
  }

  function redraw() {
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, W, H);
    ops.forEach((op) => (op.t === "line" ? strokePath(op, 0) : drawText(op)));
  }

  function apply(op) {
    switch (op.t) {
      case "line":
        ops.push(op);
        lines[op.id] = op;
        strokePath(op, 0);
        break;
      case "more": {
        const line = lines[op.id];
        if (!line) return;
        const from = line.p.length;
        line.p.push(...op.p);
        strokePath(line, from);
        break;
      }
      case "text":
        ops.push(op);
        drawText(op);
        break;
      case "undo":
        ops.pop();
        redraw();
        break;
      case "clear":
        ops = [];
        redraw();
        break;
    }
  }

  function setOpen(open) {
    isOpen = open;
    overlay.classList.toggle("show", open);
    const btn = document.getElementById("btn-whiteboard");
    if (btn) btn.classList.toggle("active-on", open);
    if (!open) cancelText();
  }

  /* ---------- receiving (everyone) ---------- */
  socket.on("wb_open", () => setOpen(true));
  socket.on("wb_close", () => setOpen(false));
  socket.on("wb_op", ({ op }) => apply(op));
  socket.on("wb_state", ({ open, ops: saved }) => {
    ops = [];
    Object.keys(lines).forEach((k) => delete lines[k]);
    redraw();
    (saved || []).forEach(apply);
    setOpen(!!open);
  });

  redraw();
  window.Whiteboard = { isOpen: () => isOpen, canvas };

  if (!isHost) return; // guests and observers only watch

  /* ---------- host tools ---------- */
  let tool = "pen";                 // pen | text | eraser
  let colour = "#111111";
  let sizeIdx = 1;
  const PEN = [0.003, 0.006, 0.012];
  const TEXT = [0.04, 0.06, 0.09];
  const ERASER = [0.02, 0.04, 0.08];

  function send(op) {
    apply(op);
    socket.emit("wb_op", { op });
  }

  function point(e) {
    const r = canvas.getBoundingClientRect();
    const x = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    const y = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
    return [Math.round(x * 10000) / 10000, Math.round(y * 10000) / 10000];
  }

  let stroke = null;   // { id, pending: [] }
  let flushTimer = null;

  function flush() {
    flushTimer = null;
    if (!stroke || !stroke.pending.length) return;
    const pts = stroke.pending.splice(0, stroke.pending.length);
    send({ t: "more", id: stroke.id, p: pts });
  }

  canvas.addEventListener("pointerdown", (e) => {
    if (tool === "text") {
      e.preventDefault();
      startText(e);
      return;
    }
    e.preventDefault();
    canvas.setPointerCapture(e.pointerId);
    const id = Math.random().toString(36).slice(2, 12);
    stroke = { id, pending: [] };
    send({
      t: "line", id,
      c: tool === "eraser" ? "#ffffff" : colour,
      w: (tool === "eraser" ? ERASER : PEN)[sizeIdx],
      p: [point(e)],
    });
  });

  canvas.addEventListener("pointermove", (e) => {
    if (!stroke) return;
    e.preventDefault();
    stroke.pending.push(point(e));
    if (!flushTimer) flushTimer = setTimeout(flush, 40);
  });

  function endStroke() {
    if (!stroke) return;
    flush();
    stroke = null;
  }
  canvas.addEventListener("pointerup", endStroke);
  canvas.addEventListener("pointercancel", endStroke);

  /* ---------- typing ---------- */
  const textBox = document.getElementById("wb-text-input");
  let textPos = null;

  function startText(e) {
    commitText();
    const r = canvas.getBoundingClientRect();
    const [fx, fy] = point(e);
    textPos = { x: fx, y: fy };
    const px = TEXT[sizeIdx] * r.height;
    textBox.style.display = "block";
    textBox.style.left = (e.clientX - stage.getBoundingClientRect().left) + "px";
    textBox.style.top = (e.clientY - stage.getBoundingClientRect().top) + "px";
    textBox.style.fontSize = px + "px";
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
    if (value.trim()) send({ t: "text", s: value, c: colour, x: pos.x, y: pos.y, size: TEXT[sizeIdx] });
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
  document.querySelectorAll("[data-wb-tool]").forEach((b) => {
    b.addEventListener("click", () => { commitText(); tool = b.dataset.wbTool; highlight("[data-wb-tool]", b); });
  });
  document.querySelectorAll("[data-wb-colour]").forEach((b) => {
    b.style.background = b.dataset.wbColour;
    b.addEventListener("click", () => {
      colour = b.dataset.wbColour;
      if (tool === "eraser") { tool = "pen"; highlight("[data-wb-tool]", document.querySelector('[data-wb-tool="pen"]')); }
      highlight("[data-wb-colour]", b);
    });
  });
  document.querySelectorAll("[data-wb-size]").forEach((b) => {
    b.addEventListener("click", () => { sizeIdx = +b.dataset.wbSize; highlight("[data-wb-size]", b); });
  });
  document.getElementById("wb-undo").addEventListener("click", () => { commitText(); send({ t: "undo" }); });
  document.getElementById("wb-clear").addEventListener("click", () => {
    if (confirm("Clear the whole board?")) send({ t: "clear" });
  });

  function openBoard() { setOpen(true); socket.emit("wb_open", {}); }
  function closeBoard() { commitText(); setOpen(false); socket.emit("wb_close", {}); }
  document.getElementById("wb-close").addEventListener("click", closeBoard);
  document.getElementById("btn-whiteboard").addEventListener("click", () => (isOpen ? closeBoard() : openBoard()));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && isOpen && !textPos) closeBoard(); });
})();
