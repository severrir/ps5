/* ===========================================================================
   Safety tab.

   The structure of this view is the point: three separate lists that are never
   merged. "Verified here" holds machine-checked facts. "Not verified" holds what
   this hub cannot know -- above all, whether the console itself can reach PSN.
   "Warnings" holds things to act on.

   There is deliberately no green "ban-safe" badge. The best state the panel will
   show is "all local checks pass", because that is the strongest claim that is
   actually true.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var levelText = null;
  var levelPip = null;
  var disclaimer = null;
  var verifiedHost = null;
  var unverifiedHost = null;
  var warningsHost = null;
  var warningsPanel = null;
  var auditBadge = null;
  var auditSummary = null;
  var auditFindings = null;
  var auditException = null;
  var bindingHost = null;
  var probeHost = null;
  var piholeHost = null;
  var checklistHost = null;
  var checklistProgress = null;
  var checklistChip = null;

  var state = null;

  function fmt() {
    return window.HUB.fmt;
  }

  function claims(host, items, emptyText) {
    var f = fmt();
    if (!items || !items.length) {
      f.fill(host, f.el("li", null, f.el("span.muted", { text: emptyText })));
      return;
    }
    f.fill(
      host,
      items.map(function (text) {
        return f.el("li", null, f.el("span", { text: text }));
      })
    );
  }

  function renderAudit(audit) {
    var f = fmt();
    if (!audit) return;

    auditBadge.textContent = audit.pass ? "PASS" : "FAIL";
    auditBadge.className = "badge " + (audit.pass ? "badge--ok" : "badge--danger");
    auditSummary.textContent =
      audit.files_scanned +
      " shipped file(s) scanned for remote references · " +
      audit.blocking_count +
      " blocking, " +
      audit.note_count +
      " note(s) · checked " +
      f.relative(audit.checked_at);

    var findings = audit.findings || [];
    if (!findings.length) {
      f.fill(
        auditFindings,
        f.el("p.small.muted", {
          text: "No http:// or https:// reference appears anywhere in static/ or templates/."
        })
      );
    } else {
      f.fill(
        auditFindings,
        findings.slice(0, 60).map(function (finding) {
          return f.el("div.finding", { class: finding.severity === "blocking" ? "is-blocking" : "" }, [
            f.el("span", {
              class: finding.severity === "blocking" ? "badge badge--danger" : "badge badge--muted",
              text: finding.severity
            }),
            f.el("span", { text: finding.file + ":" + finding.line + "  " + finding.match })
          ]);
        })
      );
    }
    auditException.textContent = audit.declared_exception || "";
  }

  function renderBinding(binding) {
    var f = fmt();
    var rows = [];
    function row(key, value) {
      rows.push(f.el("dt", { text: key }));
      rows.push(f.el("dd", { text: String(value) }));
    }
    row("Listening on", binding.bound_ip + ":" + binding.port);
    row("Console should use", binding.base_url);
    row("Subnet allowlist", (binding.subnet_allowlist || []).join(", ") || "(none)");
    row("Allowlist enforced", binding.allowlist_enforced ? "yes - other hosts get 403" : "NO - every host is answered");
    f.fill(bindingHost, rows);
  }

  function renderProbe(probe, probingEnabled) {
    var f = fmt();
    if (!probe || !probe.ran) {
      f.fill(
        probeHost,
        f.el("div.callout", null, [
          f.el("strong", { text: probingEnabled ? "Not probed yet" : "Probing is off" }),
          f.el("span", {
            text: probingEnabled
              ? "Press Probe PSN reachability above to test from this PC."
              : "Enable active probing in Settings first. It stays off by default because probing makes this machine send outbound requests - the very thing the air-gap audit checks for."
          })
        ])
      );
      return;
    }

    var children = [
      f.el("p.small.muted", {
        text: "Measured from this PC at " + f.clock(probe.last_run) + ". This is not the console's view."
      })
    ];
    (probe.results || []).forEach(function (result) {
      var reachable = result.connected === true;
      children.push(
        f.el("div.row", null, [
          f.el("div.row__index", null, f.icon(reachable ? "x" : "check", 16)),
          f.el("div.row__main", null, [
            f.el("div.row__title", null, [
              f.el("span.badge", {
                class: reachable ? "badge--danger" : "badge--ok",
                text: reachable ? "reachable" : "blocked"
              }),
              f.el("em", { text: result.domain })
            ]),
            f.el("div.row__sub", null, [
              f.el("span", {
                text: (result.resolved && result.resolved.length ? result.resolved.join(", ") : "did not resolve")
              }),
              result.note ? f.el("span", { text: result.note }) : null,
              result.connect_error ? f.el("span", { text: result.connect_error }) : null
            ])
          ]),
          f.el("div.row__actions")
        ])
      );
    });
    f.fill(probeHost, children);
  }

  function renderPihole(pihole) {
    var f = fmt();
    if (!pihole || !pihole.enabled) {
      f.fill(
        piholeHost,
        f.el("p.small.muted", {
          text: "No DNS sinkhole configured. Set safety.pihole.base_url in configs.json to read blocked-query counts here."
        })
      );
      return;
    }
    if (!pihole.ok) {
      f.fill(piholeHost, f.el("p.small.muted", { text: "Could not reach Pi-hole: " + (pihole.error || "unknown error") }));
      return;
    }
    var rows = [];
    function row(key, value) {
      if (value === null || value === undefined) return;
      rows.push(f.el("dt", { text: key }));
      rows.push(f.el("dd", { text: String(value) }));
    }
    row("Status", pihole.status);
    row("Blocked today", pihole.blocked_today);
    row("Queries today", pihole.queries_today);
    row("Block rate", pihole.block_percent ? Number(pihole.block_percent).toFixed(1) + "%" : null);
    row("Domains on blocklist", pihole.domains_on_blocklist);
    f.fill(piholeHost, rows);
  }

  function renderChecklist(items, done, total) {
    var f = fmt();
    f.fill(
      checklistHost,
      (items || []).map(function (item) {
        var box = f.el("span.check__box");
        box.appendChild(f.icon("check", 14));
        var node = f.el(
          "button.check",
          {
            type: "button",
            class: item.checked ? "is-checked" : "",
            "data-focusable": true,
            "data-nav-group": "safety",
            "data-focus-id": "check-" + item.id
          },
          [
            box,
            f.el("span.check__text", null, [
              f.el("strong", { text: item.label }),
              f.el("span", { text: item.detail })
            ])
          ]
        );
        node.addEventListener("click", function () {
          window.HUB.api
            .setChecklist(item.id, !item.checked)
            .then(function (response) {
              window.HUB.sfx.toggle();
              renderChecklist(response.checklist, response.done, response.total);
              checklistProgress.textContent = response.done + " / " + response.total;
              checklistChip.textContent = "checklist " + response.done + "/" + response.total;
              window.HUB.nav.refresh("check-" + item.id);
              return safety.load();
            })
            .catch(function (error) {
              window.HUB.toast.fromError(error, "Could not save");
            });
        });
        return node;
      })
    );
    checklistProgress.textContent = (done || 0) + " / " + (total || 0);
    checklistChip.textContent = "checklist " + (done || 0) + "/" + (total || 0);
  }

  var safety = {
    init: function () {
      levelText = document.getElementById("safety-level-text");
      levelPip = document.getElementById("safety-level-pip");
      disclaimer = document.getElementById("safety-disclaimer");
      verifiedHost = document.getElementById("safety-verified");
      unverifiedHost = document.getElementById("safety-unverified");
      warningsHost = document.getElementById("safety-warnings");
      warningsPanel = document.getElementById("safety-warnings-panel");
      auditBadge = document.getElementById("audit-badge");
      auditSummary = document.getElementById("audit-summary");
      auditFindings = document.getElementById("audit-findings");
      auditException = document.getElementById("audit-exception");
      bindingHost = document.getElementById("safety-binding");
      probeHost = document.getElementById("safety-probe-results");
      piholeHost = document.getElementById("pihole-kv");
      checklistHost = document.getElementById("safety-checklist");
      checklistProgress = document.getElementById("checklist-progress");
      checklistChip = document.getElementById("safety-checklist-chip");

      document.getElementById("safety-audit").addEventListener("click", function () {
        window.HUB.api
          .runAudit()
          .then(function (response) {
            if (response.audit.pass) window.HUB.toast.ok(response.message);
            else window.HUB.toast.error(response.message);
            renderAudit(response.audit);
            return safety.load();
          })
          .catch(function (error) {
            window.HUB.toast.fromError(error, "Audit failed");
          });
      });

      document.getElementById("safety-probe").addEventListener("click", function () {
        var enabled = state && state.active_probing;

        window.HUB.modal
          .confirm({
            title: enabled ? "Probe PSN reachability?" : "Enable probing and run it?",
            lines: [
              "This resolves and connect-tests Sony hostnames from this PC. It measures the PC's view, not the console's, and it does make outbound requests from this machine.",
              "It is the only feature in the hub that reaches outside your LAN, and only when you press confirm.",
              enabled
                ? "Active probing is already enabled."
                : "Active probing is currently off. Confirming turns it on and leaves it on; you can switch it back off in Settings."
            ],
            confirmLabel: enabled ? "Run probe" : "Enable and probe"
          })
          .then(function (confirmed) {
            if (!confirmed) return null;
            /* Turn probing on through its own endpoint first when it is off, so the
               trade-off is acknowledged at the moment it actually applies. */
            var ready = enabled ? Promise.resolve(null) : window.HUB.api.setProbing(true);
            return ready
              .then(function () {
                return window.HUB.api.runProbe();
              })
              .then(function (response) {
                if (response.probe.reachable && response.probe.reachable.length) {
                  window.HUB.toast.warn(response.message);
                } else {
                  window.HUB.toast.ok(response.message);
                }
                return safety.load();
              })
              .catch(function (error) {
                window.HUB.toast.fromError(error, "Probe failed");
              });
          });
      });
    },

    load: function () {
      return window.HUB.api
        .safety()
        .then(function (response) {
          state = response;
          safety.render(response);
          return response;
        })
        .catch(function (error) {
          window.HUB.toast.fromError(error, "Could not read the safety panel");
        });
    },

    render: function (data) {
      var f = fmt();
      if (!data) return;

      levelText.textContent = data.headline || "";
      disclaimer.textContent = data.disclaimer || "";
      levelPip.className =
        "pip " +
        (data.level === "attention" ? "pip--danger" : data.level === "checks_pass" ? "pip--ok" : "pip--warn");

      claims(verifiedHost, data.verified, "Nothing has been verified yet.");
      claims(unverifiedHost, data.unverified, "Everything checkable has been checked.");
      f.show(warningsPanel, (data.warnings || []).length > 0);
      claims(warningsHost, data.warnings, "");

      renderAudit(data.audit);
      renderBinding(data.binding || {});
      renderProbe(data.probe, data.active_probing);
      renderPihole(data.pihole);
      renderChecklist(data.checklist, data.checklist_done, data.checklist_total);
    },

    state: function () {
      return state;
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.safety = safety;
})(window, document);
