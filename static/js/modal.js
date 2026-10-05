/* ===========================================================================
   Modal dialogs -- the replacement for alert/confirm/prompt.

   Native dialogs are banned here: in the console's browser they block the page,
   and with no mouse they can be hard to dismiss. These are ordinary DOM, focus-
   trapped through a nav scope, and resolve a promise so callers read linearly.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var root = null;
  var activeResolve = null;

  function fmt() {
    return window.HUB.fmt;
  }

  function settle(value) {
    var resolve = activeResolve;
    activeResolve = null;
    modal.close();
    if (resolve) resolve(value);
  }

  var modal = {
    init: function () {
      root = document.getElementById("modal-root");
      root.addEventListener("click", function (event) {
        /* Click the backdrop to dismiss, same as Circle. */
        if (event.target === root) settle(null);
      });
    },

    isOpen: function () {
      return !!root && root.classList.contains("is-open");
    },

    /* options: {title, lines[], fields[], actions[], danger} */
    open: function (options) {
      var f = fmt();
      var settings = options || {};

      var body = f.el("div.modal__body");
      (settings.lines || []).forEach(function (line) {
        if (typeof line === "string") body.appendChild(f.el("p", { text: line }));
        else body.appendChild(line);
      });

      var inputs = {};
      (settings.fields || []).forEach(function (field) {
        var input = f.el("input.input", {
          type: field.type || "text",
          id: "modal-field-" + field.name,
          value: field.value || "",
          placeholder: field.placeholder || "",
          autocomplete: "off",
          "data-focusable": true,
          "data-nav-group": "modal",
          "data-osk-label": field.label || field.name
        });
        inputs[field.name] = input;
        body.appendChild(
          f.el("div.field", null, [
            f.el("label", { for: "modal-field-" + field.name, text: field.label || field.name }),
            input,
            field.hint ? f.el("span.hint", { text: field.hint }) : null
          ])
        );
      });

      var actionsHost = f.el("div.modal__actions");
      var actions = settings.actions || [
        { label: "Close", value: null }
      ];
      actions.forEach(function (action) {
        var button = f.el("button.btn", {
          type: "button",
          class: action.variant ? "btn--" + action.variant : "",
          "data-focusable": true,
          "data-nav-group": "modal",
          text: action.label
        });
        button.addEventListener("click", function () {
          if (typeof action.onClick === "function") {
            var result = action.onClick(
              Object.keys(inputs).reduce(function (acc, key) {
                acc[key] = inputs[key].value;
                return acc;
              }, {})
            );
            if (result === false) return; /* keep open */
          }
          if (action.resolve !== undefined) {
            settle(
              typeof action.resolve === "function"
                ? action.resolve(
                    Object.keys(inputs).reduce(function (acc, key) {
                      acc[key] = inputs[key].value;
                      return acc;
                    }, {})
                  )
                : action.resolve
            );
          } else {
            settle(action.value === undefined ? null : action.value);
          }
        });
        actionsHost.appendChild(button);
      });

      var card = f.el("div.modal", null, [
        f.el("h2", { text: settings.title || "" }),
        body,
        actionsHost
      ]);

      f.fill(root, card);
      root.classList.add("is-open");
      window.HUB.nav.pushScope(root);

      var firstField = Object.keys(inputs)[0];
      if (firstField) window.HUB.nav.focus(inputs[firstField], { silent: true });
      else window.HUB.nav.focusGroup("modal", { silent: true });

      return new Promise(function (resolve) {
        activeResolve = resolve;
      });
    },

    close: function () {
      if (!root || !root.classList.contains("is-open")) return;
      root.classList.remove("is-open");
      fmt().clear(root);
      window.HUB.nav.popScope();
    },

    /* Circle / Escape handling, routed from gamepad.js and app.js. */
    dismiss: function () {
      if (!modal.isOpen()) return false;
      settle(null);
      return true;
    },

    confirm: function (options) {
      var settings = options || {};
      return modal.open({
        title: settings.title || "Are you sure?",
        lines: settings.lines || [],
        actions: [
          { label: settings.cancelLabel || "Cancel", value: false },
          {
            label: settings.confirmLabel || "Confirm",
            variant: settings.danger ? "danger" : "accent",
            value: true
          }
        ]
      }).then(function (value) {
        return value === true;
      });
    },

    prompt: function (options) {
      var settings = options || {};
      return modal.open({
        title: settings.title || "Enter a value",
        lines: settings.lines || [],
        fields: [
          {
            name: "value",
            label: settings.label || "Value",
            value: settings.value || "",
            placeholder: settings.placeholder || "",
            type: settings.type || "text",
            hint: settings.hint
          }
        ],
        actions: [
          { label: "Cancel", value: null },
          {
            label: settings.confirmLabel || "Save",
            variant: "accent",
            resolve: function (values) {
              return values.value;
            }
          }
        ]
      });
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.modal = modal;
})(window, document);
