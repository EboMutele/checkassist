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
    date: { month: "short", day: "numeric", year: "numeric" },
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

// Live stopwatches: time taken so far on the customer's page, dashboard rows and the reviewers' queue.
// Never shows time remaining. Uses the server's clock, so a device set to the wrong time still counts right.
(function () {
  "use strict";
  var timers = document.querySelectorAll("[data-countup]");
  if (!timers.length) return;
  var serverNow = Date.parse(document.body.dataset.serverNow || "");
  var offset = isNaN(serverNow) ? 0 : serverNow - Date.now();
  var baseTitle = document.title;
  function pad(n) { return ("0" + n).slice(-2); }
  function clock(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return (h ? h + ":" + pad(m) : String(m)) + ":" + pad(s % 60);
  }
  function tick() {
    var now = Date.now() + offset;
    var title = null;
    timers.forEach(function (el) {
      var elapsed = now - Date.parse(el.dataset.countup);
      if (isNaN(elapsed)) return;
      var text = el.querySelector("[data-countup-text]") || el;
      text.textContent = clock(elapsed) + (el.dataset.suffix || "");
      var ring = el.querySelector(".timer-progress");  // sweeps once a minute, like a second hand
      if (ring) ring.setAttribute("stroke-dashoffset", String(60 - (Math.floor(Math.max(0, elapsed) / 1000) % 60)));
      if (el.hasAttribute("data-reviewer") && el.dataset.limit) {
        var limit = Number(el.dataset.limit) * 1000;
        el.classList.toggle("due-soon", elapsed >= limit - 5 * 60000 && elapsed <= limit);
        el.classList.toggle("due-late", elapsed > limit);
        var row = el.closest("tr");
        if (row) row.classList.toggle("row-late", elapsed > limit);
      }
      if (el.hasAttribute("data-title-countup")) title = clock(elapsed);
    });
    document.title = title ? title + " – " + baseTitle : baseTitle;
  }
  tick();
  setInterval(tick, 1000);
})();

// While a document is open, notice the moment its status changes (picked up, question, feedback sent).
(function () {
  "use strict";
  var w = document.querySelector("[data-watch-status]");
  if (!w || !window.fetch) return;
  var current = w.dataset.status;
  var every = Math.max(5, parseInt(w.dataset.every, 10) || 15) * 1000;
  var done = false;
  function typing() {
    var a = document.activeElement;
    if (a && /^(TEXTAREA|INPUT|SELECT)$/.test(a.tagName) && a.type !== "hidden") return true;
    return Array.prototype.some.call(document.querySelectorAll("textarea"), function (t) { return t.value.length > 0; });
  }
  function announce() {
    var main = document.getElementById("main");
    if (!main || document.querySelector("[data-status-banner]")) return;
    var p = document.createElement("p");
    p.className = "flash flash-info";
    p.setAttribute("role", "status");
    p.setAttribute("data-status-banner", "");
    p.textContent = "There's an update on this document. ";
    var a = document.createElement("a");
    a.href = window.location.pathname;
    a.textContent = "Refresh to see it";
    p.appendChild(a);
    main.insertBefore(p, main.firstChild);
  }
  function check() {
    if (done || document.visibilityState !== "visible") return;
    fetch(w.dataset.watchStatus, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || d.status === current) return;
        done = true;
        if (typing()) announce(); else window.location.reload();
      })
      .catch(function () { /* offline for a moment; try again next tick */ });
  }
  setInterval(check, every);
  document.addEventListener("visibilitychange", check);
  // Attachments on messages: paste a screenshot into the message, drop files on the form, preview, remove.
  document.querySelectorAll("[data-attach]").forEach(function (box) {
    var input = box.querySelector("input[type=file]");
    var list = box.querySelector("[data-attach-list]");
    var form = box.closest("form");
    var textarea = form && form.querySelector("textarea");
    var MAX = 5;
    var canSet = (function () { try { return !!new DataTransfer(); } catch (e) { return false; } })();

    function current() { return Array.prototype.slice.call(input.files || []); }
    function render() {
      list.textContent = "";
      current().forEach(function (file, i) {
        var li = document.createElement("li");
        li.className = "attach-item";
        if (/^image\/(png|jpeg|webp)$/.test(file.type)) {
          var img = document.createElement("img");
          img.alt = "";
          var reader = new FileReader();
          reader.onload = function () { img.src = reader.result; };  // data: URL (the page's CSP allows data: images)
          reader.readAsDataURL(file);
          li.appendChild(img);
        }
        var name = document.createElement("span");
        name.textContent = file.name;
        li.appendChild(name);
        if (canSet) {
          var remove = document.createElement("button");
          remove.type = "button";
          remove.className = "link-button";
          remove.textContent = "Remove";
          remove.setAttribute("aria-label", "Remove " + file.name);
          remove.addEventListener("click", function () {
            var files = current();
            files.splice(i, 1);
            setFiles(files);
          });
          li.appendChild(remove);
        }
        list.appendChild(li);
      });
    }
    function setFiles(files) {
      var dt = new DataTransfer();
      files.slice(0, MAX).forEach(function (f) { dt.items.add(f); });
      input.files = dt.files;
      render();
    }
    function add(files) {
      if (!canSet || !files.length) return false;
      setFiles(current().concat(files));
      return true;
    }

    input.addEventListener("change", render);

    if (textarea) {
      textarea.addEventListener("paste", function (ev) {
        var pasted = Array.prototype.slice.call((ev.clipboardData && ev.clipboardData.files) || []);
        var images = pasted.filter(function (f) { return /^image\/(png|jpeg|webp)$/.test(f.type); });
        if (!images.length) return;  // ordinary text paste
        var stamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
        var already = current().length;
        var named = images.map(function (f, n) {
          var ext = f.type.split("/")[1].replace("jpeg", "jpg");
          return new File([f], "screenshot-" + stamp + "-" + (already + n + 1) + "." + ext, { type: f.type });
        });
        if (add(named)) ev.preventDefault();
      });
    }

    function hasFiles(ev) {
      return ev.dataTransfer && Array.prototype.indexOf.call(ev.dataTransfer.types || [], "Files") !== -1;
    }
    form.addEventListener("dragover", function (ev) {
      if (!hasFiles(ev) || !canSet) return;
      ev.preventDefault();
      form.classList.add("is-over");
    });
    form.addEventListener("dragleave", function (ev) {
      if (!form.contains(ev.relatedTarget)) form.classList.remove("is-over");
    });
    form.addEventListener("drop", function (ev) {
      form.classList.remove("is-over");
      if (!hasFiles(ev) || !canSet) return;
      ev.preventDefault();
      add(Array.prototype.slice.call(ev.dataTransfer.files));
    });
  });

})();
