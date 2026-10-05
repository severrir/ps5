/* ===========================================================================
   Queue view: live rows, reordering, throttle presets, install history.

   Progress carries its provenance. When the installer daemon reports a figure the
   row says so; when it does not, the bar is driven by bytes this hub has served and
   is labelled "estimated". The difference matters -- served bytes mean the package
   left the PC, not that the console finished writing it -- so the UI never passes
   one off as the other.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var rowsHost = null;
  var emptyState = null;
  var summaryChip = null;
  var historyHost = null;
  var historyCount = null;
  var presetsHost = null;
  var concurrencySelect = null;

  var snapshot = { queue: [], history: [] };
  var lastSignature = "";
  var lastHistorySignature = "";

  function fmt() {
    return window.HUB.fmt;
  }

  function stateBadge(entry) {
    var f = fmt();
    var label = window.HUB.i18n.t("state." + entry.state, entry.state);
    var variant = "";
    if (entry.state === "done") variant = "badge--ok";
    else if (entry.state === "failed") variant = "badge--danger";
    else if (entry.state === "paused" || entry.state === "retrying") variant = "badge--warn";
    else if (entry.state === "installing" || entry.state === "starting") variant = "badge--accent";
    else variant = "badge--muted";
    return f.el("span.badge", { class: variant, text: label });
  }

  function meterFor(entry) {
    var f = fmt();
    var percent = Math.max(0, Math.min(100, Number(entry.progress) || 0));
    var fill = f.el("div.meter__fill", { style: { width: percent + "%" } });

    var right = [];
    if (entry.rate_label) right.push(entry.rate_label);
    if (entry.eta_seconds) right.push(f.duration(entry.eta_seconds) + " left");

    return f.el("div.meter.meter--thin", null, [
      f.el("div.meter__head", null, [
        f.el("span", {
          text:
            f.percent(percent) +
            (entry.progress_source === "inferred"
              ? " estimated from bytes served"
              : entry.progress_source === "daemon"
              ? " reported by etaHEN"
              : "")
        }),
        f.el("span", { text: right.join(" · ") })
      ]),
      f.el("div.meter__track", null, fill)
    ]);
  }

  function actionButton(icon, title, handler) {
    var f = fmt();
    var button = f.el("button.icon-btn", {
      type: "button",
      title: title,
      "aria-label": title,
      "data-focusable": true,
      "data-nav-group": "queue"
    });
    button.appendChild(f.icon(icon, 18));
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      handler();
    });
    return button;
  }

  function act(id, action, successMessage) {
    return window.HUB.api
      .transferAction(id, action)
      .then(function (response) {
        if (successMessage) window.HUB.toast.ok(successMessage);
        transfers.render(response);
        return response;
      })
      .catch(function (error) {
        window.HUB.toast.fromError(error, "Could not " + action);
      });
  }

  function rowFor(entry, index) {
    var f = fmt();
    var subParts = [];
    subParts.push(f.el("span", { text: entry.size_label || f.bytes(entry.size_bytes) }));
    if (entry.served_label && entry.state !== "queued") {
      subParts.push(f.el("span", { text: entry.served_label + " served" }));
    }
    if (entry.attempts > 1) subParts.push(f.el("span", { text: "attempt " + entry.attempts }));
    if (entry.task_id) subParts.push(f.el("span", { text: "task " + entry.task_id }));
    if (entry.note) subParts.push(f.el("span", { text: entry.note }));
    if (entry.error) subParts.push(f.el("span", { text: entry.error }));

    var main = f.el("div.row__main", null, [
      f.el("div.row__title", null, [stateBadge(entry), f.el("em", { text: entry.title })]),
      entry.state === "queued" || entry.state === "retrying" ? null : meterFor(entry),
      f.el("div.row__sub", null, subParts)
    ]);

    var actions = f.el("div.row__actions");
    if (entry.state === "paused") {
      actions.appendChild(
        actionButton("play", "Resume", function () {
          act(entry.id, "resume", "Resuming - the install request is re-issued.");
        })
      );
    } else if (entry.state === "installing" || entry.state === "starting" || entry.state === "queued") {
      actions.appendChild(
        actionButton("pause", "Pause", function () {
          act(entry.id, "pause");
        })
      );
    }
    if (entry.state === "failed" || entry.state === "cancelled") {
      actions.appendChild(
        actionButton("refresh", "Retry", function () {
          act(entry.id, "retry", "Retrying.");
        })
      );
    }
    if (entry.state === "queued" || entry.state === "retrying" || entry.state === "paused") {
      actions.appendChild(
        actionButton("up", "Move earlier", function () {
          window.HUB.api
            .moveTransfer(entry.id, -1)
            .then(transfers.render)
            .catch(function () {
              /* already first */
            });
        })
      );
      actions.appendChild(
        actionButton("down", "Move later", function () {
          window.HUB.api
            .moveTransfer(entry.id, 1)
            .then(transfers.render)
            .catch(function () {
              /* already last */
            });
        })
      );
    }
    if (entry.state === "done" || entry.state === "failed" || entry.state === "cancelled") {
      actions.appendChild(
        actionButton("trash", "Remove from list", function () {
          window.HUB.api
            .removeTransfer(entry.id)
            .then(transfers.render)
            .catch(function (error) {
              window.HUB.toast.fromError(error, "Could not remove");
            });
        })
      );
    } else {
      actions.appendChild(
        actionButton("stop", "Cancel", function () {
          act(entry.id, "cancel", "Cancelled.");
        })
      );
    }

    return f.el(
      "div.row",
      {
        class: "state-" + entry.state,
        "data-transfer": entry.id,
        "data-focus-id": "transfer-" + entry.id
      },
      [f.el("div.row__index", { text: String(index + 1) }), main, actions]
    );
  }

  function historyRowFor(entry) {
    var f = fmt();
    var sub = [];
    sub.push(f.el("span", { text: f.dateTime(entry.finished_at) }));
    if (entry.size_label) sub.push(f.el("span", { text: entry.size_label }));
    if (entry.started_at && entry.finished_at) {
      sub.push(f.el("span", { text: "took " + f.duration(entry.finished_at - entry.started_at) }));
    }
    if (entry.error) sub.push(f.el("span", { text: entry.error }));
    if (entry.note) sub.push(f.el("span", { text: entry.note }));

    return f.el("div.row", { class: "state-" + entry.state }, [
      f.el("div.row__index", null, f.icon(entry.state === "done" ? "check" : "x", 16)),
      f.el("div.row__main", null, [
        f.el("div.row__title", null, [stateBadge(entry), f.el("em", { text: entry.title })]),
        f.el("div.row__sub", null, sub)
      ]),
      f.el("div.row__actions")
    ]);
  }

  function renderPresets() {
    var f = fmt();
    if (!presetsHost) return;
    var presets = snapshot.throttle_presets_mbps || [5, 10, 25, 50, 0];
    var active = Number(snapshot.throttle_mbps || 0);

    f.fill(
      presetsHost,
      presets.map(function (value) {
        var mbps = Number(value) || 0;
        var button = f.el("button.btn", {
          type: "button",
          class: mbps === active ? "btn--accent" : "",
          "data-focusable": true,
          "data-nav-group": "queue-tools",
          text: mbps <= 0 ? "Unlimited" : mbps + " MB/s"
        });
        button.addEventListener("click", function () {
          window.HUB.api
            .setThrottle(mbps)
            .then(function (response) {
              window.HUB.toast.ok(response.message);
              snapshot.throttle_mbps = mbps;
              renderPresets();
              window.HUB.nav.refresh();
            })
            .catch(function (error) {
              window.HUB.toast.fromError(error, "Could not set the limit");
            });
        });
        return button;
      })
    );
  }

  var transfers = {
    init: function () {
      rowsHost = document.getElementById("queue-rows");
      emptyState = document.getElementById("queue-empty");
      summaryChip = document.getElementById("queue-summary");
      historyHost = document.getElementById("history-rows");
      historyCount = document.getElementById("history-count");
      presetsHost = document.getElementById("throttle-presets");
      concurrencySelect = document.getElementById("queue-concurrency");

      concurrencySelect.addEventListener("change", function () {
        window.HUB.api
          .setConcurrency(Number(concurrencySelect.value))
          .then(function (response) {
            window.HUB.toast.ok(response.message);
          })
          .catch(function (error) {
            window.HUB.toast.fromError(error, "Could not change concurrency");
          });
      });

      document.getElementById("queue-clear").addEventListener("click", function () {
        window.HUB.api
          .clearFinished()
          .then(function (response) {
            window.HUB.toast.ok(response.message);
            transfers.render(response);
          })
          .catch(function (error) {
            window.HUB.toast.fromError(error, "Could not clear");
          });
      });
    },

    render: function (data) {
      var f = fmt();
      if (data) snapshot = data;
      var queue = snapshot.queue || [];

      var signature = queue
        .map(function (entry) {
          return [
            entry.id,
            entry.state,
            Math.round(entry.progress || 0),
            Math.round(entry.rate_bps || 0),
            entry.note || "",
            entry.error || ""
          ].join(":");
        })
        .join("|");

      if (signature !== lastSignature) {
        lastSignature = signature;
        var focusId = null;
        var current = window.HUB.nav.current();
        if (current && current.getAttribute) focusId = current.getAttribute("data-focus-id");

        f.clear(rowsHost);
        queue.forEach(function (entry, index) {
          rowsHost.appendChild(rowFor(entry, index));
        });
        f.show(rowsHost, queue.length > 0);
        f.show(emptyState, queue.length === 0);
        window.HUB.nav.refresh(focusId);
      }

      var active = snapshot.active || 0;
      var pending = snapshot.pending || 0;
      var paused = snapshot.paused || 0;
      var parts = [];
      if (active) parts.push(active + " installing");
      if (pending) parts.push(pending + " waiting");
      if (paused) parts.push(paused + " paused");
      summaryChip.textContent = parts.length ? parts.join(" · ") : "empty";

      if (concurrencySelect && String(snapshot.max_concurrent) !== concurrencySelect.value) {
        concurrencySelect.value = String(snapshot.max_concurrent || 1);
      }
      renderPresets();

      var history = snapshot.history || [];
      var historySignature = history
        .map(function (entry) {
          return entry.id + ":" + entry.state;
        })
        .join("|");
      if (historySignature !== lastHistorySignature) {
        lastHistorySignature = historySignature;
        f.clear(historyHost);
        if (!history.length) {
          historyHost.appendChild(f.el("p.small.muted", { text: "Nothing has finished yet." }));
        } else {
          history.forEach(function (entry) {
            historyHost.appendChild(historyRowFor(entry));
          });
        }
        historyCount.textContent = String(history.length);
      }
    },

    load: function () {
      return window.HUB.api
        .transfers()
        .then(function (response) {
          transfers.render(response);
          return response;
        })
        .catch(function (error) {
          window.HUB.toast.fromError(error, "Could not load the queue");
        });
    },

    snapshot: function () {
      return snapshot;
    },

    /* Pad shortcuts while the Queue tab is active. */
    handlePadButton: function (name) {
      var current = window.HUB.nav.current();
      var row = current && current.closest ? current.closest("[data-transfer]") : null;
      if (!row) return false;
      var id = row.getAttribute("data-transfer");
      var entry = null;
      (snapshot.queue || []).forEach(function (candidate) {
        if (candidate.id === id) entry = candidate;
      });
      if (!entry) return false;

      if (name === "triangle") {
        act(entry.id, entry.state === "paused" ? "resume" : "pause");
        return true;
      }
      if (name === "square") {
        window.HUB.api
          .moveTransfer(entry.id, -1)
          .then(transfers.render)
          .catch(function () {});
        return true;
      }
      if (name === "options") {
        window.HUB.modal
          .confirm({
            title: "Cancel " + entry.title + "?",
            lines: ["The console stops receiving bytes immediately. Nothing already written is removed."],
            confirmLabel: "Cancel transfer",
            danger: true
          })
          .then(function (confirmed) {
            if (confirmed) act(entry.id, "cancel", "Cancelled.");
          });
        return true;
      }
      return false;
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.transfers = transfers;
})(window, document);
