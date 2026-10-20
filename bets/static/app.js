/* Matched betting: progressive enhancement only (every page works without it). */
(function () {
  "use strict";
  var base = (document.querySelector('meta[name="app-base"]') || {}).content || "";

  // Imports: select every pending entry for bulk confirmation.
  document.querySelectorAll("[data-select-all]").forEach(function (box) {
    box.addEventListener("change", function () {
      Array.prototype.forEach.call(box.form.elements, function (el) { if (el.name === "ids") el.checked = box.checked; });
    });
  });

  // Confirm destructive or bulk actions.
  document.addEventListener("submit", function (ev) {
    var msg = ev.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) ev.preventDefault();
  });

  // Live back/lay figures from the server calculator (one source of truth).
  document.querySelectorAll("form[data-calc]").forEach(function (form) {
    var q = function (sel) { return form.querySelector(sel); };
    var out = q("[data-calc-out]") || document.querySelector("[data-calc-out]");
    var errBox = q("[data-calc-error]") || document.querySelector("[data-calc-error]");
    var layStake = q("[data-calc-laystake]");
    var exchange = q("[data-exchange]");
    var commission = q("[data-calc-commission]");
    var freeBet = q("[data-free-bet]");
    var staleNote = q("[data-commission-warning]");
    function syncStale() {                      // an exchange whose stored promotional rate has ended
      if (!staleNote || !exchange) return;
      var opt = exchange.options[exchange.selectedIndex], msg = opt ? opt.getAttribute("data-stale") : "";
      if (msg && commission && commission.value.trim() !== opt.dataset.commission) msg = "";   // a typed rate isn't the stored one
      staleNote.textContent = msg ? msg + ". Calculations use the stored rate until you update it." : "";
      staleNote.hidden = !msg;
    }
    var timer = null;

    function value(sel) { var el = q(sel); return el ? el.value.trim() : ""; }

    function update() {
      var params = new URLSearchParams({
        kind: value("[data-calc-kind]") || "qualifying", stake: value("[data-calc-stake]"),
        back_odds: value("[data-calc-back]"), lay_odds: value("[data-calc-lay]"), commission: value("[data-calc-commission]") || "0"
      });
      if (!params.get("stake") || !params.get("back_odds") || !params.get("lay_odds")) {
        if (out) out.hidden = true;
        if (errBox) errBox.textContent = "";
        return;
      }
      fetch(base + "/api/calc?" + params.toString(), { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          if (!res.ok) { if (out) out.hidden = true; if (errBox) errBox.textContent = res.d.error || ""; return; }
          if (errBox) errBox.textContent = "";
          if (out) {
            out.hidden = false;
            ["lay_stake", "liability", "if_back_wins", "if_lay_wins"].forEach(function (k) {
              var el = out.querySelector('[data-out="' + k + '"]');
              if (el) el.textContent = res.d.text[k];
            });
          }
          if (layStake) layStake.placeholder = "auto: " + res.d.lay_stake;
        })
        .catch(function () { /* offline: the form still works */ });
    }

    form.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(update, 250); });
    form.addEventListener("change", function () { clearTimeout(timer); timer = setTimeout(update, 50); });

    if (exchange && commission) {
      // Choosing an exchange fills in its stored rate; typing over it only affects this calculation (nothing is
      // saved to the account). The first fill on load leaves a value that is already there.
      var loading = true;
      commission.addEventListener("input", syncStale);
      exchange.addEventListener("change", function () {
        var opt = exchange.options[exchange.selectedIndex];
        if (opt && opt.dataset.commission && (!loading || !commission.value)) commission.value = opt.dataset.commission;
        syncStale();
        update();
      });
    }
    if (freeBet) {
      freeBet.addEventListener("change", function () {
        var opt = freeBet.options[freeBet.selectedIndex];
        if (!opt || !opt.value) return;
        var stake = q("[data-calc-stake]"), kind = q("[data-calc-kind]"), back = form.querySelector('[name="back_account"]');
        if (stake) stake.value = opt.dataset.value;
        if (kind) kind.value = opt.dataset.kind;
        if (back) back.value = opt.dataset.account;
        update();
      });
      if (freeBet.value) freeBet.dispatchEvent(new Event("change"));
    }
    if (exchange && exchange.value && commission && !commission.value) exchange.dispatchEvent(new Event("change"));
    loading = false;
    syncStale();
    update();
  });

  // v3: sizes that come from data (the CSP forbids inline styles, so they are set here).
  document.querySelectorAll(".blk[data-v]").forEach(function (el) { el.style.flexGrow = el.getAttribute("data-v"); });
  // Chart axis labels: one grid column per bar (CSP forbids inline styles).
  document.querySelectorAll("[data-cols]").forEach(function (el) {
    el.style.gridTemplateColumns = "repeat(" + parseInt(el.getAttribute("data-cols"), 10) + ", minmax(0, 1fr))";
  });
  document.querySelectorAll("[data-of]").forEach(function (el) {
    var parts = el.getAttribute("data-of").split("/"), a = parseFloat(parts[0]), b = parseFloat(parts[1]);
    el.style.width = b > 0 ? Math.min(100, a / b * 100) + "%" : "0";
  });
  document.querySelectorAll(".drain i[data-left]").forEach(function (el) {
    el.style.width = Math.max(0, Math.min(1, parseFloat(el.getAttribute("data-left")))) * 100 + "%";
  });

  // Theme toggle (dark <-> light). Dark is the default; theme.js applies a saved "light" before paint.
  var themes = ["dark", "light"];
  function savedTheme() { try { return window.localStorage.getItem("mb-theme") === "light" ? "light" : "dark"; } catch (e) { return "dark"; } }
  function applyTheme(t) {
    if (t === "light") document.documentElement.setAttribute("data-theme", "light");
    else document.documentElement.removeAttribute("data-theme");
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) { b.title = "Theme: " + t; });
  }
  applyTheme(savedTheme());
  document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var next = themes[(themes.indexOf(savedTheme()) + 1) % themes.length];
      try { window.localStorage.setItem("mb-theme", next); } catch (e) { /* not remembered */ }
      applyTheme(next);
    });
  });

  // Calculator drawer: open with the toolbar button or C, close with Esc / the scrim. Search: Cmd/Ctrl+K or /.
  var lastFocus = null;
  function drawer(id, open) {
    var d = document.getElementById(id), scrim = document.querySelector(".scrim");
    if (!d) return;
    if (open) { lastFocus = document.activeElement; }
    d.classList.toggle("on", open); if (scrim) scrim.classList.toggle("on", open);
    d.setAttribute("aria-hidden", open ? "false" : "true");
    if (open) { var first = d.querySelector("input:not([type=hidden]):not([type=radio])"); if (first) setTimeout(function () { first.focus(); }, 60); }
    else if (lastFocus) { lastFocus.focus(); }
  }
  document.querySelectorAll("[data-drawer-open]").forEach(function (b) {
    b.addEventListener("click", function () { drawer(b.getAttribute("data-drawer-open"), true); });
  });
  document.querySelectorAll("[data-drawer-close]").forEach(function (b) {
    b.addEventListener("click", function () { document.querySelectorAll(".drawer.on").forEach(function (d) { drawer(d.id, false); }); });
  });
  document.querySelectorAll("[data-calc-kind-radio]").forEach(function (r) {
    r.addEventListener("change", function () {
      var hidden = r.form.querySelector("[data-calc-kind]"); if (hidden) { hidden.value = r.value; }
      r.form.dispatchEvent(new Event("change"));
    });
  });
  document.addEventListener("keydown", function (ev) {
    var t = ev.target, typing = t && (/INPUT|TEXTAREA|SELECT/.test(t.tagName) || t.isContentEditable);
    if (ev.key === "Escape") { document.querySelectorAll(".drawer.on").forEach(function (d) { drawer(d.id, false); }); return; }
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "k") {
      ev.preventDefault();
      var box = document.querySelector("[data-search-input]");
      if (box) { box.focus(); box.select(); } else { window.location.href = base + "/search"; }
      return;
    }
    if (typing || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (ev.key === "/") { ev.preventDefault(); window.location.href = base + "/search"; }
    else if (ev.key.toLowerCase() === "c" && document.getElementById("calc")) { ev.preventDefault(); drawer("calc", true); }
  });

  // Profit chart: hover (mouse), tap (touch) or the arrow keys read each settlement / period. The figures come
  // from the server-rendered list inside the chart (also what screen readers read); nothing is recalculated here.
  document.querySelectorAll("[data-chart]").forEach(function (wrap) {
    var pts = Array.prototype.slice.call(wrap.querySelectorAll("[data-chart-points] li"));
    var svg = wrap.querySelector("svg");
    if (!pts.length || !svg) return;
    var bars = wrap.getAttribute("data-chart") === "bars" ? svg.querySelectorAll(".bar") : [];
    function el(cls) { var d = document.createElement("div"); d.className = cls; d.hidden = true; wrap.appendChild(d); return d; }
    var guide = el("pc-guide"), dot = el("pc-dot"), tip = el("pc-tip");
    tip.setAttribute("role", "status");
    var xs = pts.map(function (li) { return parseFloat(li.getAttribute("data-x")); });
    var current = -1, pinned = false, lastPointer = "mouse";
    function nearest(pct) {
      var best = 0, gap = Infinity;
      xs.forEach(function (x, i) { var d = Math.abs(x - pct); if (d < gap) { gap = d; best = i; } });
      return best;
    }
    function show(i) {
      i = Math.max(0, Math.min(pts.length - 1, i));
      current = i;
      var li = pts[i], x = xs[i], y = parseFloat(li.getAttribute("data-y"));
      var h = svg.getBoundingClientRect().height, w = wrap.getBoundingClientRect().width;
      guide.style.left = x + "%"; guide.style.height = h + "px";
      dot.style.left = x + "%"; dot.style.top = (y / 100 * h) + "px";
      tip.innerHTML = li.innerHTML;                      // server-rendered, already escaped
      var href = li.getAttribute("data-href");
      if (href) { var a = document.createElement("a"); a.href = href; a.textContent = "See what made it"; tip.appendChild(a); }
      guide.hidden = dot.hidden = tip.hidden = false;
      var px = x / 100 * w, tw = tip.offsetWidth, th = tip.offsetHeight;
      var left = px + 14 + tw > w ? px - tw - 14 : px + 14;
      tip.style.left = Math.max(0, left) + "px";
      tip.style.top = Math.max(0, Math.min(h - th, y / 100 * h - th / 2)) + "px";
      Array.prototype.forEach.call(bars, function (b, j) { b.classList.toggle("active", j === i); });
      wrap.classList.toggle("has-active", bars.length > 0);
    }
    function hide() {
      current = -1; pinned = false;
      guide.hidden = dot.hidden = tip.hidden = true;
      wrap.classList.remove("has-active");
      Array.prototype.forEach.call(bars, function (b) { b.classList.remove("active"); });
    }
    function pctOf(ev) { var r = wrap.getBoundingClientRect(); return (ev.clientX - r.left) / r.width * 100; }
    wrap.addEventListener("pointerdown", function (ev) { lastPointer = ev.pointerType || "mouse"; });
    wrap.addEventListener("pointermove", function (ev) {
      if (ev.pointerType === "mouse" && !pinned) { show(nearest(pctOf(ev))); }
    });
    wrap.addEventListener("pointerleave", function (ev) { if (ev.pointerType === "mouse" && !pinned) hide(); });
    wrap.addEventListener("click", function (ev) {
      if (ev.target.closest(".pc-tip")) return;          // the tooltip's own link
      if (lastPointer !== "mouse") {                     // touch / pen: select, don't navigate; the tooltip links on
        ev.preventDefault();
        pinned = true;
        show(nearest(pctOf(ev)));
      }
    });
    document.addEventListener("click", function (ev) { if (pinned && !wrap.contains(ev.target)) hide(); });
    wrap.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
        ev.preventDefault(); pinned = true;
        show(current < 0 ? (ev.key === "ArrowRight" ? 0 : pts.length - 1) : current + (ev.key === "ArrowRight" ? 1 : -1));
      } else if (ev.key === "Home" || ev.key === "End") {
        ev.preventDefault(); pinned = true; show(ev.key === "Home" ? 0 : pts.length - 1);
      } else if (ev.key === "Enter" && current >= 0 && pts[current].getAttribute("data-href")) {
        window.location.href = pts[current].getAttribute("data-href");
      } else if (ev.key === "Escape") { hide(); }
    });
    wrap.addEventListener("blur", function () { if (!pinned) hide(); });
  });

  // Profit table: a year row opens into its months.
  document.querySelectorAll("[data-expand]").forEach(function (b) {
    b.addEventListener("click", function () {
      var target = document.getElementById(b.getAttribute("data-expand"));
      if (!target) return;
      var open = b.getAttribute("aria-expanded") !== "true";
      b.setAttribute("aria-expanded", open ? "true" : "false");
      b.setAttribute("aria-label", (open ? "Hide " : "Show ") + b.getAttribute("aria-label").replace(/^(Show|Hide) /, ""));
      target.hidden = !open;
    });
  });
})();
