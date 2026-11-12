/*
  Recording, from the host's browser only.

  Rather than trying to merge separate per-person recordings after the fact,
  we composite the SAME layout everyone is watching live (host spotlight +
  participant grid) onto a canvas, mix everyone's audio into one track, and
  record that as a single video. That's what makes playback later look
  exactly like the live meeting did.

  The file is uploaded in ~30 second chunks while the call is happening, so
  if the host's browser crashes, at most the last chunk is at risk — not the
  whole recording.
*/

const CANVAS_W = 960;
const CANVAS_H = 540;
const SPOTLIGHT_W = 340; // matches roughly the live CSS spotlight tile proportion

let recorder = null;
let recordingActive = false;
let drawTimer = null;
let audioCtx = null;
let audioDest = null;
const connectedAudioSids = new Set();
let localAudioConnected = false;

const canvas = document.createElement("canvas");
canvas.width = CANVAS_W;
canvas.height = CANVAS_H;
const ctx = canvas.getContext("2d");

function drawCover(video, x, y, w, h) {
  if (!video || !video.videoWidth) return;
  const scale = Math.max(w / video.videoWidth, h / video.videoHeight);
  const sw = w / scale;
  const sh = h / scale;
  const sx = (video.videoWidth - sw) / 2;
  const sy = (video.videoHeight - sh) / 2;
  ctx.drawImage(video, sx, sy, sw, sh, x, y, w, h);
}

function drawFrame() {
  ctx.fillStyle = "#0E1A22";
  ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);

  // Host spotlight — reuse the actual <video> element already rendering it.
  const spotlightVideo = document.querySelector("#spotlight-tile video");
  if (spotlightVideo) {
    drawCover(spotlightVideo, 0, 0, SPOTLIGHT_W, CANVAS_H);
  }
  ctx.strokeStyle = "#2B3E45";
  ctx.strokeRect(0, 0, SPOTLIGHT_W, CANVAS_H);

  // Participant grid — reuse the actual tile <video> elements. Selecting by
  // class (not "#filmstrip .mini-tile") so this keeps working even when
  // gallery view has moved the tiles into #gallery-grid.
  const tiles = Array.from(document.querySelectorAll(".mini-tile video"));
  const gridX = SPOTLIGHT_W + 12;
  const gridW = CANVAS_W - gridX;
  if (tiles.length > 0) {
    const cols = Math.min(3, Math.ceil(Math.sqrt(tiles.length)));
    const rows = Math.ceil(tiles.length / cols);
    const cellW = (gridW - (cols - 1) * 8) / cols;
    const cellH = (CANVAS_H - (rows - 1) * 8) / rows;

    tiles.forEach((video, i) => {
      const col = i % cols;
      const row = Math.floor(i / cols);
      const x = gridX + col * (cellW + 8);
      const y = row * (cellH + 8);
      drawCover(video, x, y, cellW, cellH);
      ctx.strokeStyle = "#2B3E45";
      ctx.strokeRect(x, y, cellW, cellH);
    });
  }
  drawWhiteboard();
}

// While the whiteboard is open it covers everyone's screen, so it also covers
// the recording: the board is painted over the normal layout.
function drawWhiteboard() {
  const wb = window.Whiteboard;
  if (!wb || !wb.isOpen()) return;
  const scale = Math.min(CANVAS_W / wb.canvas.width, CANVAS_H / wb.canvas.height);
  const w = wb.canvas.width * scale, h = wb.canvas.height * scale;
  ctx.fillStyle = "#0E1A22";
  ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);
  ctx.drawImage(wb.canvas, (CANVAS_W - w) / 2, (CANVAS_H - h) / 2, w, h);
}

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

async function uploadChunk(blob) {
  try {
    await fetch(`/room/${window.ROOM_CONFIG.roomCode}/recording/chunk`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: blob,
    });
  } catch (err) {
    console.warn("Recording chunk upload failed (will keep trying on next chunk):", err);
  }
}

function startRecording() {
  if (recordingActive) return;

  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  audioDest = audioCtx.createMediaStreamDestination();

  const canvasStream = canvas.captureStream(10); // 10 fps is plenty for a consultation call
  const combined = new MediaStream([
    ...canvasStream.getVideoTracks(),
    ...audioDest.stream.getAudioTracks(),
  ]);

  const mimeType = pickMimeType();
  recorder = new MediaRecorder(combined, mimeType ? { mimeType } : undefined);
  recorder.onerror = () => window.dispatchEvent(new CustomEvent("recording-failed"));
  recorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) uploadChunk(e.data);
  };
  recorder.start(30000); // flush a chunk to the server every 30s

  drawTimer = setInterval(() => {
    drawFrame();
    connectAudioSources();
  }, 100); // ~10fps drawing to match the captured framerate

  recordingActive = true;
}

// window.stopRecordingAndWait(): used by main.js when the host clicks "End meeting".
// Resolves once the final chunk has been uploaded, so it's safe to call the
// /end endpoint right after. Resolves immediately if nothing was recording.
window.stopRecordingAndWait = function () {
  return new Promise((resolve) => {
    if (!recordingActive || !recorder) {
      resolve();
      return;
    }
    recorder.onstop = async () => {
      clearInterval(drawTimer);
      recordingActive = false;
      resolve();
    };
    recorder.stop();
  });
};

// Recording is automatic: main.js calls start() the moment the host is let into
// their own room, and nothing in the UI can stop it — only ending the session
// (stopRecordingAndWait, called from "End lesson"). If this browser can't
// record, "recording-failed" blocks the room rather than letting a session run
// unrecorded.
window.RecordingControl = {
  start() {
    if (typeof MediaRecorder === "undefined" || !canvas.captureStream) {
      window.dispatchEvent(new CustomEvent("recording-failed"));
      return;
    }
    const go = () => {
      try {
        startRecording();
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
};
