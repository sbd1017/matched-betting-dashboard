/* Apply the saved theme before the page paints. Dark is the default; light is opt-in. Toggle lives in app.js. */
(function () {
  "use strict";
  var t = null;
  try { t = window.localStorage.getItem("mb-theme"); } catch (e) { /* storage blocked: stay dark */ }
  if (t === "light") document.documentElement.setAttribute("data-theme", "light");
})();
