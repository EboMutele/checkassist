// Mobile number field: keeps the compact "🇰🇪 +254" label in step with the country picker, pre-selects
// the visitor's country from their time zone (phone-zones.js) when the server only guessed it, and
// follows a pasted or autofilled "+44 …" number. The form works without this file.
(function () {
  "use strict";

  function zoneCountry() {
    try {
      var zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      return (window.PHONE_ZONES || {})[zone] || null;
    } catch (e) {
      return null;
    }
  }

  document.querySelectorAll("[data-phone]").forEach(function (box) {
    var select = box.querySelector("select");
    var input = box.querySelector("input");
    var shown = box.querySelector("[data-phone-shown]");

    function option() { return select.options[select.selectedIndex]; }
    function sync() {
      var o = option();
      shown.textContent = o.getAttribute("data-flag") + " +" + o.getAttribute("data-dial");
      input.placeholder = o.getAttribute("data-example") || "";
    }

    if (select.hasAttribute("data-guess")) {
      var iso = zoneCountry();
      if (iso && select.querySelector('option[value="' + iso + '"]')) select.value = iso;
    }
    sync();

    select.addEventListener("change", function () {
      select.removeAttribute("data-guess");
      sync();
    });

    // "+44 7700 900123" or "0044…": switch the picker to that code (the server reads it either way)
    input.addEventListener("input", function () {
      var digits = input.value.replace(/[^\d+]/g, "");
      if (digits.indexOf("00") === 0) digits = "+" + digits.slice(2);
      if (digits.charAt(0) !== "+") return;
      digits = digits.slice(1);
      if (digits.indexOf(option().getAttribute("data-dial")) === 0) return; // keeps Canada for +1, Guernsey for +44
      for (var n = 1; n <= 3 && n <= digits.length; n++) {
        var match = select.querySelector('option[data-main][data-dial="' + digits.slice(0, n) + '"]');
        if (match) {
          select.value = match.value;
          sync();
          return;
        }
      }
    });
  });
})();
