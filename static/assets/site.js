(function () {
  "use strict";

  // ---- mobile navigation
  var toggle = document.querySelector(".navtoggle"), nav = document.getElementById("nav");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", open);
      document.body.style.overflow = open ? "hidden" : "";
    });
    nav.querySelectorAll(".subtoggle").forEach(function (b) {
      b.addEventListener("click", function () {
        var sub = b.nextElementSibling, open = sub.classList.toggle("is-open");
        b.setAttribute("aria-expanded", open);
      });
    });
  }

  function post(url, data) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(data)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok || !j.ok) throw new Error(j.error || "Something went wrong. Please call us instead.");
        return j;
      });
    });
  }
  function formData(form) {
    var o = {};
    new FormData(form).forEach(function (v, k) { o[k] = v; });
    return o;
  }
  function msg(form, text, ok) {
    var m = form.querySelector(".formmsg");
    if (m) { m.textContent = text; m.className = "formmsg " + (ok ? "is-ok" : "is-err"); }
  }

  // ---- lead / contact forms
  document.querySelectorAll("form[data-ajax]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var btn = form.querySelector("button[type=submit]");
      btn.disabled = true;
      post(form.action, formData(form)).then(function () {
        window.location.href = "/thank-you/";
      }).catch(function (err) {
        msg(form, err.message, false);
        btn.disabled = false;
      });
    });
  });

  // ---- feedback / review flow
  var fb = document.getElementById("fb");
  if (fb) {
    var params = new URLSearchParams(location.search);
    var who = (params.get("n") || params.get("name") || "").slice(0, 60);
    var ref = (params.get("ref") || params.get("p") || "").slice(0, 80);
    if (who) { var hi = fb.querySelector(".fb__hi"); hi.textContent = "Hi " + who + ","; hi.hidden = false; }
    var state = { id: null, rating: 0 };

    function show(step) {
      fb.querySelectorAll(".fb__step").forEach(function (s) { s.hidden = s.getAttribute("data-step") !== step; });
      fb.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    var stars = fb.querySelectorAll(".star");
    function paint(n) { stars.forEach(function (s, i) { s.classList.toggle("is-on", i < n); }); }
    stars.forEach(function (s) {
      var v = +s.getAttribute("data-v");
      s.addEventListener("mouseenter", function () { paint(v); });
      s.addEventListener("mouseleave", function () { paint(state.rating); });
      s.addEventListener("click", function () {
        state.rating = v; paint(v);
        stars.forEach(function (x) { x.setAttribute("aria-checked", x === s); });
        post("/api/feedback", { rating: v, name: who, ref: ref, page: location.pathname })
          .then(function (j) { state.id = j.id; }).catch(function () {});
        setTimeout(function () { show(v >= 4 ? "happy" : "unhappy"); }, 250);
      });
    });
    fb.querySelectorAll("[data-track=google]").forEach(function (a) {
      a.addEventListener("click", function () {
        if (navigator.sendBeacon) {
          navigator.sendBeacon("/api/feedback/click", new Blob([JSON.stringify({ id: state.id })], { type: "application/json" }));
        }
      });
    });
    fb.querySelectorAll("form[data-ajax-feedback]").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        var d = formData(form);
        d.id = state.id; d.rating = state.rating; d.ref = ref;
        if (!d.name) d.name = who;
        var btn = form.querySelector("button[type=submit]"); btn.disabled = true;
        post("/api/feedback", d).then(function () {
          if (state.rating >= 4) { msg(form, "Thank you, your note was sent to our team.", true); }
          else { show("done"); }
        }).catch(function (err) { msg(form, err.message, false); btn.disabled = false; });
      });
    });
  }
})();
