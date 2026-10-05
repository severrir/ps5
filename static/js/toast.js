/* ===========================================================================
   Toast notifications.

   Non-blocking by design: the hub must never open a native alert(), confirm() or
   prompt(). In the console's browser a modal dialog blocks the whole page, and
   with no mouse it can be genuinely hard to dismiss from the sofa.

   Identical consecutive messages collapse into a counter instead of stacking, so a
   retry loop cannot bury the screen.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var MAX_VISIBLE = 4;
  var DEFAULT_MS = 4200;

  var container = null;
  var last = null;

  function host() {
    if (!container) container = document.getElementById("toasts");
    return container;
  }

  function dismiss(node) {
    if (!node || !node.parentNode) return;
    node.classList.add("is-leaving");
    window.setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
      if (last && last.node === node) last = null;
    }, 220);
  }

  function show(message, type, options) {
    var root = host();
    if (!root) return null;

    var text = String(message === undefined || message === null ? "" : message);
    if (!text) return null;
    var kind = type || "info";
    var settings = options || {};
    var duration = settings.duration === undefined ? DEFAULT_MS : settings.duration;

    /* Collapse a repeat of the message currently on screen. */
    if (last && last.text === text && last.node.parentNode) {
      last.count += 1;
      last.countNode.textContent = " x" + last.count;
      window.clearTimeout(last.timer);
      if (duration > 0) {
        last.timer = window.setTimeout(function () {
          dismiss(last.node);
        }, duration);
      }
      return last.node;
    }

    var fmt = window.HUB.fmt;
    var countNode = fmt.el("span", { class: "muted small" });
    var node = fmt.el("div.toast", { class: "toast--" + kind, role: "status" }, [
      fmt.el("i.toast__dot"),
      fmt.el("div", null, [fmt.el("span", { text: text }), countNode])
    ]);

    root.appendChild(node);

    while (root.children.length > MAX_VISIBLE) {
      root.removeChild(root.firstChild);
    }

    var timer = 0;
    if (duration > 0) {
      timer = window.setTimeout(function () {
        dismiss(node);
      }, duration);
    }
    last = { text: text, node: node, count: 1, countNode: countNode, timer: timer };
    return node;
  }

  var toast = {
    show: show,
    info: function (message, options) {
      return show(message, "info", options);
    },
    ok: function (message, options) {
      return show(message, "ok", options);
    },
    warn: function (message, options) {
      return show(message, "warn", options);
    },
    error: function (message, options) {
      return show(message, "error", options);
    },

    /* Standard handler for a rejected api promise. */
    fromError: function (error, prefix) {
      var message = error && error.message ? error.message : "Something went wrong";
      return show((prefix ? prefix + ": " : "") + message, "error", { duration: 6500 });
    },

    setPosition: function (position) {
      var root = host();
      if (root) root.setAttribute("data-position", position === "top-center" ? "top-center" : "bottom-right");
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.toast = toast;
})(window, document);
