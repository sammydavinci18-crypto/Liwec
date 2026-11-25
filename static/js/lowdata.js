/*
  Low-data mode: a student on a weak connection can drop to smaller video, or to
  audio only (slides, whiteboard, chat and the teacher's voice all keep working),
  without being kicked out of the lesson.

    Normal      everything.
    Low data    smaller video each way; only the teacher, anyone presenting and a
                couple of speakers are shown.
    Audio only  no video sent or received. The teacher sees the headphones "audio only" label next to
                the name, and a compulsory-lesson student is not logged as away.

  The mode itself is applied by webrtc.js (window.setDataMode). This file is the
  button, the menu, the "your connection looks weak" suggestion, and remembering
  the choice if the page is reloaded.
*/
(function () {
  const cfg = window.ROOM_CONFIG;
  if (!cfg) return;

  const btn = document.getElementById("btn-lowdata");
  const menu = document.getElementById("lowdata-menu");
  const bar = document.getElementById("net-banner");
  if (!btn || !menu || !bar) return;

  const KEY = "liwec-data-" + cfg.roomCode;
  const LABELS = { normal: "Data", low: "Low data", audio: "Audio only" };
  let forcedAudio = false;
  let suggestTimer = null;
  let dismissed = false;

  const load = () => { try { return sessionStorage.getItem(KEY) || "normal"; } catch (e) { return "normal"; } };
  const save = (m) => { try { sessionStorage.setItem(KEY, m); } catch (e) { /* ignore */ } };

  // The host sends the lesson: audio-only makes no sense for them.
  if (cfg.isHost) menu.querySelector('[data-mode="audio"]').hidden = true;

  function paint(mode) {
    btn.classList.toggle("active-on", mode !== "normal");
    btn.querySelector(".ctrl-label").textContent = LABELS[mode] || "Data";
    menu.querySelectorAll("button[data-mode]").forEach((b) => {
      b.classList.toggle("selected", b.dataset.mode === mode);
      b.disabled = forcedAudio && b.dataset.mode !== "audio";
    });
  }

  function choose(mode) {
    menu.classList.remove("show");
    save(mode);
    window.setDataMode(mode);
    hideBanner();
  }

  btn.addEventListener("click", (e) => { e.stopPropagation(); menu.classList.toggle("show"); });
  document.addEventListener("click", () => menu.classList.remove("show"));
  menu.addEventListener("click", (e) => e.stopPropagation());
  menu.querySelectorAll("button[data-mode]").forEach((b) => b.addEventListener("click", () => choose(b.dataset.mode)));
  window.addEventListener("data-mode-changed", (e) => paint(e.detail.mode));

  /* ---------- the banner (also used by webrtc.js for connection problems) ---------- */
  const textEl = bar.querySelector(".net-text");
  const actionEl = bar.querySelector(".net-action");
  const closeEl = bar.querySelector(".net-close");

  function showBanner(text, opts) {
    opts = opts || {};
    textEl.textContent = text;
    bar.dataset.kind = opts.kind || "info";
    actionEl.hidden = !opts.action;
    if (opts.action) {
      actionEl.textContent = opts.action;
      actionEl.onclick = () => { opts.onAction && opts.onAction(); };
    }
    closeEl.hidden = !!opts.sticky;
    bar.classList.add("show");
  }
  function hideBanner() { bar.classList.remove("show"); }
  closeEl.addEventListener("click", () => { dismissed = true; hideBanner(); });

  window.addEventListener("net-banner", (e) => {
    const d = e.detail || {};
    if (d.clear) return hideBanner();
    showBanner(d.text, d);
  });

  /* ---------- suggest it when the connection looks weak ---------- */
  function suggest(why) {
    if (dismissed || forcedAudio || window.__dataMode !== "normal") return;
    showBanner(why + " Switch to low-data mode so you stay in the lesson?", {
      kind: "warn",
      action: "Low data",
      onAction: () => choose("low"),
    });
  }

  // Poor link for a sustained 10 seconds (media server reports it; see webrtc.js).
  window.addEventListener("link-quality", (e) => {
    const q = e.detail.quality; // "excellent" | "good" | "poor" | "lost"
    clearTimeout(suggestTimer);
    if (q === "poor" || q === "lost") suggestTimer = setTimeout(() => suggest("Your connection looks weak."), 10000);
    else if (window.__dataMode === "normal") hideBanner();
  });

  window.addEventListener("admitted", () => {
    const c = navigator.connection;
    if (c && (c.saveData || /(^|-)2g$/.test(c.effectiveType || "") || c.effectiveType === "3g")) {
      suggest("You seem to be on a slow or limited mobile connection.");
    }
    const saved = load();
    if (saved !== "normal" && !forcedAudio) {
      if (cfg.isHost && saved === "audio") return;
      window.setDataMode(saved);
    }
  });

  // Busy peer-to-peer lesson: this person joins with video off, and can't turn it on.
  window.addEventListener("mesh-full", () => {
    if (cfg.isHost) return;
    // Only YOUR camera is switched off (webrtc.js). You still receive the
    // teacher's video and sound, so this is not "audio only" mode.
    showBanner("This lesson is busy, so your camera is off to keep it smooth for everyone. You can still see and hear your teacher and everything they share.", { kind: "info" });
  });

  // Host only: nothing relays video for students behind strict networks.
  if (cfg.isHost && cfg.turnMissing) {
    window.addEventListener("admitted", () => {
      showBanner("No relay server (TURN) is set up, so students on some mobile or school networks won't be able to connect. Ask whoever runs this site to set TURN_URLS / TURN_SECRET (see README).", { kind: "warn" });
    }, { once: true });
  }

  paint("normal");
})();
