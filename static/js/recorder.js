/*
  Recording from the host's browser — the FALLBACK recorder.

  When a media server and recording storage are configured, the SERVER records
  the lesson (LiveKit Egress, rtc.py / recording_service.py) and this file does
  nothing: the teacher's laptop isn't involved at all. This recorder runs only
  when the server tells the host's browser to (the "recording_mode" event in
  main.js): no media server, or the server recorder failed to start or died.

  It draws the teacher's lesson window (whiteboard, slides or shared screen; never the teacher's face or the students) onto a
  canvas, mixes everyone's audio into one track, and records that as a single video.

  Hardening, because this used to depend on one fragile browser tab:
    * Chunks are small (10 s, not 30 s) and numbered. The server accepts each
      number once, so a retry can never duplicate or reorder data.
    * Every chunk is kept in the browser (IndexedDB) until the server confirms
      it, and is retried with back-off. A dropped connection loses nothing; a
      reloaded or crashed tab resumes by sending whatever the server is missing.
    * Frames are drawn from a Web Worker timer, so switching to another tab or
      window (e.g. to present) doesn't make the browser throttle the recording
      to one frame a second.
    * If drawing is too slow for this computer, the frame rate steps down
      instead of the whole recording stuttering.
    * If the recorder itself stops unexpectedly, a new one is started.
*/

// The recording is a film of the TEACHER'S OWN WINDOW, full frame, so it shows everything that happens
// in the lesson as the teacher sees it: whiteboard, slides, shared screens, the class. The teacher is
// asked once, when the lesson starts, to choose the window to film (screen capture, getDisplayMedia).
// The capture is drawn onto a canvas that is recorded, so the video stays one continuous file even if
// the teacher stops and re-chooses the window. If capture is unavailable or refused, the app's own
// lesson content (whiteboard / slides / shared screen) is recorded instead, never the teacher's camera.
const CANVAS_W = 1600;
const CANVAS_H = 900;
const CHUNK_MS = 10000;
const ROOM = window.ROOM_CONFIG.roomCode;

let recorder = null;
let recordingActive = false;
let stopping = false;
let tickWorker = null;
let audioCtx = null;
let audioDest = null;
let fps = 10;
let drawMs = 0; // smoothed time one frame takes to draw
let restarts = 0;
const connectedAudioSids = new Set();
let localAudioConnected = false;

const canvas = document.createElement("canvas");
canvas.width = CANVAS_W;
canvas.height = CANVAS_H;
const ctx = canvas.getContext("2d");

/* ---------------------------------------------------------------- drawing */

function drawContain(video, x, y, w, h) {
  if (!video || !video.videoWidth) return false;
  const scale = Math.min(w / video.videoWidth, h / video.videoHeight);
  const dw = video.videoWidth * scale, dh = video.videoHeight * scale;
  ctx.drawImage(video, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh);
  return true;
}

// The teacher's captured window (null until they choose one, or after they stop sharing it).
let displayVideo = null;
let displayStream = null;
const captureLive = () => !!(displayVideo && displayStream && displayStream.active && displayVideo.videoWidth);

// What the lesson window is showing right now: "board" | "slides" | "screen" | "idle".
function stageMode() {
  const wb = window.Whiteboard;
  if (wb && wb.isOpen()) return "board";
  const sl = window.Slides;
  if (sl && sl.currentImage && sl.currentImage()) return "slides";
  const tile = document.querySelector("#spotlight-tile");
  const video = tile && tile.classList.contains("is-screen") ? tile.querySelector("video") : null;
  if (video && video.videoWidth) return "screen";
  return "idle";
}

function drawFrame() {
  ctx.fillStyle = "#05090c";
  ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);
  if (captureLive()) { drawContain(displayVideo, 0, 0, CANVAS_W, CANVAS_H); return; }
  const mode = stageMode();
  if (mode === "board") drawWhiteboard();
  else if (mode === "slides") drawSlides();
  else if (mode === "screen") drawContain(document.querySelector("#spotlight-tile video"), 0, 0, CANVAS_W, CANVAS_H);
  else drawIdle();
}

