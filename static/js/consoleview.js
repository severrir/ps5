/* ===========================================================================
   Console tab: storage meters, hardware telemetry, connection state, kernel log.

   Every reading here is attributed. A console volume says "console"; this PC's
   drives say "PC". When etaHEN exposes no storage or hardware endpoint, the panel
   says exactly that instead of showing an empty bar that looks like zero usage, and
   mock figures are labelled as simulated so a demo is never mistaken for live data.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var storageHost = null;
  var hardwareHost = null;
  var kvHost = null;
  var klogHost = null;
  var modeChip = null;
  var targetChip = null;

  function fmt() {
    return window.HUB.fmt;
  }

  function meter(volume) {
    var f = fmt();
    var used = Number(volume.used_percent) || 0;
    var fill = f.el("div.meter__fill", {
      class: used > 92 ? "is-danger" : used > 78 ? "is-warn" : "",
      style: { width: Math.max(0, Math.min(100, used)) + "%" }
    });

    return f.el("div.meter", null, [
      f.el("div.meter__head", null, [
        f.el("span", { text: volume.label + (volume.source === "console" ? "" : " (PC)") }),
        f.el("span", { text: volume.free_label + " free of " + volume.total_label })
      ]),
      f.el("div.meter__track", null, fill)
    ]);
  }

  var consoleView = {
    init: function () {
      storageHost = document.getElementById("storage-meters");
      hardwareHost = document.getElementById("hardware-metrics");
      kvHost = document.getElementById("console-kv");
      klogHost = document.getElementById("klog-lines");
      modeChip = document.getElementById("console-mode");
      targetChip = document.getElementById("console-target");

      document.getElementById("console-refresh").addEventListener("click", function () {
        window.HUB.app.refreshNow();
        window.HUB.toast.info("Refreshed.");
      });

      document.getElementById("klog-refresh").addEventListener("click", consoleView.loadKlog);
      document.getElementById("activity-refresh").addEventListener("click", consoleView.loadActivity);
    },

    render: function (state) {
      var f = fmt();
      if (!state) return;

      /* ---------------------------------------------------------- storage -- */
      var storage = state.storage || {};
      var children = [];
      var consoleStorage = storage.console || {};

      if (consoleStorage.available) {
        if (consoleStorage.simulated) {
          children.push(
            f.el("span.badge.badge--warn", { text: "Simulated figures (etaHEN mode is mock)" })
          );
        }
        (consoleStorage.volumes || []).forEach(function (volume) {
          children.push(meter(volume));
        });
      } else {
        children.push(
          f.el("div.callout.callout--warn", null, [
            f.el("strong", { text: "Console storage unavailable" }),
            f.el("span", {
              text:
                consoleStorage.reason ||
                "The installer daemon did not answer a storage request. The PC figures below are still accurate."
            })
          ])
        );
      }

      (storage.host || []).forEach(function (volume) {
        children.push(meter(volume));
      });
      f.fill(storageHost, children);

      /* --------------------------------------------------------- hardware -- */
      var hardware = state.hardware || {};
      if (hardware.available) {
        var metrics = [];
        if (hardware.simulated) {
          metrics.push(f.el("span.badge.badge--warn", { text: "Simulated" }));
        }
        (hardware.metrics || []).forEach(function (metric) {
          var fill = f.el("div.meter__fill", {
            class: metric.percent > 85 ? "is-danger" : metric.percent > 70 ? "is-warn" : "",
            style: { width: metric.percent + "%" }
          });
          metrics.push(
            f.el("div.meter", null, [
              f.el("div.meter__head", null, [
                f.el("span", { text: metric.label }),
                f.el("span", {
                  text: metric.value + (metric.unit === "degC" ? " °C" : " " + metric.unit)
                })
              ]),
              f.el("div.meter__track", null, fill)
            ])
          );
        });
        if (hardware.uptime_seconds) {
          metrics.push(
            f.el("p.small.muted", { text: "Console uptime " + f.duration(hardware.uptime_seconds) })
          );
        }
        f.fill(hardwareHost, metrics);
      } else {
        f.fill(
          hardwareHost,
          f.el("div.callout", null, [
            f.el("strong", { text: "No hardware metrics" }),
            f.el("span", {
              text:
                hardware.reason ||
                "This etaHEN build exposes no temperature or fan endpoint. Add one to etahen.endpoints.hardware if yours does."
            })
          ])
        );
      }

      /* ------------------------------------------------------- connection -- */
      var console_ = state.console || {};
      var rows = [];
      function row(key, value) {
        if (value === null || value === undefined || value === "") return;
        rows.push(f.el("dt", { text: key }));
        rows.push(f.el("dd", { text: String(value) }));
      }
      row("State", console_.connected ? "connected" : "not answering");
      row("Mode", console_.mode);
      row("Address", console_.ip);
      row("Installer port", console_.installer_port);
      row("Control port", console_.control_port);
      row("Last heartbeat", console_.last_ok ? f.relative(console_.last_ok) : "never");
      row("Successful handshakes", console_.handshakes);
      row("Consecutive failures", console_.consecutive_failures);
      row("Last error", console_.last_error);
      row("Hub URL for the console", state.base_url);
      f.fill(kvHost, rows);

      modeChip.textContent = "mode: " + (console_.mode || "?");
      targetChip.textContent =
        (console_.ip || "?") + ":" + (console_.installer_port || "?") + " / " + (console_.control_port || "?");
    },

    /* The session activity log: queued, installed, failed, audited, wiped.
       Distinct from the developer log drawer -- this is the record of what the hub
       did, in plain language, and it is one of the things the wipe clears. */
    loadActivity: function () {
      var f = fmt();
      var host = document.getElementById("activity-rows");
      return window.HUB.api
        .events(80)
        .then(function (response) {
          var events = response.events || [];
          if (!events.length) {
            f.fill(host, f.el("p.small.muted", { text: "Nothing recorded yet this session." }));
            return response;
          }

          var icons = {
            transfer: "queue",
            install: "download",
            library: "library",
            safety: "shield",
            wipe: "recovery",
            backup: "save",
            system: "settings"
          };

          f.fill(
            host,
            events.map(function (event) {
              var family = String(event.kind || "").split(".")[0];
              var failed = /fail|cancel/.test(String(event.kind));
              return f.el("div.row", { class: failed ? "state-failed" : "" }, [
                f.el("div.row__index", null, f.icon(icons[family] || "terminal", 16)),
                f.el("div.row__main", null, [
                  f.el("div.row__title", null, [
                    f.el("span.badge", { class: failed ? "badge--danger" : "badge--muted", text: event.kind }),
                    f.el("em", { text: event.message })
                  ]),
                  f.el("div.row__sub", null, [f.el("span", { text: f.dateTime(event.ts) })])
                ]),
                f.el("div.row__actions")
              ]);
            })
          );
          return response;
        })
        .catch(function (error) {
          f.fill(host, f.el("p.small.muted", { text: error.message }));
        });
    },

    loadKlog: function () {
      var f = fmt();
      return window.HUB.api
        .klog()
        .then(function (response) {
          var lines = response.lines || [];
          if (!lines.length) {
            f.fill(
              klogHost,
              f.el("p.small.muted", {
                text: response.message || "No kernel log lines were returned."
              })
            );
            return response;
          }
          f.fill(
            klogHost,
            lines.map(function (line) {
              return f.el("div.finding", null, [f.el("span.muted", { text: ">" }), f.el("span", { text: line })]);
            })
          );
          return response;
        })
        .catch(function (error) {
          f.fill(klogHost, f.el("p.small.muted", { text: error.message }));
        });
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.consoleView = consoleView;
})(window, document);
