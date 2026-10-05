/* ===========================================================================
   Background music player.

   Plays whatever you put in the local bgm/ folder. The hub ships no audio: nothing
   is bundled and nothing is downloaded, which keeps both the air-gap claim and the
   copyright position simple.

   Browsers block audio that starts without a user gesture, so playback begins on
   the first press of the play control rather than on load, and a blocked attempt is
   reported honestly instead of silently doing nothing.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var audio = null;
  var tracks = [];
  var index = 0;
  var enabled = false;
  var controlsHost = null;
  var countLabel = null;
  var playButton = null;

  function fmt() {
    return window.HUB.fmt;
  }

  function label() {
    if (!tracks.length) return "No tracks in bgm/";
    return tracks[index].name;
  }

  function renderControls() {
    var f = fmt();
    if (!controlsHost) return;

    playButton = f.el("button.btn", {
      type: "button",
      "data-focusable": true,
      "data-nav-group": "settings",
      text: audio && !audio.paused ? "Pause" : "Play"
    });
    playButton.addEventListener("click", bgm.toggle);

    var previous = f.el("button.icon-btn", {
      type: "button",
      title: "Previous track",
      "data-focusable": true,
      "data-nav-group": "settings"
    });
    previous.appendChild(f.icon("back", 16));
    previous.addEventListener("click", function () {
      bgm.step(-1);
    });

    var next = f.el("button.icon-btn", {
      type: "button",
      title: "Next track",
      "data-focusable": true,
      "data-nav-group": "settings"
    });
    next.appendChild(f.icon("forward", 16));
    next.addEventListener("click", function () {
      bgm.step(1);
    });

    f.fill(controlsHost, [previous, playButton, next, f.el("span.small.muted", { text: label() })]);
  }

  var bgm = {
    init: function () {
      controlsHost = document.getElementById("bgm-controls");
      countLabel = document.getElementById("bgm-count");

      var volume = document.getElementById("set-bgm-volume");
      volume.addEventListener("input", function () {
        bgm.setVolume(Number(volume.value) / 100);
      });
    },

    load: function () {
      return window.HUB.api
        .bgm()
        .then(function (response) {
          tracks = response.tracks || [];
          countLabel.textContent = tracks.length
            ? tracks.length + " track(s) in bgm/"
            : "No tracks found. Drop audio files into " + response.folder;
          renderControls();
          return response;
        })
        .catch(function () {
          /* Optional feature; stay quiet. */
        });
    },

    setEnabled: function (value) {
      enabled = !!value;
      if (!enabled) bgm.stop();
      return enabled;
    },

    setVolume: function (value) {
      if (audio) audio.volume = Math.max(0, Math.min(1, value));
    },

    play: function () {
      if (!tracks.length) {
        window.HUB.toast.warn("No audio files in the bgm/ folder.");
        return;
      }
      if (!audio) {
        audio = new Audio();
        audio.loop = false;
        audio.addEventListener("ended", function () {
          bgm.step(1);
        });
        audio.addEventListener("error", function () {
          window.HUB.toast.warn("Could not play " + label() + ".");
        });
      }
      audio.src = tracks[index].url;
      var volumeInput = document.getElementById("set-bgm-volume");
      audio.volume = volumeInput ? Number(volumeInput.value) / 100 : 0.4;

      var attempt = audio.play();
      if (attempt && attempt.catch) {
        attempt.catch(function () {
          window.HUB.toast.info("The browser blocked autoplay. Press Play once to start audio.");
        });
      }
      renderControls();
    },

    stop: function () {
      if (audio) {
        audio.pause();
        renderControls();
      }
    },

    toggle: function () {
      if (audio && !audio.paused) bgm.stop();
      else bgm.play();
    },

    step: function (delta) {
      if (!tracks.length) return;
      index = (index + delta + tracks.length) % tracks.length;
      bgm.play();
    },

    isEnabled: function () {
      return enabled;
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.bgm = bgm;
})(window, document);
