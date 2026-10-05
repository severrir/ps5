/* ===========================================================================
   DualSense input via the HTML5 Gamepad API.

   Polled inside requestAnimationFrame, because the Gamepad API has no events for
   button state -- you must read the snapshot each frame. Edge detection gives one
   action per press; the D-pad and left stick auto-repeat after a hold, with the same
   delay/rate the console UI uses so it feels native.

   Button mapping (standard layout indices):
     0 Cross     confirm / activate
     1 Circle    back
     2 Square    pin (Library) / move up (Queue) / fullscreen (Browser)
     3 Triangle  quick queue (Library) / pause-resume (Queue) / send link (Browser)
     4 L1  5 R1  previous / next tab
     6 L2  7 R2  page up / page down
     9 Options   context menu
     12-15       D-pad up / down / left / right

   If the API is unavailable -- some firmware versions do not expose it inside the
   browser -- everything still works from the keyboard, and app.js says so once.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var BUTTONS = {
    0: "cross",
    1: "circle",
    2: "square",
    3: "triangle",
    4: "l1",
    5: "r1",
    6: "l2",
    7: "r2",
    8: "create",
    9: "options",
    10: "l3",
    11: "r3",
    12: "up",
    13: "down",
    14: "left",
    15: "right",
    16: "ps",
    17: "touchpad"
  };

  var REPEATABLE = { up: true, down: true, left: true, right: true, l2: true, r2: true };
  var REPEAT_DELAY_MS = 380;
  var REPEAT_RATE_MS = 110;
  var STICK_THRESHOLD = 0.55;

  var held = {};
  var running = false;
  var supported = false;
  var everSeen = false;
  var rumbleEnabled = true;

  function now() {
    return window.performance && window.performance.now ? window.performance.now() : Date.now();
  }

  function pads() {
    if (!navigator.getGamepads) return [];
    var list = navigator.getGamepads();
    var out = [];
    for (var i = 0; i < list.length; i += 1) {
      if (list[i] && list[i].connected) out.push(list[i]);
    }
    return out;
  }

  function pressed(pad) {
    var active = {};
    for (var i = 0; i < pad.buttons.length; i += 1) {
      var name = BUTTONS[i];
      if (!name) continue;
      var button = pad.buttons[i];
      var value = typeof button === "object" ? button.value : button;
      if (button.pressed || value > 0.5) active[name] = true;
    }

    /* Left stick doubles as the D-pad, with a dead zone. */
    var axes = pad.axes || [];
    if (axes.length >= 2) {
      if (axes[1] < -STICK_THRESHOLD) active.up = true;
      if (axes[1] > STICK_THRESHOLD) active.down = true;
      if (axes[0] < -STICK_THRESHOLD) active.left = true;
      if (axes[0] > STICK_THRESHOLD) active.right = true;
    }
    return active;
  }

  function dispatch(name) {
    /* Overlays get first refusal, so focus can never escape them. */
    if (window.HUB.osk.isOpen()) {
      if (window.HUB.osk.handlePadButton(name)) return;
      if (name === "up" || name === "down" || name === "left" || name === "right") {
        window.HUB.nav.move(name);
      }
      return;
    }

    if (window.HUB.modal.isOpen()) {
      if (name === "cross") window.HUB.nav.activate();
      else if (name === "circle") window.HUB.modal.dismiss();
      else if (name === "up" || name === "down" || name === "left" || name === "right") window.HUB.nav.move(name);
      return;
    }

    if (window.HUB.logs.isOpen() && name === "circle") {
      window.HUB.logs.close();
      return;
    }

    switch (name) {
      case "up":
      case "down":
      case "left":
      case "right":
        window.HUB.nav.move(name);
        return;
      case "cross":
        window.HUB.nav.activate();
        return;
      case "circle":
        window.HUB.app.goBack();
        return;
      case "l1":
        window.HUB.app.cycleSection(-1);
        return;
      case "r1":
        window.HUB.app.cycleSection(1);
        return;
      case "l2":
        window.HUB.nav.page("up");
        return;
      case "r2":
        window.HUB.nav.page("down");
        return;
      case "touchpad":
        window.HUB.logs.toggle();
        return;
      default:
        /* Square / Triangle / Options are section-specific. */
        window.HUB.app.handleSectionButton(name);
    }
  }

  function tick() {
    if (!running) return;
    var list = pads();
    if (list.length) {
      if (!everSeen) {
        everSeen = true;
        window.HUB.toast.ok("Controller connected: " + (list[0].id || "gamepad").split("(")[0].trim());
      }
      var active = {};
      list.forEach(function (pad) {
        var padActive = pressed(pad);
        Object.keys(padActive).forEach(function (key) {
          active[key] = true;
        });
      });

      var time = now();
      Object.keys(active).forEach(function (name) {
        var state = held[name];
        if (!state) {
          held[name] = { since: time, next: time + REPEAT_DELAY_MS };
          dispatch(name);
        } else if (REPEATABLE[name] && time >= state.next) {
          state.next = time + REPEAT_RATE_MS;
          dispatch(name);
        }
      });

      Object.keys(held).forEach(function (name) {
        if (!active[name]) delete held[name];
      });
    }
    window.requestAnimationFrame(tick);
  }

  var gamepad = {
    init: function () {
      supported = !!navigator.getGamepads;
      if (!supported) return false;

      window.addEventListener("gamepadconnected", function () {
        if (!running) gamepad.start();
      });
      window.addEventListener("gamepaddisconnected", function () {
        held = {};
      });

      gamepad.start();
      return true;
    },

    start: function () {
      if (running || !supported) return;
      running = true;
      window.requestAnimationFrame(tick);
    },

    stop: function () {
      running = false;
    },

    isSupported: function () {
      return supported;
    },

    isConnected: function () {
      return pads().length > 0;
    },

    setRumbleEnabled: function (value) {
      rumbleEnabled = !!value;
    },

    /* Short, feature-detected haptic nudge. Silently does nothing when the pad or
       firmware has no actuator, which is the common case over Bluetooth. */
    rumble: function (durationMs, strength) {
      if (!rumbleEnabled) return;
      var list = pads();
      for (var i = 0; i < list.length; i += 1) {
        var pad = list[i];
        var actuator = pad.vibrationActuator;
        if (actuator && actuator.playEffect) {
          try {
            actuator.playEffect("dual-rumble", {
              startDelay: 0,
              duration: durationMs || 90,
              weakMagnitude: strength === undefined ? 0.35 : strength,
              strongMagnitude: strength === undefined ? 0.25 : strength * 0.7
            });
          } catch (error) {
            /* Not supported here; nothing to report. */
          }
        }
      }
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.gamepad = gamepad;
})(window, document);
