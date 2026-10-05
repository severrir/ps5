/* ===========================================================================
   Navigation sounds, synthesised in the browser.

   Short WebAudio blips rather than audio files: nothing to download, nothing to
   ship, and the air-gap claim stays intact. Off by default (ui.sfx_enabled),
   because sound on a TV UI is a taste thing.

   The AudioContext is created lazily on first use. Browsers suspend contexts that
   are made before a user gesture, so building it on demand is what keeps the first
   sound from being silently dropped.
   =========================================================================== */
(function (window) {
  "use strict";

  var ctx = null;
  var enabled = false;
  var masterGain = null;

  function context() {
    if (ctx) return ctx;
    var Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    try {
      ctx = new Ctor();
      masterGain = ctx.createGain();
      masterGain.gain.value = 0.16;
      masterGain.connect(ctx.destination);
    } catch (error) {
      ctx = null;
    }
    return ctx;
  }

  function blip(frequency, durationMs, type, volume) {
    if (!enabled) return;
    var audio = context();
    if (!audio) return;
    if (audio.state === "suspended" && audio.resume) {
      audio.resume();
    }

    try {
      var now = audio.currentTime;
      var osc = audio.createOscillator();
      var gain = audio.createGain();
      osc.type = type || "sine";
      osc.frequency.setValueAtTime(frequency, now);
      var peak = volume === undefined ? 1 : volume;
      /* Fast attack, exponential tail: reads as a UI tick rather than a note. */
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(peak, now + 0.008);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + durationMs / 1000);
      osc.connect(gain);
      gain.connect(masterGain);
      osc.start(now);
      osc.stop(now + durationMs / 1000 + 0.02);
    } catch (error) {
      /* A failed blip is never worth surfacing. */
    }
  }

  var sfx = {
    setEnabled: function (value) {
      enabled = !!value;
      if (enabled) context();
      return enabled;
    },
    isEnabled: function () {
      return enabled;
    },
    move: function () {
      blip(620, 55, "sine", 0.55);
    },
    select: function () {
      blip(880, 90, "triangle", 0.8);
    },
    back: function () {
      blip(380, 95, "sine", 0.6);
    },
    toggle: function () {
      blip(740, 70, "square", 0.35);
    },
    error: function () {
      blip(180, 180, "sawtooth", 0.45);
    },
    done: function () {
      blip(660, 90, "triangle", 0.7);
      window.setTimeout(function () {
        blip(990, 140, "triangle", 0.6);
      }, 90);
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.sfx = sfx;
})(window);