// Nothing is being shown (the teacher is only on camera, or has not started yet): a plain card with
// the lesson title. The teacher's face is deliberately never recorded.
function drawIdle() {
  const title = (window.ROOM_CONFIG && window.ROOM_CONFIG.title) || document.title.split(" \u00b7 ")[0] || "";
  ctx.fillStyle = "#0E1A22";
  ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillStyle = "#fff";
  ctx.font = "700 40px system-ui, Arial, sans-serif";
  let t = title;
  while (t.length > 1 && ctx.measureText(t).width > CANVAS_W - 120) t = t.slice(0, -2);
  if (t !== title) t += "\u2026";
  ctx.fillText(t, CANVAS_W / 2, CANVAS_H / 2 - 18);
  ctx.fillStyle = "#8FA3AD";
  ctx.font = "22px system-ui, Arial, sans-serif";
  ctx.fillText("The lesson is in progress", CANVAS_W / 2, CANVAS_H / 2 + 28);
  ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
}

// The whiteboard fills the frame while it is open.
function drawWhiteboard() {
  const wb = window.Whiteboard;
  const scale = Math.min(CANVAS_W / wb.canvas.width, CANVAS_H / wb.canvas.height);
  const w = wb.canvas.width * scale, h = wb.canvas.height * scale;
  ctx.fillStyle = "#0E1A22";
  ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);
  ctx.drawImage(wb.canvas, (CANVAS_W - w) / 2, (CANVAS_H - h) / 2, w, h);
}

// Likewise a presentation. The picture is same-origin (served by our own server), so painting it
// here can't taint the canvas and break the recording.
function drawSlides() {
  const im = window.Slides.currentImage();
  if (!im.naturalWidth) return;
  const scale = Math.min(CANVAS_W / im.naturalWidth, CANVAS_H / im.naturalHeight);
  const w = im.naturalWidth * scale, h = im.naturalHeight * scale;
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);
  ctx.drawImage(im, (CANVAS_W - w) / 2, (CANVAS_H - h) / 2, w, h);
  // The teacher's drawing and laser pointer sit on the slide the same way.
  const layers = window.SlideInk && window.SlideInk.layers ? window.SlideInk.layers() : [];
  layers.forEach((layer) => ctx.drawImage(layer, (CANVAS_W - w) / 2, (CANVAS_H - h) / 2, w, h));
}

/* ------------------------------------------------------------------ timer */

// setInterval in a background tab is throttled to ~1 call a second. A Worker's
// timer is not, so the recording keeps its frame rate while the teacher works
// in another tab or window.
function startTicker(onTick) {
  try {
    const src = "let t=null;onmessage=(e)=>{clearInterval(t);if(e.data>0)t=setInterval(()=>postMessage(0),e.data);};";
    tickWorker = new Worker(URL.createObjectURL(new Blob([src], { type: "text/javascript" })));
    tickWorker.onmessage = onTick;
    tickWorker.postMessage(Math.round(1000 / fps));
    return;
  } catch (err) {
    tickWorker = null;
  }
  const id = setInterval(onTick, Math.round(1000 / fps));
  tickWorker = { postMessage() {}, terminate() { clearInterval(id); } }; // fallback handle
}

function setFps(next) {
  if (next === fps) return;
  fps = next;
  if (tickWorker && tickWorker.postMessage) tickWorker.postMessage(Math.round(1000 / fps));
}

function onTick() {
  const t0 = performance.now();
  drawFrame();
  connectAudioSources();
  const took = performance.now() - t0;
  drawMs = drawMs ? drawMs * 0.9 + took * 0.1 : took;
  // This computer can't keep up: draw less often rather than falling behind.
  if (drawMs > 0.8 * (1000 / fps) && fps > 4) setFps(Math.max(4, Math.floor(fps / 2)));
}

/* ------------------------------------------------------------------ audio */

