/*
  Quick poll: "raise A, B, C or D".

  The teacher asks (optionally with a question, and optionally marking the right
  answer to make it a quiz). Every student sees big A / B / C / D buttons, taps one
  (and may change it until voting closes) and the teacher watches the count fill
  in live, with the names of anyone who has not answered yet. The teacher can
  close voting, show the counts to the class, and reveal the right answer.

  Students never get the right answer until it is revealed (the server holds it:
  classroom_state.Poll.public_view). Each answer is tied to the student's
  connection, so a reload counts as a new student for that poll.
*/
(function () {
  const T = window.TeachTools;
  if (!T) return;
  const { el, socket, isHost, isObserver } = T;
  const LETTERS = "ABCDEF";

  /* ======================= the teacher ======================= */
  if (isHost) {
    const card = T.card("poll", "Quick poll");
    let n = 4;
    let correct = null;
    let live = null;               // the teacher's view of the running poll (poll_host)

    // ---- setup ----
    const setup = el("div", "tt-setup");
    const q = el("input", "tt-input");
    q.type = "text"; q.maxLength = 300; q.placeholder = "Question (optional): say it out loud or type it";
    q.setAttribute("aria-label", "Question");
    const nRow = el("div", "tt-row");
    nRow.appendChild(el("span", "tt-label", "Choices"));
    const nBtns = el("div", "tt-seg");
    nRow.appendChild(nBtns);
    const cRow = el("div", "tt-row");
    cRow.appendChild(el("span", "tt-label", "Right answer"));
    const cBtns = el("div", "tt-seg");
    cRow.appendChild(cBtns);
    const hint = el("p", "tt-hint", "Mark a right answer to make it a quiz. Students will not see it until you reveal it.");
    const go = el("button", "btn btn-primary btn-sm tt-go", "Ask the class");
    go.type = "button";
    setup.append(q, nRow, cRow, hint, go);

    function seg(box, items, current, pick) {
      box.textContent = "";
      items.forEach(([value, label]) => {
        const b = el("button", "tt-seg-btn" + (value === current ? " is-on" : ""), label);
        b.type = "button";
        b.setAttribute("aria-pressed", value === current ? "true" : "false");
        b.addEventListener("click", () => pick(value));
        box.appendChild(b);
      });
    }
    function drawSetup() {
      seg(nBtns, [2, 3, 4, 5, 6].map((v) => [v, String(v)]), n, (v) => { n = v; if (correct !== null && correct >= n) correct = null; drawSetup(); });
      seg(cBtns, [[null, "None"]].concat(Array.from({ length: n }, (_, i) => [i, LETTERS[i]])), correct, (v) => { correct = v; drawSetup(); });
    }
    go.addEventListener("click", () => {
      socket.emit("poll_start", { q: q.value, n, correct });
      q.value = "";
    });
    q.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Enter") go.click(); });

    // ---- the running poll ----
    const run = el("div", "tt-run");
    const status = el("div", "tt-status");
    const question = el("div", "tt-question");
    const bars = el("div", "tt-bars");
    const waiting = el("div", "tt-waiting");
    const actions = el("div", "tt-actions");
    run.append(status, question, bars, waiting, actions);
    card.body.append(setup, run);

    function btn(label, fn, cls) {
      const b = el("button", "btn btn-sm " + (cls || "btn-ghost"), label);
      b.type = "button";
      b.addEventListener("click", fn);
      return b;
    }

    function drawRun() {
      const p = live;
      setup.hidden = !!p;
      run.hidden = !p;
      if (!p) return;
      const total = Math.max(1, p.counts.reduce((a, b) => a + b, 0));
      const top = Math.max(1, ...p.counts);
      status.textContent = (p.open ? "Voting open" : p.revealed ? "Answer revealed" : "Voting closed") +
        " · " + p.answered + " of " + p.total + " answered";
      status.classList.toggle("is-open", p.open);
      question.textContent = p.q || "";
      question.hidden = !p.q;

      bars.textContent = "";
      p.counts.forEach((c, i) => {
        const row = el("div", "tt-bar" + (p.correct === i ? " is-correct" : ""));
        const letter = el("span", "tt-bar-letter", LETTERS[i]);
        if (p.correct === i) letter.insertAdjacentHTML("beforeend", window.iconHTML("check"));
        row.appendChild(letter);
        const track = el("span", "tt-bar-track");
        const fill = el("span", "tt-bar-fill");
        fill.style.width = (c / top * 100) + "%";
        track.appendChild(fill);
        row.append(track, el("span", "tt-bar-count", String(c) + (p.answered ? "  (" + Math.round(c / total * 100) + "%)" : "")));
        bars.appendChild(row);
      });
      if (p.correct !== null && p.right !== null && p.answered) {
        bars.appendChild(el("div", "tt-score", p.right + " of " + p.answered + " chose the right answer"));
      }

      waiting.textContent = "";
      if (p.open && p.waiting.length) {
        const shown = p.waiting.slice(0, 6).join(", ");
        waiting.textContent = "Still to answer: " + shown + (p.waiting.length > 6 ? " and " + (p.waiting.length - 6) + " more" : "");
      }

      actions.textContent = "";
      if (p.open) actions.appendChild(btn("Close voting", () => socket.emit("poll_close", {}), "btn-primary"));
      else if (!p.revealed) actions.appendChild(btn("Reopen voting", () => socket.emit("poll_reopen", {})));
      if (p.correct !== null && !p.revealed) actions.appendChild(btn("Reveal the answer", () => socket.emit("poll_reveal", {}), p.open ? "btn-ghost" : "btn-primary"));
      if (!p.revealed) actions.appendChild(btn(p.shared ? "Hide counts from class" : "Show counts to class", () => socket.emit("poll_share", { shared: !p.shared })));
      actions.appendChild(btn("New poll", () => socket.emit("poll_end", {})));
    }

    socket.on("poll_host", (p) => {
      live = p && p.id ? p : null;
      drawRun();
      if (live && !card.visible && live.open && live.answered === 0) card.show();
    });

    drawSetup();
    drawRun();
    T.addTool({
      id: "poll", label: "Quick poll", icon: "poll",
      hint: "Raise A, B, C or D. See the count live.",
      open: () => card.show(),
    });
  }

  /* ======================= students ======================= */
  if (isHost || isObserver) return;

  const sheet = el("section", "tt-poll");
  sheet.hidden = true;
  sheet.setAttribute("role", "group");
  sheet.setAttribute("aria-label", "Quick poll");
  T.layer.el.appendChild(sheet);

  let state = null;
  let dismissed = "";               // what the student closed, so a change (results, answer) opens it again

  const signature = (s) => [s.id, s.open, s.shared, s.revealed].join(":");

  function draw() {
    const s = state;
    if (!s || dismissed === signature(s)) { sheet.hidden = true; return; }
    sheet.hidden = false;
    sheet.textContent = "";

    const head = el("div", "tt-poll-head");
    head.appendChild(el("strong", null, s.quiz ? "Quiz question" : "Quick poll"));
    const x = el("button", "tt-x", "×");
    x.type = "button"; x.setAttribute("aria-label", "Hide");
    x.addEventListener("click", () => { dismissed = signature(s); draw(); });
    head.appendChild(x);
    sheet.appendChild(head);
    if (s.q) sheet.appendChild(el("p", "tt-poll-q", s.q));

    const choices = el("div", "tt-choices");
    for (let i = 0; i < s.n; i++) {
      const b = el("button", "tt-choice");
      b.type = "button";
      b.disabled = !s.open;
      b.setAttribute("aria-pressed", s.mine === i ? "true" : "false");
      b.setAttribute("aria-label", "Answer " + LETTERS[i]);
      if (s.mine === i) b.classList.add("is-mine");
      if (s.revealed && s.correct === i) b.classList.add("is-correct");
      if (s.revealed && s.mine === i && s.correct !== i) b.classList.add("is-wrong");
      b.appendChild(el("span", "tt-choice-letter", LETTERS[i]));
      if (s.counts) {
        const total = Math.max(1, s.counts.reduce((a, c) => a + c, 0));
        const bar = el("span", "tt-choice-bar");
        bar.style.height = Math.round(s.counts[i] / total * 100) + "%";
        b.appendChild(bar);
        b.appendChild(el("span", "tt-choice-count", String(s.counts[i])));
      }
      b.addEventListener("click", () => answer(i));
      choices.appendChild(b);
    }
    sheet.appendChild(choices);

    let note;
    if (s.revealed) note = s.mine === s.correct ? "You got it." : s.mine == null ? "No answer from you this time." : "The right answer was " + LETTERS[s.correct] + ".";
    else if (s.open) note = s.mine == null ? "Tap your answer. You can change it until voting closes." : "Answer sent: " + LETTERS[s.mine] + ". You can still change it.";
    else note = s.mine == null ? "Voting is closed." : "Voting is closed. You answered " + LETTERS[s.mine] + ".";
    const foot = el("p", "tt-poll-note", note);
    foot.setAttribute("aria-live", "polite");
    sheet.appendChild(foot);
  }

  function answer(i) {
    if (!state || !state.open) return;
    socket.emit("poll_answer", { id: state.id, choice: i });
    state.mine = i;                                   // at once; the server confirms
    draw();
  }

  socket.on("poll_state", (s) => {
    const fresh = !state || !s || s.id !== state.id;
    state = s && s.id ? s : null;
    if (state && fresh) { dismissed = ""; try { if (navigator.vibrate) navigator.vibrate(120); } catch (e) { /* ignore */ } }
    draw();
    T.layer.place();
  });

  // Keys A to F (or 1 to 6) answer, unless the student is typing in a box.
  document.addEventListener("keydown", (e) => {
    if (!state || !state.open || e.ctrlKey || e.metaKey || e.altKey || T.isTyping(e.target)) return;
    const k = e.key.length === 1 ? e.key.toUpperCase() : "";
    let i = LETTERS.indexOf(k);
    if (i < 0 && k >= "1" && k <= "6") i = +k - 1;
    if (i >= 0 && i < state.n && sheet.hidden === false) { e.preventDefault(); answer(i); }
  });
})();
