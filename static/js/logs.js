/* ===========================================================================
   Developer log drawer.

   Streams the backend's ring buffer. Polling only runs while the drawer is open, so
   a hidden debug panel never costs the console a request per second. Opened with the
   terminal button in the rail or the tilde key.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var drawer = null;
  var linesHost = null;
  var metaHost = null;
  var timer = 0;
  var lastSeq = 0;
  var open = false;

  function fmt() {
    return window.HUB.fmt;
  }

  function appendLines(lines) {
    var f = fmt();
    var atBottom = linesHost.scrollTop + linesHost.clientHeight >= linesHost.scrollHeight - 30;

    lines.forEach(function (line) {
      linesHost.appendChild(
        f.el("div.log-line", { class: "lvl-" + line.level }, [
          f.el("span.ts", { text: line.time }),
          f.el("span.lvl", { text: line.level }),
          document.createTextNode(line.message)
        ])
      );
    });

    /* Keep the DOM bounded; the server's buffer is the real history. */
    while (linesHost.children.length > 600) {
      linesHost.removeChild(linesHost.firstChild);
    }
    if (atBottom) linesHost.scrollTop = linesHost.scrollHeight;
  }

  function poll() {
    window.HUB.api
      .logs(lastSeq)
      .then(function (response) {
        if (response.lines && response.lines.length) {
          appendLines(response.lines);
          lastSeq = response.last_seq;
        }
        metaHost.textContent = "buffer " + response.capacity + " lines · seq " + response.last_seq;
      })
      .catch(function () {
        metaHost.textContent = "hub not responding";
      });
  }

  var logs = {
    init: function () {
      drawer = document.getElementById("log-drawer");
      linesHost = document.getElementById("log-lines");
      metaHost = document.getElementById("log-meta");

      document.getElementById("btn-log-drawer").addEventListener("click", logs.toggle);
      document.getElementById("log-close").addEventListener("click", logs.close);
      document.getElementById("log-klog").addEventListener("click", function () {
        window.HUB.api
          .klog()
          .then(function (response) {
            var lines = (response.lines || []).map(function (line) {
              return { time: "klog", level: "INFO", message: line };
            });
            if (!lines.length) {
              window.HUB.toast.info(response.message || "No kernel log lines returned.");
              return;
            }
            appendLines(lines);
          })
          .catch(function (error) {
            window.HUB.toast.fromError(error, "Kernel log");
          });
      });
    },

    isOpen: function () {
      return open;
    },

    toggle: function () {
      if (open) logs.close();
      else logs.openDrawer();
    },

    openDrawer: function () {
      if (open) return;
      open = true;
      drawer.classList.add("is-open");
      poll();
      timer = window.setInterval(poll, 1200);
      window.HUB.sfx.toggle();
    },

    close: function () {
      if (!open) return;
      open = false;
      drawer.classList.remove("is-open");
      window.clearInterval(timer);
      timer = 0;
      window.HUB.sfx.back();
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.logs = logs;
})(window, document);