function pickMimeType() {
  const candidates = [
    "video/webm;codecs=vp8,opus",
    "video/webm;codecs=vp9,opus",
    "video/webm",
  ];
  return candidates.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

function connectAudioSources() {
  if (!audioCtx) return;

  if (!localAudioConnected && window._localStreamRef && window._localStreamRef.getAudioTracks().length) {
    audioCtx.createMediaStreamSource(window._localStreamRef).connect(audioDest);
    localAudioConnected = true;
  }

  Object.entries(window._remoteStreams || {}).forEach(([sid, info]) => {
    if (connectedAudioSids.has(sid)) return;
    if (!info.stream || info.stream.getAudioTracks().length === 0) return;
    try {
      audioCtx.createMediaStreamSource(info.stream).connect(audioDest);
      connectedAudioSids.add(sid);
    } catch (err) {
      // Stream might not be ready yet; we'll retry on the next tick.
    }
  });
}

/* ------------------------------------------- numbered, retried, saved chunks */

// Chunks are written to IndexedDB first so a reload/crash can resume; if the
// browser won't allow it (private mode), they are only held in memory.
const IDB = {
  db: null,
  async open() {
    if (this.db) return this.db;
    this.db = await new Promise((resolve) => {
      try {
        const req = indexedDB.open("liwec-recording", 1);
        req.onupgradeneeded = () => req.result.createObjectStore("chunks", { keyPath: "k" });
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => resolve(null);
      } catch (e) { resolve(null); }
    });
    return this.db;
  },
  async tx(mode, fn) {
    const db = await this.open();
    if (!db) return null;
    return new Promise((resolve) => {
      try {
        const t = db.transaction("chunks", mode);
        const out = fn(t.objectStore("chunks"));
        t.oncomplete = () => resolve(out && out.result !== undefined ? out.result : true);
        t.onerror = t.onabort = () => resolve(null);
      } catch (e) { resolve(null); }
    });
  },
  put(seq, blob) { return this.tx("readwrite", (s) => s.put({ k: `${ROOM}:${seq}`, room: ROOM, seq, blob })); },
  del(seq) { return this.tx("readwrite", (s) => s.delete(`${ROOM}:${seq}`)); },
  async all() {
    const rows = await this.tx("readonly", (s) => s.getAll());
    return Array.isArray(rows) ? rows.filter((r) => r.room === ROOM).sort((a, b) => a.seq - b.seq) : [];
  },
};

const queue = []; // { seq, blob } in order, not yet confirmed by the server
let nextSeq = 0;
let pumping = false;
let giveUpAt = 0; // set when the session is ending: stop retrying after this time
let lastOkAt = 0;  // when the server last confirmed a chunk (0 = never yet)
let startedAt = 0;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function sendChunk(item) {
  const res = await fetch(`/room/${encodeURIComponent(ROOM)}/recording/chunk?seq=${item.seq}`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: item.blob,
  });
  if (res.status === 409) {
    // The server is missing earlier chunks (e.g. it restarted). Resend from what it needs.
    const j = await res.json().catch(() => ({}));
    return { gap: typeof j.expected === "number" ? j.expected : null };
  }
  if (res.status === 410) return { dropped: true }; // the session already ended
  if (res.status === 403 || res.status === 404) {
    // This account is no longer the host of the session: the chunks are being refused.
    window.dispatchEvent(new CustomEvent("recording-warning", { detail: { reason: "refused" } }));
    return { dropped: true };
  }
  if (!res.ok) throw new Error("HTTP " + res.status);
  return { ok: true };
}

async function pump() {
  if (pumping) return;
  pumping = true;
  try {
    let attempt = 0;
    while (queue.length) {
      if (giveUpAt && Date.now() > giveUpAt) break;
      const item = queue[0];
      try {
        const r = await sendChunk(item);
        if (r.gap !== null && r.gap !== undefined && r.gap !== item.seq) {
          // Put back anything from the server's number onward that we still hold.
          const held = (await IDB.all()).filter((x) => x.seq >= r.gap && x.seq < item.seq);
          if (held.length && held[0].seq === r.gap) {
            queue.unshift(...held.filter((h) => !queue.some((q) => q.seq === h.seq)));
          } else {
            item.seq = r.gap; // those chunks are gone for good: carry on from where the server is
          }
          continue;
        }
        queue.shift();
        IDB.del(item.seq);
        attempt = 0;
        lastOkAt = Date.now();
      } catch (err) {
        attempt += 1;
        await sleep(Math.min(20000, 1000 * 2 ** Math.min(attempt, 5)));
      }
    }
  } finally {
    pumping = false;
  }
}

