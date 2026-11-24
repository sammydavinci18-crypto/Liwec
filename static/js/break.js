/*
  Taking a break.

  The teacher presses "Break" in the control bar, picks how long (1, 2, 5, 10, 15
  or 20 minutes, or any number of minutes) and may add a short note. Everyone then
  sees a card: "Taking a break. Back in 5 minutes", a countdown and the clock time
  the class resumes. At zero it says "We're back!" with a short chime. The teacher
  can add a minute or end the break early. Students can close the card if it is in
  the way; it comes back if the teacher changes the break.

  The server sends the seconds LEFT (classroom_state.Break), not a clock time, so a
  student's wrong computer clock never shows the wrong countdown.
*/
(function () {
  const T = window.TeachTools;
  if (!T) return;
  const { el, socket, isHost } = T;

  /* ---------- the card everyone sees ---------- */
  const card = el("div", "tt-break");
  card.hidden = true;
  card.setAttribute("role", "status");
  const icon = el("span", "tt-break-icon");
  icon.setAttribute("aria-hidden", "true");
  if (window.ICONS && window.ICONS.coffee) icon.innerHTML = window.ICONS.coffee;
  const text = el("div", "tt-break-text");
  const title = el("b", "tt-break-title");
  const note = el("span", "tt-break-note");
  const back = el("span", "tt-break-back");
  text.append(title, note, back);
  const time = el("span", "tt-break-time");
  time.setAttribute("aria-hidden", "true");
  const side = el("div", "tt-break-side");
  card.append(icon, text, time, side);
  T.layer.el.appendChild(card);

  const live = el("span", "tt-sr");
  live.setAttribute("role", "status");
  card.appendChild(live);

  let st = null;          // last state from the server
  let endsAt = 0;         // performance.now() when the break ends
  let doneAt = 0;
  let chimed = false;
  let dismissed = false;  // a student closed the card
  let lastSpoken = "";

  const left = () => (st && st.active ? Math.max(0, (endsAt - performance.now()) / 1000) : 0);

  const plural = (n, w) => n + " " + w + (n === 1 ? "" : "s");
  function lengthText(sec) {
    const m = Math.round(sec / 60);
    return m >= 1 ? plural(m, "minute") : plural(Math.round(sec), "second");
  }
  function clockTime(ms) {
    try { return new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
    catch (e) { return ""; }
  }

  function paint() {
    if (!st || !st.active || (dismissed && !isHost)) {
      card.hidden = true;
      return;
    }
    const s = left();
    const done = s <= 0;
    card.hidden = false;
    card.classList.toggle("is-done", done);
    if (done) {
      title.textContent = "We're back!";
      note.textContent = "The break is over. Thank you for waiting.";
      back.textContent = "";
      time.textContent = "";
      if (!chimed) { chimed = true; doneAt = performance.now(); T.chime(); }
      if (performance.now() - doneAt > 15000) { st = null; paint(); return; }
    } else {
      title.textContent = "We are taking a break";
      // "Back in 5 minutes" counts down in whole minutes so it reads naturally.
      const mins = Math.ceil(s / 60);
      note.textContent = (st.message ? st.message + " · " : "") + "Back in " + plural(mins, "minute") + ".";
      back.textContent = "Back at " + clockTime(Date.now() + s * 1000);
      time.textContent = T.clock(Math.ceil(s));
    }
    // For screen readers: say it once at the start and once at the end, not every second.
    const spoken = done ? "The break is over. We are back."
      : "Taking a break. Back in " + lengthText(st.total) + ".";
    if (spoken !== lastSpoken) { lastSpoken = spoken; live.textContent = spoken; }
  }
  setInterval(paint, 500);

  function drawSide() {
    side.textContent = "";
    if (isHost) {
      if (!st) return;
      const mk = (label, tip, fn) => {
        const b = el("button", "tt-timer-btn tt-break-btn", label);
        b.type = "button"; b.title = tip; b.setAttribute("aria-label", tip);
        b.addEventListener("click", fn);
        side.appendChild(b);
      };
      if (left() > 0) mk("+1 min", "Add a minute to the break", () => socket.emit("break_control", { action: "add", seconds: 60 }));
      mk(left() > 0 ? "End break" : "Close", left() > 0 ? "End the break now" : "Close this message", () => socket.emit("break_control", { action: "end" }));
    } else {
      const x = el("button", "tt-x", "×");
      x.type = "button"; x.setAttribute("aria-label", "Hide the break message");
      x.addEventListener("click", () => { dismissed = true; paint(); });
      side.appendChild(x);
    }
  }

  socket.on("break_state", (s) => {
    const wasActive = !!(st && st.active);
    st = s && s.active ? s : null;
    dismissed = false;
    if (st) {
      endsAt = performance.now() + st.remaining * 1000;
      if (st.remaining > 0) { chimed = false; doneAt = 0; lastSpoken = ""; if (!wasActive && !isHost) T.chime(); }
      else if (!wasActive) chimed = true;      // joined after it finished: no chime
    } else if (wasActive && !isHost) {
      T.toast("The break has ended", 3000);
    }
    drawSide();
    paint();
    T.layer.place();
  });

  // The teacher's controls change when the break finishes (the "+1 min" button goes away).
  setInterval(() => { if (isHost && st && left() <= 0 && side.children.length > 1) drawSide(); }, 1000);

  if (!isHost) return;

  /* ---------- the teacher's card ---------- */
  const panel = T.card("break", "Take a break");
  let minutes = 5;

  const presets = el("div", "tt-seg tt-presets");
  const custom = el("input", "tt-input tt-minutes");
  custom.type = "number"; custom.min = "1"; custom.max = "180"; custom.step = "1"; custom.placeholder = "min";
  custom.setAttribute("aria-label", "Minutes");
  custom.addEventListener("input", () => { const v = parseFloat(custom.value); if (v > 0) { minutes = Math.min(180, v); drawPresets(); } });
  custom.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Enter") go.click(); });

  function drawPresets() {
    presets.textContent = "";
    [1, 2, 5, 10, 15, 20].forEach((m) => {
      const b = el("button", "tt-seg-btn" + (minutes === m ? " is-on" : ""), m + " min");
      b.type = "button";
      b.setAttribute("aria-pressed", minutes === m ? "true" : "false");
      b.addEventListener("click", () => { minutes = m; custom.value = ""; drawPresets(); });
      presets.appendChild(b);
    });
    drawPreview();
  }

  const msg = el("input", "tt-input");
  msg.type = "text"; msg.maxLength = 200; msg.placeholder = "Optional note, e.g. Stretch and drink some water";
  msg.setAttribute("aria-label", "Optional note for students");
  msg.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Enter") go.click(); });

  const preview = el("p", "tt-hint tt-break-hint");
  const row = el("div", "tt-row");
  row.append(el("span", "tt-label", "Or minutes"), custom);

  const go = el("button", "btn btn-sm tt-go tt-break-go", "Start break");
  go.type = "button";
  go.addEventListener("click", () => {
    if (!(minutes > 0)) return;
    socket.emit("break_start", { minutes, message: msg.value });
    msg.value = ""; custom.value = "";
    panel.hide();
  });

  function drawPreview() {
    preview.textContent = "Students will see: “We are taking a break. Back in " + plural(Math.max(1, Math.round(minutes)), "minute") + ".”";
  }

  panel.body.append(presets, row, msg, preview, go);
  drawPresets();

  // A button in the control bar, next to Tools.
  const btn = document.getElementById("btn-break");
  if (btn) btn.addEventListener("click", () => {
    if (st && st.active && left() > 0) { panel.hide(); T.toast("A break is already running. Use +1 min or End break.", 3500); return; }
    panel.toggle();
  });

  // Also in the Tools menu, for the presenter view and the whiteboard, which have no control bar.
  T.addTool({
    id: "break", label: "Take a break", icon: "coffee",
    hint: "Tell students you will be back in a few minutes.",
    open: () => { if (st && st.active && left() > 0) T.toast("A break is already running. Use +1 min or End break.", 3500); else panel.show(); },
  });
})();
