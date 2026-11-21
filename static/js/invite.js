/* Host-only: the "Invite" panel inside the room. Copies / shares the student
   link and lets the teacher choose how guests come in. The server does the
   real work (routes/meetings.py: guest_link_settings); this only drives it. */
(function () {
  "use strict";
  var panel = document.getElementById("invite-panel");
  var btn = document.getElementById("btn-invite");
  if (!panel || !btn) return;

  var urlBox = document.getElementById("invite-url");
  var copyBtn = document.getElementById("invite-copy");
  var shareBtn = document.getElementById("invite-share");
  var autoBox = document.getElementById("invite-auto");
  var openBox = document.getElementById("invite-open");
  var newBtn = document.getElementById("invite-new");
  var statusEl = document.getElementById("invite-status");
  var closeBtn = document.getElementById("invite-close");
  var endpoint = panel.getAttribute("data-url");

  function say(msg) { statusEl.textContent = msg || ""; }

  function post(action) {
    var body = new URLSearchParams();
    body.set("action", action);
    return fetch(endpoint, { method: "POST", body: body })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || "Could not save that.");
        urlBox.value = data.url;
        autoBox.checked = !!data.auto_admit;
        openBox.checked = !!data.open;
        return data;
      });
  }

  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    urlBox.focus();
    urlBox.select();
    document.execCommand("copy");
    return Promise.resolve();
  }

  btn.addEventListener("click", function () {
    panel.classList.toggle("open");
    if (panel.classList.contains("open")) say("");
  });
  closeBtn.addEventListener("click", function () { panel.classList.remove("open"); });
  urlBox.addEventListener("focus", function () { urlBox.select(); });

  copyBtn.addEventListener("click", function () {
    copy(urlBox.value).then(function () {
      say("Link copied. Paste it wherever your students are.");
    }, function () { say("Couldn't copy. Select the link and copy it by hand."); });
  });

  if (navigator.share) {
    shareBtn.hidden = false;
    shareBtn.addEventListener("click", function () {
      navigator.share({ title: document.title, text: "Join my lesson:", url: urlBox.value }).catch(function () {});
    });
  }

  autoBox.addEventListener("change", function () {
    post(autoBox.checked ? "auto_admit_on" : "auto_admit_off").then(function () {
      say(autoBox.checked ? "Students now come straight in." : "You will admit each student yourself.");
    }, function (e) { autoBox.checked = !autoBox.checked; say(e.message); });
  });

  openBox.addEventListener("change", function () {
    post(openBox.checked ? "open" : "close").then(function () {
      say(openBox.checked ? "Link is open." : "Nobody new can join with this link.");
    }, function (e) { openBox.checked = !openBox.checked; say(e.message); });
  });

  newBtn.addEventListener("click", function () {
    if (!confirm("The old link will stop working for anyone who hasn't joined yet. Make a new link?")) return;
    post("regenerate").then(function () {
      say("New link ready. Send this one instead.");
    }, function (e) { say(e.message); });
  });
})();
