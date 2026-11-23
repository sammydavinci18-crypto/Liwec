/*
  Ink engine: draws pen, highlighter, eraser, shapes, text and pictures.

  Used by the whiteboard (whiteboard.js), by the teacher's drawing on top of
  slides (slideink.js) and by the server recorder's page (egress_view.html), so
  all of them paint exactly the same picture from the same list of operations.

  An operation is a small object in 0..1 coordinates (fractions of the board),
  so any screen size shows the same drawing. The server validates them
  (classroom_state.clean_ink_op); this file only draws:

    { t:"line",  id, k:"pen"|"hi"|"erase", c, w, p:[[x,y],...] }
    { t:"more",  id, p:[[x,y],...] }                 more points for a line being drawn
    { t:"shape", id, k:"line"|"arrow"|"rect"|"ellipse", c, w, a:[x,y], b:[x,y] }
    { t:"text",  s, c, x, y, size }
    { t:"image", id, src, x, y, w, h }
    { t:"undo" }  { t:"clear" }

  Layers, bottom to top: paper (plain / graph / axes / lined), highlighter (drawn
  see-through, so it never hides writing), ink. The eraser rubs out of both.
*/
(function () {
  "use strict";

  const HIGHLIGHT_ALPHA = 0.4;
  const FONT = '"Segoe UI", Arial, sans-serif';

  function makeLayer(w, h) {
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    return c;
  }

  /* ---------- paper ---------- */
  function drawPaper(ctx, W, H, bg) {
    const cell = W / 40;                        // square cells: 40 across
    const grid = (minor, major) => {
      ctx.lineWidth = 1;
      for (let i = 0, x = 0; x <= W + 0.5; i++, x = i * cell) {
        ctx.strokeStyle = i % 5 === 0 ? major : minor;
        ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, 0); ctx.lineTo(Math.round(x) + 0.5, H); ctx.stroke();
      }
      for (let j = 0, y = 0; y <= H + 0.5; j++, y = j * cell) {
        ctx.strokeStyle = j % 5 === 0 ? major : minor;
        ctx.beginPath(); ctx.moveTo(0, Math.round(y) + 0.5); ctx.lineTo(W, Math.round(y) + 0.5); ctx.stroke();
      }
    };
    if (bg === "grid" || bg === "axes") {
      grid("#d6e6f2", "#a9c9e0");
    }
    if (bg === "axes") {
      const cx = Math.round(W / 2 / cell) * cell, cy = Math.round(H / 2 / cell) * cell;
      ctx.strokeStyle = "#222"; ctx.fillStyle = "#222"; ctx.lineWidth = 2.5; ctx.lineCap = "round";
      ctx.beginPath(); ctx.moveTo(8, cy); ctx.lineTo(W - 8, cy); ctx.moveTo(cx, H - 8); ctx.lineTo(cx, 8); ctx.stroke();
      const head = 12;
      ctx.beginPath();
      ctx.moveTo(W - 6, cy); ctx.lineTo(W - 6 - head, cy - head / 2); ctx.lineTo(W - 6 - head, cy + head / 2); ctx.closePath();
      ctx.moveTo(cx, 6); ctx.lineTo(cx - head / 2, 6 + head); ctx.lineTo(cx + head / 2, 6 + head); ctx.closePath();
      ctx.fill();
      ctx.lineWidth = 1.5;
      for (let x = cx % cell; x < W; x += cell) { ctx.beginPath(); ctx.moveTo(x, cy - 5); ctx.lineTo(x, cy + 5); ctx.stroke(); }
      for (let y = cy % cell; y < H; y += cell) { ctx.beginPath(); ctx.moveTo(cx - 5, y); ctx.lineTo(cx + 5, y); ctx.stroke(); }
      ctx.font = `italic ${Math.round(cell * 0.8)}px Georgia, serif`;
      ctx.textBaseline = "alphabetic";
      ctx.fillText("x", W - cell * 0.9, cy - 12);
      ctx.fillText("y", cx + 12, cell * 1.1);
    }
    if (bg === "lines") {
      const gap = H / 15;
      ctx.lineWidth = 1.5; ctx.strokeStyle = "#c9dcef";
      for (let y = gap * 1.5; y < H; y += gap) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
      ctx.strokeStyle = "#e6a3a3";
      ctx.beginPath(); ctx.moveTo(W * 0.09, 0); ctx.lineTo(W * 0.09, H); ctx.stroke();
    }
  }

  /* ---------- the drawing primitives (also used for live previews) ---------- */
  function strokeLine(ctx, W, H, op, from) {
    const pts = op.p;
    ctx.lineWidth = op.w * W;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.strokeStyle = op.c;
    ctx.beginPath();
    const start = Math.max(0, (from || 0) - 1);
    ctx.moveTo(pts[start][0] * W, pts[start][1] * H);
    if (pts.length - start === 1) ctx.lineTo(pts[start][0] * W + 0.01, pts[start][1] * H);   // a dot
    for (let i = start + 1; i < pts.length; i++) ctx.lineTo(pts[i][0] * W, pts[i][1] * H);
    ctx.stroke();
  }

  function drawShape(ctx, W, H, op) {
    const x1 = op.a[0] * W, y1 = op.a[1] * H, x2 = op.b[0] * W, y2 = op.b[1] * H;
    ctx.strokeStyle = op.c;
    ctx.lineWidth = op.w * W;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    if (op.k === "rect") {
      ctx.rect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
    } else if (op.k === "ellipse") {
      ctx.ellipse((x1 + x2) / 2, (y1 + y2) / 2, Math.abs(x2 - x1) / 2 || 0.5, Math.abs(y2 - y1) / 2 || 0.5, 0, 0, Math.PI * 2);
    } else {
      ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
      if (op.k === "arrow") {
        const ang = Math.atan2(y2 - y1, x2 - x1);
        const head = Math.max(14, op.w * W * 4.5);
        ctx.moveTo(x2, y2); ctx.lineTo(x2 - head * Math.cos(ang - 0.45), y2 - head * Math.sin(ang - 0.45));
        ctx.moveTo(x2, y2); ctx.lineTo(x2 - head * Math.cos(ang + 0.45), y2 - head * Math.sin(ang + 0.45));
      }
    }
    ctx.stroke();
  }

  function drawText(ctx, W, H, op) {
    const px = op.size * H;
    ctx.fillStyle = op.c;
    ctx.font = `${px}px ${FONT}`;
    ctx.textBaseline = "top";
    op.s.split("\n").forEach((line, i) => ctx.fillText(line, op.x * W, op.y * H + i * px * 1.25));
  }

  /* ---------- the engine ---------- */
  function InkEngine(canvas, options) {
    options = options || {};
    const self = this;
    const W = canvas.width, H = canvas.height;
    const ctx = canvas.getContext("2d");
    const transparent = !!options.transparent;            // slides: let the slide show through

    const hiLayer = makeLayer(W, H), hiCtx = hiLayer.getContext("2d");
    const inkLayer = makeLayer(W, H), inkCtx = inkLayer.getContext("2d");
    const paperLayer = makeLayer(W, H), paperCtx = paperLayer.getContext("2d");

    let ops = [];
    const lines = Object.create(null);                      // id -> line op, so "more" can extend it
    const images = Object.create(null);                     // id -> { img, ready }
    let bg = "plain";
    let queued = false;

    self.canvas = canvas;
    self.width = W;
    self.height = H;
    self.overlay = null;                                    // (ctx, W, H) => draw rulers, live previews...
    Object.defineProperty(self, "ops", { get: () => ops });
    Object.defineProperty(self, "bg", { get: () => bg });

    function paintPaper() {
      paperCtx.clearRect(0, 0, W, H);
      drawPaper(paperCtx, W, H, bg);
    }

    function ctxFor(op) { return op.k === "hi" ? hiCtx : inkCtx; }

    function paintOp(op, fromIndex) {
      switch (op.t) {
        case "line":
          if (op.k === "erase") {
            [hiCtx, inkCtx].forEach((c) => { c.save(); c.globalCompositeOperation = "destination-out"; strokeLine(c, W, H, op, fromIndex); c.restore(); });
          } else {
            strokeLine(ctxFor(op), W, H, op, fromIndex);
          }
          break;
        case "shape": drawShape(inkCtx, W, H, op); break;
        case "text": drawText(inkCtx, W, H, op); break;
        case "image": {
          const rec = images[op.id];
          if (rec && rec.ready) inkCtx.drawImage(rec.img, op.x * W, op.y * H, op.w * W, op.h * H);
          break;
        }
      }
    }

    function loadImage(op) {
      if (images[op.id] && images[op.id].src === op.src) return;
      const img = new Image();
      const rec = { img, ready: false, src: op.src };
      images[op.id] = rec;
      img.onload = () => { rec.ready = true; self.redraw(); };
      img.src = op.src;
    }

    self.redraw = function () {
      hiCtx.clearRect(0, 0, W, H);
      inkCtx.clearRect(0, 0, W, H);
      ops.forEach((op) => paintOp(op, 0));
      self.present();
    };

    self.present = function (now) {
      if (now || document.hidden) { render(); return; }     // a hidden tab gets no animation frames
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; render(); });
    };

    function render() {
      if (transparent) ctx.clearRect(0, 0, W, H);
      else { ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, W, H); ctx.drawImage(paperLayer, 0, 0); }
      ctx.globalAlpha = HIGHLIGHT_ALPHA;
      ctx.drawImage(hiLayer, 0, 0);
      ctx.globalAlpha = 1;
      ctx.drawImage(inkLayer, 0, 0);
      if (self.overlay) self.overlay(ctx, W, H);
    }

    self.setBg = function (name) {
      const next = name || "plain";
      if (next === bg && paperLayer._painted) return;
      bg = next;
      paintPaper();
      paperLayer._painted = true;
      self.present();
    };

    self.apply = function (op) {
      switch (op.t) {
        case "line":
          ops.push(op);
          lines[op.id] = op;
          paintOp(op, 0);
          break;
        case "more": {
          const line = lines[op.id];
          if (!line) return;
          const from = line.p.length;
          line.p.push.apply(line.p, op.p);
          paintOp(line, from);
          break;
        }
        case "shape": ops.push(op); paintOp(op); break;
        case "text": ops.push(op); paintOp(op); break;
        case "image": ops.push(op); loadImage(op); paintOp(op); break;
        case "undo": ops.pop(); self.redraw(); return;
        case "clear": ops = []; Object.keys(lines).forEach((k) => delete lines[k]); self.redraw(); return;
        default: return;
      }
      self.present();
    };

    // Replace everything (a page change, or joining late).
    self.setOps = function (list) {
      ops = [];
      Object.keys(lines).forEach((k) => delete lines[k]);
      (list || []).forEach((op) => {
        // Work on copies: the server's list is shared with whoever sent it.
        const copy = Object.assign({}, op);
        if (op.p) copy.p = op.p.slice();
        ops.push(copy);
        if (copy.t === "line") lines[copy.id] = copy;
        if (copy.t === "image") loadImage(copy);
      });
      self.redraw();
    };

    paintPaper();
    paperLayer._painted = true;
    self.present(true);
  }

  InkEngine.drawShape = drawShape;
  InkEngine.drawText = drawText;
  InkEngine.strokeLine = strokeLine;
  window.InkEngine = InkEngine;
})();
