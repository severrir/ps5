/* ===========================================================================
   On-screen keyboard.

   The console's own text-entry panel is hard to reach from a web page and varies by
   firmware, so the hub brings its own. Any text input or textarea marked
   data-focusable opens this when activated; the keys are ordinary focusable
   elements, so the same spatial navigation drives them.

   While it is open a scope is pushed, which traps focus inside the keyboard. A
   physical keyboard also works, for development at the desk.
   =========================================================================== */
(function (window, document) {
  "use strict";

  /* Rows of [lower, upper]. "wide" keys span two columns. */
  var LAYOUT = [
    [
      ["1", "!"], ["2", "@"], ["3", "#"], ["4", "$"], ["5", "%"],
      ["6", "^"], ["7", "&"], ["8", "*"], ["9", "("], ["0", ")"]
    ],
    [
      ["q", "Q"], ["w", "W"], ["e", "E"], ["r", "R"], ["t", "T"],
      ["y", "Y"], ["u", "U"], ["i", "I"], ["o", "O"], ["p", "P"]
    ],
    [
      ["a", "A"], ["s", "S"], ["d", "D"], ["f", "F"], ["g", "G"],
      ["h", "H"], ["j", "J"], ["k", "K"], ["l", "L"], [":", ";"]
    ],
    [
      ["z", "Z"], ["x", "X"], ["c", "C"], ["v", "V"], ["b", "B"],
      ["n", "N"], ["m", "M"], [",", "<"], [".", ">"], ["/", "?"]
    ],
    [
      ["-", "_"], ["+", "="], ["@", "@"], [":", ":"], ["//", "//"],
      ["~", "`"], ["'", "\""], ["[", "{"], ["]", "}"], ["\\", "|"]
    ]
  ];

  var root = null;
  var preview = null;
  var rowsHost = null;
  var labelHost = null;

  var target = null;
  var buffer = "";
  var shifted = false;
  var open = false;
  var keyHandler = null;

  function fmt() {
    return window.HUB.fmt;
  }

  function renderPreview() {
    var f = fmt();
    /* A PIN typed on a TV is visible to the whole room, so password fields show
       bullets here even though the buffer keeps the real characters. */
    var masked = preview.getAttribute("data-mask") === "1";
    var shown = masked ? new Array(buffer.length + 1).join("•") : buffer;
    f.fill(preview, [document.createTextNode(shown), f.el("span.caret")]);
  }

  function insert(text) {
    buffer += text;
    renderPreview();
    window.HUB.sfx.toggle();
  }

  function backspace() {
    buffer = buffer.slice(0, -1);
    renderPreview();
    window.HUB.sfx.back();
  }

  function keyButton(label, options) {
    var settings = options || {};
    var f = fmt();
    var node = f.el("button.osk__key", {
      type: "button",
      "data-focusable": true,
      "data-nav-group": "osk",
      class: settings.className || "",
      text: label
    });
    node.addEventListener("click", settings.onClick);
    return node;
  }

  function render() {
    var f = fmt();
    var children = [];

    LAYOUT.forEach(function (row) {
      var rowNode = f.el("div.osk__row");
      row.forEach(function (pair) {
        var value = shifted ? pair[1] : pair[0];
        rowNode.appendChild(
          keyButton(value, {
            onClick: function () {
              insert(value);
            }
          })
        );
      });
      children.push(rowNode);
    });

    var actions = f.el("div.osk__row");
    actions.appendChild(
      keyButton(shifted ? "Shift ON" : "Shift", {
        className: shifted ? "osk__key--action" : "",
        onClick: function () {
          shifted = !shifted;
          render();
          window.HUB.nav.focusGroup("osk", { silent: true });
        }
      })
    );
    actions.appendChild(
      keyButton("Space", {
        className: "osk__key--wide",
        onClick: function () {
          insert(" ");
        }
      })
    );
    actions.appendChild(
      keyButton("Backspace", {
        className: "osk__key--wide",
        onClick: backspace
      })
    );
    actions.appendChild(
      keyButton("Clear", {
        onClick: function () {
          buffer = "";
          renderPreview();
          window.HUB.sfx.back();
        }
      })
    );
    actions.appendChild(
      keyButton("Cancel", {
        onClick: function () {
          osk.close(false);
        }
      })
    );
    actions.appendChild(
      keyButton("Done", {
        className: "osk__key--action osk__key--wide",
        onClick: function () {
          osk.close(true);
        }
      })
    );
    children.push(actions);

    f.fill(rowsHost, children);
  }

  function onPhysicalKey(event) {
    if (!open) return;
    if (event.key === "Enter") {
      event.preventDefault();
      osk.close(true);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      osk.close(false);
      return;
    }
    if (event.key === "Backspace") {
      event.preventDefault();
      backspace();
      return;
    }
    if (event.key && event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      insert(event.key);
    }
  }

  var osk = {
    init: function () {
      root = document.getElementById("osk");
      preview = document.getElementById("osk-preview");
      rowsHost = document.getElementById("osk-rows");
      labelHost = document.getElementById("osk-label");
    },

    isOpen: function () {
      return open;
    },

    open: function (input) {
      if (!root || open) return;
      target = input;
      buffer = input && input.value !== undefined ? String(input.value) : "";
      shifted = false;
      open = true;

      labelHost.textContent =
        (input && input.getAttribute("data-osk-label")) ||
        (input && input.getAttribute("placeholder")) ||
        "Input";

      /* Mask the preview for password fields so a PIN is not shown on a TV. */
      var isPassword = input && (input.getAttribute("type") || "").toLowerCase() === "password";
      preview.setAttribute("data-mask", isPassword ? "1" : "0");

      render();
      renderPreview();
      root.classList.add("is-open");
      window.HUB.nav.pushScope(root);
      window.HUB.nav.focusGroup("osk", { silent: true });

      keyHandler = onPhysicalKey;
      document.addEventListener("keydown", keyHandler, true);
    },

    close: function (commit) {
      if (!root || !open) return;
      open = false;
      root.classList.remove("is-open");
      if (keyHandler) {
        document.removeEventListener("keydown", keyHandler, true);
        keyHandler = null;
      }

      if (commit && target) {
        target.value = buffer;
        /* Fire both so listeners that debounce on input and ones that wait for
           change both see the value. */
        target.dispatchEvent(new Event("input", { bubbles: true }));
        target.dispatchEvent(new Event("change", { bubbles: true }));
        window.HUB.sfx.select();
      } else {
        window.HUB.sfx.back();
      }

      target = null;
      buffer = "";
      window.HUB.nav.popScope();
    },

    /* Pad shortcuts routed from gamepad.js. */
    handlePadButton: function (name) {
      if (!open) return false;
      if (name === "cross") {
        window.HUB.nav.activate();
        return true;
      }
      if (name === "circle") {
        osk.close(false);
        return true;
      }
      if (name === "square") {
        backspace();
        return true;
      }
      if (name === "triangle") {
        insert(" ");
        return true;
      }
      if (name === "r1" || name === "l1") {
        shifted = !shifted;
        render();
        window.HUB.nav.focusGroup("osk", { silent: true });
        return true;
      }
      if (name === "options") {
        osk.close(true);
        return true;
      }
      return false;
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.osk = osk;
})(window, document);
