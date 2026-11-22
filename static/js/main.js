document.addEventListener("DOMContentLoaded", () => {
  const HOME = window.ROOM_CONFIG.homeUrl || "/dashboard";
  const btnMic = document.getElementById("btn-mic");
  const btnCam = document.getElementById("btn-cam");
  const btnLeave = document.getElementById("btn-leave");
  const btnEndMeeting = document.getElementById("btn-end-meeting");
  const btnNotes = document.getElementById("btn-notes");
  const notesPanel = document.getElementById("notes-panel");
  const notesClose = document.getElementById("notes-close");
  const notesForm = document.getElementById("notes-form");
  const notesInput = document.getElementById("notes-input");
  const notesList = document.getElementById("notes-list");

  let micOn = true;
  let camOn = true;
  let screenSharing = false;

  /* ---------------- Screen sharing ----------------
     Computers share their real screen (getDisplayMedia). Phone and tablet
     browsers don't have that, so there the Share button offers what a
     mobile browser CAN send: the back camera (show a book or paper) or a
     photo / screenshot. Either way the class sees it like a screen share. */
  const btnScreenShare = document.getElementById("btn-screen-share");
  const shareLabel = btnScreenShare ? btnScreenShare.querySelector(".ctrl-label") : null;
  const sheet = document.getElementById("share-sheet");
  const canCaptureScreen = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
  let activeShareTrack = null;
  let shareCleanup = null;

  // Keep text and slides sharp (drop frame rate, not resolution, when bandwidth is tight).
  async function preferSharpness(on) {
    const conns = Object.values(window.peerConnections || {}).concat(Object.values(window.observerConnections || {}));
    for (const pc of conns) {
      const sender = pc.getSenders().find((s) => s.track && s.track.kind === "video");
      if (!sender || !sender.getParameters) continue;
      try {
        const params = sender.getParameters();
        params.degradationPreference = on ? "maintain-resolution" : "balanced";
        await sender.setParameters(params);
      } catch (err) { /* not supported: harmless */ }
    }
  }

  function beginShare(track, cleanup) {
    activeShareTrack = track;
    shareCleanup = cleanup || null;
    try { track.contentHint = "detail"; } catch (e) { /* ignore */ }
    window.replaceOutgoingVideoTrack(track);
    window.setLocalVideoTrack(track);
    screenSharing = true;
    btnScreenShare.classList.add("active-on");
    btnScreenShare.title = "Stop presenting";
    if (shareLabel) shareLabel.textContent = "Stop share";
    if (window.setTileScreenSharing) window.setTileScreenSharing("local", true);
    window._socket.emit("screen_share", { sharing: true });
    preferSharpness(true);
    // The browser's own "Stop sharing" button ends the track.
    track.onended = () => stopScreenShare();
  }

  async function getRestoreTrack() {
    let t = window._cameraOrBackgroundTrack || window._originalVideoTrack;
    if (t && t.readyState === "live") return t;
    // Some phones close the front camera while the back camera is in use: reopen it.
    try {
      const cam = await navigator.mediaDevices.getUserMedia({ video: true });
      t = cam.getVideoTracks()[0];
      t.enabled = camOn;
      window._originalVideoTrack = window._cameraOrBackgroundTrack = t;
      return t;
    } catch (err) {
      return null;
    }
  }

  async function stopScreenShare() {
    if (!screenSharing) return;
    screenSharing = false;
    const finished = activeShareTrack;
    activeShareTrack = null;
    btnScreenShare.classList.remove("active-on");
    btnScreenShare.title = "Share your screen";
    if (shareLabel) shareLabel.textContent = "Share";
    if (window.setTileScreenSharing) window.setTileScreenSharing("local", false);
    window._socket.emit("screen_share", { sharing: false });

    const restoreTrack = await getRestoreTrack();
    window.replaceOutgoingVideoTrack(restoreTrack);
    window.setLocalVideoTrack(restoreTrack);
    preferSharpness(false);

    // Release the capture so the browser's "sharing your screen" indicator goes away.
    if (finished) { finished.onended = null; finished.stop(); }
    if (shareCleanup) { shareCleanup(); shareCleanup = null; }
  }

  async function startDesktopShare() {
    let screenStream;
    try {
      screenStream = await navigator.mediaDevices.getDisplayMedia({
        video: { width: { ideal: 1920 }, height: { ideal: 1080 }, frameRate: { ideal: 15, max: 30 } },
        audio: false,
      });
    } catch (err) {
      if (err && err.name !== "NotAllowedError" && err.name !== "AbortError") {
        alert("Screen sharing could not start (" + (err.name || "error") + "). Try again, or use Chrome, Edge or Firefox on a computer.");
      }
      return; // cancelled the picker
    }
    beginShare(screenStream.getVideoTracks()[0]);
  }

  function openSheet() { if (sheet) sheet.classList.add("show"); }
  function closeSheet() { if (sheet) sheet.classList.remove("show"); }

  async function shareBackCamera() {
    closeSheet();
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
        audio: false,
      });
    } catch (err) {
      alert("The camera could not be opened. Allow camera access for this site and try again.");
      return;
    }
    beginShare(stream.getVideoTracks()[0]);
  }

  function shareImage(file) {
    closeSheet();
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = 1920; canvas.height = 1080;
      const ctx = canvas.getContext("2d");
      const draw = () => {
        ctx.fillStyle = "#000";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        const scale = Math.min(canvas.width / img.naturalWidth, canvas.height / img.naturalHeight);
        const w = img.naturalWidth * scale, h = img.naturalHeight * scale;
        ctx.drawImage(img, (canvas.width - w) / 2, (canvas.height - h) / 2, w, h);
      };
      draw();
      // Redraw regularly so the picture keeps being sent to people who join later.
      const timer = setInterval(draw, 500);
      const stream = canvas.captureStream(5);
      beginShare(stream.getVideoTracks()[0], () => { clearInterval(timer); URL.revokeObjectURL(url); });
    };
    img.onerror = () => { URL.revokeObjectURL(url); alert("That picture could not be opened."); };
    img.src = url;
  }

  if (btnScreenShare) {
    btnScreenShare.addEventListener("click", () => {
      if (screenSharing) return stopScreenShare();
      if (canCaptureScreen) return startDesktopShare();
      openSheet();
    });
  }
  if (sheet) {
    document.getElementById("share-camera").addEventListener("click", shareBackCamera);
    document.getElementById("share-cancel").addEventListener("click", closeSheet);
    const photoInput = document.getElementById("share-photo-input");
    document.getElementById("share-photo").addEventListener("click", () => photoInput.click());
    photoInput.addEventListener("change", () => {
      const f = photoInput.files && photoInput.files[0];
      photoInput.value = "";
      if (f) shareImage(f);
    });
  }

  btnMic.addEventListener("click", () => {
    if (!window.localStream) return;
    micOn = !micOn;
    window.localStream.getAudioTracks().forEach((t) => (t.enabled = micOn));
    btnMic.classList.toggle("active-off", !micOn);
    btnMic.querySelector(".icon").innerHTML = window.ICONS[micOn ? "mic" : "micOff"];
    btnMic.querySelector(".ctrl-label").textContent = micOn ? "Mute" : "Unmute";
    delete btnMic.dataset.forcedMute;
  });

  btnCam.addEventListener("click", () => {
    if (!window.localStream) return;
    camOn = !camOn;
    window.localStream.getVideoTracks().forEach((t) => (t.enabled = camOn));
    btnCam.classList.toggle("active-off", !camOn);
    btnCam.querySelector(".icon").innerHTML = window.ICONS[camOn ? "video" : "videoOff"];
    btnCam.querySelector(".ctrl-label").textContent = camOn ? "Stop video" : "Start video";
  });

  /* ---------------- Recording: automatic and compulsory ----------------
     The server announces "Recording" the moment the host is in the room, and
     the banner is always visible to everyone. There is no Record / Stop
     control: only ending the session stops the recording. */
  const recordingNotice = document.getElementById("recording-notice");
  const recordFailedOverlay = document.getElementById("record-failed-overlay");

  function setRecordingBanner(isRecording) {
    if (!recordingNotice) return;
    recordingNotice.style.display = "inline-flex"; // always on: every session is recorded
  }
  setRecordingBanner(true);

  if (window.ROOM_CONFIG.isHost) {
    // Start as soon as the host is let into their own room.
    window.addEventListener("admitted", () => {
      if (window.RecordingControl) window.RecordingControl.start();
    }, { once: true });
    window.addEventListener("recording-failed", () => {
      if (recordFailedOverlay) recordFailedOverlay.classList.add("show");
    });
  }

  // Closing the tab mid-lesson (host) or mid-lesson as a student would drop the
  // recording / the attendance, so the browser is asked to confirm first.
  window.addEventListener("beforeunload", (e) => {
    if (window.__sessionOver) return;
    if (window.ROOM_CONFIG.isHost || window.ROOM_CONFIG.attendanceRequired) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  if (btnLeave) {
    btnLeave.addEventListener("click", () => {
      window.__sessionOver = true;
      window.location.href = HOME;
    });
  }

  if (btnEndMeeting) {
    btnEndMeeting.addEventListener("click", async () => {
      btnEndMeeting.disabled = true;
      btnEndMeeting.textContent = "Ending…";
      try {
        if (window.stopRecordingAndWait) {
          await window.stopRecordingAndWait();
        }
        const res = await fetch(`/room/${window.ROOM_CONFIG.roomCode}/end`, { method: "POST" });
        const data = await res.json();
        window.__sessionOver = true;
        window.location.href = data.redirect || HOME;
      } catch (err) {
        console.error("Failed to end meeting cleanly:", err);
        window.__sessionOver = true;
        window.location.href = HOME;
      }
    });
  }

  function loadNotes() {
    fetch(`/room/${window.ROOM_CONFIG.roomCode}/notes`)
      .then((r) => r.json())
      .then((data) => renderNotes(data.notes));
  }

  function renderNotes(notes) {
    notesList.innerHTML = "";
    if (!notes.length) {
      notesList.innerHTML = '<div class="filmstrip-empty" id="notes-empty">No notes yet for this consultation.</div>';
      return;
    }
    notes.forEach((n) => appendNoteToDom(n, false));
    notesList.scrollTop = notesList.scrollHeight;
  }

  function appendNoteToDom(n, scrollIntoView) {
    const empty = document.getElementById("notes-empty");
    if (empty) empty.remove();

    const item = document.createElement("div");
    item.className = "note-item";
    item.innerHTML = `
      <div class="note-meta">${escapeHtml(n.author)} · ${escapeHtml(n.created_at)}</div>
      <div class="note-content"></div>
    `;
    item.querySelector(".note-content").textContent = n.content;
    notesList.appendChild(item);
    if (scrollIntoView) notesList.scrollTop = notesList.scrollHeight;
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  let notesUnread = 0;
  const notesUnreadBadge = document.getElementById("notes-unread");

  btnNotes.addEventListener("click", () => {
    notesPanel.classList.add("open");
    notesUnread = 0;
    if (notesUnreadBadge) notesUnreadBadge.style.display = "none";
    loadNotes();
  });
  notesClose.addEventListener("click", () => notesPanel.classList.remove("open"));

  notesForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const content = notesInput.value.trim();
    if (!content) return;
    notesInput.value = "";
    fetch(`/room/${window.ROOM_CONFIG.roomCode}/notes`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: `content=${encodeURIComponent(content)}`,
    }).catch((err) => console.warn("Failed to save note:", err));
    // The note itself gets added to the panel via the "note_added" socket
    // broadcast below (the server pushes it to everyone in the room,
    // including us), so there's nothing else to do here.
  });

  // Live push: every participant (including whoever just wrote it) gets
  // notes the moment they're saved — no more closing/reopening the panel.
  if (window._socket) {
    window._socket.on("note_added", (note) => {
      appendNoteToDom(note, true);
      if (!notesPanel.classList.contains("open")) {
        notesUnread += 1;
        if (notesUnreadBadge) {
          notesUnreadBadge.textContent = notesUnread;
          notesUnreadBadge.style.display = "inline-flex";
        }
      }
    });
  }
});

// expose localStream to main.js once webrtc.js sets it up
Object.defineProperty(window, "localStream", {
  get() {
    return window._localStreamRef;
  },
});
