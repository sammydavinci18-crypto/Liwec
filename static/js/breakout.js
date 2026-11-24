/*
  Breakout rooms: pairs or groups for an exercise, then everyone back.

  THE TEACHER  picks pairs or a number of groups (and optionally how many
               minutes), and the server splits the students at random. The
               teacher can move a student, "visit" one group (hear it, and be
               heard by it), talk to everyone at once, send a short message to
               every group, add a minute, and bring everyone back. A student can
               ask for help, which shows up for the teacher with the group's name.
  A STUDENT    sees which group they are in and who with, hears and sees only
               their group (and the teacher while the teacher is with them), and
               is returned to the main room when the teacher ends it or the time
               runs out (the server does that: sockets_tools._auto_return).

  Who hears whom is applied in each browser by muting and hiding the other
  groups' tiles. The picture and sound still arrive (like the rest of this
  platform's media rules), and, like the rest of the lesson, every group's
  conversation is part of the lesson recording. The recording is taken from the
  streams, not from what the speakers play, so it keeps all groups.
*/
(function () {
  const T = window.TeachTools;
  if (!T) return;
  const { el, socket, isHost, isObserver } = T;

  let hostState = null;      // breakout_host
  let myState = null;        // breakout_state (a student)

  /* ======================= who hears whom ======================= */
  function verdict(sid) {
    if (isObserver) return { audio: true, video: true };
    if (isHost) {
      if (!hostState || !hostState.active) return { audio: true, video: true };
      const g = hostState.groups.find((x) => x.members.some((m) => m.sid === sid));
      const heard = hostState.visit === "all" || (g && hostState.visit === g.id);
      return { audio: !!heard, video: true };                 // the teacher always sees everyone
    }
    if (!myState || !myState.active) return { audio: true, video: true };
    const meta = (window.peerMeta || {})[sid] || {};
    if (meta.isHost) return { audio: !!myState.teacher_here, video: !!myState.teacher_here, label: "Your teacher is with another group" };
    const mate = (myState.mate_sids || []).includes(sid);
    return { audio: mate, video: mate, label: "In another group" };
  }

  function applyGate() {
    const streams = window._remoteStreams || {};
    const owner = new Map();
    Object.keys(streams).forEach((sid) => { if (streams[sid] && streams[sid].stream) owner.set(streams[sid].stream, sid); });
    document.querySelectorAll("video").forEach((v) => {
      const sid = v.srcObject && owner.get(v.srcObject);
      if (!sid) return;
      const ok = verdict(sid);
      if (v.dataset.gateOrig === undefined) v.dataset.gateOrig = v.muted ? "1" : "0";
      const wantMuted = !ok.audio || v.dataset.gateOrig === "1";
      if (v.muted !== wantMuted) v.muted = wantMuted;
      const tile = v.closest(".mini-tile, .spotlight-tile");
      if (tile) {
        tile.classList.toggle("is-gated", !ok.video);
        if (ok.label) tile.dataset.gateLabel = ok.label;
      }
    });
  }
  let gateQueued = false;
  function queueGate() {
    if (gateQueued) return;
    gateQueued = true;
    queueMicrotask(() => { gateQueued = false; applyGate(); });
  }
  ["stage-area", "gallery-grid"].forEach((id) => {
    const node = document.getElementById(id);
    if (node) new MutationObserver(queueGate).observe(node, { childList: true, subtree: true });
  });
  window.addEventListener("participants-changed", queueGate);
  window.addEventListener("remote-stream", queueGate);
  setInterval(applyGate, 1500);

  /* ======================= shared: the clock ======================= */
  let endsAt = null;                       // performance.now() when the groups end, or null
  function setLeft(left) { endsAt = left == null ? null : performance.now() + left * 1000; }
  const secondsLeft = () => (endsAt == null ? null : Math.max(0, (endsAt - performance.now()) / 1000));

  socket.on("breakout_message", ({ message }) => T.toast((isHost ? "Sent to all groups: " : "Your teacher: ") + message, 8000));

  /* ======================= the teacher ======================= */
  if (isHost) {
    const card = T.card("breakout", "Breakout rooms");
    let mode = "groups";
    let groups = 3;
    let minutes = 0;

    const setup = el("div", "tt-setup");
    const count = el("p", "tt-hint");
    const modeRow = el("div", "tt-row"); modeRow.appendChild(el("span", "tt-label", "Split into"));
    const modeSeg = el("div", "tt-seg"); modeRow.appendChild(modeSeg);
    const nRow = el("div", "tt-row"); nRow.appendChild(el("span", "tt-label", "Groups"));
    const nSeg = el("div", "tt-seg"); nRow.appendChild(nSeg);
    const mRow = el("div", "tt-row"); mRow.appendChild(el("span", "tt-label", "Time"));
    const mSeg = el("div", "tt-seg"); mRow.appendChild(mSeg);
    const go = el("button", "btn btn-primary btn-sm tt-go", "Start breakout rooms");
    go.type = "button";
    const err = el("p", "tt-error"); err.setAttribute("role", "alert"); err.hidden = true;
    const rec = el("p", "tt-hint", "Students are put in groups at random. Group conversations are part of the lesson recording.");
    setup.append(count, modeRow, nRow, mRow, rec, go, err);

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
    function studentCount() {
      const meta = window.peerMeta || {};
      return Object.keys(meta).filter((sid) => !meta[sid].isHost).length;
    }
    function drawSetup() {
      const n = studentCount();
      count.textContent = n + (n === 1 ? " student is" : " students are") + " in the room now.";
      seg(modeSeg, [["pairs", "Pairs"], ["groups", "Groups"]], mode, (v) => { mode = v; drawSetup(); });
      nRow.hidden = mode === "pairs";
      seg(nSeg, [2, 3, 4, 5, 6, 8].map((v) => [v, String(v)]), groups, (v) => { groups = v; drawSetup(); });
      seg(mSeg, [[0, "Until I end it"], [5, "5 min"], [10, "10 min"], [15, "15 min"], [20, "20 min"]], minutes, (v) => { minutes = v; drawSetup(); });
      go.disabled = n < 2;
    }
    go.addEventListener("click", () => { err.hidden = true; socket.emit("breakout_start", { mode, value: groups, minutes }); });
    window.addEventListener("participants-changed", () => { if (!hostState || !hostState.active) drawSetup(); });
    socket.on("breakout_error", ({ message }) => { err.textContent = message; err.hidden = false; });

    // ---- while the groups are open ----
    const run = el("div", "tt-run");
    const top = el("div", "tt-breakout-top");
    const clockEl = el("span", "tt-clock");
    const listening = el("span", "tt-listening");
    const plus = el("button", "btn btn-ghost btn-sm", "+1 min"); plus.type = "button";
    plus.addEventListener("click", () => socket.emit("breakout_extend", { minutes: 1 }));
    top.append(clockEl, plus);
    const helpBox = el("div", "tt-help"); helpBox.hidden = true;
    const talk = el("div", "tt-actions");
    const allBtn = el("button", "btn btn-ghost btn-sm", "Talk to everyone"); allBtn.type = "button";
    allBtn.addEventListener("click", () => socket.emit("breakout_visit", { where: hostState && hostState.visit === "all" ? null : "all" }));
    const msg = el("input", "tt-input");
    msg.type = "text"; msg.maxLength = 200; msg.placeholder = "Message to every group…"; msg.setAttribute("aria-label", "Message to every group");
    msg.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter" && msg.value.trim()) { socket.emit("breakout_broadcast", { message: msg.value }); msg.value = ""; }
    });
    talk.append(allBtn);
    const list = el("div", "tt-groups");
    const end = el("button", "btn btn-danger btn-sm tt-go", "Bring everyone back"); end.type = "button";
    end.addEventListener("click", () => socket.emit("breakout_end", {}));
    run.append(top, listening, helpBox, talk, msg, list, end);
    card.body.append(setup, run);

    function drawRun() {
      const s = hostState;
      const active = !!(s && s.active);
      setup.hidden = active;
      run.hidden = !active;
      if (!active) { drawSetup(); return; }
      setLeft(s.left);
      tick();
      listening.textContent = "You can hear: " + (s.visit === "all" ? "everyone (you are talking to the whole class)" :
        s.visit ? (s.groups.find((g) => g.id === s.visit) || { name: "a group" }).name : "nobody. Visit a group to join it.");
      allBtn.classList.toggle("is-on", s.visit === "all");
      allBtn.textContent = s.visit === "all" ? "Stop talking to everyone" : "Talk to everyone";
      plus.hidden = s.left == null && false;

      list.textContent = "";
      s.groups.forEach((g) => {
        const box = el("div", "tt-group" + (s.visit === g.id ? " is-visited" : ""));
        const head = el("div", "tt-group-head");
        head.appendChild(el("strong", null, g.name + " (" + g.members.length + ")"));
        const visit = el("button", "btn btn-sm " + (s.visit === g.id ? "btn-primary" : "btn-ghost"), s.visit === g.id ? "Leave" : "Visit");
        visit.type = "button";
        visit.addEventListener("click", () => socket.emit("breakout_visit", { where: s.visit === g.id ? null : g.id }));
        head.appendChild(visit);
        box.appendChild(head);
        g.members.forEach((m) => {
          const row = el("div", "tt-member");
          row.appendChild(el("span", "tt-member-name", m.name));
          const sel = el("select", "tt-select");
          sel.setAttribute("aria-label", "Move " + m.name + " to another group");
          s.groups.forEach((o) => { const opt = el("option", null, o.name); opt.value = o.id; sel.appendChild(opt); });
          sel.value = g.id;
          sel.addEventListener("change", () => socket.emit("breakout_move", { sid: m.sid, group: +sel.value }));
          row.appendChild(sel);
          box.appendChild(row);
        });
        list.appendChild(box);
      });
      queueGate();
    }

    function tick() {
      const left = secondsLeft();
      clockEl.textContent = left == null ? "No time limit" : T.clock(left) + " left";
    }
    setInterval(() => { if (hostState && hostState.active) tick(); }, 500);

    socket.on("breakout_host", (s) => { hostState = s; drawRun(); if (s && s.active && !card.visible) card.show(); });
    socket.on("breakout_ended", () => { hostState = { active: false }; drawRun(); queueGate(); });

    socket.on("breakout_help", ({ group, name, from }) => {
      helpBox.hidden = false;
      const row = el("div", "tt-help-row");
      const who = el("span");
      who.innerHTML = window.iconHTML("hand");
      who.appendChild(document.createTextNode(" " + name + " (" + from + ") needs you"));
      row.appendChild(who);
      const visit = el("button", "btn btn-primary btn-sm", "Visit"); visit.type = "button";
      visit.addEventListener("click", () => { socket.emit("breakout_visit", { where: group }); row.remove(); if (!helpBox.children.length) helpBox.hidden = true; });
      const dismiss = el("button", "tt-x", "×"); dismiss.type = "button"; dismiss.setAttribute("aria-label", "Dismiss");
      dismiss.addEventListener("click", () => { row.remove(); if (!helpBox.children.length) helpBox.hidden = true; });
      row.append(visit, dismiss);
      helpBox.appendChild(row);
      T.toast(name + " asked for help", 5000);
      T.chime();
      card.show();
    });

    drawSetup();
    T.addTool({
      id: "breakout", label: "Breakout rooms", icon: "groups",
      hint: "Pairs or groups for an exercise, then bring everyone back.",
      open: () => card.show(),
    });
    return;
  }

  /* ======================= students ======================= */
  if (isObserver) return;

  const banner = el("div", "tt-breakout");
  banner.hidden = true;
  banner.setAttribute("role", "status");
  const title = el("strong", "tt-breakout-title");
  const who = el("span", "tt-breakout-who");
  const clockEl = el("span", "tt-clock");
  const helpLabel = () => { help.innerHTML = window.iconHTML("hand") + " Ask teacher to join"; };
  const help = el("button", "wb-btn"); help.type = "button"; helpLabel();
  help.addEventListener("click", () => {
    socket.emit("breakout_help", {});
    help.disabled = true; help.textContent = "Teacher asked";
    setTimeout(() => { help.disabled = false; helpLabel(); }, 20000);
  });
  banner.append(title, clockEl, who, help);
  T.layer.el.appendChild(banner);

  function draw() {
    const s = myState;
    const active = !!(s && s.active);
    banner.hidden = !active;
    document.body.classList.toggle("in-breakout", active);
    if (!active) { queueGate(); return; }
    title.textContent = s.group.name;
    let text = s.mates.length ? "with " + s.mates.join(", ") : "on your own for now";
    if (s.everyone) text += " · your teacher is talking to everyone";
    else if (s.teacher_here) text += " · your teacher is here";
    who.textContent = text;
    setLeft(s.left);
    tickS();
    T.layer.place();
    queueGate();
  }
  function tickS() {
    const left = secondsLeft();
    clockEl.textContent = left == null ? "" : T.clock(left) + " left";
    clockEl.hidden = left == null;
  }
  setInterval(() => { if (myState && myState.active) tickS(); }, 500);

  socket.on("breakout_state", (s) => {
    const was = !!(myState && myState.active);
    myState = s;
    if (s.active && !was) { T.toast("Breakout rooms: you are in " + s.group.name + (s.mates.length ? " with " + s.mates.join(", ") : ""), 7000); T.chime(); }
    draw();
  });
  socket.on("breakout_ended", ({ message }) => {
    myState = { active: false };
    draw();
    T.toast(message || "Back in the main room.", 6000);
    T.chime();
  });
})();
