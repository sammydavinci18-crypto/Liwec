/*
  Recording from the host's browser — the FALLBACK recorder.

  When a media server and recording storage are configured, the SERVER records
  the lesson (LiveKit Egress, rtc.py / recording_service.py) and this file does
  nothing: the teacher's laptop isn't involved at all. This recorder runs only
  when the server tells the host's browser to (the "recording_mode" event in
  main.js): no media server, or the server recorder failed to start or died.

  It composites the lesson screen (stage + students present + who is here) onto a canvas, mixes everyone's audio into one track, and
  records that as a single video, so playback looks exactly like the lesson.

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

// The recording is a picture of the whole lesson, like the teacher's own screen: a big STAGE
// (whiteboard, slides, or the teacher's camera / shared screen), a column down the right with
// the students present, and a bar along the bottom with the lesson and who is here.
const CANVAS_W = 1280;
const CANVAS_H = 720;
const SIDE_W = 320;                      // student column
const BAR_H = 52;                        // bottom bar
const STAGE_W = CANVAS_W - SIDE_W;
const STAGE_H = CANVAS_H - BAR_H;
const MAX_TILES = 12;                    // students drawn as pictures; the bar names everyone
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

function drawCover(video, x, y, w, h) {
  if (!video || !video.videoWidth) return;
  const scale = Math.max(w / video.videoWidth, h / video.videoHeight);
  const sw = w / scale;
  const sh = h / scale;
  const sx = (video.videoWidth - sw) / 2;
  const sy = (video.videoHeight - sh) / 2;
  ctx.drawImage(video, sx, sy, sw, sh, x, y, w, h);
}

function drawContain(video, x, y, w, h) {
  if (!video || !video.videoWidth) return false;
  const scale = Math.min(w / video.videoWidth, h / video.videoHeight);
  const dw = video.videoWidth * scale, dh = video.videoHeight * scale;
  ctx.drawImage(video, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh);
  return true;
}

// The name on a tile, without the little icons/badges (hand, audio only, ...) the live page adds.
function tileName(container, labelSel) {
  const label = container && container.querySelector(labelSel);
  if (!label) return "";
  const c = label.cloneNode(true);
  c.querySelectorAll(".hand-badge, .data-badge, .host-badge, .ico").forEach((n) => n.remove());
  return (c.textContent || "").replace(/\s+/g, " ").trim();
}

function drawLabel(text, x, y, maxW, size) {
  if (!text) return;
  ctx.font = `${size}px system-ui, Arial, sans-serif`;
  let t = text;
  while (t.length > 1 && ctx.measureText(t).width > maxW - 14) t = t.slice(0, -2);
  if (t !== text) t += "…";
  const w = ctx.measureText(t).width + 14;
  ctx.fillStyle = "rgba(0,0,0,.55)";
  ctx.fillRect(x, y - size - 4, w, size + 8);
  ctx.fillStyle = "#fff";
  ctx.textBaseline = "alphabetic";
  ctx.fillText(t, x + 7, y);
}

// A student tile: their picture, or their initial when the camera is off (so they still show as present).
function drawStudent(video, name, x, y, w, h) {
  ctx.fillStyle = "#0A1318";
  ctx.fillRect(x, y, w, h);
  const hasPicture = video && video.videoWidth && !video.closest(".no-video");
  if (hasPicture) {
    drawCover(video, x, y, w, h);
  } else {
    const r = Math.min(w, h) * 0.22;
    ctx.fillStyle = "#16262f";
    ctx.fillRect(x, y, w, h);
    ctx.fillStyle = "#2A3F6B";
    ctx.beginPath(); ctx.arc(x + w / 2, y + h / 2 - 6, r, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#fff";
    ctx.font = `700 ${Math.round(r * 1.1)}px system-ui, Arial, sans-serif`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(((name || "?").trim()[0] || "?").toUpperCase(), x + w / 2, y + h / 2 - 5);
    ctx.textAlign = "left";
  }
  ctx.strokeStyle = "#2B3E45";
  ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
  drawLabel(name, x + 6, y + h - 8, w - 12, 13);
}

function drawFrame() {
  ctx.fillStyle = "#0E1A22";
  ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);

  // Stage: the teacher's camera or shared screen (the picture already on the big tile).
  ctx.fillStyle = "#05090c";
  ctx.fillRect(0, 0, STAGE_W, STAGE_H);
  const spotlightTile = document.querySelector("#spotlight-tile");
  const spotlightVideo = spotlightTile && spotlightTile.querySelector("video");
  if (!drawContain(spotlightVideo, 0, 0, STAGE_W, STAGE_H)) {
    ctx.fillStyle = "#8FA3AD";
    ctx.font = "22px system-ui, Arial, sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("Waiting for the teacher…", STAGE_W / 2, STAGE_H / 2);
    ctx.textAlign = "left";
  }
  const stageName = tileName(document, "#spotlight-label");
  if (stageName) drawLabel(stageName + (spotlightTile && spotlightTile.classList.contains("is-screen") ? " is sharing their screen" : ""), 12, STAGE_H - 14, STAGE_W - 24, 16);
  drawWhiteboard();
  drawSlides();

  // Student column. Selecting by class (not "#filmstrip .mini-tile") so this keeps working
  // when gallery view has moved the tiles into #gallery-grid.
  ctx.fillStyle = "#0E1A22";
  ctx.fillRect(STAGE_W, 0, SIDE_W, STAGE_H);
  ctx.strokeStyle = "#2B3E45";
  ctx.beginPath(); ctx.moveTo(STAGE_W + 0.5, 0); ctx.lineTo(STAGE_W + 0.5, STAGE_H); ctx.stroke();
  const all = Array.from(document.querySelectorAll(".mini-tile")).map((t) => ({
    video: t.querySelector("video"), name: tileName(t, ".tile-label"),
  }));
  all.sort((a, b) => a.name.localeCompare(b.name));
  const shown = all.slice(0, MAX_TILES);
  const pad = 6, cols = shown.length <= 1 ? 1 : 2;
  const rows = Math.max(1, Math.ceil(shown.length / cols));
  const cellW = (SIDE_W - pad * (cols + 1)) / cols;
  const cellH = Math.min(cellW * 9 / 16 * 1.0, (STAGE_H - pad * (rows + 1)) / rows);
  shown.forEach((st, i) => {
    drawStudent(st.video, st.name, STAGE_W + pad + (i % cols) * (cellW + pad), pad + Math.floor(i / cols) * (cellH + pad), cellW, cellH);
  });

  // Bottom bar: the lesson, how many are here, and their names.
  ctx.fillStyle = "#0A1318";
  ctx.fillRect(0, STAGE_H, CANVAS_W, BAR_H);
  ctx.strokeStyle = "#2B3E45";
  ctx.beginPath(); ctx.moveTo(0, STAGE_H + 0.5); ctx.lineTo(CANVAS_W, STAGE_H + 0.5); ctx.stroke();
  const title = (window.ROOM_CONFIG && window.ROOM_CONFIG.title) || document.title.split(" · ")[0] || "";
  ctx.textBaseline = "middle";
  ctx.font = "700 15px system-ui, Arial, sans-serif";
  ctx.fillStyle = "#fff";
  let x = 16;
  const tw = Math.min(ctx.measureText(title).width, 380);
  if (title) { ctx.save(); ctx.beginPath(); ctx.rect(x, STAGE_H, 380, BAR_H); ctx.clip(); ctx.fillText(title, x, STAGE_H + BAR_H / 2); ctx.restore(); }
  x += tw + 16;
  const count = all.length === 1 ? "1 student present" : all.length + " students present";
  ctx.font = "600 15px system-ui, Arial, sans-serif";
  ctx.fillStyle = "#E8C65A";
  ctx.fillText(count, x, STAGE_H + BAR_H / 2);
  x += ctx.measureText(count).width + 16;
  ctx.font = "13px system-ui, Arial, sans-serif";
  ctx.fillStyle = "#C9D6DC";
  let roster = all.map((st) => st.name).filter(Boolean).join("  ·  ");
  const maxW = CANVAS_W - x - 16;
  while (roster.length > 3 && ctx.measureText(roster).width > maxW) roster = roster.slice(0, -4) + "…";
  ctx.fillText(roster, x, STAGE_H + BAR_H / 2);
  ctx.textBaseline = "alphabetic";
}

// While the whiteboard is open it replaces the stage (everyone's screen shows it); the students stay visible.
function drawWhiteboard() {
  const wb = window.Whiteboard;
  if (!wb || !wb.isOpen()) return;
  const scale = Math.min(STAGE_W / wb.canvas.width, STAGE_H / wb.canvas.height);
  const w = wb.canvas.width * scale, h = wb.canvas.height * scale;
  ctx.fillStyle = "#0E1A22";
  ctx.fillRect(0, 0, STAGE_W, STAGE_H);
  ctx.drawImage(wb.canvas, (STAGE_W - w) / 2, (STAGE_H - h) / 2, w, h);
}

// Likewise a presentation takes over the stage. The picture is same-origin (served by our own
// server), so painting it here can't taint the canvas and break the recording.
function drawSlides() {
  const sl = window.Slides;
  const im = sl && sl.currentImage && sl.currentImage();
  if (!im) return;
  const scale = Math.min(STAGE_W / im.naturalWidth, STAGE_H / im.naturalHeight);
  const w = im.naturalWidth * scale, h = im.naturalHeight * scale;
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, STAGE_W, STAGE_H);
  ctx.drawImage(im, (STAGE_W - w) / 2, (STAGE_H - h) / 2, w, h);
  // The teacher's drawing and laser pointer sit on the slide the same way.
  const layers = window.SlideInk && window.SlideInk.layers ? window.SlideInk.layers() : [];
  layers.forEach((layer) => ctx.drawImage(layer, (STAGE_W - w) / 2, (STAGE_H - h) / 2, w, h));
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
    { videoBitsPerSecond: 1200000, audioBitsPerSecond: 64000 },
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

async function startRecording() {
  if (recordingActive) return;
  await resume();

  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  // Browsers start an AudioContext "suspended" until the person has interacted
  // with the page; a suspended one records silence. Resume it now and again on
  // the first click/tap/key if it is still suspended.
  const wake = () => { if (audioCtx && audioCtx.state === "suspended") audioCtx.resume().catch(() => {}); };
  wake();
  ["click", "keydown", "touchstart"].forEach((ev) => document.addEventListener(ev, wake, { passive: true }));
  audioDest = audioCtx.createMediaStreamDestination();

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
window.RecordingControl = {
  start() {
    if (typeof MediaRecorder === "undefined" || !canvas.captureStream) {
      window.dispatchEvent(new CustomEvent("recording-failed"));
      return;
    }
    const go = async () => {
      try {
        await startRecording();
      } catch (err) {
        console.error("Recording failed to start:", err);
        window.dispatchEvent(new CustomEvent("recording-failed"));
      }
    };
    // Camera/mic stream is set by webrtc.js; wait for it if it isn't ready yet.
    if (window._localStreamRef) {
      go();
    } else {
      const wait = () => {
        if (window._localStreamRef) go();
        else setTimeout(wait, 300);
      };
      wait();
    }
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
