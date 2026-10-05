/* ===========================================================================
   Localisation.

   Every user-facing string that lives in markup carries data-i18n="<key>", and
   code calls HUB.i18n.t("<key>", "<fallback>"). Adding a language means adding one
   object to STRINGS -- no build step, no extra request, nothing fetched.

   The fallback argument is deliberate: a missing key renders the English text
   rather than a bare key, so a partial translation degrades gracefully.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var STRINGS = {
    en: {
      label: "English",
      "nav.library": "Library",
      "nav.queue": "Queue",
      "nav.console": "Console",
      "nav.browser": "Browser",
      "nav.safety": "Safety",
      "nav.recovery": "Recovery",
      "nav.settings": "Settings",
      "sub.library": "Packages found on this PC, ready to send to the console.",
      "sub.queue": "What is installing now, what is waiting, and what finished.",
      "sub.console": "Storage, hardware health and the etaHEN connection.",
      "sub.browser": "Embedded browsing, with the console's own renderer.",
      "sub.safety": "What is verified, what is not, and what you still need to do.",
      "sub.recovery": "Emergency wipe and the Safe Mode route back.",
      "sub.settings": "Everything in configs.json that is safe to change from here.",
      "hint.move": "D-pad Move",
      "hint.select": "Cross Select",
      "hint.back": "Circle Back",
      "hint.pin": "Square Pin",
      "hint.queue": "Triangle Queue",
      "hint.tabs": "L1/R1 Tabs",
      "hint.menu": "Options Menu",
      "state.queued": "Queued",
      "state.starting": "Starting",
      "state.installing": "Installing",
      "state.paused": "Paused",
      "state.retrying": "Retrying",
      "state.done": "Done",
      "state.failed": "Failed",
      "state.cancelled": "Cancelled"
    },

    /* Georgian. Nav and status only; untranslated keys fall back to English. */
    ka: {
      label: "ქართული",
      "nav.library": "ბიბლიოთეკა",
      "nav.queue": "რიგი",
      "nav.console": "კონსოლი",
      "nav.browser": "ბრაუზერი",
      "nav.safety": "უსაფრთხოება",
      "nav.recovery": "აღდგენა",
      "nav.settings": "ჩვეულებები",
      "state.installing": "ინსტალირდება",
      "state.done": "დამთავრდა",
      "state.failed": "ათამება"
    },

    /* Russian, same scope. */
    ru: {
      label: "Русский",
      "nav.library": "Библиотека",
      "nav.queue": "Очередь",
      "nav.console": "Консоль",
      "nav.browser": "Браузер",
      "nav.safety": "Безопасность",
      "nav.recovery": "Восстановление",
      "nav.settings": "Настройки",
      "state.installing": "Установка",
      "state.done": "Готово",
      "state.failed": "Ошибка"
    }
  };

  var current = "en";

  var i18n = {
    strings: STRINGS,

    languages: function () {
      return Object.keys(STRINGS).map(function (code) {
        return { code: code, label: STRINGS[code].label || code };
      });
    },

    language: function () {
      return current;
    },

    setLanguage: function (code) {
      current = STRINGS[code] ? code : "en";
      document.documentElement.setAttribute("lang", current);
      i18n.apply(document);
      return current;
    },

    t: function (key, fallback) {
      var table = STRINGS[current] || {};
      if (table[key]) return table[key];
      if (STRINGS.en[key]) return STRINGS.en[key];
      return fallback !== undefined ? fallback : key;
    },

    /* Replace the text of every [data-i18n] inside root. */
    apply: function (root) {
      var nodes = (root || document).querySelectorAll("[data-i18n]");
      for (var i = 0; i < nodes.length; i += 1) {
        var node = nodes[i];
        node.textContent = i18n.t(node.getAttribute("data-i18n"), node.textContent);
      }
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.i18n = i18n;
})(window, document);
