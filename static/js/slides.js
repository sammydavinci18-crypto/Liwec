/*
  Presentations (PowerPoint / PDF).

  The teacher uploads a file from the Slides panel; the server turns it into one
  picture per slide (slides.py). When the teacher presents it:

    EVERYONE ELSE   The slide fills their whole display (nothing but the
                    picture) and follows the teacher's slide changes live. The
                    small bar on top hides itself after a few seconds. As with a
                    shared screen, a presentation from the teacher asks the
                    browser for real full screen automatically; browsers only
                    allow that after a tap, so the first tap anywhere finishes
                    it, and a "Full screen" button is always in the bar. Students
                    already in a compulsory lesson are in full screen, so the
                    slide simply covers it and we never exit it (that would
                    count as leaving). On a phone held upright, "Rotate" turns
                    the slide sideways to use the whole screen.

    THE TEACHER     A presenter view: the current slide (exactly what the class
                    sees), a preview of the next one, Previous / Next buttons,
                    a "go to slide" box, a strip of every slide, and any raised
                    hands. Arrow keys, Space, Page Up/Down, Home/End and a swipe
                    on a tablet all move the slides too.

  Only the host can start, stop or move the presentation: the server checks
  every message (sockets.py). People who join or reload mid-presentation are
  put straight on the current slide. recorder.js paints the slide into the
  session recording while it is on screen.

  Slide numbers: 0-based inside this file and in the socket messages; shown to
  people as 1-based.
*/
(function () {
  const socket = window._socket;
  const overlay = document.getElementById("sl-overlay");
  if (!socket || !overlay) return;

  const cfg = window.ROOM_CONFIG;
  const isHost = !!cfg.isHost;
  const base = "/room/" + encodeURIComponent(cfg.roomCode) + "/slides";
  const $ = (id) => document.getElementById(id);

  const img = $("sl-img");
  const loading = $("sl-loading");
  const failed = $("sl-failed");
  const titleEl = $("sl-title");
  const chip = $("sl-chip");
  const btnFull = $("sl-full");

  let deck = null;          // { id, title, pages } while a presentation is on screen
  let index = 0;            // current slide, 0-based
  let showing = false;
  const cache = new Map();  // slide number (1-based) -> Image, so changing slides is instant

  const slideUrl = (n) => base + "/" + deck.id + "/" + n + ".jpg?v=" + deck.v;
  const thumbUrl = (n) => base + "/" + deck.id + "/" + n + "-thumb.jpg?v=" + deck.v;

  /* ---------- pictures: load ahead so a change of slide is instant ---------- */
  function getImage(n) {
    if (!deck || n < 1 || n > deck.pages) return null;
    let im = cache.get(n);
    if (!im) {
      im = new Image();
      im.decoding = "async";
      im.addEventListener("error", () => { if (cache.get(n) === im) cache.delete(n); }); // so "Try again" really retries
      im.src = slideUrl(n);
      cache.set(n, im);
    }
    return im;
  }

  // Only a few slides ahead (and one back): the whole deck would cost every
  // student's mobile data for slides the teacher may never show.
  function preloadAround(i) {
    for (let n = i; n <= i + 4; n++) getImage(n);   // n is 1-based: i+1 is the current slide
  }

  function show(i) {
    if (!deck) return;
    index = Math.max(0, Math.min(deck.pages - 1, i));
    const n = index + 1;
    const im = getImage(n);
    preloadAround(index);
    updateChrome();
    if (window.SlideInk) window.SlideInk.slideChanged(deck.id, index);   // the drawing belongs to a slide

    const apply = () => {
      if (!showing || !deck || index + 1 !== n) return;   // the teacher already moved on
      if (img.getAttribute("src") !== im.src) img.src = im.src;
      if (window.SlideInk) window.SlideInk.imageShown(im);   // so the drawing layer has the slide's shape
      img.alt = "Slide " + n + " of " + deck.pages;
      loading.hidden = true;
      failed.hidden = true;
      if (isHost) renderNext();   // (the next-slide preview exists only in the teacher's view)
    };

    if (im.complete && im.naturalWidth) {
      apply();
    } else {
      loading.hidden = false;   // the previous slide stays up underneath until this one is ready
      failed.hidden = true;
      im.addEventListener("load", apply, { once: true });
      im.addEventListener("error", () => {
        if (showing && index + 1 === n) { loading.hidden = true; failed.hidden = false; }
      }, { once: true });
    }
  }

  $("sl-retry").addEventListener("click", () => show(index));

  /* ---------- full screen (same approach as the shared-screen viewer) ---------- */
  const fsElement = () => document.fullscreenElement || document.webkitFullscreenElement || null;
  const canFullscreen = !!(overlay.requestFullscreen || overlay.webkitRequestFullscreen);
  const isTouch = !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
  let weEnteredFullscreen = false;
  let armedHandler = null;

  function lockLandscape() {
    if (!isTouch) return;
    try { if (screen.orientation && screen.orientation.lock) screen.orientation.lock("landscape").catch(() => {}); } catch (e) { /* not supported */ }
  }
  function unlockOrientation() {
    try { if (screen.orientation && screen.orientation.unlock) screen.orientation.unlock(); } catch (e) { /* ignore */ }
  }

  function enterFullscreen() {
    if (!showing) return Promise.resolve(false);
    if (fsElement()) { lockLandscape(); return Promise.resolve(true); }   // already full screen: we sit inside it
    const req = overlay.requestFullscreen || overlay.webkitRequestFullscreen;
    if (!req) return Promise.resolve(false);
    try {
      return Promise.resolve(req.call(overlay, { navigationUI: "hide" }))
        .then(() => { weEnteredFullscreen = true; lockLandscape(); return true; })
        .catch(() => false);
    } catch (e) { return Promise.resolve(false); }
  }

  function exitFullscreen() {
    if (weEnteredFullscreen && fsElement()) {
      const exit = document.exitFullscreen || document.webkitExitFullscreen;
      if (exit) { try { Promise.resolve(exit.call(document)).catch(() => {}); } catch (e) { /* ignore */ } }
    }
    weEnteredFullscreen = false;
  }

  // Browsers only allow full screen after a tap or key press: wait for the first one.
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

  function updateButtons() {
    if (window.innerWidth >= window.innerHeight) overlay.classList.remove("rotated");
    btnFull.hidden = !canFullscreen;
    btnFull.innerHTML = window.iconHTML(fsElement() ? "shrink" : "expand") + (fsElement() ? " Exit full screen" : " Full screen");
    const rotate = $("sl-rotate");
    if (rotate) rotate.hidden = !(isTouch && window.innerHeight > window.innerWidth) && !overlay.classList.contains("rotated");
  }

  btnFull.addEventListener("click", () => {
    if (fsElement() && weEnteredFullscreen) { exitFullscreen(); updateButtons(); return; }
    enterFullscreen().then(updateButtons);
  });
  const onFsChange = () => {
    if (!fsElement()) { weEnteredFullscreen = false; unlockOrientation(); }
    updateButtons();
  };
  document.addEventListener("fullscreenchange", onFsChange);
  document.addEventListener("webkitfullscreenchange", onFsChange);
  window.addEventListener("resize", updateButtons);
  window.addEventListener("orientationchange", updateButtons);

  const rotateBtn = $("sl-rotate");
  if (rotateBtn) rotateBtn.addEventListener("click", () => { overlay.classList.toggle("rotated"); updateButtons(); showBar(); });

  /* ---------- the bar on top: floats over the slide for students and hides itself ---------- */
  const topbar = $("sl-topbar");
  let barTimer = null;
  function showBar() {
    topbar.classList.remove("hidden");
    clearTimeout(barTimer);
    if (!isHost) barTimer = setTimeout(() => { if (showing) topbar.classList.add("hidden"); }, 4000);
  }
  if (!isHost) {
    ["pointermove", "pointerdown", "touchstart"].forEach((t) =>
      overlay.addEventListener(t, () => { if (showing) showBar(); }, { passive: true }));
  }

  /* ---------- what is written around the slide ---------- */
  function updateChrome() {
    if (!deck) return;
    const n = index + 1;
    if (isHost) {
      $("sl-jump").value = n;
      $("sl-jump").max = deck.pages;
      $("sl-total").textContent = deck.pages;
      $("sl-first").disabled = $("sl-prev").disabled = index === 0;
      $("sl-last").disabled = $("sl-next").disabled = index === deck.pages - 1;
      chip.textContent = "Showing to the class";
      markCurrentThumb();
    } else {
      chip.textContent = "Slide " + n + " of " + deck.pages;
      $("sl-counter").textContent = n + " / " + deck.pages;
    }
  }

  /* ---------- opening and closing ---------- */
  function open(state) {
    const sameDeck = deck && deck.id === state.deck_id && deck.v === state.v;
    const wasShowing = showing;
    if (!sameDeck) {
      cache.clear();
      deck = { id: state.deck_id, v: state.v, title: state.title || "Presentation", pages: state.pages };
      if (isHost) buildStrip();
    }
    titleEl.textContent = deck.title;
    showing = true;
    overlay.classList.add("show");
    showBar();
    show(state.index || 0);
    updateButtons();

    if (isHost) {
      // Don't leave focus on the button that was clicked to start: Space would press it again.
      if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
      closePanel();
      // The whiteboard and the slides can't both be on everyone's screen.
      if (window.Whiteboard && window.Whiteboard.isOpen()) { const wb = $("wb-close"); if (wb) wb.click(); }
    } else if (!wasShowing || !sameDeck) {
      // A presentation from the teacher goes full screen for everyone automatically.
      enterFullscreen().then((ok) => { if (!ok) arm(); updateButtons(); });
    }
  }

  function close() {
    showing = false;
    deck = null;
    cache.clear();
    overlay.classList.remove("show", "rotated", "peek");
    const peekBarEl = document.getElementById("sl-peek-bar");
    if (peekBarEl) peekBarEl.hidden = true;
    img.removeAttribute("src");
    loading.hidden = failed.hidden = true;
    clearTimeout(barTimer);
    disarm();
    exitFullscreen();
    unlockOrientation();
    if (isHost) { hands.clear(); renderHands(); refreshLibrary(); }
  }

  socket.on("slides_state", (state) => {
    if (state && state.active) open(state);
    else if (showing || deck) close();
  });

  // The host's own screen already moved before this was sent; it reaches everyone else.
  socket.on("slides_goto", ({ index: i }) => { if (showing) show(i); });

  // Exposed for recorder.js (paints the slide into the recording).
  window.Slides = {
    isHost,
    isOpen: () => showing,
    currentImage: () => (showing && img.complete && img.naturalWidth ? img : null),
  };

  /* ---------- people (students): raise a hand without leaving the slide ---------- */
  const handBtn = $("sl-hand");
  const realHand = document.getElementById("btn-hand-raise");
  if (handBtn && realHand) {
    handBtn.addEventListener("click", () => { realHand.click(); showBar(); });
    const sync = () => {
      const on = realHand.classList.contains("active-on");
      handBtn.classList.toggle("is-on", on);
      handBtn.innerHTML = window.iconHTML("hand") + (on ? " Lower hand" : " Raise hand");
    };
    new MutationObserver(sync).observe(realHand, { attributes: true, attributeFilter: ["class"] });
    sync();
  }

  if (!isHost) return;   // ---------------- everything below is the teacher's ----------------

  /* ---------- moving through the slides ---------- */
  function go(i) {
    if (!deck) return;
    const target = Math.max(0, Math.min(deck.pages - 1, i));
    if (target === index) return;
    show(target);                                      // the teacher's screen moves at once...
    socket.emit("slides_goto", { index: target });     // ...and the server moves everyone else
  }
  const next = () => go(index + 1);
  const prev = () => go(index - 1);

  $("sl-next").addEventListener("click", next);
  $("sl-prev").addEventListener("click", prev);
  $("sl-first").addEventListener("click", () => go(0));
  $("sl-last").addEventListener("click", () => deck && go(deck.pages - 1));

  const jump = $("sl-jump");
  const jumpNow = () => {
    const n = parseInt(jump.value, 10);
    if (Number.isFinite(n)) go(n - 1);
    jump.value = index + 1;
  };
  jump.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); jumpNow(); jump.blur(); }
  });
  jump.addEventListener("change", jumpNow);
  jump.addEventListener("focus", () => jump.select());

  document.addEventListener("keydown", (e) => {
    if (!showing || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && t.closest && t.closest("input, textarea, select")) return;
    const onButton = !!(t && t.closest && t.closest("button"));
    switch (e.key) {
      case "ArrowRight": case "ArrowDown": case "PageDown": e.preventDefault(); next(); break;
      case "ArrowLeft": case "ArrowUp": case "PageUp": e.preventDefault(); prev(); break;
      case " ": if (!onButton) { e.preventDefault(); e.shiftKey ? prev() : next(); } break;
      case "Home": e.preventDefault(); go(0); break;
      case "End": e.preventDefault(); if (deck) go(deck.pages - 1); break;
    }
  });

  // A swipe on a tablet.
  const stage = $("sl-stage");
  let swipe = null;
  stage.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse") return;
    swipe = { x: e.clientX, y: e.clientY };
  });
  stage.addEventListener("pointerup", (e) => {
    if (!swipe) return;
    const dx = e.clientX - swipe.x, dy = e.clientY - swipe.y;
    swipe = null;
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) (dx < 0 ? next : prev)();
  });
  stage.addEventListener("pointercancel", () => { swipe = null; });

  /* ---------- next-slide preview ---------- */
  const nextImg = $("sl-next-img");
  const lastNote = $("sl-last-note");
  function renderNext() {
    const im = getImage(index + 2);
    if (im) {
      if (nextImg.getAttribute("src") !== im.src) nextImg.src = im.src;
      nextImg.hidden = false;
      lastNote.hidden = true;
    } else {
      nextImg.hidden = true;
      nextImg.removeAttribute("src");
      lastNote.hidden = false;
    }
  }

  /* ---------- strip of every slide ---------- */
  const strip = $("sl-strip");
  function buildStrip() {
    strip.textContent = "";
    for (let n = 1; n <= deck.pages; n++) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "sl-thumb";
      b.dataset.n = n;
      b.setAttribute("aria-label", "Go to slide " + n);
      const t = document.createElement("img");
      t.loading = "lazy";
      t.decoding = "async";
      t.alt = "";
      t.draggable = false;
      t.src = thumbUrl(n);
      const num = document.createElement("span");
      num.textContent = n;
      b.append(t, num);
      strip.appendChild(b);
    }
  }
  strip.addEventListener("click", (e) => {
    const b = e.target.closest(".sl-thumb");
    if (b) go(parseInt(b.dataset.n, 10) - 1);
  });
  function markCurrentThumb() {
    const old = strip.querySelector(".is-current");
    if (old) old.classList.remove("is-current");
    const el = strip.children[index];
    if (!el) return;
    el.classList.add("is-current");
    // Scroll the strip itself (not the whole page) so the current slide stays in view.
    const left = el.offsetLeft - (strip.clientWidth - el.offsetWidth) / 2;
    strip.scrollTo({ left: Math.max(0, left), behavior: "smooth" });
  }

  /* ---------- raised hands, so a question isn't missed behind the slide ---------- */
  const handsEl = $("sl-hands");
  const hands = new Map(); // sid -> name
  function renderHands() {
    syncPeekHands();
    if (!hands.size) { handsEl.hidden = true; handsEl.textContent = ""; return; }
    handsEl.hidden = false;
    handsEl.textContent = "\u270B " + Array.from(hands.values()).join(", ");
  }
  function syncPeekHands() {   // raised hands stay visible while the slide is hidden
    const el = $("sl-peek-hands");
    if (!el) return;
    el.hidden = !hands.size;
    el.textContent = hands.size ? "\u270B " + Array.from(hands.values()).join(", ") : "";
  }
  socket.on("hand_raise", ({ sid, name, raised }) => {
    if (raised) hands.set(sid, name || "Someone"); else hands.delete(sid);
    renderHands();
  });
  window.addEventListener("participants-changed", () => {
    let changed = false;
    hands.forEach((_, sid) => { if (!(window.peerMeta && window.peerMeta[sid])) { hands.delete(sid); changed = true; } });
    if (changed) renderHands();
  });

  $("sl-stop").addEventListener("click", () => socket.emit("slides_stop", {}));

  /* ---------- hide the slide on the teacher's screen only, to see the class ---------- */
  const peekBar = $("sl-peek-bar");
  function setPeek(on) {
    if (on && !showing) return;
    if (on && fsElement() === overlay) {            // the overlay can't stay full screen while hidden
      const exit = document.exitFullscreen || document.webkitExitFullscreen;
      if (exit) { try { Promise.resolve(exit.call(document)).catch(() => {}); } catch (e) { /* ignore */ } }
      weEnteredFullscreen = false;
    }
    overlay.classList.toggle("peek", on);
    peekBar.hidden = !on;
    if (on) syncPeekHands();
    else if (showing) show(index);                  // coming back: make sure the picture is current
  }
  $("sl-peek").addEventListener("click", () => setPeek(true));
  $("sl-peek-back").addEventListener("click", () => setPeek(false));
  $("sl-peek-stop").addEventListener("click", () => socket.emit("slides_stop", {}));
  $("sl-peek-prev").addEventListener("click", prev);
  $("sl-peek-next").addEventListener("click", next);
  $("sl-edge-prev").addEventListener("click", prev);
  $("sl-edge-next").addEventListener("click", next);

  // After a click, give focus back to the page so Space / arrows move the slides
  // (a focused button would otherwise swallow Space and repeat its own action).
  overlay.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b && b !== btnFull) b.blur();
  });

  /* ---------- the Slides panel: upload, then choose what to show ---------- */
  const panel = $("slides-panel");
  const btnSlides = $("btn-slides");
  const pick = $("slides-pick");
  const fileInput = $("slides-file");
  const hint = $("slides-hint");
  const note = $("slides-note");
  const progress = $("slides-progress");
  const list = $("slides-list");

  const ACCEPT_ALL = ".pptx,.ppt,.odp,.pdf,application/pdf,application/vnd.openxmlformats-officedocument.presentationml.presentation,application/vnd.ms-powerpoint";
  let officeOk = true;
  let maxMb = 40;
  let decks = [];
  let pollTimer = null;
  let uploading = false;

  function say(msg, isError) {
    note.textContent = msg || "";
    note.classList.toggle("is-error", !!isError);
  }

  function openPanel() {
    document.querySelectorAll(".notes-panel.open").forEach((p) => { if (p !== panel) p.classList.remove("open"); });
    panel.classList.add("open");
    btnSlides.classList.add("active-on");
    refreshLibrary();
  }
  function closePanel() {
    panel.classList.remove("open");
    btnSlides.classList.remove("active-on");
    clearTimeout(pollTimer);
  }
  btnSlides.addEventListener("click", () => (panel.classList.contains("open") ? closePanel() : openPanel()));
  $("slides-close").addEventListener("click", closePanel);

  async function refreshLibrary() {
    clearTimeout(pollTimer);
    if (!panel.classList.contains("open")) return;
    try {
      const res = await fetch(panel.dataset.url, { credentials: "same-origin" });
      if (!res.ok) throw new Error("status " + res.status);
      const data = await res.json();
      decks = data.decks || [];
      officeOk = !!data.office_ok;
      maxMb = data.max_mb || maxMb;
      fileInput.accept = officeOk ? ACCEPT_ALL : ".pdf,application/pdf";
      hint.textContent = officeOk
        ? "Tap to choose a file, or drop it here (PowerPoint or PDF, up to " + maxMb + " MB)"
        : "PowerPoint upload isn't switched on for this server yet, so use a PDF (up to " + maxMb + " MB). In PowerPoint: File \u203A Save As \u203A PDF.";
      renderLibrary();
      // Keep checking while anything is still being prepared.
      if (decks.some((d) => d.status === "processing")) pollTimer = setTimeout(refreshLibrary, 1500);
    } catch (err) {
      say("Could not load your presentations. Check your connection.", true);
    }
  }

  function renderLibrary() {
    list.textContent = "";
    if (!decks.length) {
      const empty = document.createElement("p");
      empty.className = "slides-empty";
      empty.textContent = "Nothing uploaded yet. Presentations stay available until this session ends.";
      list.appendChild(empty);
      return;
    }
    decks.forEach((d) => {
      const row = document.createElement("div");
      row.className = "slides-item";

      const thumb = document.createElement("div");
      thumb.className = "slides-thumb";
      if (d.status === "ready") {
        const t = document.createElement("img");
        t.alt = "";
        t.loading = "lazy";
        t.src = base + "/" + d.id + "/1-thumb.jpg?v=" + d.v;
        thumb.appendChild(t);
      } else if (d.status === "processing") {
        thumb.classList.add("is-busy");
      }

      const info = document.createElement("div");
      info.className = "slides-info";
      const name = document.createElement("strong");
      name.textContent = d.title;
      const meta = document.createElement("span");
      meta.className = "slides-meta";
      if (d.status === "ready") meta.textContent = d.pages + (d.pages === 1 ? " slide" : " slides");
      else if (d.status === "processing") meta.textContent = "Preparing your slides\u2026";
      else { meta.textContent = d.error || "This file could not be prepared."; meta.classList.add("is-error"); }
      info.append(name, meta);

      const actions = document.createElement("div");
      actions.className = "slides-actions";
      if (d.status === "ready") {
        const present = document.createElement("button");
        present.type = "button";
        present.className = "btn btn-primary btn-sm";
        present.textContent = "Present";
        present.addEventListener("click", () => socket.emit("slides_present", { deck_id: d.id }));
        actions.appendChild(present);
      }
      if (d.status !== "processing") {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn btn-ghost btn-sm";
        remove.textContent = "Remove";
        remove.addEventListener("click", () => removeDeck(d));
        actions.appendChild(remove);
      }

      row.append(thumb, info, actions);
      list.appendChild(row);
    });
  }

  async function removeDeck(d) {
    if (!confirm("Remove \u201C" + d.title + "\u201D from this session?")) return;
    try {
      const res = await fetch(base + "/" + d.id + "/delete", { method: "POST", credentials: "same-origin" });
      if (!res.ok) throw new Error("status " + res.status);
      say("");
    } catch (err) {
      say("That could not be removed. Please try again.", true);
    }
    refreshLibrary();
  }

  function upload(file) {
    if (uploading || !file) return;
    const ext = (file.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
    const allowed = officeOk ? [".pptx", ".ppt", ".odp", ".pdf"] : [".pdf"];
    if (!allowed.includes(ext)) {
      say(officeOk ? "Choose a PowerPoint (.pptx) or a PDF." : "This server shows PDFs. In PowerPoint choose File \u203A Save As \u203A PDF, then upload that.", true);
      return;
    }
    if (file.size > maxMb * 1024 * 1024) { say("That file is too big. The limit is " + maxMb + " MB.", true); return; }

    uploading = true;
    pick.disabled = true;
    progress.hidden = false;
    progress.value = 0;
    say("Uploading\u2026");

    const done = () => { uploading = false; pick.disabled = false; progress.hidden = true; };
    const form = new FormData();
    form.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", base + "/upload");
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      progress.value = Math.round((e.loaded / e.total) * 100);
      say("Uploading\u2026 " + progress.value + "%");
    };
    xhr.onload = () => {
      done();
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300 && data.ok) {
        say("Uploaded. Preparing your slides\u2026 you can present it as soon as it says ready.");
        refreshLibrary();
      } else {
        say(data.error || (xhr.status === 413 ? "That file is too big." : "The upload failed. Please try again."), true);
      }
    };
    xhr.onerror = () => { done(); say("The upload failed. Check your connection and try again.", true); };
    xhr.send(form);
  }

  pick.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    const f = fileInput.files && fileInput.files[0];
    fileInput.value = "";
    upload(f);
  });
  ["dragenter", "dragover"].forEach((t) => pick.addEventListener(t, (e) => { e.preventDefault(); pick.classList.add("is-drag"); }));
  ["dragleave", "drop"].forEach((t) => pick.addEventListener(t, (e) => { e.preventDefault(); pick.classList.remove("is-drag"); }));
  pick.addEventListener("drop", (e) => upload(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]));

  socket.on("slides_error", ({ message }) => {
    openPanel();
    say(message || "That presentation could not be shown.", true);
  });
})();
