document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".password-field-wrap").forEach((wrap) => {
    const input = wrap.querySelector("input");
    const toggle = wrap.querySelector(".password-toggle-btn");
    if (!input || !toggle) return;

    if (window.ICONS) toggle.innerHTML = window.ICONS.eye;

    toggle.addEventListener("click", () => {
      const revealed = input.type === "text";
      input.type = revealed ? "password" : "text";
      toggle.innerHTML = window.ICONS ? window.ICONS[revealed ? "eye" : "eyeOff"] : "";
      toggle.setAttribute("aria-label", revealed ? "Show password" : "Hide password");
      toggle.setAttribute("aria-pressed", revealed ? "false" : "true");
    });
  });
});
