document.addEventListener("DOMContentLoaded", () => {
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

  /* ---------------- Screen sharing ---------------- */
  const btnScreenShare = document.getElementById("btn-screen-share");
  if (btnScreenShare) {
    btnScreenShare.addEventListener("click", async () => {
      if (!screenSharing) {
        let screenStream;
        try {
          screenStream = await navigator.mediaDevices.getDisplayMedia({ video: true });
        } catch (err) {
          return; // user cancelled the picker
        }
        const screenTrack = screenStream.getVideoTracks()[0];

        window.replaceOutgoingVideoTrack(screenTrack);
        window.setLocalVideoTrack(screenTrack);
        screenSharing = true;
        btnScreenShare.classList.add("active-on");
        btnScreenShare.title = "Stop presenting";
        window._socket.emit("screen_share", { sharing: true });

        // If the user stops sharing via the browser's native "Stop sharing"
        // control (rather than our button), fall back cleanly too.
        screenTrack.onended = () => stopScreenShare();
      } else {
        stopScreenShare();
      }
    });
  }

  function stopScreenShare() {
    if (!screenSharing) return;
    const restoreTrack = window._cameraOrBackgroundTrack || window._originalVideoTrack;
    window.replaceOutgoingVideoTrack(restoreTrack);
    window.setLocalVideoTrack(restoreTrack);
    screenSharing = false;
    btnScreenShare.classList.remove("active-on");
    btnScreenShare.title = "Share your screen";
    window._socket.emit("screen_share", { sharing: false });
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
      window.location.href = "/dashboard";
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
        window.location.href = data.redirect || "/dashboard";
      } catch (err) {
        console.error("Failed to end meeting cleanly:", err);
        window.__sessionOver = true;
        window.location.href = "/dashboard";
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
