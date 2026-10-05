/* ===========================================================================
   Recovery tab: PIN management, the emergency wipe, and the Safe Mode guide.

   The wipe flow in this file mirrors the server's and cannot shortcut it:

     dry run  ->  review the real file list  ->  type the phrase  ->  execute

   The target preview is visible without credentials, on purpose: you should be able
   to see what the wipe would touch before you ever enter a PIN. Executing needs the
   PIN, the exact phrase, and a token that only a dry run issues and which expires.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var pinChip = null;
  var targetChip = null;
  var pinCurrentField = null;
  var pinCurrentInput = null;
  var pinNewInput = null;
  var pinStatus = null;
  var wipePinInput = null;
  var wipePhraseInput = null;
  var wipePhraseHint = null;
  var wipeStatus = null;
  var targetsHost = null;
  var totalChip = null;

  var status = null;
  var guide = null;

  function fmt() {
    return window.HUB.fmt;
  }

  function renderTargets() {
    var f = fmt();
    if (!status) return;
    var targets = status.targets || {};
    var children = [];

    (targets.actionable || []).forEach(function (item) {
      children.push(
        f.el("div.row", null, [
          f.el("div.row__index", null, f.icon("trash", 16)),
          f.el("div.row__main", null, [
            f.el("div.row__title", null, [
              f.el("span.badge.badge--danger", { text: item.kind }),
              f.el("em", { text: item.path })
            ]),
            f.el("div.row__sub", null, [
              f.el("span", { text: item.file_count + " file(s)" }),
              f.el("span", { text: item.size_label })
            ])
          ]),
          f.el("div.row__actions")
        ])
      );
    });

    (targets.missing || []).forEach(function (item) {
      children.push(
        f.el("div.row", null, [
          f.el("div.row__index", null, f.icon("x", 16)),
          f.el("div.row__main", null, [
            f.el("div.row__title", null, [
              f.el("span.badge.badge--muted", { text: "absent" }),
              f.el("em", { text: item.path })
            ]),
            f.el("div.row__sub", null, [f.el("span", { text: item.reason })])
          ]),
          f.el("div.row__actions")
        ])
      );
    });

    (targets.refused || []).forEach(function (item) {
      children.push(
        f.el("div.row", null, [
          f.el("div.row__index", null, f.icon("lock", 16)),
          f.el("div.row__main", null, [
            f.el("div.row__title", null, [
              f.el("span.badge.badge--warn", { text: "refused" }),
              f.el("em", { text: item.path })
            ]),
            f.el("div.row__sub", null, [f.el("span", { text: item.reason })])
          ]),
          f.el("div.row__actions")
        ])
      );
    });

    (targets.console || []).forEach(function (item) {
      children.push(
        f.el("div.row", null, [
          f.el("div.row__index", null, f.icon("console", 16)),
          f.el("div.row__main", null, [
            f.el("div.row__title", null, [
              f.el("span.badge", {
                class: item.supported ? "badge--danger" : "badge--muted",
                text: item.supported ? "console" : "console (skipped)"
              }),
              f.el("em", { text: item.path })
            ]),
            f.el("div.row__sub", null, [f.el("span", { text: item.reason || "Will be sent to the console." })])
          ]),
          f.el("div.row__actions")
        ])
      );
    });

    if (!children.length) {
      children.push(
        f.el("p.small.muted", {
          text: "Nothing in wipe.allowlist currently exists on disk, so a wipe would do nothing."
        })
      );
    }

    children.push(
      f.el("p.small.muted", {
        text:
          "Protected and unreachable by any wipe: " +
          (status.protected || []).join(", ")
      })
    );

    fmt().fill(targetsHost, children);
    totalChip.textContent = status.total_label || "0 B";
    targetChip.textContent = (targets.actionable || []).length + " target(s) · " + (status.total_label || "0 B");
  }

  function renderPinState() {
    var configured = !!(status && status.pin_configured);
    fmt().show(pinCurrentField, configured);
    pinChip.textContent = configured ? "PIN set" : "No PIN yet";
    pinStatus.textContent = configured
      ? "A PIN is set (PBKDF2-SHA256, " +
        (status.iterations || 0).toLocaleString() +
        " iterations). Enter the current one to change it."
      : "No PIN yet. Set one before the wipe can be used.";

    if (status) {
      wipePhraseHint.textContent = 'Type exactly: "' + status.confirm_phrase + '"';
      var locked = Number(status.locked_seconds || 0);
      wipeStatus.textContent = locked
        ? "Locked for another " + locked + "s after failed attempts."
        : status.pin_configured
        ? (status.attempts_remaining || 0) + " attempt(s) before a lockout."
        : "Set a PIN first.";
    }
  }

  function renderGuide() {
    var f = fmt();
    if (!guide) return;

    document.getElementById("safe-mode-title").textContent = guide.title;
    document.getElementById("safe-mode-intro").textContent = guide.intro;

    f.fill(
      document.getElementById("safe-mode-entering"),
      (guide.entering || []).map(function (step) {
        return f.el("li", null, f.el("span", { text: step }));
      })
    );

    f.fill(
      document.getElementById("safe-mode-options"),
      (guide.options || []).map(function (option) {
        return f.el("div.option", null, [
          f.el("span.option__n", { text: String(option.n) }),
          f.el("div", null, [f.el("strong", { text: option.name }), f.el("span", { text: option.use })])
        ]);
      })
    );

    f.fill(
      document.getElementById("safe-mode-usb"),
      (guide.usb_reinstall || []).map(function (step) {
        return f.el("li", null, f.el("span", { text: step }));
      })
    );

    f.fill(
      document.getElementById("safe-mode-cautions"),
      [f.el("strong", { text: "Before you use options 6 or 7" })].concat(
        (guide.cautions || []).map(function (line) {
          return f.el("p", { text: line });
        })
      )
    );
  }

  function executeWipe(plan) {
    var f = fmt();
    var lines = [
      f.el("p", {
        text:
          "This will delete " +
          (plan.targets.actionable || []).length +
          " target(s), freeing about " +
          plan.total_label +
          ", then stop the hub."
      })
    ];
    (plan.targets.actionable || []).forEach(function (item) {
      lines.push(f.el("p.small.muted", { text: item.path + "  (" + item.file_count + " files, " + item.size_label + ")" }));
    });
    lines.push(
      f.el("p.small.muted", {
        text: "The hub's own source is not touched. Restart it later with start_hub.bat."
      })
    );

    window.HUB.modal
      .confirm({
        title: "Execute the wipe?",
        lines: lines,
        confirmLabel: "Delete now",
        cancelLabel: "Keep everything",
        danger: true
      })
      .then(function (confirmed) {
        if (!confirmed) {
          window.HUB.toast.info("Nothing was deleted.");
          return null;
        }
        return window.HUB.api
          .wipeExecute(plan.token)
          .then(function (report) {
            window.HUB.toast.warn(
              "Wipe done: " + report.removed_files + " file(s) removed, " + report.freed_label + " freed.",
              { duration: 15000 }
            );
            window.HUB.modal.open({
              title: "Wipe complete",
              lines: [
                f.el("p", {
                  text:
                    report.removed_files +
                    " file(s) removed, " +
                    report.truncated_files +
                    " truncated (in use), " +
                    report.freed_label +
                    " freed."
                }),
                report.errors && report.errors.length
                  ? f.el("div.callout.callout--warn", null, [
                      f.el("strong", { text: report.errors.length + " item(s) could not be removed" }),
                      f.el("span", { text: report.errors.slice(0, 6).join(" | ") })
                    ])
                  : null,
                f.el("p.small.muted", { text: report.note }),
                f.el("p.small.muted", {
                  text: report.server_stopping
                    ? "The hub is shutting down now. This page will stop responding; that is expected."
                    : "The hub is still running."
                })
              ],
              actions: [{ label: "Understood", value: null }]
            });
            window.HUB.app.stopPolling();
            return report;
          })
          .catch(function (error) {
            window.HUB.toast.fromError(error, "Wipe failed");
          });
      });
  }

  var recovery = {
    init: function () {
      pinChip = document.getElementById("wipe-pin-chip");
      targetChip = document.getElementById("wipe-target-chip");
      pinCurrentField = document.getElementById("pin-current-field");
      pinCurrentInput = document.getElementById("pin-current");
      pinNewInput = document.getElementById("pin-new");
      pinStatus = document.getElementById("pin-status");
      wipePinInput = document.getElementById("wipe-pin");
      wipePhraseInput = document.getElementById("wipe-phrase");
      wipePhraseHint = document.getElementById("wipe-phrase-hint");
      wipeStatus = document.getElementById("wipe-status");
      targetsHost = document.getElementById("wipe-targets");
      totalChip = document.getElementById("wipe-total");

      document.getElementById("pin-save").addEventListener("click", function () {
        var next = pinNewInput.value;
        if (next.length < 4) {
          window.HUB.toast.warn("The PIN needs at least 4 characters.");
          return;
        }
        window.HUB.api
          .setPin(next, pinCurrentInput.value)
          .then(function (response) {
            window.HUB.toast.ok(response.message);
            pinNewInput.value = "";
            pinCurrentInput.value = "";
            return recovery.load();
          })
          .catch(function (error) {
            window.HUB.toast.fromError(error, "Could not save the PIN");
          });
      });

      document.getElementById("wipe-plan").addEventListener("click", function () {
        if (!status || !status.pin_configured) {
          window.HUB.toast.warn("Set a PIN first.");
          return;
        }
        window.HUB.api
          .wipePlan(wipePinInput.value, wipePhraseInput.value)
          .then(function (plan) {
            wipePinInput.value = "";
            wipePhraseInput.value = "";
            window.HUB.toast.ok("Dry run complete. Nothing has been deleted.");
            executeWipe(plan);
            return recovery.load();
          })
          .catch(function (error) {
            window.HUB.sfx.error();
            window.HUB.toast.fromError(error, "Authentication failed");
            return recovery.load();
          });
      });
    },

    load: function () {
      return window.HUB.api
        .wipeStatus()
        .then(function (response) {
          status = response;
          renderPinState();
          renderTargets();
          return response;
        })
        .catch(function (error) {
          window.HUB.toast.fromError(error, "Could not read the wipe status");
        });
    },

    loadGuide: function () {
      if (guide) return Promise.resolve(guide);
      return window.HUB.api
        .safeModeGuide()
        .then(function (response) {
          guide = response.guide;
          renderGuide();
          return guide;
        })
        .catch(function (error) {
          window.HUB.toast.fromError(error, "Could not load the recovery guide");
        });
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.recovery = recovery;
})(window, document);
