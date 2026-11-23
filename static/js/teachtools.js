/*
  Teaching tools: the shared pieces.

  The quick poll (polls.js), breakout rooms (breakout.js) and the timer
  (timer.js) all live in the same full-screen-safe layer that classview.js keeps
  on top of the room, the whiteboard and full-screen slides. This file gives them:

    * a "Tools" menu the teacher opens from the control bar, the presenter view
      or the whiteboard (any button with data-open-tools),
    * a dock of cards down the right-hand side where the teacher's controls sit,
    * small helpers: toast, chime, element builder, clock format.

  Everything the teacher changes goes through the server (sockets_tools.py),
  which checks the teacher is the host.
*/
(function () {
  const socket = window._socket;
  const cfg = window.ROOM_CONFIG;
  const L = window.ClassLayer;
  if (!socket || !cfg || !L) return;

  const isHost = !!cfg.isHost;
  const isObserver = !!cfg.isObserver;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // Toasts stack in one place, so two at once never sit on top of each other.
  const toasts = el("div", "tt-toasts");
  toasts.setAttribute("aria-live", "polite");
  L.el.appendChild(toasts);
  function toast(text, ms) {
    const t = el("div", "tt-toast", text);
    toasts.appendChild(t);
    L.place();
    setTimeout(() => t.remove(), ms || 4000);
  }

  // A short two-note chime made in the browser (no sound file to load).
  let audio = null;
  function chime() {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      if (audio.state === "suspended") audio.resume();
      [[880, 0], [660, 0.22], [880, 0.44]].forEach(([freq, at]) => {
        const o = audio.createOscillator(), g = audio.createGain();
        o.type = "sine"; o.frequency.value = freq;
        const t0 = audio.currentTime + at;
        g.gain.setValueAtTime(0.0001, t0);
        g.gain.exponentialRampToValueAtTime(0.25, t0 + 0.03);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.4);
        o.connect(g); g.connect(audio.destination);
        o.start(t0); o.stop(t0 + 0.45);
      });
    } catch (e) { /* no audio available: the bar still flashes */ }
  }

  const clock = (sec) => {
    sec = Math.max(0, Math.round(sec));
    const m = Math.floor(sec / 60), s = sec % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  };

  // Typing in a box must not also fire a shortcut (A-D answers a poll, arrows move slides).
  const isTyping = (t) => !!(t && t.closest && t.closest("input, textarea, select, [contenteditable]"));

  const api = { isHost, isObserver, socket, layer: L, el, toast, chime, clock, isTyping, tools: [] };
  window.TeachTools = api;

  if (!isHost) return;     // ---------------- the teacher's menu and dock ----------------

  /* ---------- the dock: cards down the right-hand side ---------- */
  const dock = el("div", "tt-dock");
  dock.setAttribute("aria-label", "Teaching tools");
  L.el.appendChild(dock);

  api.card = function (id, title) {
    const card = el("section", "tt-card");
    card.id = "tt-card-" + id;
    card.hidden = true;
    const head = el("header", "tt-card-head");
    head.appendChild(el("h3", null, title));
    const x = el("button", "tt-x", "×");
    x.type = "button";
    x.setAttribute("aria-label", "Hide " + title);
    head.appendChild(x);
    const body = el("div", "tt-card-body");
    card.append(head, body);
    dock.appendChild(card);
    const handle = {
      el: card, body,
      show() { card.hidden = false; L.place(); },
      hide() { card.hidden = true; },
      toggle() { card.hidden ? handle.show() : handle.hide(); },
      get visible() { return !card.hidden; },
    };
    x.addEventListener("click", handle.hide);
    return handle;
  };

  /* ---------- the Tools menu ---------- */
  const menu = el("div", "tt-menu");
  menu.hidden = true;
  menu.setAttribute("role", "menu");
  L.el.appendChild(menu);

  // Each tool registers { id, label, hint, icon, open }.
  api.addTool = function (tool) { api.tools.push(tool); renderMenu(); };

  function renderMenu() {
    menu.textContent = "";
    menu.appendChild(el("div", "tt-menu-title", "Teaching tools"));
    api.tools.forEach((tool) => {
      const b = el("button", "tt-menu-item");
      b.type = "button";
      b.setAttribute("role", "menuitem");
      const icon = el("span", "tt-menu-icon");
      if (window.ICONS && window.ICONS[tool.icon]) icon.innerHTML = window.ICONS[tool.icon];
      const text = el("span", "tt-menu-text");
      text.append(el("b", null, tool.label), el("small", null, tool.hint));
      b.append(icon, text);
      b.addEventListener("click", () => { closeMenu(); tool.open(); });
      menu.appendChild(b);
    });
  }

  let menuAnchor = null;
  function openMenu(anchor) {
    menuAnchor = anchor;
    menu.hidden = false;
    L.place();
    const r = anchor.getBoundingClientRect();
    const mw = menu.offsetWidth || 280, mh = menu.offsetHeight || 220;
    let left = Math.min(Math.max(8, r.left + r.width / 2 - mw / 2), window.innerWidth - mw - 8);
    // Buttons at the bottom of the screen open the menu upwards.
    let top = r.top > window.innerHeight / 2 ? r.top - mh - 8 : r.bottom + 8;
    top = Math.max(8, Math.min(top, window.innerHeight - mh - 8));
    menu.style.left = left + "px";
    menu.style.top = top + "px";
    const first = menu.querySelector("button");
    if (first) first.focus();
  }
  function closeMenu() { menu.hidden = true; if (menuAnchor && menuAnchor.focus) menuAnchor.focus(); menuAnchor = null; }

  document.addEventListener("click", (e) => {
    const opener = e.target.closest && e.target.closest("[data-open-tools]");
    if (opener) { e.stopPropagation(); menu.hidden ? openMenu(opener) : closeMenu(); return; }
    if (!menu.hidden && !menu.contains(e.target)) { menu.hidden = true; menuAnchor = null; }
  }, true);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !menu.hidden) { e.stopPropagation(); closeMenu(); } }, true);
})();
