/*
  Seeing the class, and calling on students.

  1. PRESENTER VIEW (teacher). While slides cover the screen the teacher still
     sees the class: a grid of student faces (raised hands first) and the chat,
     in the side panel of the presenter view.
  2. TEACHER VIDEO (students). While a slide is up, a small picture of the
     teacher stays in the corner. Students can hide it.
  3. CALLING ON A STUDENT. The teacher can
       Spotlight   put a student's video large on everyone's screen, or
       Call on     the same, plus an invitation to speak.
     The student's microphone is never switched on for them: they get a prompt
     and choose "Unmute and speak" or "Not now". The teacher ends the turn (or
     the student stops it) and the microphone returns to how it was.
     The server enforces who may do this (sockets.py: host_call_on etc).

  Everything that must stay visible over a full-screen slide lives in one layer
  that is moved into whichever element is full screen (a full-screen element
  hides everything outside it).
*/
(function () {
  const socket = window._socket;
  const cfg = window.ROOM_CONFIG;
  if (!socket || !cfg) return;
  const isHost = !!cfg.isHost;
  const isObserver = !!cfg.isObserver;
  const $ = (id) => document.getElementById(id);
  const overlay = $("sl-overlay");

  const meta = () => window.peerMeta || {};
  const streamOf = (sid) => (sid === "local" ? (window.getLocalStream && window.getLocalStream()) : (window._remoteStreams[sid] || {}).stream);
  const hasLiveVideo = (st) => !!(st && st.getVideoTracks().some((t) => t.readyState === "live"));
  const initial = (name) => ((name || "?").trim()[0] || "?").toUpperCase();

  /* ================= layer that survives full screen ================= */
  const layer = document.createElement("div");
  layer.id = "classview-layer";
  document.body.appendChild(layer);
  function placeLayer() {
    const host = document.fullscreenElement || document.webkitFullscreenElement || document.body;
    if (layer.parentNode !== host) host.appendChild(layer);
  }
  document.addEventListener("fullscreenchange", placeLayer);
  document.addEventListener("webkitfullscreenchange", placeLayer);
  socket.on("slides_state", () => setTimeout(placeLayer, 400));
  // The poll, breakout and timer tools (teachtools.js and friends) live in this same layer.
  window.ClassLayer = { el: layer, place: placeLayer };

  /* ================= 3. spotlight card and invitation ================= */
  let spot = null; // { sid, name, speak, accepted } while somebody is in the spotlight
  window.__spotlightSid = null;
  let micChangedByCall = false; // we unmuted the student for a call-on and must put it back
  let micWasMuted = false;

  const card = document.createElement("div");
  card.className = "cv-card";
  card.hidden = true;
  card.innerHTML = '<div class="cv-media"><video autoplay playsinline></video><div class="cv-avatar"></div></div>' +
    '<div class="cv-info"><div class="cv-name"></div><div class="cv-status"></div></div>' +
    '<div class="cv-actions"></div>';
  layer.appendChild(card);
  const cardVideo = card.querySelector("video");

  function renderCard() {
    if (!spot) { card.hidden = true; cardVideo.srcObject = null; return; }
    const mine = spot.sid === socket.id;
    const st = streamOf(mine ? "local" : spot.sid);
    card.hidden = false;
    card.classList.toggle("is-speaking", !!spot.accepted);
    cardVideo.muted = true; // their voice already plays from their tile; no echo
    if (cardVideo.srcObject !== st) cardVideo.srcObject = st || null;
    const live = hasLiveVideo(st);
    card.querySelector(".cv-media").classList.toggle("no-video", !live);
    card.querySelector(".cv-avatar").textContent = initial(spot.name);
    card.querySelector(".cv-name").textContent = (mine ? "You" : spot.name);
    let status = "In the spotlight";
    let statusIcon = "";
    if (spot.speak) {
      if (spot.accepted) { status = "Speaking"; statusIcon = "mic"; }
      else if (mine) status = "Your turn!";
      else { status = "Waiting to unmute…"; statusIcon = "clock"; }
    }
    const statusEl = card.querySelector(".cv-status");
    statusEl.textContent = status;
    if (statusIcon) statusEl.insertAdjacentHTML("afterbegin", window.iconHTML(statusIcon));

    const actions = card.querySelector(".cv-actions");
    actions.innerHTML = "";
    const addBtn = (label, fn, cls) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "wb-btn " + (cls || ""); b.textContent = label; b.addEventListener("click", fn);
      actions.appendChild(b);
    };
    if (isHost) addBtn(spot.speak ? "Done" : "End spotlight", () => socket.emit("host_call_on_end", { room_code: cfg.roomCode }));
    else if (mine && spot.speak && spot.accepted) addBtn("Stop speaking", () => socket.emit("call_on_response", { accepted: false }));
  }

  socket.on("spotlight", (st) => {
    const prev = spot;
    spot = st && st.active ? st : null;
    window.__spotlightSid = spot ? spot.sid : null;
    renderCard();
    window.dispatchEvent(new CustomEvent("spotlight-changed", { detail: { sid: window.__spotlightSid } }));
    if (!spot) hideInvite();
    renderFaces();
  });
  window.addEventListener("remote-stream", renderCard);
  window.addEventListener("participants-changed", renderCard);
  setInterval(() => { if (spot) renderCard(); }, 3000); // a video track may come or go (low-data, camera off)

  // The invitation (student side)
  const invite = document.createElement("div");
  invite.className = "cv-invite";
  invite.hidden = true;
  invite.setAttribute("role", "alertdialog");
  invite.innerHTML = '<div class="cv-invite-title"></div><div class="cv-invite-text">Everyone can see you and will hear you once you unmute.</div>' +
    '<div class="cv-invite-actions"><button type="button" class="wb-btn sl-primary" id="cv-accept">Unmute and speak</button>' +
    '<button type="button" class="wb-btn" id="cv-decline">Not now</button></div>';
  layer.appendChild(invite);
  function hideInvite() { invite.hidden = true; }

  function setMic(muted) {
    const btn = $("btn-mic");
    if (!btn) return;
    const isMuted = btn.classList.contains("active-off");
    if (isMuted !== muted) btn.click();
  }

  socket.on("call_on", ({ by }) => {
    if (isHost || isObserver) return;
    // Calling on a student answers their raised hand.
    const hand = $("btn-hand-raise");
    if (hand && hand.classList.contains("active-on")) hand.click();
    invite.querySelector(".cv-invite-title").textContent = (by || "Your teacher") + " is calling on you";
    invite.hidden = false;
    placeLayer();
    try { if (navigator.vibrate) navigator.vibrate(200); } catch (e) { /* ignore */ }
  });
  invite.querySelector("#cv-accept").addEventListener("click", () => {
    const btn = $("btn-mic");
    micWasMuted = !!(btn && btn.classList.contains("active-off"));
    micChangedByCall = micWasMuted;
    setMic(false);
    hideInvite();
    socket.emit("call_on_response", { accepted: true });
  });
  invite.querySelector("#cv-decline").addEventListener("click", () => {
    hideInvite();
    socket.emit("call_on_response", { accepted: false });
  });
  // The turn is over (the teacher ended it, or I stopped): back to how it was.
  function turnOver() {
    hideInvite();
    if (micChangedByCall) { setMic(true); micChangedByCall = false; }
  }
  socket.on("call_on_end", turnOver);
  socket.on("spotlight", (st) => { if (!st || !st.active) turnOver(); });

  socket.on("call_on_declined", ({ name, was_speaking }) => {
    if (!isHost) return;
    toast(was_speaking ? name + " has finished speaking." : name + " isn't ready to speak right now.");
  });

  function toast(text) {
    const t = document.createElement("div");
    t.className = "cv-toast"; t.textContent = text;
    layer.appendChild(t);
    setTimeout(() => t.remove(), 3500);
  }

  /* ---- teacher actions, also used by the participants panel (collab.js) ---- */
  window.CallOn = {
    spotlight: (sid) => socket.emit("host_call_on", { room_code: cfg.roomCode, sid, speak: false }),
    call: (sid) => socket.emit("host_call_on", { room_code: cfg.roomCode, sid, speak: true }),
    end: () => socket.emit("host_call_on_end", { room_code: cfg.roomCode }),
    current: () => spot,
  };

  /* ================= 2. teacher video for students during slides ================= */
  const pip = $("sl-pip");
  if (pip && !isHost) {
    const pipVideo = pip.querySelector("video");
    const pipName = $("sl-pip-name");
    let userHidden = false;
    const toggle = $("sl-pip-toggle");

    function hostSid() {
      return Object.keys(meta()).find((sid) => meta()[sid].isHost);
    }
    function refreshPip() {
      const sid = hostSid();
      const st = sid ? streamOf(sid) : null;
      if (pipVideo.srcObject !== st) pipVideo.srcObject = st || null;
      pipName.textContent = sid ? meta()[sid].name : "";
      const show = !userHidden && hasLiveVideo(st);
      pip.hidden = !show;
      if (toggle) toggle.classList.toggle("is-on", !userHidden);
    }
    pip.querySelector("#sl-pip-x").addEventListener("click", () => { userHidden = true; refreshPip(); });
    if (toggle) toggle.addEventListener("click", () => { userHidden = !userHidden; refreshPip(); });
    window.addEventListener("remote-stream", refreshPip);
    window.addEventListener("participants-changed", refreshPip);
    window.addEventListener("data-mode-changed", refreshPip);
    socket.on("slides_state", () => setTimeout(refreshPip, 50));
    setInterval(refreshPip, 3000);
  }

  /* ================= 1. presenter view: faces + chat ================= */
  const facesEl = $("sl-faces");
  const countEl = $("sl-class-count");
  const faceMap = new Map(); // sid -> { el, video, label }
  const handOrder = []; // sids, oldest raised hand first

  socket.on("hand_raise", ({ sid, raised }) => {
    const i = handOrder.indexOf(sid);
    if (raised && i < 0) handOrder.push(sid);
    if (!raised && i >= 0) handOrder.splice(i, 1);
    renderFaces();
  });

  function showing() { return overlay && overlay.classList.contains("show"); }

  function renderFaces() {
    if (!facesEl || !isHost || !showing()) return;
    const students = Object.entries(meta()).filter(([, m]) => !m.isHost);
    countEl.textContent = students.length ? "(" + students.length + ")" : "";
    // Raised hands first (in the order they went up), then the rest by name.
    students.sort((a, b) => {
      const ha = handOrder.indexOf(a[0]), hb = handOrder.indexOf(b[0]);
      if (ha >= 0 || hb >= 0) return (ha < 0 ? 1e9 : ha) - (hb < 0 ? 1e9 : hb);
      return (a[1].name || "").localeCompare(b[1].name || "");
    });

    const live = new Set(students.map(([sid]) => sid));
    for (const [sid, f] of faceMap) if (!live.has(sid)) { f.el.remove(); faceMap.delete(sid); }

    students.forEach(([sid, m], idx) => {
      let f = faceMap.get(sid);
      if (!f) {
        const el = document.createElement("div");
        el.className = "sl-face";
        el.innerHTML = '<div class="sl-face-media"><video autoplay playsinline muted></video><span class="sl-face-initial"></span></div>' +
          '<div class="sl-face-name"></div><div class="sl-face-actions">' +
          '<button type="button" class="wb-btn" data-act="call">Call on</button>' +
          '<button type="button" class="wb-btn" data-act="spot" title="Show this student to everyone">Spotlight</button></div>';
        el.querySelector('[data-act="call"]').addEventListener("click", () => window.CallOn.call(sid));
        el.querySelector('[data-act="spot"]').addEventListener("click", () => window.CallOn.spotlight(sid));
        f = { el, video: el.querySelector("video"), name: el.querySelector(".sl-face-name"), initial: el.querySelector(".sl-face-initial") };
        faceMap.set(sid, f);
        facesEl.appendChild(el);
      }
      const st = streamOf(sid);
      if (f.video.srcObject !== (st || null)) f.video.srcObject = st || null;
      const hasVideo = hasLiveVideo(st);
      f.el.classList.toggle("no-video", !hasVideo);
      f.el.classList.toggle("hand", handOrder.includes(sid));
      f.el.classList.toggle("in-spotlight", !!spot && spot.sid === sid);
      f.initial.textContent = initial(m.name);
      f.name.textContent = m.name || "";
      if (handOrder.includes(sid)) f.name.insertAdjacentHTML("beforeend", window.iconHTML("hand"));
      if (m.dataMode === "audio") f.name.insertAdjacentHTML("beforeend", window.iconHTML("headphones"));
      else if (m.dataMode === "low") f.name.insertAdjacentHTML("beforeend", window.iconHTML("lowData"));
      // Keep DOM order equal to the sort (cheap: only moves what changed).
      if (facesEl.children[idx] !== f.el) facesEl.insertBefore(f.el, facesEl.children[idx] || null);
    });
  }

  if (facesEl) {
    window.addEventListener("participants-changed", renderFaces);
    window.addEventListener("remote-stream", renderFaces);
    window.addEventListener("data-mode-changed", renderFaces);
    socket.on("slides_state", () => setTimeout(renderFaces, 50));
    setInterval(renderFaces, 2500);
  }

  // Chat mirror: the last messages, always collecting, so the log is complete
  // when the teacher starts presenting.
  const logEl = $("sl-chat-log");
  if (logEl) {
    socket.on("chat_message", ({ name, message }) => {
      const row = document.createElement("div");
      row.className = "sl-chat-row";
      const n = document.createElement("b");
      n.textContent = name + ": ";
      row.appendChild(n);
      row.appendChild(document.createTextNode(message));
      logEl.appendChild(row);
      while (logEl.children.length > 40) logEl.firstChild.remove();
      logEl.scrollTop = logEl.scrollHeight;
    });
    $("sl-chat-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const input = $("sl-chat-input");
      const message = input.value.trim();
      if (!message) return;
      socket.emit("chat_message", { message });
      input.value = "";
    });
    // Typing here must not move the slides (arrows/Space are slide keys).
    ["keydown", "keyup", "keypress"].forEach((ev) => $("sl-chat-input").addEventListener(ev, (e) => e.stopPropagation()));
  }
})();
