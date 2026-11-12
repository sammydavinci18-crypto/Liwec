/*
  Compulsory attendance for students in teacher-led lessons.

  A web page cannot stop someone closing a tab, so this does what a page can:
    - removes every way of leaving from the room (see meeting_room.html),
    - asks the browser to confirm before the tab is closed (main.js),
    - asks for full screen, and keeps the camera on,
    - the moment the student leaves the lesson screen (other tab, minimised,
      left full screen, camera off) it covers the lesson with a "return to the
      lesson" screen and tells the server, which logs how often and for how
      long (shown to the admin) and tells the teacher.
*/
(function () {
  const socket = window._socket;
  const overlay = document.getElementById("attendance-overlay");
  const title = document.getElementById("attendance-title");
  const text = document.getElementById("attendance-text");
  const enterBtn = document.getElementById("attendance-enter");
  if (!socket || !overlay) return;

  const canFullscreen = !!(document.documentElement.requestFullscreen);
  let started = false;     // student has pressed "Enter the lesson"
  let away = false;
  let awayReason = "";

  function show(heading, body, button) {
    title.textContent = heading;
    text.textContent = body;
    enterBtn.textContent = button;
    enterBtn.style.display = "";
    overlay.classList.add("show");
  }

  function setAway(isAway, reason) {
    if (!started) return;
    if (isAway && !away) {
      away = true;
      awayReason = reason;
      socket.emit("attendance_away", { away: true });
      show("You've left the lesson", reason + " Come back to continue. Your teacher has been told.", "Return to the lesson");
    } else if (!isAway && away) {
      away = false;
      socket.emit("attendance_away", { away: false });
      overlay.classList.remove("show");
    }
  }

  function cameraOk() {
    const stream = window.getLocalStream && window.getLocalStream();
    const track = stream && stream.getVideoTracks()[0];
    return !!(track && track.readyState === "live");
  }

  function check() {
    if (!started) return;
    if (document.visibilityState === "hidden") return setAway(true, "This lesson is not the tab you are looking at.");
    if (canFullscreen && !document.fullscreenElement) return setAway(true, "The lesson must stay in full screen.");
    if (!cameraOk()) return setAway(true, "Your camera must stay on for the whole lesson.");
    setAway(false);
  }

  // Not allowed to hide the lesson: re-enable the camera track if it gets switched off.
  setInterval(() => {
    const stream = window.getLocalStream && window.getLocalStream();
    if (stream) stream.getVideoTracks().forEach((t) => { if (!t.enabled) t.enabled = true; });
    check();
  }, 1000);

  document.addEventListener("visibilitychange", check);
  document.addEventListener("fullscreenchange", check);
  window.addEventListener("blur", () => setTimeout(() => { if (started && !document.hasFocus()) check(); }, 800));
  window.addEventListener("focus", check);

  async function enter() {
    if (canFullscreen && !document.fullscreenElement) {
      try { await document.documentElement.requestFullscreen(); } catch (err) { /* e.g. iOS: continue without it */ }
    }
    if (!cameraOk()) {
      show("Camera needed", "Allow camera access for this site, then reload the page. You can't attend without it.", "Reload");
      enterBtn.onclick = () => location.reload();
      return;
    }
    overlay.classList.remove("show");
    started = true;
    check();
  }

  enterBtn.onclick = enter;

  // After the teacher admits the student, ask them to enter (full screen needs a click).
  window.addEventListener("admitted", () => {
    show("Your lesson is starting",
         "This lesson is compulsory. Keep this screen open, in full screen, with your camera on, until your teacher ends the lesson.",
         "Enter the lesson");
  });
})();
