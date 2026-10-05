/* ===========================================================================
   Application shell: section routing, the poll loop, chrome, and boot.

   One request per tick. /api/system/state carries the console heartbeat, library
   counts, queue, storage, hardware and the safety pip, so the console's browser makes
   a single request per second instead of six. Views that are expensive to assemble
   (the safety panel, the wipe targets, settings) load when their tab is opened and
   on demand after a change.

   Keyboard parity is deliberate: arrows/Enter/Escape mirror the D-pad and Cross and
   Circle, so the whole UI can be driven from a desk while developing.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var SECTIONS = ["library", "queue", "console", "browser", "safety", "recovery", "settings"];

  var current = "library";
  var pollTimer = 0;
  var pollInterval = 1000;
  var polling = false;
  var inFlight = false;
  var consecutiveFailures = 0;
  var lastState = null;

  function fmt() {
    return window.HUB.fmt;
  }

  function byId(id) {
    return document.getElementById(id);
  }

  /* ----------------------------------------------------------------- chrome */

  function renderHints() {
    var f = fmt();
    var t = window.HUB.i18n.t;
    var hints = [t("hint.move", "D-pad Move"), t("hint.select", "Cross Select"), t("hint.back", "Circle Back")];

    if (current === "library") {
      hints.push(t("hint.pin", "Square Pin"), t("hint.queue", "Triangle Queue"));
    } else if (current === "queue") {
      hints.push("Square Move up", "Triangle Pause/Resume");
    } else if (current === "browser") {
      hints.push("Square Fullscreen", "Triangle Send link");
    }
    hints.push(t("hint.tabs", "L1/R1 Tabs"), t("hint.menu", "Options Menu"));

    f.fill(
      byId("button-hints"),
      hints.map(function (text) {
        return f.el("span", { text: text });
      })
    );
  }

  function renderChromeFor(section) {
    var t = window.HUB.i18n.t;
    byId("section-title").textContent = t("nav." + section, section);
    byId("section-sub").textContent = t("sub." + section, "");
    renderHints();
  }

  function renderTopbar(state) {
    var f = fmt();
    if (!state) return;

    var console_ = state.console || {};
    var chip = byId("chip-console");
    var pip = chip.querySelector(".pip");
    var label = chip.querySelector("span");
    var connected = !!console_.connected;

    pip.className = "pip " + (connected ? "pip--ok" : "pip--danger");
    label.textContent = connected
      ? (console_.mode === "mock" ? "mock daemon" : "console " + console_.ip)
      : (console_.mode === "mock" ? "mock not answering" : "console offline");

    byId("pip-console").className = "pip " + (connected ? "pip--ok" : "pip--danger");

    var level = state.safety_level || "incomplete";
    byId("pip-safety").className =
      "pip " + (level === "attention" ? "pip--danger" : level === "checks_pass" ? "pip--ok" : "pip--warn");

    /* Storage chip prefers the console's internal volume, falling back to the PC. */
    var storage = state.storage || {};
    var volume = null;
    if (storage.console && storage.console.available && storage.console.volumes.length) {
      volume = storage.console.volumes[0];
    } else if (storage.host && storage.host.length) {
      volume = storage.host[0];
    }
    byId("chip-storage").textContent = volume
      ? volume.free_label + " free" + (volume.source === "host" ? " (PC)" : "")
      : "storage unknown";

    var transfers = state.transfers || {};
    var rate = Number(transfers.total_rate_bps || 0);
    byId("chip-rate").textContent = rate > 0 ? f.rate(rate) : transfers.active ? "installing" : "idle";

    var queuePip = byId("pip-queue");
    var busy = (transfers.active || 0) + (transfers.pending || 0) > 0;
    f.show(queuePip, busy);
    if (busy) queuePip.className = "pip pip--warn";
  }

  /* ------------------------------------------------------------------- poll */

  function pollOnce() {
    if (inFlight) return Promise.resolve(null);
    inFlight = true;
    return window.HUB.api
      .state()
      .then(function (state) {
        inFlight = false;
        consecutiveFailures = 0;
        lastState = state;
        renderTopbar(state);
        window.HUB.library.syncQueueState(state.transfers);
        window.HUB.library.syncStatus(state.library);
        window.HUB.transfers.render(state.transfers);
        if (current === "console") window.HUB.consoleView.render(state);
        return state;
      })
      .catch(function (error) {
        inFlight = false;
        consecutiveFailures += 1;
        /* Say something once, then stay quiet: a sleeping PC should not produce a
           toast per second. */
        if (consecutiveFailures === 3) {
          window.HUB.toast.error("Lost contact with the hub. Retrying quietly.", { duration: 8000 });
        }
        return null;
      });
  }

  /* ---------------------------------------------------------------- routing */

  function showSection(name) {
    if (SECTIONS.indexOf(name) < 0) return;
    current = name;

    SECTIONS.forEach(function (section) {
      var node = byId("section-" + section);
      if (node) node.classList.toggle("is-active", section === name);
    });

    var buttons = document.querySelectorAll(".rail__btn[data-section]");
    for (var i = 0; i < buttons.length; i += 1) {
      buttons[i].classList.toggle("is-active", buttons[i].getAttribute("data-section") === name);
    }

    renderChromeFor(name);

    /* Lazy-load the views that are not part of the 1 Hz payload. */
    if (name === "safety") window.HUB.safety.load();
    if (name === "recovery") {
      window.HUB.recovery.load();
      window.HUB.recovery.loadGuide();
    }
    if (name === "settings") {
      window.HUB.settings.load();
      window.HUB.bgm.load();
    }
    if (name === "console") {
      if (lastState) window.HUB.consoleView.render(lastState);
      window.HUB.consoleView.loadKlog();
      window.HUB.consoleView.loadActivity();
    }
    if (name === "browser") {
      window.HUB.browser.loadBookmarks();
      window.HUB.browser.loadHistory();
    }

    window.HUB.nav.focusFirst(byId("section-" + name), { silent: true });
    window.HUB.gamepad.rumble(40, 0.18);
  }

  /* --------------------------------------------------------------- keyboard */

  function bindKeyboard() {
    document.addEventListener("keydown", function (event) {
      if (window.HUB.osk.isOpen()) return; /* the keyboard owns input while open */

      var tag = event.target && event.target.tagName;
      var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";

      if (event.key === "`" || event.key === "~") {
        event.preventDefault();
        window.HUB.logs.toggle();
        return;
      }

      if (typing && event.key !== "Escape") return;

      switch (event.key) {
        case "ArrowUp":
          event.preventDefault();
          window.HUB.nav.move("up");
          break;
        case "ArrowDown":
          event.preventDefault();
          window.HUB.nav.move("down");
          break;
        case "ArrowLeft":
          event.preventDefault();
          window.HUB.nav.move("left");
          break;
        case "ArrowRight":
          event.preventDefault();
          window.HUB.nav.move("right");
          break;
        case "Enter":
          event.preventDefault();
          window.HUB.nav.activate();
          break;
        case "Escape":
          event.preventDefault();
          app.goBack();
          break;
        case "PageUp":
          event.preventDefault();
          window.HUB.nav.page("up");
          break;
        case "PageDown":
          event.preventDefault();
          window.HUB.nav.page("down");
          break;
        case "[":
          app.cycleSection(-1);
          break;
        case "]":
          app.cycleSection(1);
          break;
        case "/":
          event.preventDefault();
          if (current === "library") window.HUB.library.focusSearch();
          break;
        default:
          break;
      }
    });
  }

  /* -------------------------------------------------------------------- app */

  var app = {
    boot: function () {
      var body = document.body;
      pollInterval = Math.max(400, Number(body.getAttribute("data-poll")) || 1000);

      window.HUB.nav.init();
      window.HUB.osk.init();
      window.HUB.modal.init();
      window.HUB.toast.setPosition(body.getAttribute("data-toast-position"));

      window.HUB.library.init();
      window.HUB.transfers.init();
      window.HUB.consoleView.init();
      window.HUB.browser.init();
      window.HUB.safety.init();
      window.HUB.recovery.init();
      window.HUB.settings.init();
      window.HUB.logs.init();
      window.HUB.bgm.init();

      app.applyUiPreferences({
        cards_per_row: Number(body.getAttribute("data-cards")) || 5,
        overscan_percent: Number(body.getAttribute("data-overscan")) || 5,
        performance_mode: body.getAttribute("data-performance") === "1",
        language: body.getAttribute("data-language") || "en"
      });

      var railButtons = document.querySelectorAll(".rail__btn[data-section]");
      for (var i = 0; i < railButtons.length; i += 1) {
        railButtons[i].addEventListener("click", function (event) {
          showSection(event.currentTarget.getAttribute("data-section"));
        });
      }

      bindKeyboard();

      var hasPad = window.HUB.gamepad.init();
      if (!hasPad) {
        window.HUB.toast.info(
          "This browser exposes no Gamepad API, so controller input is unavailable. Arrow keys, Enter and Escape work.",
          { duration: 9000 }
        );
      }

      renderChromeFor(current);
      window.HUB.library.load().then(function () {
        window.HUB.nav.focusFirst(byId("section-library"), { silent: true });
      });
      window.HUB.transfers.load();
      app.startPolling();

      /* Pause polling while the tab is hidden; the console browser keeps timers
         running in the background otherwise. */
      document.addEventListener("visibilitychange", function () {
        if (document.hidden) app.stopPolling();
        else app.startPolling();
      });
    },

    applyUiPreferences: function (ui) {
      var root = document.documentElement;
      if (ui.cards_per_row) root.style.setProperty("--cards-per-row", String(ui.cards_per_row));
      if (ui.overscan_percent !== undefined) root.style.setProperty("--overscan", ui.overscan_percent + "%");
      if (ui.performance_mode !== undefined) {
        document.body.setAttribute("data-performance", ui.performance_mode ? "1" : "0");
      }
      if (ui.language) window.HUB.i18n.setLanguage(ui.language);
      if (ui.sfx_enabled !== undefined) window.HUB.sfx.setEnabled(ui.sfx_enabled);
      if (ui.rumble_enabled !== undefined) window.HUB.gamepad.setRumbleEnabled(ui.rumble_enabled);
      if (ui.bgm_enabled !== undefined) window.HUB.bgm.setEnabled(ui.bgm_enabled);
      if (ui.bgm_volume !== undefined) window.HUB.bgm.setVolume(ui.bgm_volume);
      renderChromeFor(current);
    },

    refreshChrome: function () {
      window.HUB.i18n.apply(document);
      renderChromeFor(current);
    },

    startPolling: function () {
      if (polling) return;
      polling = true;
      pollOnce();
      pollTimer = window.setInterval(pollOnce, pollInterval);
    },

    stopPolling: function () {
      polling = false;
      window.clearInterval(pollTimer);
      pollTimer = 0;
    },

    refreshNow: function () {
      return pollOnce();
    },

    pokeTransfers: function () {
      /* Called right after an action that changes the queue, so the UI updates
         without waiting for the next tick. */
      window.setTimeout(pollOnce, 120);
    },

    showSection: showSection,

    cycleSection: function (delta) {
      var index = SECTIONS.indexOf(current);
      var next = (index + delta + SECTIONS.length) % SECTIONS.length;
      showSection(SECTIONS[next]);
    },

    goBack: function () {
      if (window.HUB.osk.isOpen()) {
        window.HUB.osk.close(false);
        return;
      }
      if (window.HUB.modal.dismiss()) return;
      if (window.HUB.logs.isOpen()) {
        window.HUB.logs.close();
        return;
      }
      if (window.HUB.browser.isFullscreen()) {
        window.HUB.browser.toggleFullscreen();
        return;
      }
      if (current !== "library") {
        showSection("library");
        return;
      }
      /* Already home: put focus on the rail, which is where Back lands in the
         console's own UI. */
      window.HUB.nav.focusGroup("rail");
      window.HUB.sfx.back();
    },

    handleSectionButton: function (name) {
      var handlers = {
        library: window.HUB.library.handlePadButton,
        queue: window.HUB.transfers.handlePadButton,
        browser: window.HUB.browser.handlePadButton
      };
      var handler = handlers[current];
      if (handler && handler(name)) {
        window.HUB.gamepad.rumble(55, 0.3);
        return true;
      }
      if (name === "options") {
        window.HUB.nav.activate();
        return true;
      }
      return false;
    },

    section: function () {
      return current;
    },

    state: function () {
      return lastState;
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.app = app;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", app.boot);
  } else {
    app.boot();
  }
})(window, document);
