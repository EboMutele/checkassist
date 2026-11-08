// The blog editor: rich text in a contenteditable box, a cover image, autosave for drafts.
// The server cleans everything it's sent (app/richtext.py); the clean-up here only keeps the
// editor tidy while you work.
(function () {
  "use strict";
  var form = document.querySelector("[data-editor]");
  if (!form) return;

  var body = form.querySelector("[data-body]");
  var bodyField = form.querySelector("[data-body-field]");
  var statusEl = form.querySelector("[data-save-text]");
  var isDraft = form.dataset.status === "draft";
  var csrf = form.querySelector("input[name=csrf_token]").value;
  var uploadUrl = form.dataset.uploadUrl;
  // Not form.action: the buttons are named "action", which shadows the property.
  var saveUrl = form.getAttribute("action");
  var dirty = false;
  var saving = false;
  var timer = null;
  var savedRange = null;

  try {
    document.execCommand("defaultParagraphSeparator", false, "p");
    document.execCommand("styleWithCSS", false, false);
  } catch (e) { /* older browsers */ }

  // ---------- helpers ----------
  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }
  function blockOf(node) {
    while (node && node !== body) {
      if (node.nodeType === 1 && /^(P|H2|H3|BLOCKQUOTE|PRE|LI|FIGURE|DIV)$/.test(node.tagName)) return node;
      node = node.parentNode;
    }
    return null;
  }
  function insideBody(node) { return node && (node === body || body.contains(node)); }
  // Chrome sometimes puts a new list inside the paragraph it came from; lift it out.
  function liftLists() {
    body.querySelectorAll("p > ul, p > ol").forEach(function (list) {
      var p = list.parentNode;
      p.parentNode.insertBefore(list, p);
      if (!p.textContent.trim() && !p.querySelector("img")) p.parentNode.removeChild(p);
    });
  }
  function saveSelection() {
    var sel = window.getSelection();
    if (sel.rangeCount && insideBody(sel.getRangeAt(0).commonAncestorContainer)) savedRange = sel.getRangeAt(0).cloneRange();
  }
  function restoreSelection() {
    body.focus();
    if (!savedRange) return;
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(savedRange);
  }
  function ensureParagraph() {
    if (!body.innerHTML.trim()) body.innerHTML = "<p><br></p>";
  }
  function updateEmpty() {
    var empty = !body.textContent.trim() && !body.querySelector("img, hr, li");
    body.classList.toggle("is-empty", empty);
  }
  function words() {
    var t = body.innerText || "";
    var n = (t.match(/\S+/g) || []).length;
    var stats = form.querySelector("[data-stats]");
    if (stats) stats.textContent = n + (n === 1 ? " word" : " words") + " · " + Math.max(1, Math.ceil(n / 230)) + " min read";
  }

  // ---------- paste: keep structure, drop the rest ----------
  var KEEP = { P: "p", DIV: "p", H1: "h2", H2: "h2", H3: "h3", H4: "h3", H5: "h3", H6: "h3", BLOCKQUOTE: "blockquote",
    UL: "ul", OL: "ol", LI: "li", PRE: "pre", B: "strong", STRONG: "strong", I: "em", EM: "em", S: "s", DEL: "s",
    STRIKE: "s", CODE: "code", A: "a", BR: "br", HR: "hr" };
  var DROP = /^(SCRIPT|STYLE|IFRAME|OBJECT|EMBED|SVG|MATH|TEMPLATE|NOSCRIPT|HEAD|TITLE|META|LINK|IMG|PICTURE|VIDEO|AUDIO|CANVAS|FORM|INPUT|BUTTON|SELECT|TEXTAREA)$/;
  function safeHref(h) {
    h = (h || "").trim();
    return /^(https?:\/\/|mailto:|\/(?!\/)|#)/i.test(h) && !/[\s<>"']/.test(h) ? h : null;
  }
  function cleanNode(src, out) {
    Array.prototype.forEach.call(src.childNodes, function (n) {
      if (n.nodeType === 3) { out.appendChild(document.createTextNode(n.nodeValue.replace(/\s+/g, " "))); return; }
      if (n.nodeType !== 1 || DROP.test(n.tagName)) return;
      // Google Docs wraps everything in <b style="font-weight:normal">; keep only real bold.
      var tag = KEEP[n.tagName];
      if (tag === "strong" && n.style && n.style.fontWeight === "normal") tag = null;
      if (!tag && n.style && n.style.fontWeight && parseInt(n.style.fontWeight, 10) >= 600) tag = "strong";
      if (!tag && n.style && n.style.fontStyle === "italic") tag = "em";
      if (!tag) { cleanNode(n, out); return; }
      var el = document.createElement(tag);
      if (tag === "a") {
        var href = safeHref(n.getAttribute("href"));
        if (!href) { cleanNode(n, out); return; }
        el.setAttribute("href", href);
      }
      cleanNode(n, el);
      out.appendChild(el);
    });
    return out;
  }
  body.addEventListener("paste", function (e) {
    var data = e.clipboardData;
    if (!data) return;
    e.preventDefault();
    var html = data.getData("text/html");
    if (html) {
      var doc = new DOMParser().parseFromString(html, "text/html");
      var box = cleanNode(doc.body, document.createElement("div"));
      document.execCommand("insertHTML", false, box.innerHTML);
    } else {
      var text = data.getData("text/plain");
      var paras = text.split(/\n\s*\n/);
      if (paras.length > 1) {
        document.execCommand("insertHTML", false, paras.map(function (p) {
          var d = document.createElement("p");
          d.textContent = p.replace(/\n/g, " ").trim();
          return d.outerHTML;
        }).join(""));
      } else {
        document.execCommand("insertText", false, text);
      }
    }
  });

  // ---------- toolbar ----------
  var toolbar = form.querySelector("[data-toolbar]");
  var linkBar = form.querySelector("[data-link-bar]");
  var linkInput = form.querySelector("[data-link-input]");
  var imageFile = form.querySelector("[data-image-file]");

  function currentBlockTag() {
    var v = (document.queryCommandValue("formatBlock") || "").toLowerCase().replace(/[<>]/g, "");
    return v;
  }
  function toggleBlock(tag) {
    var now = currentBlockTag();
    document.execCommand("formatBlock", false, now === tag ? "p" : tag);
  }
  function refreshToolbar() {
    if (!insideBody(window.getSelection().anchorNode)) return;
    var block = currentBlockTag();
    toolbar.querySelectorAll("[data-cmd]").forEach(function (b) {
      var c = b.dataset.cmd, on = false;
      try {
        if (c === "bold" || c === "italic" || c === "strikeThrough") on = document.queryCommandState(c);
        else if (c === "ul") on = document.queryCommandState("insertUnorderedList");
        else if (c === "ol") on = document.queryCommandState("insertOrderedList");
        else if (c === "h2" || c === "h3" || c === "blockquote" || c === "pre") on = block === c;
        else if (c === "p") on = block === "p" || block === "div" || block === "";
        else if (c === "link") on = !!closestTag(window.getSelection().anchorNode, "A");
      } catch (e) { on = false; }
      if (b.hasAttribute("aria-pressed")) b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }
  function closestTag(node, tag) {
    while (node && node !== body) {
      if (node.nodeType === 1 && node.tagName === tag) return node;
      node = node.parentNode;
    }
    return null;
  }
  function openLinkBar() {
    saveSelection();
    var a = savedRange && closestTag(savedRange.startContainer, "A");
    linkInput.value = a ? a.getAttribute("href") : "";
    linkBar.hidden = false;
    linkInput.focus();
  }
  function applyLink() {
    var url = linkInput.value.trim();
    if (url && !/^(https?:|mailto:|\/|#)/i.test(url)) url = (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(url) ? "mailto:" : "https://") + url;
    restoreSelection();
    if (url && safeHref(url)) {
      if (window.getSelection().isCollapsed) {
        var a = closestTag(window.getSelection().anchorNode, "A");
        if (a) a.setAttribute("href", url);
        else document.execCommand("insertHTML", false, '<a href="' + url.replace(/"/g, "&quot;") + '">' + url.replace(/</g, "&lt;") + "</a>");
      } else {
        document.execCommand("createLink", false, url);
      }
    }
    linkBar.hidden = true;
    linkInput.value = "";
    changed();
  }

  toolbar.addEventListener("mousedown", function (e) {
    if (e.target.closest("button")) e.preventDefault();  // keep the selection in the body
  });
  toolbar.addEventListener("click", function (e) {
    var b = e.target.closest("[data-cmd]");
    if (!b) return;
    var c = b.dataset.cmd;
    if (!insideBody(window.getSelection().anchorNode) && c !== "image") { body.focus(); }
    if (c === "bold" || c === "italic" || c === "strikeThrough") document.execCommand(c);
    else if (c === "p") document.execCommand("formatBlock", false, "p");
    else if (c === "h2" || c === "h3" || c === "blockquote" || c === "pre") toggleBlock(c);
    else if (c === "ul") { document.execCommand("insertUnorderedList"); liftLists(); }
    else if (c === "ol") { document.execCommand("insertOrderedList"); liftLists(); }
    else if (c === "hr") { document.execCommand("insertHorizontalRule"); document.execCommand("formatBlock", false, "p"); }
    else if (c === "link") { openLinkBar(); return; }
    else if (c === "image") { saveSelection(); imageFile.click(); return; }
    refreshToolbar();
    changed();
  });
  form.querySelector("[data-link-apply]").addEventListener("click", applyLink);
  form.querySelector("[data-link-remove]").addEventListener("click", function () {
    restoreSelection();
    var a = closestTag(window.getSelection().anchorNode, "A");
    if (a && window.getSelection().isCollapsed) {
      var r = document.createRange(); r.selectNodeContents(a);
      window.getSelection().removeAllRanges(); window.getSelection().addRange(r);
    }
    document.execCommand("unlink");
    linkBar.hidden = true;
    changed();
  });
  linkInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); applyLink(); }
    if (e.key === "Escape") { linkBar.hidden = true; restoreSelection(); }
  });

  // Keyboard: Ctrl/Cmd+K for links; markdown-style shortcuts at the start of an empty line.
  body.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openLinkBar(); return; }
    // Enter on an empty quote, heading or code line goes back to normal text, like most editors.
    if (e.key === "Enter" && !e.shiftKey) {
      var block = blockOf(window.getSelection().anchorNode);
      if (block && /^(BLOCKQUOTE|H2|H3|PRE)$/.test(block.tagName) && !block.textContent.replace(/\u00a0/g, "").trim()) {
        e.preventDefault();
        document.execCommand("formatBlock", false, "p");
        changed();
      }
    }
  });
  var SHORTCUTS = [[/^#{3}$/, "h3"], [/^#{1,2}$/, "h2"], [/^>$/, "blockquote"], [/^[-*]$/, "ul"], [/^1[.)]$/, "ol"], [/^```$/, "pre"]];
  body.addEventListener("input", function (e) {
    if (e.inputType === "insertText" && e.data === " ") {
      var block = blockOf(window.getSelection().anchorNode);
      if (block && /^(P|DIV)$/.test(block.tagName)) {
        var text = block.textContent.replace(/ /g, " ");
        for (var i = 0; i < SHORTCUTS.length; i++) {
          var m = text.slice(0, -1);
          if (text.slice(-1) === " " && SHORTCUTS[i][0].test(m)) {
            block.textContent = "";
            block.appendChild(document.createElement("br"));
            var r = document.createRange(); r.setStart(block, 0); r.collapse(true);
            window.getSelection().removeAllRanges(); window.getSelection().addRange(r);
            var tag = SHORTCUTS[i][1];
            if (tag === "ul") document.execCommand("insertUnorderedList");
            else if (tag === "ol") document.execCommand("insertOrderedList");
            else document.execCommand("formatBlock", false, tag);
            liftLists();
            break;
          }
        }
      }
    }
    changed();
  });
  document.addEventListener("selectionchange", refreshToolbar);

  // ---------- images ----------
  function upload(file, done) {
    var data = new FormData();
    data.append("image", file);
    setStatus("Uploading image…");
    fetch(uploadUrl, { method: "POST", body: data, headers: { "X-CSRF-Token": csrf }, credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.j.ok) throw new Error(res.j.error || "Upload failed.");
        setStatus(isDraft ? "Image added" : "Unsaved changes");
        done(res.j);
      })
      .catch(function (err) { setStatus(err.message || "Upload failed. Try again."); window.alert(err.message || "Upload failed."); });
  }
  imageFile.addEventListener("change", function () {
    var file = imageFile.files[0];
    imageFile.value = "";
    if (!file) return;
    upload(file, function (j) {
      restoreSelection();
      var block = savedRange && blockOf(savedRange.startContainer);
      var html = '<figure><img src="' + j.url + '" alt=""><figcaption></figcaption></figure><p><br></p>';
      if (block && block.tagName === "P" && !block.textContent.trim()) {
        block.insertAdjacentHTML("beforebegin", html.replace("<p><br></p>", ""));
      } else {
        document.execCommand("insertHTML", false, html);
      }
      changed();
    });
  });

  // Cover image
  var cover = form.querySelector("[data-cover]");
  var coverName = form.querySelector("[data-cover-name]");
  var coverImg = form.querySelector("[data-cover-img]");
  var coverFile = form.querySelector("[data-cover-file]");
  var coverAlt = form.querySelector("[data-cover-alt]");
  function setCover(name, url) {
    coverName.value = name || "";
    coverImg.hidden = !name;
    coverAlt.hidden = !name;
    if (url) coverImg.src = url; else coverImg.removeAttribute("src");
    cover.classList.toggle("has-image", !!name);
    changed();
  }
  form.querySelectorAll("[data-cover-pick]").forEach(function (b) {
    b.addEventListener("click", function () { coverFile.click(); });
  });
  form.querySelector("[data-cover-remove]").addEventListener("click", function () { setCover("", ""); });
  coverFile.addEventListener("change", function () {
    var file = coverFile.files[0];
    coverFile.value = "";
    if (file) upload(file, function (j) { setCover(j.name, j.url); });
  });
  ["dragenter", "dragover"].forEach(function (t) {
    cover.addEventListener(t, function (e) { e.preventDefault(); cover.classList.add("is-over"); });
  });
  ["dragleave", "drop"].forEach(function (t) {
    cover.addEventListener(t, function () { cover.classList.remove("is-over"); });
  });
  cover.addEventListener("drop", function (e) {
    e.preventDefault();
    var file = e.dataTransfer.files[0];
    if (file) upload(file, function (j) { setCover(j.name, j.url); });
  });

  // ---------- title, subtitle, address, description ----------
  form.querySelectorAll("[data-autogrow]").forEach(function (t) {
    function grow() { t.style.height = "auto"; t.style.height = t.scrollHeight + "px"; }
    t.addEventListener("input", grow);
    t.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        if (t.name === "title") form.querySelector("[name=subtitle]").focus();
        else { body.focus(); }
      }
    });
    grow();
  });
  var slug = form.querySelector("[data-slug]");
  var title = form.querySelector("[name=title]");
  function slugify(s) {
    return s.toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 70).replace(/-+$/, "");
  }
  title.addEventListener("input", function () { if (slug.dataset.auto) slug.value = slugify(title.value); });
  slug.addEventListener("input", function () { delete slug.dataset.auto; });
  slug.addEventListener("change", function () { slug.value = slugify(slug.value); });
  var excerpt = form.querySelector("[data-count]");
  var countOut = form.querySelector("[data-count-out]");
  excerpt.addEventListener("input", function () {
    countOut.textContent = excerpt.value.length;
    countOut.parentNode.classList.toggle("is-over", excerpt.value.length > parseInt(excerpt.dataset.count, 10));
  });

  // Publish time: shown in the admin's own time zone, sent as UTC.
  var when = form.querySelector("[data-when]");
  var whenUtc = form.querySelector("[data-when-utc]");
  function pad(n) { return ("0" + n).slice(-2); }
  if (when.dataset.utc) {
    var d = new Date(when.dataset.utc);
    if (!isNaN(d)) when.value = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function syncWhen() {
    var v = when.value ? new Date(when.value) : null;
    whenUtc.value = v && !isNaN(v) ? v.toISOString().replace(/\.\d{3}Z$/, "Z") : "";
    var publish = form.querySelector("[data-publish]");
    if (publish) publish.textContent = v && v > new Date() ? "Schedule" : "Publish";
  }
  when.addEventListener("input", syncWhen);
  syncWhen();

  // ---------- saving ----------
  function serialize() {
    ensureParagraph();
    bodyField.value = body.innerHTML;
    syncWhen();
  }
  function changed() {
    dirty = true;
    updateEmpty();
    words();
    if (isDraft) {
      clearTimeout(timer);
      timer = setTimeout(autosave, 1500);
      setStatus("Editing…");
    } else {
      setStatus("Unsaved changes");
      var s = form.querySelector("[data-save-status]");
      if (s && !s.querySelector("[data-save-text]")) {
        statusEl = document.createElement("span");
        statusEl.setAttribute("data-save-text", "");
        s.appendChild(document.createTextNode(" "));
        s.appendChild(statusEl);
        setStatus("Unsaved changes");
      }
    }
  }
  function autosave() {
    if (!isDraft || !dirty || saving) return;
    saving = true;
    dirty = false;
    serialize();
    var data = new FormData(form);
    data.set("action", "autosave");
    data.delete("publish_at");
    setStatus("Saving…");
    fetch(saveUrl, { method: "POST", body: data, headers: { "X-CSRF-Token": csrf, "Accept": "application/json" }, credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        saving = false;
        if (!j.ok) { dirty = true; setStatus(j.error || "Couldn't save"); return; }
        setStatus("Saved");
        if (slug.dataset.auto && j.slug && j.slug.indexOf("draft-") !== 0) slug.value = j.slug;
        if (dirty) { clearTimeout(timer); timer = setTimeout(autosave, 1500); }
      })
      .catch(function () { saving = false; dirty = true; setStatus("Offline: not saved yet"); });
  }
  form.querySelectorAll("input, textarea, select").forEach(function (el) {
    if (el === bodyField || el.type === "hidden" || el.type === "file") return;
    el.addEventListener("input", changed);
  });
  form.addEventListener("submit", function () {
    serialize();
    dirty = false;
    clearTimeout(timer);
  });
  window.addEventListener("beforeunload", function (e) {
    if (dirty && !isDraft) { e.preventDefault(); e.returnValue = ""; }
    if (dirty && isDraft) {  // last-chance save
      serialize();
      var data = new FormData(form);
      data.set("action", "autosave");
      data.delete("publish_at");
      if (navigator.sendBeacon) navigator.sendBeacon(saveUrl, data);
    }
  });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden" && dirty && isDraft) autosave();
  });

  // Preview shows the saved version, so save first.
  var previewLink = form.querySelector("[data-preview]");
  previewLink.addEventListener("click", function () { if (isDraft && dirty) autosave(); });

  ensureParagraph();
  updateEmpty();
  words();
})();
