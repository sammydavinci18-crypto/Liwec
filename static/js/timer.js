/*
  Timer and "stay focused" bar.

  The teacher starts a countdown (optionally with the task written on it). Every
  screen shows a thin bar across the top that drains as time runs out, plus a
  clock. It turns amber, then red, near the end; at zero everyone hears a short
  chime and sees "Time's up". The teacher can pause, add a minute, or stop it.

  The server holds the time (classroom_state.Timer) and sends the seconds LEFT,
  not a clock time, so a student's wrong computer clock makes no difference.
*/
(function () {
  const T = window.TeachTools;
  if (!T) return;
  const { el, socket, isHost } = T;

  /* ---------- the bar everyone sees ---------- */
  const strip = el("div", "tt-strip");
  const pill = el("div", "tt-timer");
  strip.hidden = pill.hidden = true;
  const fill = el("div", "tt-strip-fill");
  strip.appendChild(fill);
  const label = el("span", "tt-timer-label");
  const time = el("span", "tt-timer-time");
  time.setAttribute("aria-hidden", "true");
  const live = el("span", "tt-sr");          // a calm spoken update, not every second
  live.setAttribute("role", "status");
  const controls = el("span", "tt-timer-controls");
  pill.append(label, time, controls, live);
  T.layer.el.append(strip, pill);

  let st = null;                 // last state from the server
  let endsAt = 0;                // performance.now() when it reaches zero (while running)
  let doneAt = 0;                // when it reached zero here
  let chimed = false;
  let lastSpoken = "";

  const left = () => {
    if (!st || !st.active) return 0;
    return st.paused ? st.remaining : Math.max(0, (endsAt - performance.now()) / 1000);
  };

  function paint() {
    if (!st || !st.active) { strip.hidden = pill.hidden = true; document.body.classList.remove("tt-timer-on"); return; }
    const s = left();
    const frac = st.total ? Math.min(1, s / st.total) : 0;
    strip.hidden = pill.hidden = false;
    document.body.classList.add("tt-timer-on");
    fill.style.width = (frac * 100) + "%";
    const done = s <= 0;
    const level = done ? "done" : frac <= 0.1 ? "red" : frac <= 0.25 ? "amber" : "ok";
    [strip, pill].forEach((n) => { n.dataset.level = level; n.classList.toggle("is-focus", !!st.focus); n.classList.toggle("is-paused", !!st.paused); });
    time.textContent = done ? "Time's up!" : T.clock(Math.ceil(s));
    label.textContent = st.label || (st.focus ? "Focus time" : "");
    label.hidden = !label.textContent;

    if (done && !chimed) { chimed = true; doneAt = performance.now(); T.chime(); if (isHost) T.toast("Timer finished", 3000); }
    if (done && performance.now() - doneAt > 20000) { st = null; paint(); return; }

    // For screen readers: once a minute, and at the end, not every second.
    const spoken = done ? "Time is up" : st.paused ? "Timer paused" : Math.ceil(s) % 60 === 0 || (s <= 10 && s > 9) ? T.clock(Math.ceil(s)) + " left" : lastSpoken;
    if (spoken !== lastSpoken) { lastSpoken = spoken; live.textContent = spoken; }
  }
  setInterval(paint, 250);

  socket.on("timer_state", (s) => {
    const wasActive = !!(st && st.active);
    st = s && s.active ? s : null;
    if (st) {
      if (!st.paused) endsAt = performance.now() + st.remaining * 1000;
      if (st.remaining > 0) { chimed = false; doneAt = 0; }
      else if (!wasActive) chimed = true;      // joined after it finished: no chime
    }
    drawControls();
    paint();
    T.layer.place();
  });

  /* ---------- the teacher's controls ---------- */
  function drawControls() {
    if (!isHost) return;
    controls.textContent = "";
    if (!st) return;
    const mk = (text, title, fn) => {
      const b = el("button", "tt-timer-btn", text);
      b.type = "button"; b.title = title; b.setAttribute("aria-label", title);
      b.addEventListener("click", fn);
      controls.appendChild(b);
    };
    if (left() > 0) mk(st.paused ? "▶" : "⏸", st.paused ? "Resume" : "Pause", () => socket.emit("timer_control", { action: st.paused ? "resume" : "pause" }));
    mk("+1", "Add a minute", () => socket.emit("timer_control", { action: "add", seconds: 60 }));
    mk("■", "Stop the timer", () => socket.emit("timer_control", { action: "stop" }));
  }

  if (!isHost) return;

  const card = T.card("timer", "Timer");
  let seconds = 300;
  let focus = true;

  const task = el("input", "tt-input");
  task.type = "text"; task.maxLength = 140; task.placeholder = "Task shown on the bar, e.g. Questions 1 to 5";
  task.setAttribute("aria-label", "Task shown on the bar");
  task.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Enter") go.click(); });

  const presets = el("div", "tt-seg tt-presets");
  const custom = el("input", "tt-input tt-minutes");
  custom.type = "number"; custom.min = "1"; custom.max = "180"; custom.placeholder = "min";
  custom.setAttribute("aria-label", "Minutes");
  custom.addEventListener("input", () => { const v = parseFloat(custom.value); if (v > 0) { seconds = Math.round(Math.min(180, v) * 60); drawPresets(); } });
  custom.addEventListener("keydown", (e) => e.stopPropagation());
  function drawPresets() {
    presets.textContent = "";
    [1, 2, 5, 10, 15, 20].forEach((m) => {
      const b = el("button", "tt-seg-btn" + (seconds === m * 60 ? " is-on" : ""), m + " min");
      b.type = "button";
      b.setAttribute("aria-pressed", seconds === m * 60 ? "true" : "false");
      b.addEventListener("click", () => { seconds = m * 60; custom.value = ""; drawPresets(); });
      presets.appendChild(b);
    });
  }

  const focusRow = el("label", "tt-check");
  const cb = el("input"); cb.type = "checkbox"; cb.checked = focus;
  cb.addEventListener("change", () => { focus = cb.checked; });
  focusRow.append(cb, document.createTextNode(" Stay-focused bar: bigger, with the task on it"));

  const go = el("button", "btn btn-primary btn-sm tt-go", "Start timer");
  go.type = "button";
  go.addEventListener("click", () => { socket.emit("timer_start", { seconds, label: task.value, focus }); card.hide(); });

  card.body.append(task, presets, el("div", "tt-row"), focusRow, go);
  card.body.children[2].append(el("span", "tt-label", "Or minutes"), custom);
  drawPresets();

  T.addTool({
    id: "timer", label: "Timer and focus bar", icon: "timer",
    hint: "A visible countdown for a task.",
    open: () => card.show(),
  });
})();
