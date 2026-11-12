/*
  Enlarge a shared screen.

  When someone else starts sharing their screen, it fills the whole window for
  a moment so it can be read properly. Anyone can close it (✕, Esc, or the
  Enlarge button) and go back to the normal layout, and reopen it from the
  Enlarge button while the share is still going. The sharer doesn't get an
  enlarged view of their own screen (it would just mirror itself).
*/
(function () {
  const overlay = document.getElementById("focus-overlay");
  if (!overlay) return;
  const video = document.getElementById("focus-video");
  const label = document.getElementById("focus-label");
  const btn = document.getElementById("btn-enlarge");

  let sharer = null;   // sid of the participant currently sharing
  let showing = false;

  function nameOf(sid) {
    const meta = window.peerMeta && window.peerMeta[sid];
    return meta ? meta.name : "Someone";
  }

  function open() {
    const remote = sharer && window._remoteStreams[sharer];
    if (!remote) return;
    video.srcObject = remote.stream;
    video.play().catch(() => {});
    label.textContent = `${nameOf(sharer)} is presenting`;
    overlay.classList.add("show");
    showing = true;
  }

  function close() {
    overlay.classList.remove("show");
    video.srcObject = null;
    showing = false;
  }

  function stopSharing() {
    sharer = null;
    if (btn) btn.hidden = true;
    close();
  }

  window.addEventListener("screen-share-changed", (e) => {
    const { sid, sharing } = e.detail;
    if (window._socket && sid === window._socket.id) return; // your own share: nothing to enlarge
    if (sharing) {
      sharer = sid;
      if (btn) btn.hidden = false;
      open();
    } else if (sid === sharer) {
      stopSharing();
    }
  });

  // The sharer left the session.
  window.addEventListener("participants-changed", () => {
    if (sharer && !(window.peerMeta && window.peerMeta[sharer])) stopSharing();
  });

  document.getElementById("focus-close").addEventListener("click", close);
  if (btn) btn.addEventListener("click", () => (showing ? close() : open()));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && showing) close(); });
})();