function enqueue(blob) {
  const item = { seq: nextSeq++, blob };
  queue.push(item);
  IDB.put(item.seq, blob);
  pump();
}

// After a reload: find what the server already has and send what it doesn't.
async function resume() {
  let received = 0;
  try {
    const r = await fetch(`/room/${encodeURIComponent(ROOM)}/recording/status`);
    if (r.ok) received = (await r.json()).received || 0;
  } catch (e) { /* offline: the local copies are still there */ }
  const saved = await IDB.all();
  const pending = saved.filter((x) => x.seq >= received);
  saved.filter((x) => x.seq < received).forEach((x) => IDB.del(x.seq));
  pending.forEach((x) => queue.push({ seq: x.seq, blob: x.blob }));
  nextSeq = pending.length ? pending[pending.length - 1].seq + 1 : received;
  if (queue.length) pump();
}

/* --------------------------------------------------------------- recording */

function startRecorder(combined) {
  const mimeType = pickMimeType();
  recorder = new MediaRecorder(combined, Object.assign(
    { videoBitsPerSecond: 2000000, audioBitsPerSecond: 64000 },
    mimeType ? { mimeType } : {}
  ));
  recorder.onerror = () => recoverRecorder(combined);
  recorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) enqueue(e.data);
  };
  recorder.start(CHUNK_MS);
}

// The browser stopped the recorder without being asked: start another rather
// than silently losing the rest of the lesson.
function recoverRecorder(combined) {
  if (stopping) return;
  restarts += 1;
  if (restarts > 5) {
    window.dispatchEvent(new CustomEvent("recording-failed"));
    return;
  }
  try { startRecorder(combined); } catch (err) { window.dispatchEvent(new CustomEvent("recording-failed")); }
}

async function startRecording(display) {
  if (recordingActive) return;
  await resume();
  if (display) attachDisplay(display);

  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  // Browsers start an AudioContext "suspended" until the person has interacted
  // with the page; a suspended one records silence. Resume it now and again on
  // the first click/tap/key if it is still suspended.
  const wake = () => { if (audioCtx && audioCtx.state === "suspended") audioCtx.resume().catch(() => {}); };
  wake();
  ["click", "keydown", "touchstart"].forEach((ev) => document.addEventListener(ev, wake, { passive: true }));
  audioDest = audioCtx.createMediaStreamDestination();
  // A silent source keeps the audio track producing samples. Without it, a lesson with no microphone
  // connected yet (muted, denied, or nobody speaking) gives the recorder no data and the video stalls.
  try {
    const keep = audioCtx.createConstantSource();
    const quiet = audioCtx.createGain();
    quiet.gain.value = 0;
    keep.connect(quiet).connect(audioDest);
    keep.start();
  } catch (e) { /* older browsers: the real audio sources still work */ }

  const canvasStream = canvas.captureStream(fps);
  const combined = new MediaStream([
    ...canvasStream.getVideoTracks(),
    ...audioDest.stream.getAudioTracks(),
  ]);

  startRecorder(combined);
  startTicker(onTick);

  // Safety net: if the recorder quietly died, bring it back.
  setInterval(() => {
    if (!stopping && recorder && recorder.state === "inactive") recoverRecorder(combined);
  }, 5000);

  // Leaving the page: hand over whatever has been recorded so far.
  window.addEventListener("pagehide", () => {
    try { if (recorder && recorder.state === "recording") recorder.requestData(); } catch (e) { /* ignore */ }
  });

  recordingActive = true;
  startedAt = Date.now();
}

// window.stopRecordingAndWait(): used by main.js when the host clicks "End meeting".
// Resolves once the final chunk has been uploaded (or after a grace period if the
// connection is down), so it's safe to call the /end endpoint right after.
// Resolves immediately if nothing was recording.
window.stopRecordingAndWait = function () {
  return new Promise((resolve) => {
    if (!recordingActive || !recorder) {
      resolve();
      return;
    }
    stopping = true;
    const finish = async () => {
      if (tickWorker) { try { tickWorker.terminate(); } catch (e) { /* ignore */ } }
      recordingActive = false;
      giveUpAt = Date.now() + 45000;
      await pump();
      // Wait for any in-flight upload that began before we got here.
      while (pumping && Date.now() < giveUpAt) await sleep(250);
      resolve();
    };
    if (recorder.state === "inactive") { finish(); return; }
    recorder.onstop = finish;
    recorder.stop();
  });
};

