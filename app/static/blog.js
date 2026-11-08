// Article pages: copy-link button and the table of contents' current section.
(function () {
  "use strict";
  document.querySelectorAll("[data-copy-link]").forEach(function (b) {
    b.addEventListener("click", function () {
      var url = b.dataset.copyLink, label = b.textContent;
      function done() { b.textContent = "Link copied"; setTimeout(function () { b.textContent = label; }, 2000); }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(done, function () { window.prompt("Copy this link", url); });
      } else {
        window.prompt("Copy this link", url);
      }
    });
  });

  var links = Array.prototype.slice.call(document.querySelectorAll("[data-toc-link]"));
  if (!links.length) return;
  var heads = links.map(function (a) { return document.getElementById(decodeURIComponent(a.hash.slice(1))); });
  var queued = false;
  function mark() {
    queued = false;
    var current = -1;
    for (var i = 0; i < heads.length; i++) {
      if (heads[i] && heads[i].getBoundingClientRect().top < window.innerHeight * 0.3) current = i;
    }
    links.forEach(function (a, i) {
      if (i === current) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current");
    });
  }
  window.addEventListener("scroll", function () {
    if (!queued) { queued = true; window.requestAnimationFrame(mark); }
  }, { passive: true });
  mark();
})();
