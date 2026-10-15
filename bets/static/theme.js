/* Apply the saved theme before the page paints (system / light / dark). Toggle lives in app.js. */
(function () {
  "use strict";
  var t = null;
  try { t = window.localStorage.getItem("mb-theme"); } catch (e) { /* storage blocked: follow the system */ }
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
})();
