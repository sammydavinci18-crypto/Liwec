/*
  YouTube video in a lesson.

  The teacher opens the Video panel, searches YouTube (or pastes a link) and taps
  a result. The video then covers everyone's screen, played by YouTube's own
  embedded player, and follows the teacher exactly:

    THE TEACHER  Has the controls: play / pause, back and forward 10 seconds, a
                 seek bar and a speed menu (0.25x to 2x). Every change is sent to
                 the server (sockets.py: video_ctrl) and, while playing, a
                 heartbeat is sent every few seconds.
    EVERYONE     Cannot touch the video (a shield sits over it). Their player
    ELSE         is nudged to the teacher's position and speed whenever it drifts
                 by more than a second and a half. A browser may refuse to start
                 sound on its own: then a "Tap to play" button appears.

  People who join or reload mid-video are put at the right place by the
  video_state message the server sends on join.
*/
(function () {
  const socket = window._socket;
  const overlay = document.getElementById("vd-overlay");
  if (!socket || !overlay) return;

  const cfg = window.ROOM_CONFIG;
  const isHost = !!cfg.isHost;
  const $ = (id) => document.getElementById(id);

  const titleEl = $("vd-title");
  const tapBtn = $("vd-tap");
  const statusEl = $("vd-status");
  const RATES = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];

  let player = null;
  let ready = false;
  let apiPromise = null;
  let current = null;        // { id } of the video loaded
  let desired = null;        // the latest state from the teacher
  let hostPlaying = false;   // host only: what we last told the class
  let peeked = false;
  let tapTimer = null;
  let beat = null;
  let loadTimer = null;

  /* ---------- YouTube's player API, loaded the first time it is needed ---------- */
  function loadApi() {
    if (window.YT && window.YT.Player) return Promise.resolve();
    if (apiPromise) return apiPromise;
    apiPromise = new Promise((resolve) => {
      const prev = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = () => { if (typeof prev === "function") prev(); resolve(); };
      const tag = document.createElement("script");
      tag.src = "https://www.youtube.com/iframe_api";
      tag.async = true;
      document.head.appendChild(tag);
    });
    return apiPromise;
  }

  function buildPlayer(videoId, start) {
    return new Promise((resolve) => {
      player = new window.YT.Player("vd-player", {
        host: "https://www.youtube-nocookie.com",
        videoId: videoId,
        width: "100%",
        height: "100%",
        playerVars: {
          controls: 0, disablekb: 1, rel: 0, modestbranding: 1, playsinline: 1, fs: 0,
          iv_load_policy: 3, autoplay: 1, start: Math.floor(start || 0), origin: location.origin,
        },
        events: {
          onReady: () => { ready = true; resolve(); },
          onStateChange: onState,
          onError: onError,
        },
      });
    });
  }

  // A line over the video, so a student never just sees a black screen with no explanation.
  function setStatus(msg) {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.hidden = !msg;
  }

  /* ---------- showing and hiding ---------- */
  function show() {
    overlay.classList.add("show");
    document.body.classList.add("video-on");
    if (isHost) $("vd-peek-btn").hidden = !peeked;
    overlay.classList.toggle("peeked", peeked);
  }

  function stopShowing() {
    overlay.classList.remove("show", "peeked");
    document.body.classList.remove("video-on");
    tapBtn.hidden = true;
    setStatus("");
    clearTimeout(loadTimer);
    clearTimeout(tapTimer);
    clearInterval(beat);
    beat = null;
    current = null;
    desired = null;
    hostPlaying = false;
    peeked = false;
    if (isHost) $("vd-peek-btn").hidden = true;
    try { if (player && ready) player.stopVideo(); } catch (e) { /* ignore */ }
  }

  /* ---------- following the teacher ---------- */
  async function apply(s) {
    if (!s || !s.active) { stopShowing(); return; }
    desired = s;
    show();
    if (!current || current.id !== s.id) {
      current = { id: s.id };
      titleEl.textContent = s.title || "";
      if (!isHost) {
        setStatus("Loading the video\u2026");
        clearTimeout(loadTimer);
        loadTimer = setTimeout(() => {
          if (!ready || !desired) setStatus("The video is taking a long time to load. YouTube may be blocked or slow on this connection.");
        }, 12000);
      }
      await loadApi();
      if (!current || current.id !== s.id) return;   // changed again while loading
      if (!player) {
        await buildPlayer(s.id, s.t);
      } else {
        player.loadVideoById({ videoId: s.id, startSeconds: s.t || 0 });
      }
      setStatus("");
      clearTimeout(loadTimer);
      armTap();
      if (isHost) startBeat();
      return;
    }
    follow(s);
  }

  function follow(s) {
    if (!player || !ready) return;
    try {
      if (player.getPlaybackRate() !== s.rate) player.setPlaybackRate(s.rate);
      if (Math.abs(player.getCurrentTime() - s.t) > 1.5) player.seekTo(s.t, true);
      const st = player.getPlayerState();
      if (s.playing && st !== 1 && st !== 3) player.playVideo();
      if (!s.playing && st === 1) player.pauseVideo();
    } catch (e) { /* the player is not ready yet */ }
  }

  // A browser may block sound-on autoplay for someone who has not tapped yet.
  function armTap() {
    clearTimeout(tapTimer);
    tapTimer = setTimeout(() => {
      try {
        const st = player.getPlayerState();
        if (desired && desired.playing && st !== 1 && st !== 3) tapBtn.hidden = false;
      } catch (e) { /* ignore */ }
    }, 2500);
  }
  tapBtn.addEventListener("click", () => {
    try { player.playVideo(); if (desired) follow(desired); } catch (e) { /* ignore */ }
    tapBtn.hidden = true;
  });

  function onState(e) {
    const st = e.data;
    if (st === 1) {
      tapBtn.hidden = true;
      setStatus("");
      try { const d = player.getVideoData(); if (d && d.title) titleEl.textContent = d.title; } catch (x) { /* ignore */ }
      // A video the teacher had already paused starts playing when loaded: pause it again.
      if (desired && !desired.playing && !isHost) follow(desired);
      if (isHost) syncSeekBar(true);
    }
    if (st === 0 && isHost) { hostPlaying = false; setPlayIcon(); sendState(); }
  }

  function onError(e) {
    if (!isHost) {
      setStatus("This video cannot be played on your device. Ask your teacher to choose another one.");
      return;
    }
    const blocked = e && (e.data === 101 || e.data === 150);
    setNote(blocked
      ? "The owner of that video does not allow it to be played here. Choose another one."
      : "That video could not be played. Choose another one.", true);
    socket.emit("video_stop", {});
    openPanel();
  }

  socket.on("video_state", apply);
  socket.on("video_error", (d) => { if (isHost) setNote((d && d.message) || "That video could not be started.", true); });

  // Safety net for students: if the "video started / stopped" message never reached
  // this device, ask the server what is playing, when the connection comes back, when
  // the tab is looked at again, and every few seconds.
  if (!isHost) {
    const ask = () => { if (socket.connected !== false) socket.emit("video_sync", {}); };
    socket.on("connect", ask);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) ask(); });
    setInterval(ask, 6000);
    return;
  }

  /* ====================== the teacher's side ====================== */
  const btn = $("btn-video");
  const panel = $("video-panel");
  const q = $("vd-q");
  const resultsEl = $("vd-results");
  const noteEl = $("vd-note");
  const linkIn = $("vd-link");
  const playBtn = $("vd-play");
  const seek = $("vd-seek");
  const timeEl = $("vd-time");
  const rateSel = $("vd-rate");
  const searchOn = panel.dataset.search === "1";
  const searchUrl = panel.dataset.url;

  function setNote(msg, isError) {
    noteEl.textContent = msg || "";
    noteEl.classList.toggle("is-error", !!isError);
  }

  function openPanel() {
    document.querySelectorAll(".notes-panel.open").forEach((p) => { if (p !== panel) p.classList.remove("open"); });
    panel.classList.add("open");
    btn.classList.add("active-on");
    setTimeout(() => q.focus(), 50);
  }
  function closePanel() { panel.classList.remove("open"); btn.classList.remove("active-on"); }
  btn.addEventListener("click", () => (panel.classList.contains("open") ? closePanel() : openPanel()));
  $("vd-panel-close").addEventListener("click", closePanel);

  /* ---------- search / paste a link ---------- */
  function idFromLink(text) {
    const t = (text || "").trim();
    if (/^[A-Za-z0-9_-]{11}$/.test(t)) return t;
    const m = t.match(/(?:youtu\.be\/|youtube(?:-nocookie)?\.com\/(?:watch\?(?:.*&)?v=|embed\/|shorts\/|live\/|v\/))([A-Za-z0-9_-]{11})/);
    return m ? m[1] : null;
  }

  function startVideo(id, title) {
    socket.emit("video_play", { id: id, title: title || "" });
    closePanel();
  }

  $("vd-link-go").addEventListener("click", () => {
    const id = idFromLink(linkIn.value);
    if (!id) { setNote("That does not look like a YouTube link. Copy it from the YouTube address bar or its Share button.", true); return; }
    setNote("");
    startVideo(id, "");
    linkIn.value = "";
  });
  linkIn.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("vd-link-go").click(); } });

  async function doSearch() {
    const text = q.value.trim();
    if (text.length < 2) return;
    if (!searchOn) {
      window.open("https://www.youtube.com/results?search_query=" + encodeURIComponent(text), "_blank", "noopener");
      setNote("YouTube opened in a new tab. Find the video, copy its link, and paste it below.");
      return;
    }
    setNote("Searching\u2026");
    resultsEl.textContent = "";
    try {
      const res = await fetch(searchUrl + "?q=" + encodeURIComponent(text), { credentials: "same-origin" });
      const data = await res.json();
      if (data.error) { setNote(data.error, true); return; }
      if (!data.results.length) { setNote("Nothing found. Try different words."); return; }
      setNote("");
      data.results.forEach((r) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "vd-result";
        const img = document.createElement("img");
        img.alt = ""; img.loading = "lazy";
        if (/^https:\/\/i\d?\.ytimg\.com\//.test(r.thumb)) img.src = r.thumb;
        const span = document.createElement("span");
        const strong = document.createElement("strong");
        strong.textContent = r.title;
        const small = document.createElement("small");
        small.textContent = r.channel;
        span.append(strong, small);
        b.append(img, span);
        b.addEventListener("click", () => startVideo(r.id, r.title));
        resultsEl.appendChild(b);
      });
    } catch (e) {
      setNote("Could not search. Check your connection, or paste a link.", true);
    }
  }
  $("vd-search-go").addEventListener("click", doSearch);
  q.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doSearch(); } });

  /* ---------- the teacher's player controls ---------- */
  function now() { try { return player.getCurrentTime() || 0; } catch (e) { return 0; } }
  function rateNow() { try { return player.getPlaybackRate() || 1; } catch (e) { return 1; } }

  function sendState() {
    if (!player || !ready) return;
    socket.emit("video_ctrl", { t: now(), playing: hostPlaying, rate: rateNow() });
  }
  function startBeat() {
    clearInterval(beat);
    beat = setInterval(() => { if (hostPlaying) sendState(); syncSeekBar(); }, 3000);
  }

  function setPlayIcon() { playBtn.textContent = hostPlaying ? "Pause" : "Play"; }

  playBtn.addEventListener("click", () => {
    if (!player || !ready) return;
    hostPlaying = !hostPlaying;
    try { hostPlaying ? player.playVideo() : player.pauseVideo(); } catch (e) { /* ignore */ }
    setPlayIcon();
    sendState();
  });

  function jump(delta) {
    if (!player || !ready) return;
    let dur = 0; try { dur = player.getDuration() || 0; } catch (e) { /* ignore */ }
    const to = Math.max(0, Math.min(dur || 1e9, now() + delta));
    try { player.seekTo(to, true); } catch (e) { /* ignore */ }
    sendState();
    syncSeekBar();
  }
  $("vd-back").addEventListener("click", () => jump(-10));
  $("vd-fwd").addEventListener("click", () => jump(10));

  RATES.forEach((r) => {
    const o = document.createElement("option");
    o.value = String(r);
    o.textContent = r === 1 ? "Normal speed" : r + "x";
    if (r === 1) o.selected = true;
    rateSel.appendChild(o);
  });
  rateSel.addEventListener("change", () => {
    if (!player || !ready) return;
    try { player.setPlaybackRate(parseFloat(rateSel.value)); } catch (e) { /* ignore */ }
    sendState();
  });

  let scrubbing = false;
  seek.addEventListener("input", () => { scrubbing = true; timeEl.textContent = fmt(+seek.value) + " / " + fmt(+seek.max); });
  seek.addEventListener("change", () => {
    scrubbing = false;
    try { player.seekTo(+seek.value, true); } catch (e) { /* ignore */ }
    sendState();
  });

  function fmt(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const m = Math.floor(sec / 60), s = sec % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }
  function syncSeekBar(resetControls) {
    if (!player || !ready || scrubbing) return;
    try {
      const dur = player.getDuration() || 0;
      seek.max = String(Math.max(1, Math.floor(dur)));
      seek.value = String(Math.floor(now()));
      timeEl.textContent = fmt(now()) + " / " + fmt(dur);
      if (resetControls) {
        hostPlaying = desired ? !!desired.playing : true;
        setPlayIcon();
        rateSel.value = String(rateNow());
      }
    } catch (e) { /* ignore */ }
  }
  setInterval(() => { if (overlay.classList.contains("show")) syncSeekBar(); }, 500);

  $("vd-stop").addEventListener("click", () => socket.emit("video_stop", {}));
  $("vd-change").addEventListener("click", openPanel);

  // Hide the video on the teacher's own screen to look at the class; everyone else keeps watching.
  $("vd-hide").addEventListener("click", () => { peeked = true; show(); });
  $("vd-peek-btn").addEventListener("click", () => { peeked = false; show(); });
})();
