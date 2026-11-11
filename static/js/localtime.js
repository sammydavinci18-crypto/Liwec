// All times are stored in UTC. Show them in the viewer's own timezone, and let
// the admin type session times in THEIR timezone (converted back to UTC on submit).
(function () {
  function pad(n) { return String(n).padStart(2, "0"); }
  function toLocalInput(d) {
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
           "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function toUtcInput(d) {
    return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) +
           "T" + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
  }

  document.querySelectorAll("time.local-time").forEach(function (el) {
    var d = new Date(el.getAttribute("datetime"));
    if (isNaN(d)) return;
    el.textContent = d.toLocaleString(undefined, {
      weekday: "short", day: "2-digit", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit", timeZoneName: "short"
    });
  });

  document.querySelectorAll('input[type="datetime-local"][data-utc]').forEach(function (input) {
    if (input.value) {
      var d = new Date(input.value + "Z");           // server value is UTC
      if (!isNaN(d)) input.value = toLocalInput(d);  // show it as local time
    }
    if (input.form && !input.form.dataset.utcBound) {
      input.form.dataset.utcBound = "1";
      input.form.addEventListener("submit", function () {
        input.form.querySelectorAll('input[type="datetime-local"][data-utc]').forEach(function (i) {
          if (!i.value) return;
          var d = new Date(i.value);                  // typed value is local
          if (!isNaN(d)) i.value = toUtcInput(d);     // send UTC
        });
      });
    }
  });
})();
