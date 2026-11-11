/* Notification bell + toasts. Polls /notifications/feed; dismissed items are
   remembered per browser so they don't keep coming back. */
(function () {
  var root = document.getElementById("notif-root");
  if (!root) return;

  var feedUrl = root.dataset.feed;
  var bell = document.getElementById("notif-bell");
  var panel = document.getElementById("notif-panel");
  var list = document.getElementById("notif-list");
  var countEl = document.getElementById("notif-count");
  var clearBtn = document.getElementById("notif-clear");
  var desktopBtn = document.getElementById("notif-desktop");
  var POLL_MS = 30000;
  var KEY = "lw-notif-dismissed";

  var items = [];
  var announced = {}; // ids already toasted in this page load

  function loadDismissed() {
    try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (e) { return []; }
  }
  function saveDismissed(arr) {
    try { localStorage.setItem(KEY, JSON.stringify(arr.slice(-200))); } catch (e) {}
  }
  function dismiss(id) {
    var d = loadDismissed();
    if (d.indexOf(id) === -1) d.push(id);
    saveDismissed(d);
    render();
  }

  var toastStack = document.createElement("div");
  toastStack.className = "toast-stack";
  toastStack.setAttribute("aria-live", "polite");
  document.body.appendChild(toastStack);

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function visible() {
    var d = loadDismissed();
    return items.filter(function (i) { return d.indexOf(i.id) === -1; });
  }

  function itemNode(i, toast) {
    var n = el("div", (toast ? "toast " : "notif-item ") + "lvl-" + i.level);
    var body = el("div", "notif-body");
    body.appendChild(el("strong", null, i.title));
    body.appendChild(el("span", null, i.body));
    var row = el("div", "notif-actions");
    if (i.url) {
      var a = el("a", "btn btn-primary btn-sm", i.action || "Open");
      a.href = i.url;
      a.addEventListener("click", function () { dismiss(i.id); });
      row.appendChild(a);
    }
    var x = el("button", "notif-link", toast ? "Dismiss" : "Clear");
    x.type = "button";
    x.addEventListener("click", function () {
      dismiss(i.id);
      if (toast && n.parentNode) n.parentNode.removeChild(n);
    });
    row.appendChild(x);
    body.appendChild(row);
    n.appendChild(body);
    return n;
  }

  function render() {
    var vis = visible();
    countEl.textContent = vis.length;
    countEl.hidden = vis.length === 0;
    bell.classList.toggle("has-urgent", vis.some(function (i) { return i.level === "urgent"; }));
    list.textContent = "";
    if (!vis.length) {
      list.appendChild(el("p", "notif-empty", "You're all caught up."));
    } else {
      vis.forEach(function (i) { list.appendChild(itemNode(i, false)); });
    }
    clearBtn.hidden = !vis.length;
  }

  function toastNew() {
    visible().forEach(function (i) {
      if (announced[i.id]) return;
      announced[i.id] = true;
      // Only pop up for things that need action now; the rest wait in the bell.
      if (i.level === "info") return;
      var t = itemNode(i, true);
      toastStack.appendChild(t);
      if (i.level === "warn") setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 20000);
      if ("Notification" in window && Notification.permission === "granted" && document.hidden) {
        try {
          var n = new Notification(i.title, { body: i.body, tag: i.id });
          n.onclick = function () { window.focus(); if (i.url) location.href = i.url; };
        } catch (e) {}
      }
    });
  }

  function poll() {
    fetch(feedUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        items = data.items || [];
        render();
        toastNew();
      })
      .catch(function () {});
  }

  bell.addEventListener("click", function (e) {
    e.stopPropagation();
    var open = panel.hidden;
    panel.hidden = !open;
    bell.setAttribute("aria-expanded", open ? "true" : "false");
  });
  document.addEventListener("click", function (e) {
    if (!panel.hidden && !root.contains(e.target)) {
      panel.hidden = true;
      bell.setAttribute("aria-expanded", "false");
    }
  });
  clearBtn.addEventListener("click", function () {
    var d = loadDismissed();
    visible().forEach(function (i) { d.push(i.id); });
    saveDismissed(d);
    toastStack.textContent = "";
    render();
  });

  if ("Notification" in window && Notification.permission === "default") {
    desktopBtn.hidden = false;
    desktopBtn.addEventListener("click", function () {
      Notification.requestPermission().then(function () { desktopBtn.hidden = true; });
    });
  }

  document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });
  poll();
  setInterval(poll, POLL_MS);
})();
