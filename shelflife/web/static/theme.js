// The theme button in the header: automatic (your system's light or dark), dark, or light.
// The choice is kept in this browser; the page's <head> applies it before drawing, so nothing flashes.
(function () {
  var KEY = "shelflife-theme";
  var ORDER = ["auto", "dark", "light"];
  var ICONS = { auto: "◐", dark: "☾", light: "☀" };
  var NAMES = { auto: "Theme: automatic, like your system", dark: "Theme: dark", light: "Theme: light" };
  var button = document.querySelector(".theme-toggle");
  if (!button) return;

  function current() {
    var t = document.documentElement.dataset.theme;
    return t === "dark" || t === "light" ? t : "auto";
  }

  function show(theme) {
    button.textContent = ICONS[theme];
    button.title = NAMES[theme] + " (click to change)";
    button.setAttribute("aria-label", NAMES[theme]);
  }

  button.hidden = false;
  show(current());
  button.addEventListener("click", function () {
    var next = ORDER[(ORDER.indexOf(current()) + 1) % ORDER.length];
    if (next === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = next;
    try {
      if (next === "auto") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, next);
    } catch (e) {}  // storage blocked: the choice lasts until the page is left
    show(next);
  });
})();
