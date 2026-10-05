/* ===========================================================================
   Built-in browser.

   The page is embedded in an iframe rendered by the console itself. The hub never
   fetches, proxies or rewrites the page -- that is a deliberate limit, because a
   server-side proxy would make this machine a relay for external traffic and void
   the air-gap audit next door in the Safety tab.

   The honest consequence: sites that send X-Frame-Options or a restrictive
   frame-ancestors CSP will not embed. Rather than leaving a blank white box, a load
   watchdog detects it and says so, and offers the two things that still work --
   retry, or send the link to the install queue.

   Back/forward walk a local history array, because a cross-origin iframe's own
   history is not readable from here.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var LOAD_TIMEOUT_MS = 7000;

  var frame = null;
  var urlInput = null;
  var overlay = null;
  var overlayTitle = null;
  var overlayText = null;
  var overlayActions = null;
  var bookmarksHost = null;
  var historyHost = null;

  var stack = [];
  var cursor = -1;
  var loadTimer = 0;
  var pending = null;
  var home = "about:blank";
  var bookmarks = [];

  function fmt() {
    return window.HUB.fmt;
  }

  function normalise(raw) {
    var text = String(raw || "").trim();
    if (!text) return "";
    if (text === "about:blank") return text;
    if (!/^[a-z][a-z0-9+.-]*:/i.test(text)) text = "https://" + text;
    return text;
  }

  function setOverlay(title, text, actions) {
    var f = fmt();
    overlayTitle.textContent = title;
    overlayText.textContent = text;
    f.fill(overlayActions, actions || []);
    overlay.classList.add("is-visible");
  }

  function hideOverlay() {
    overlay.classList.remove("is-visible");
  }

  function overlayButton(label, variant, handler) {
    var f = fmt();
    var button = f.el("button.btn", {
      type: "button",
      class: variant ? "btn--" + variant : "",
      "data-focusable": true,
      "data-nav-group": "browser-overlay",
      text: label
    });
    button.addEventListener("click", handler);
    return button;
  }

  function blocked(url) {
    setOverlay(
      "That site refused to be embedded",
      "It sent an X-Frame-Options or frame-ancestors header, which tells the browser not to display it inside another page. " +
        "The hub does not proxy pages, so there is nothing to work around here - but the link can still go straight to the install queue.",
      [
        overlayButton("Try again", null, function () {
          browser.navigate(url, { record: false });
        }),
        overlayButton("Send link to queue", "accent", function () {
          browser.sendToQueue(url);
        }),
        overlayButton("Back", null, function () {
          browser.back();
        })
      ]
    );
  }

  function recordVisit(url, framed) {
    window.HUB.api.addBrowserHistory(url, url, framed).then(browser.loadHistory, function () {
      /* History is a convenience; a failure to record must not interrupt browsing. */
    });
  }

  var browser = {
    init: function () {
      frame = document.getElementById("browser-frame");
      urlInput = document.getElementById("browser-url");
      overlay = document.getElementById("browser-overlay");
      overlayTitle = document.getElementById("browser-overlay-title");
      overlayText = document.getElementById("browser-overlay-text");
      overlayActions = document.getElementById("browser-overlay-actions");
      bookmarksHost = document.getElementById("browser-bookmarks");
      historyHost = document.getElementById("browser-history");

      frame.addEventListener("load", function () {
        window.clearTimeout(loadTimer);
        if (!pending) return;
        var url = pending;
        pending = null;

        /* A same-origin-accessible but empty document is the other way a refusal
           shows up in some engines. */
        var looksEmpty = false;
        try {
          var doc = frame.contentDocument;
          if (doc && doc.body && doc.body.childNodes.length === 0 && url !== "about:blank") {
            looksEmpty = true;
          }
        } catch (error) {
          /* Cross-origin: the page loaded, we simply cannot inspect it. Good. */
        }

        if (looksEmpty) {
          blocked(url);
          recordVisit(url, false);
          return;
        }
        hideOverlay();
        if (url !== "about:blank") recordVisit(url, true);
      });

      document.getElementById("browser-go").addEventListener("click", function () {
        browser.navigate(urlInput.value);
      });
      urlInput.addEventListener("keydown", function (event) {
        if (event.key === "Enter") browser.navigate(urlInput.value);
      });
      document.getElementById("browser-home").addEventListener("click", function () {
        browser.navigate(home);
      });
      document.getElementById("browser-back").addEventListener("click", browser.back);
      document.getElementById("browser-forward").addEventListener("click", browser.forward);
      document.getElementById("browser-fullscreen").addEventListener("click", browser.toggleFullscreen);

      document.getElementById("browser-bookmark-add").addEventListener("click", function () {
        var url = normalise(urlInput.value);
        if (!url || url === "about:blank") {
          window.HUB.toast.warn("Load a page first.");
          return;
        }
        window.HUB.modal
          .prompt({
            title: "Bookmark this page",
            label: "Name",
            value: url.replace(/^https?:\/\//, "").split("/")[0],
            confirmLabel: "Save"
          })
          .then(function (name) {
            if (name === null) return null;
            bookmarks = bookmarks.concat([{ title: name || url, url: url }]);
            return window.HUB.api.saveBookmarks(bookmarks).then(function (response) {
              window.HUB.toast.ok(response.message);
              bookmarks = response.bookmarks;
              browser.renderBookmarks();
            });
          })
          .catch(function (error) {
            window.HUB.toast.fromError(error, "Could not save the bookmark");
          });
      });

      document.getElementById("browser-history-clear").addEventListener("click", function () {
        window.HUB.modal
          .confirm({
            title: "Clear browser history?",
            lines: ["Only this hub's local history list is cleared. The console's own browser is untouched."],
            confirmLabel: "Clear"
          })
          .then(function (confirmed) {
            if (!confirmed) return null;
            return window.HUB.api.clearBrowserHistory().then(function (response) {
              window.HUB.toast.ok(response.message);
              return browser.loadHistory();
            });
          })
          .catch(function (error) {
            window.HUB.toast.fromError(error, "Could not clear history");
          });
      });

      setOverlay(
        "Nothing loaded",
        "Type an address above and press Go. Many sites refuse to be embedded; if one does, this panel will say so rather than showing a blank frame.",
        [
          overlayButton("Open home page", "accent", function () {
            browser.navigate(home);
          })
        ]
      );
    },

    navigate: function (raw, options) {
      var settings = options || {};
      var url = normalise(raw);
      if (!url) {
        window.HUB.toast.warn("Enter an address first.");
        return;
      }

      urlInput.value = url;
      pending = url;
      window.clearTimeout(loadTimer);

      if (url === "about:blank") {
        frame.setAttribute("src", "about:blank");
        setOverlay("Blank page", "Enter an address above to start browsing.", []);
        pending = null;
        return;
      }

      setOverlay("Loading", url, []);
      frame.setAttribute("src", url);

      loadTimer = window.setTimeout(function () {
        if (pending === url) {
          pending = null;
          blocked(url);
          recordVisit(url, false);
        }
      }, LOAD_TIMEOUT_MS);

      if (settings.record !== false) {
        /* Drop any forward entries, exactly like a real address bar. */
        stack = stack.slice(0, cursor + 1);
        stack.push(url);
        cursor = stack.length - 1;
      }
    },

    back: function () {
      if (cursor <= 0) {
        window.HUB.toast.info("No earlier page in this session.");
        return;
      }
      cursor -= 1;
      browser.navigate(stack[cursor], { record: false });
    },

    forward: function () {
      if (cursor >= stack.length - 1) {
        window.HUB.toast.info("No later page in this session.");
        return;
      }
      cursor += 1;
      browser.navigate(stack[cursor], { record: false });
    },

    toggleFullscreen: function () {
      var isFull = document.body.classList.toggle("is-browser-fullscreen");
      var wrap = frame.parentNode;
      try {
        if (isFull && wrap.requestFullscreen) wrap.requestFullscreen();
        else if (isFull && wrap.webkitRequestFullscreen) wrap.webkitRequestFullscreen();
        else if (!isFull && document.exitFullscreen && document.fullscreenElement) document.exitFullscreen();
        else if (!isFull && document.webkitExitFullscreen && document.webkitFullscreenElement) {
          document.webkitExitFullscreen();
        }
      } catch (error) {
        /* The CSS fallback above already gave us the full viewport. */
      }
      window.HUB.sfx.toggle();
    },

    sendToQueue: function (url) {
      window.HUB.api
        .addUrl(url || urlInput.value)
        .then(function (response) {
          return window.HUB.api.enqueue(response.item.id).then(function () {
            window.HUB.toast.ok("Sent to the install queue: " + response.item.title);
            window.HUB.app.pokeTransfers();
          });
        })
        .catch(function (error) {
          window.HUB.toast.fromError(error, "Could not queue that link");
        });
    },

    renderBookmarks: function () {
      var f = fmt();
      if (!bookmarksHost) return;
      if (!bookmarks.length) {
        f.fill(bookmarksHost, f.el("p.small.muted", { text: "No bookmarks yet. Load a page and press Bookmark current." }));
        return;
      }
      f.fill(
        bookmarksHost,
        bookmarks.map(function (entry, index) {
          var button = f.el("button.btn", {
            type: "button",
            "data-focusable": true,
            "data-nav-group": "browser",
            text: entry.title
          });
          button.addEventListener("click", function () {
            browser.navigate(entry.url);
          });
          var remove = f.el("button.icon-btn", {
            type: "button",
            title: "Remove bookmark",
            "data-focusable": true,
            "data-nav-group": "browser"
          });
          remove.appendChild(f.icon("x", 15));
          remove.addEventListener("click", function () {
            bookmarks = bookmarks.filter(function (_entry, position) {
              return position !== index;
            });
            window.HUB.api.saveBookmarks(bookmarks).then(function (response) {
              bookmarks = response.bookmarks;
              browser.renderBookmarks();
              window.HUB.nav.refresh();
            });
          });
          return f.el("span.inline", null, [button, remove]);
        })
      );
    },

    loadBookmarks: function () {
      return window.HUB.api
        .bookmarks()
        .then(function (response) {
          bookmarks = response.bookmarks || [];
          home = response.home || "about:blank";
          browser.renderBookmarks();
          return response;
        })
        .catch(function () {
          /* Bookmarks are optional chrome; stay quiet on failure. */
        });
    },

    loadHistory: function () {
      var f = fmt();
      return window.HUB.api
        .browserHistory()
        .then(function (response) {
          var entries = response.history || [];
          if (!entries.length) {
            f.fill(historyHost, f.el("p.small.muted", { text: "No history in this session." }));
            return response;
          }
          f.fill(
            historyHost,
            entries.slice(0, 40).map(function (entry) {
              var row = f.el(
                "div.row",
                { "data-focusable": true, "data-nav-group": "browser", role: "button" },
                [
                  f.el("div.row__index", null, f.icon(entry.framed ? "browser" : "x", 16)),
                  f.el("div.row__main", null, [
                    f.el("div.row__title", null, [f.el("em", { text: entry.title || entry.url })]),
                    f.el("div.row__sub", null, [
                      f.el("span", { text: f.relative(entry.at) }),
                      f.el("span", { text: entry.framed ? "embedded" : "refused embedding" })
                    ])
                  ]),
                  f.el("div.row__actions")
                ]
              );
              row.addEventListener("click", function () {
                browser.navigate(entry.url);
              });
              return row;
            })
          );
          return response;
        })
        .catch(function () {
          /* Same as bookmarks: optional. */
        });
    },

    handlePadButton: function (name) {
      if (name === "triangle") {
        browser.sendToQueue(urlInput.value);
        return true;
      }
      if (name === "square") {
        browser.toggleFullscreen();
        return true;
      }
      return false;
    },

    isFullscreen: function () {
      return document.body.classList.contains("is-browser-fullscreen");
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.browser = browser;
})(window, document);
