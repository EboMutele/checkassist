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