// Recording is automatic: main.js calls start() when the server says the
// host's browser is the recorder, and nothing in the UI can stop it — only
// ending the session (stopRecordingAndWait, called from "End lesson"). If this
// browser can't record, "recording-failed" blocks the room rather than letting
// a session run unrecorded.
let starting = false;

// Film this window: remember the stream, draw it each frame, and ask again if the teacher stops sharing.
function attachDisplay(stream) {
  displayStream = stream;
  if (!displayVideo) {
    displayVideo = document.createElement("video");
    displayVideo.muted = true;
    displayVideo.playsInline = true;
    displayVideo.style.cssText = "position:fixed;left:0;top:0;width:2px;height:2px;opacity:0;pointer-events:none";
    document.body.appendChild(displayVideo);
  }
  displayVideo.srcObject = stream;
  displayVideo.play().catch(() => {});
  const track = stream.getVideoTracks()[0];
  if (track) track.addEventListener("ended", () => {
    if (displayStream === stream) { displayStream = null; if (!stopping) showOverlay(true); }
  });
}

function overlayEls() {
  return {
    box: document.getElementById("record-window-overlay"),
    start: document.getElementById("record-window-start"),
    skip: document.getElementById("record-window-skip"),
    msg: document.getElementById("record-window-msg"),
  };
}

function showOverlay(lost) {
  const o = overlayEls();
  if (!o.box) return;
  if (o.msg) o.msg.textContent = lost
    ? "Sharing of your window stopped, so the recording is only keeping the lesson board and slides. Choose your window again to keep filming it."
    : "";
  o.box.classList.add("show");
}

async function chooseWindow() {
  const o = overlayEls();
  try {
    const stream = await navigator.mediaDevices.getDisplayMedia({
      video: { frameRate: { ideal: 15, max: 24 }, displaySurface: "browser" },
      audio: false,
      preferCurrentTab: true,
      selfBrowserSurface: "include",
      surfaceSwitching: "include",
    });
    if (o.box) o.box.classList.remove("show");
    if (recordingActive) attachDisplay(stream);
    else await beginRecording(stream);
  } catch (err) {
    if (o.msg) o.msg.textContent = "The window was not shared. Please press the button again and choose this tab (or your window) to start the lesson recording.";
  }
}

async function beginRecording(display) {
  try {
    await startRecording(display);
  } catch (err) {
    console.error("Recording failed to start:", err);
    window.dispatchEvent(new CustomEvent("recording-failed"));
  } finally {
    starting = false;
  }
}

// Camera/mic stream is set by webrtc.js; wait for it if it isn't ready yet.
function whenMediaReady(fn) {
  if (window._localStreamRef) { fn(); return; }
  const wait = () => (window._localStreamRef ? fn() : setTimeout(wait, 300));
  wait();
}

window.RecordingControl = {
  start() {
    if (recordingActive || starting) return;
    if (typeof MediaRecorder === "undefined" || !canvas.captureStream) {
      window.dispatchEvent(new CustomEvent("recording-failed"));
      return;
    }
    starting = true;
    const o = overlayEls();
    const canCapture = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia) && o.box && o.start;
    if (!canCapture) { whenMediaReady(() => beginRecording(null)); return; }  // e.g. a phone: record the lesson content
    // Screen capture needs a click, so the teacher starts the lesson recording with one button.
    o.start.onclick = () => whenMediaReady(chooseWindow);
    if (o.skip) o.skip.onclick = () => { o.box.classList.remove("show"); whenMediaReady(() => beginRecording(null)); };
    showOverlay(false);
  },
  isActive() {
    return recordingActive;
  },
  // For the "Recording" badge: is saving keeping up with recording?
  status() {
    const sinceOk = Date.now() - (lastOkAt || startedAt || Date.now());
    return {
      active: recordingActive,
      backlog: queue.length,
      behind: recordingActive && (queue.length >= 6 || sinceOk > 60000),
    };
  },
};
