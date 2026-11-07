// Small progressive enhancements. Every page works without this file.
(function () {
  "use strict";

  function formatSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  // Drag-and-drop file picker that shows the chosen file.
  document.querySelectorAll("[data-dropzone]").forEach(function (zone) {
    var input = zone.querySelector("input[type=file]");
    var text = zone.querySelector("[data-dropzone-text]");
    var original = text.innerHTML;
    function show() {
      var f = input.files && input.files[0];
      if (f) {
        text.textContent = f.name + " (" + formatSize(f.size) + ")";
        zone.classList.add("has-file");
      } else {
        text.innerHTML = original;
        zone.classList.remove("has-file");
      }
    }
    input.addEventListener("change", show);
    ["dragenter", "dragover"].forEach(function (t) {
      zone.addEventListener(t, function () { zone.classList.add("is-over"); });
    });
    ["dragleave", "drop"].forEach(function (t) {
      zone.addEventListener(t, function () { zone.classList.remove("is-over"); });
    });
  });

  // Confirm before destructive or customer-visible actions.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var submitter = e.submitter;
    var message = (submitter && submitter.dataset.confirm) || form.dataset.confirm;
    if (message && !window.confirm(message)) {
      e.preventDefault();
      return;
    }
    // Stop double submits. Disable after the browser has captured the clicked button's value.
    setTimeout(function () {
      form.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = true; });
    }, 0);
  });

  // Hide "mark as waiting on customer" when writing an internal note.
  document.querySelectorAll("[data-compose]").forEach(function (form) {
    var ask = form.querySelector("[data-ask]");
    if (!ask) return;
    function sync() {
      var internal = form.querySelector("input[name=visibility]:checked").value === "internal";
      ask.hidden = internal;
      if (internal) ask.querySelector("input").checked = false;
    }
    form.addEventListener("change", sync);
    sync();
  });

  // Re-enable buttons if the page is restored from the back/forward cache.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("button[type=submit][disabled]").forEach(function (b) { b.disabled = false; });
  });
})();

// Show times in the viewer's own time zone (the server renders UTC as a fallback).
(function () {
  "use strict";
  var formats = {
    time: { hour: "numeric", minute: "2-digit" },
    datetime: { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }
  };
  document.querySelectorAll("time[data-local]").forEach(function (el) {
    var d = new Date(el.getAttribute("datetime"));
    var opts = formats[el.dataset.local];
    if (isNaN(d.getTime()) || !opts) return;
    try { el.textContent = new Intl.DateTimeFormat(undefined, opts).format(d); } catch (e) { /* keep UTC */ }
  });
})();

// Keep waiting pages fresh without losing anything the person is typing.
(function () {
  "use strict";
  var marker = document.querySelector("[data-autorefresh]");
  if (!marker) return;
  var seconds = Math.max(30, parseInt(marker.dataset.autorefresh, 10) || 60);
  function busy() {
    var a = document.activeElement;
    if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) && a.type !== "hidden") return true;
    return Array.prototype.some.call(document.querySelectorAll("textarea, input[type=file]"), function (el) {
      return el.value && el.value.length > 0;
    });
  }
  setInterval(function () {
    if (document.visibilityState === "visible" && !busy()) window.location.reload();
  }, seconds * 1000);
})();
