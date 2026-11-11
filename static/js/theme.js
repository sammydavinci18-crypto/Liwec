/* Light / dark toggle (remembered per browser) and nav-menu tidying. */
(function () {
  var root = document.documentElement;
  function effective() {
    var t = root.getAttribute("data-theme");
    if (t) return t;
    return window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-theme-toggle]");
    if (btn) {
      var next = effective() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("lw-theme", next); } catch (err) {}
      return;
    }
    // close any open admin dropdown when clicking elsewhere
    document.querySelectorAll("details.nav-menu[open]").forEach(function (d) {
      if (!d.contains(e.target)) d.removeAttribute("open");
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") document.querySelectorAll("details.nav-menu[open]").forEach(function (d) { d.removeAttribute("open"); });
  });
})();
