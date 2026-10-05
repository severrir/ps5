/* ===========================================================================
   Settings tab.

   Only writes paths the server's allowlist accepts, so a field here can never
   reach the wipe configuration or the PIN. Changes that affect how the page looks
   (performance mode, overscan, cards per row, language) are applied locally the
   moment they are saved, without a reload.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var el = {};
  var settings = null;
  var toggles = {};

  function fmt() {
    return window.HUB.fmt;
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function setToggle(node, value) {
    if (!node) return;
    if (value) node.classList.add("is-on");
    else node.classList.remove("is-on");
  }

  function toggleState(node) {
    return node.classList.contains("is-on");
  }

  function linesToList(text) {
    return String(text || "")
      .split(/[\r\n]+/)
      .map(function (line) {
        return line.trim();
      })
      .filter(function (line) {
        return line.length > 0;
      });
  }

  function numberOr(value, fallback) {
    var parsed = Number(value);
    return isFinite(parsed) ? parsed : fallback;
  }

  var settingsView = {
    init: function () {
      el = {
        version: byId("settings-version"),
        host: byId("settings-host"),
        mode: byId("set-mode"),
        consoleIp: byId("set-console-ip"),
        installerPort: byId("set-installer-port"),
        controlPort: byId("set-control-port"),
        pkgDirs: byId("set-pkg-dirs"),
        scanInterval: byId("set-scan-interval"),
        cards: byId("set-cards"),
        overscan: byId("set-overscan"),
        language: byId("set-language"),
        bgmVolume: byId("set-bgm-volume"),
        logRetention: byId("set-log-retention"),
        piholeUrl: byId("set-pihole-url"),
        retries: byId("set-retries"),
        backoff: byId("set-backoff"),
        backupDirs: byId("set-backup-dirs"),
        save: byId("settings-save")
      };

      toggles = {
        performance: byId("set-performance"),
        sfx: byId("set-sfx"),
        rumble: byId("set-rumble"),
        bgm: byId("set-bgm"),
        probing: byId("set-probing")
      };

      Object.keys(toggles).forEach(function (key) {
        toggles[key].addEventListener("click", function () {
          toggles[key].classList.toggle("is-on");
          window.HUB.sfx.toggle();
          /* Visual settings should react immediately rather than waiting for Save. */
          if (key === "performance") {
            document.body.setAttribute("data-performance", toggleState(toggles[key]) ? "1" : "0");
          }
          if (key === "sfx") window.HUB.sfx.setEnabled(toggleState(toggles[key]));
        });
      });

      var languages = window.HUB.i18n.languages();
      fmt().fill(
        el.language,
        languages.map(function (entry) {
          return fmt().el("option", { value: entry.code, text: entry.label });
        })
      );
      el.language.addEventListener("change", function () {
        window.HUB.i18n.setLanguage(el.language.value);
        window.HUB.app.refreshChrome();
      });

      el.save.addEventListener("click", settingsView.save);

      byId("settings-update-check").addEventListener("click", function () {
        window.HUB.api
          .versionCheck()
          .then(function (response) {
            if (response.update_available) {
              window.HUB.toast.ok("Staged update found: v" + response.staged.version);
            } else {
              window.HUB.toast.info(
                "Running v" + response.version + ". No staged update in " + response.manifest_path + "."
              );
            }
          })
          .catch(function (error) {
            window.HUB.toast.fromError(error, "Version check failed");
          });
      });

      byId("settings-backup").addEventListener("click", function () {
        window.HUB.toast.info("Bundling save data...");
        window.HUB.api
          .backup()
          .then(function (response) {
            window.HUB.toast.ok(response.message);
          })
          .catch(function (error) {
            window.HUB.toast.fromError(error, "Backup");
          });
      });
    },

    load: function () {
      return window.HUB.api
        .settings()
        .then(function (response) {
          settings = response.settings || {};
          var ui = settings.ui || {};
          var etahen = settings.etahen || {};
          var library = settings.library || {};
          var transfers = settings.transfers || {};
          var safety = settings.safety || {};
          var backup = settings.backup || {};

          el.version.textContent = "v" + (settings.version || "?");
          el.host.textContent = response.base_url || "";

          el.mode.value = etahen.mode || "mock";
          el.consoleIp.value = etahen.console_ip || "";
          el.installerPort.value = String(etahen.installer_port || 12800);
          el.controlPort.value = String(etahen.control_port || 9090);

          el.pkgDirs.value = (library.pkg_directories || []).join("\n");
          el.scanInterval.value = String(library.scan_interval_seconds || 60);

          el.cards.value = String(ui.cards_per_row || 5);
          el.overscan.value = String(ui.overscan_percent || 5);
          el.language.value = ui.language || "en";
          el.bgmVolume.value = String(Math.round((ui.bgm_volume || 0.4) * 100));

          el.logRetention.value = String(safety.log_retention_days || 7);
          el.piholeUrl.value = (safety.pihole && safety.pihole.base_url) || "";

          el.retries.value = String(transfers.max_retries || 3);
          el.backoff.value = String(transfers.retry_backoff_seconds || 5);
          el.backupDirs.value = (backup.source_dirs || []).join("\n");

          setToggle(toggles.performance, !!ui.performance_mode);
          setToggle(toggles.sfx, !!ui.sfx_enabled);
          setToggle(toggles.rumble, ui.rumble_enabled !== false);
          setToggle(toggles.bgm, !!ui.bgm_enabled);
          setToggle(toggles.probing, !!safety.active_probing);

          return response;
        })
        .catch(function (error) {
          window.HUB.toast.fromError(error, "Could not load settings");
        });
    },

    save: function () {
      var changes = {
        "etahen.mode": el.mode.value,
        "etahen.console_ip": el.consoleIp.value.trim(),
        "etahen.installer_port": numberOr(el.installerPort.value, 12800),
        "etahen.control_port": numberOr(el.controlPort.value, 9090),
        "library.pkg_directories": linesToList(el.pkgDirs.value),
        "library.scan_interval_seconds": Math.max(10, numberOr(el.scanInterval.value, 60)),
        "ui.cards_per_row": Math.max(2, Math.min(10, numberOr(el.cards.value, 5))),
        "ui.overscan_percent": Math.max(0, Math.min(12, numberOr(el.overscan.value, 5))),
        "ui.language": el.language.value,
        "ui.performance_mode": toggleState(toggles.performance),
        "ui.sfx_enabled": toggleState(toggles.sfx),
        "ui.rumble_enabled": toggleState(toggles.rumble),
        "ui.bgm_enabled": toggleState(toggles.bgm),
        "ui.bgm_volume": Math.max(0, Math.min(1, numberOr(el.bgmVolume.value, 40) / 100)),
        "safety.active_probing": toggleState(toggles.probing),
        "safety.log_retention_days": Math.max(1, numberOr(el.logRetention.value, 7)),
        "safety.pihole.base_url": el.piholeUrl.value.trim(),
        "safety.pihole.enabled": el.piholeUrl.value.trim().length > 0,
        "transfers.max_retries": Math.max(0, numberOr(el.retries.value, 3)),
        "transfers.retry_backoff_seconds": Math.max(1, numberOr(el.backoff.value, 5)),
        "backup.source_dirs": linesToList(el.backupDirs.value)
      };

      return window.HUB.api
        .saveSettings(changes)
        .then(function (response) {
          window.HUB.toast.ok(response.message);
          window.HUB.app.applyUiPreferences({
            cards_per_row: changes["ui.cards_per_row"],
            overscan_percent: changes["ui.overscan_percent"],
            performance_mode: changes["ui.performance_mode"],
            language: changes["ui.language"],
            sfx_enabled: changes["ui.sfx_enabled"],
            bgm_enabled: changes["ui.bgm_enabled"],
            bgm_volume: changes["ui.bgm_volume"]
          });
          window.HUB.library.load();
          return settingsView.load();
        })
        .catch(function (error) {
          window.HUB.toast.fromError(error, "Could not save");
        });
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.settings = settingsView;
})(window, document);
