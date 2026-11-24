/*
  The teacher's room fills the whole browser.

  The site's own top menu is tucked away while a lesson runs, so the video, slides
  and tools get the full height. The "Menu" button slides it down; its links open in
  a NEW tab, so the live lesson (and its recording) is never interrupted.
  "Full screen" takes the lesson over the whole display, hiding the browser's own bars.
*/
(function () {
  const body = document.body;
  const topbar = document.querySelector(".topbar");
  const menuBtn = document.getElementById("btn-site-menu");
  const fullBtn = document.getElementById("btn-browser-full");
  const backdrop = document.getElementById("site-menu-backdrop");
  const note = document.getElementById("site-menu-note");
  const closeBtn = document.getElementById("site-menu-close");

  /* ---------- the site menu ---------- */
  if (topbar && menuBtn) {
    // Anything clicked in the menu must not replace the live lesson in this tab.
    topbar.querySelectorAll("a[href]").forEach((a) => {
      if ((a.getAttribute("href") || "").startsWith("#")) return;
      a.target = "_blank";
      a.rel = "noopener";
    });

    const setMenu = (open) => {
      body.classList.toggle("site-menu-open", open);
      menuBtn.setAttribute("aria-expanded", String(open));
      menuBtn.classList.toggle("active-on", open);
      if (backdrop) backdrop.hidden = !open;
      if (note) note.hidden = !open;
    };
    menuBtn.addEventListener("click", () => setMenu(!body.classList.contains("site-menu-open")));
    if (backdrop) backdrop.addEventListener("click", () => setMenu(false));
    if (closeBtn) closeBtn.addEventListener("click", () => setMenu(false));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && body.classList.contains("site-menu-open")) setMenu(false);
    });
  } else if (menuBtn) {
    menuBtn.hidden = true;     // no site menu on this page: nothing to open
  }

  /* ---------- the whole browser ---------- */
  const root = document.documentElement;
  const canFull = !!(root.requestFullscreen || root.webkitRequestFullscreen);
  const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement || null;
  if (!fullBtn) return;
  if (!canFull) { fullBtn.hidden = true; return; }   // e.g. iPhone Safari: no page full screen

  const label = fullBtn.querySelector(".ctrl-label");
  const sync = () => {
    const on = !!fsEl();
    fullBtn.classList.toggle("active-on", on);
    fullBtn.setAttribute("aria-pressed", String(on));
    if (label) label.textContent = on ? "Exit full" : "Full screen";
  };
  fullBtn.addEventListener("click", () => {
    try {
      if (fsEl()) (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      else Promise.resolve((root.requestFullscreen || root.webkitRequestFullscreen).call(root)).catch(() => {});
    } catch (e) { /* the browser said no: leave the button as it was */ }
  });
  document.addEventListener("fullscreenchange", sync);
  document.addEventListener("webkitfullscreenchange", sync);
  sync();
})();
