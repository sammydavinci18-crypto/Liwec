// Shared interface behaviour for every page. Small, dependency-free, and
// progressive: if this script fails to load, every form and link still works.
(function () {
  "use strict";

  /* ------------------------------------------------------------------
     1. Confirmation before risky actions.
        Add  data-confirm="Question?"  to a <form>. Optional extras:
          data-confirm-title="Cancel this session?"
          data-confirm-action="Yes, cancel it"
          data-confirm-tone="danger"   (red button)
     ------------------------------------------------------------------ */
  var dialog = null;

  function buildDialog() {
    var d = document.createElement("dialog");
    d.className = "confirm-dialog";
    d.setAttribute("aria-labelledby", "confirm-title");
    d.innerHTML =
      '<form method="dialog" class="confirm-body">' +
      '<h3 id="confirm-title"></h3>' +
      '<p id="confirm-text"></p>' +
      '<div class="confirm-actions">' +
      '<button type="submit" value="cancel" class="btn btn-ghost" id="confirm-cancel">Go back</button>' +
      '<button type="submit" value="ok" class="btn btn-primary" id="confirm-ok">Confirm</button>' +
      "</div></form>";
    document.body.appendChild(d);
    return d;
  }

  function askToConfirm(form, submitter) {
    var message = form.getAttribute("data-confirm");
    var title = form.getAttribute("data-confirm-title") || "Please confirm";
    var action = form.getAttribute("data-confirm-action") || "Confirm";
    var tone = form.getAttribute("data-confirm-tone");

    if (!window.HTMLDialogElement || typeof document.createElement("dialog").showModal !== "function") {
      if (window.confirm(message)) proceed(form, submitter);
      return;
    }
    dialog = dialog || buildDialog();
    dialog.querySelector("#confirm-title").textContent = title;
    dialog.querySelector("#confirm-text").textContent = message;
    var ok = dialog.querySelector("#confirm-ok");
    ok.textContent = action;
    ok.className = "btn " + (tone === "danger" ? "btn-danger" : "btn-primary");

    dialog.returnValue = "";
    dialog.onclose = function () {
      if (dialog.returnValue === "ok") proceed(form, submitter);
    };
    dialog.showModal();
    // Safer default for destructive actions: focus "Go back", not the red button.
    (tone === "danger" ? dialog.querySelector("#confirm-cancel") : ok).focus();
  }

  function proceed(form, submitter) {
    form.dataset.confirmed = "1";
    if (form.requestSubmit) {
      try { form.requestSubmit(submitter || undefined); return; } catch (e) { /* fall through */ }
    }
    form.submit();
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.hasAttribute("data-confirm") && form.dataset.confirmed !== "1") {
      e.preventDefault();
      e.stopImmediatePropagation();
      askToConfirm(form, e.submitter);
    }
  }, true);

  /* ------------------------------------------------------------------
     2. Stop accidental double submits (two lessons, two approvals...).
        Buttons show "Working…" until the page changes. Opt out with
        data-no-lock on the form.
     ------------------------------------------------------------------ */
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || !(form instanceof HTMLFormElement)) return;
    if ((form.method || "get").toLowerCase() !== "post" || form.hasAttribute("data-no-lock")) return;
    if (form.dataset.locked === "1") { e.preventDefault(); return; }
    form.dataset.locked = "1";
    setTimeout(function () {
      form.querySelectorAll('button[type="submit"], button:not([type])').forEach(function (b) {
        b.dataset.label = b.innerHTML;
        b.disabled = true;
        b.setAttribute("aria-busy", "true");
        if (b.classList.contains("btn-block") || b.classList.contains("btn-primary") || b.classList.contains("btn-danger")) {
          b.textContent = "Working\u2026";
        }
      });
    }, 0);
  });

  // Coming back with the Back button should not leave buttons frozen.
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    document.querySelectorAll("form[data-locked]").forEach(function (f) {
      delete f.dataset.locked;
      delete f.dataset.confirmed;
      f.querySelectorAll("button[data-label]").forEach(function (b) {
        b.innerHTML = b.dataset.label; b.disabled = false; b.removeAttribute("aria-busy");
      });
    });
  });

  /* ------------------------------------------------------------------
     3. Dismissible messages (they stay until the person closes them).
     ------------------------------------------------------------------ */
  function removeFlash(el) {
    el.classList.add("is-leaving");
    setTimeout(function () {
      var stack = el.parentNode;
      if (el.parentNode) el.parentNode.removeChild(el);
      if (stack && stack.classList.contains("flash-stack") && !stack.children.length) stack.remove();
    }, 200);
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".flash-close");
    if (btn) removeFlash(btn.closest(".flash"));
  });

  /* ------------------------------------------------------------------
     4. Navigation: the mobile menu button.
     ------------------------------------------------------------------ */
  var toggle = document.getElementById("nav-toggle");
  var nav = document.getElementById("site-nav");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // Escape closes the mobile menu (dropdown closing is handled in theme.js).
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && nav && toggle && nav.classList.contains("is-open")) {
      nav.classList.remove("is-open");
      toggle.setAttribute("aria-expanded", "false");
      toggle.focus();
    }
  });

  /* ------------------------------------------------------------------
     5. Pickers: any  <div data-picker>  with a filter box gets
        "Select shown / Clear" and a live count. (Used on the guest list.)
     ------------------------------------------------------------------ */
  document.querySelectorAll("[data-picker]").forEach(function (root) {
    var items = root.querySelectorAll("[data-picker-item]");
    var counter = root.querySelector("[data-picker-count]");
    function visible(el) { return el.style.display !== "none"; }
    function update() {
      var n = 0;
      items.forEach(function (el) { var c = el.querySelector('input[type="checkbox"]'); if (c && c.checked) n++; });
      if (counter) counter.textContent = n + (n === 1 ? " person selected" : " people selected");
    }
    root.addEventListener("change", update);
    var all = root.querySelector("[data-picker-all]");
    var none = root.querySelector("[data-picker-none]");
    if (all) all.addEventListener("click", function () {
      items.forEach(function (el) { var c = el.querySelector('input[type="checkbox"]'); if (c && visible(el)) c.checked = true; });
      update();
    });
    if (none) none.addEventListener("click", function () {
      items.forEach(function (el) { var c = el.querySelector('input[type="checkbox"]'); if (c) c.checked = false; });
      update();
    });
    update();
  });
})();
