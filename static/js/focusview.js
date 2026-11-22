/*
  Shared-screen viewer.

  When someone starts sharing their screen, everyone else gets it on their
  whole display:

    - The video fills 100% of the window (nothing but the picture), and the
      small control bar floats on top and hides itself after a few seconds.
    - When the TEACHER (host) presents, the viewer also asks the browser for
      real full screen automatically. Browsers only allow that after a tap or
      key press, so if it is refused the first tap/click/key press anywhere
      completes it, and a "Full screen" button is always in the bar.
    - Students in a compulsory lesson are already in full screen (the page
      itself is the full screen element), so the viewer simply covers it and
      we never swap or exit their full screen (that would count as leaving).
    - The whole screen is shown by default ("Fit"), so nothing is cropped.
      "Fill" crops to fill the display. On a phone held upright, "Rotate"
      turns the picture sideways so a laptop screen uses the full phone
      screen, and we ask the phone to switch to landscape where it allows it.
    - People who join (or reload) while a share is going see it straight away.

  Anyone can close it and reopen it from the Enlarge button while the share
  is still going. The sharer doesn't get an enlarged view of their own screen.
*/
(function () {
  const overlay = document.getElementById("focus-overlay");
  if (!overlay) return;
  const video = document.getElementById("focus-video");
  const label = document.getElementById("focus-label");
  const btn = document.getElementById("btn-enlarge");
  const btnFull = document.getElementById("focus-full");
  const btnFit = document.getElementById("focus-fit");
  const btnRotate = document.getElementById("focus-rotate");
  const bar = document.getElementById("focus-bar");

  let sharer = null;        // sid of the participant currently sharing
  let showing = false;
  let pendingOpen = false;  // share started before its video arrived
  let weEnteredFullscreen = false;
  let armedHandler = null;
  let barTimer = null;

  /* ---------- Fullscreen helpers (standard + Safari prefixes) ---------- */
  const fsElement = () => document.fullscreenElement || document.webkitFullscreenElement || null;
  const canElementFullscreen = !!(overlay.requestFullscreen || overlay.webkitRequestFullscreen);
  const canVideoFullscreen = !!video.webkitEnterFullscreen; // iPhone: only <video> can go full screen
  const isTouch = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;

  function nameOf(sid) {
    const meta = window.peerMeta && window.peerMeta[sid];
    return meta ? meta.name : "Someone";
  }
  function isHostSid(sid) {
    const meta = window.peerMeta && window.peerMeta[sid];
    return !!(meta && meta.isHost);
  }

  function lockLandscape() {
    if (!isTouch) return;
    try {
      const o = screen.orientation;
      if (o && o.lock) o.lock("landscape").catch(() => {});
    } catch (e) { /* not supported here */ }
  }
  function unlockOrientation() {
    try {
      const o = screen.orientation;
      if (o && o.unlock) o.unlock();
    } catch (e) { /* ignore */ }
  }

  // Resolves true if the display is (now) full screen.
  function enterFullscreen() {
    if (!showing) return Promise.resolve(false);
    if (fsElement()) {
      // Already full screen (e.g. compulsory lesson): the viewer sits inside it and covers everything.
      lockLandscape();
      return Promise.resolve(true);
    }
    const req = overlay.requestFullscreen || overlay.webkitRequestFullscreen;
    if (req) {
      try {
        return Promise.resolve(req.call(overlay, { navigationUI: "hide" }))
          .then(() => { weEnteredFullscreen = true; lockLandscape(); return true; })
          .catch(() => false);
      } catch (e) { return Promise.resolve(false); }
    }
    if (canVideoFullscreen) {
      try { video.webkitEnterFullscreen(); return Promise.resolve(true); } catch (e) { /* fall through */ }
    }
    return Promise.resolve(false);
  }

  function exitFullscreen() {
    if (weEnteredFullscreen && fsElement()) {
      const exit = document.exitFullscreen || document.webkitExitFullscreen;
      if (exit) { try { Promise.resolve(exit.call(document)).catch(() => {}); } catch (e) { /* ignore */ } }
    }
    weEnteredFullscreen = false;
  }

  /* ---------- Waiting for a tap (browsers need a gesture for full screen) ---------- */
  function arm() {
    if (armedHandler) return;
    armedHandler = () => {
      disarm();
      if (showing && !fsElement()) enterFullscreen().then(updateButtons);
    };
    ["click", "pointerup", "keydown"].forEach((t) => document.addEventListener(t, armedHandler, true));
    overlay.classList.add("needs-tap");
  }
  function disarm() {
    if (!armedHandler) return;
    ["click", "pointerup", "keydown"].forEach((t) => document.removeEventListener(t, armedHandler, true));
    armedHandler = null;
    overlay.classList.remove("needs-tap");
  }

  /* ---------- Control bar: floats over the picture, auto-hides ---------- */
  function showBar() {
    bar.classList.remove("hidden");
    clearTimeout(barTimer);
    barTimer = setTimeout(() => { if (showing) bar.classList.add("hidden"); }, 4000);
  }

  function updateButtons() {
    // Turning the phone sideways makes "Rotate" unnecessary: put the picture back.
    if (window.innerWidth >= window.innerHeight) overlay.classList.remove("rotated");
    const full = !!fsElement();
    btnFull.hidden = !(canElementFullscreen || canVideoFullscreen);
    btnFull.innerHTML = full ? "&#10006; Exit full screen" : "&#9974; Full screen";
    btnFit.textContent = overlay.classList.contains("fill") ? "Fit" : "Fill";
    // Offer "Rotate" only when the screen is held upright.
    btnRotate.hidden = !(isTouch && window.innerHeight > window.innerWidth) && !overlay.classList.contains("rotated");
  }

  /* ---------- Open / close ---------- */
  function open(auto) {
    const remote = sharer && window._remoteStreams[sharer];
    if (!remote) { if (auto) pendingOpen = true; return; }
    pendingOpen = false;
    video.srcObject = remote.stream;
    video.play().catch(() => {});
    label.textContent = `${nameOf(sharer)} is presenting`;
    overlay.classList.add("show");
    showing = true;
    showBar();
    updateButtons();
    // The teacher's presentation goes full screen for everyone automatically.
    if (auto && isHostSid(sharer)) {
      enterFullscreen().then((ok) => { if (!ok) arm(); updateButtons(); });
    }
  }

  function close() {
    overlay.classList.remove("show", "rotated");
    video.srcObject = null;
    showing = false;
    pendingOpen = false;
    clearTimeout(barTimer);
    disarm();
    exitFullscreen();
    unlockOrientation();
  }

  function stopSharing() {
    sharer = null;
    pendingOpen = false;
    if (btn) btn.hidden = true;
    close();
  }

  window.addEventListener("screen-share-changed", (e) => {
    const { sid, sharing } = e.detail;
    if (window._socket && sid === window._socket.id) return; // your own share: nothing to enlarge
    if (sharing) {
      sharer = sid;
      if (btn) btn.hidden = false;
      open(true);
    } else if (sid === sharer) {
      stopSharing();
    }
  });

  // The video of a share we were told about has arrived.
  window.addEventListener("remote-stream", (e) => {
    if (pendingOpen && e.detail.sid === sharer) open(true);
  });

  // The sharer left the session.
  window.addEventListener("participants-changed", () => {
    if (sharer && !(window.peerMeta && window.peerMeta[sharer])) stopSharing();
  });

  /* ---------- Buttons ---------- */
  document.getElementById("focus-close").addEventListener("click", close);

  btnFull.addEventListener("click", () => {
    if (fsElement() && weEnteredFullscreen) { exitFullscreen(); updateButtons(); return; }
    enterFullscreen().then(updateButtons);
  });

  btnFit.addEventListener("click", () => {
    overlay.classList.toggle("fill");
    updateButtons();
    showBar();
  });

  btnRotate.addEventListener("click", () => {
    overlay.classList.toggle("rotated");
    updateButtons();
    showBar();
  });

  if (btn) {
    // A tap on Enlarge is a real user gesture, so full screen is allowed here.
    btn.addEventListener("click", () => {
      if (showing) { close(); return; }
      open(false);
      enterFullscreen().then(updateButtons);
    });
  }

  // Tap / move over the picture brings the bar back.
  ["pointermove", "pointerdown", "touchstart"].forEach((t) =>
    overlay.addEventListener(t, () => { if (showing) showBar(); }, { passive: true })
  );

  const onFsChange = () => {
    if (!fsElement()) { weEnteredFullscreen = false; unlockOrientation(); }
    updateButtons();
  };
  document.addEventListener("fullscreenchange", onFsChange);
  document.addEventListener("webkitfullscreenchange", onFsChange);
  window.addEventListener("resize", updateButtons);
  window.addEventListener("orientationchange", updateButtons);

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && showing && !fsElement()) close();
  });
})();
