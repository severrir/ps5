/* ===========================================================================
   The library grid.

   Filtering and sorting happen client-side against the full item list, so typing
   in the search box redraws instantly instead of waiting on a round trip. Wrapping
   the query in slashes (/mario.*kart/) switches to a regular expression; anything
   else is treated as space-separated substrings, all of which must match.

   Cover art is either covers/<TITLEID>.png from this machine or a generated
   placeholder: a gradient hashed from the title plus its initials. Nothing is
   fetched from the internet to draw this grid.

   Buttons: Cross opens the item sheet, Triangle queues immediately, Square pins.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var grid = null;
  var emptyState = null;
  var emptyDetail = null;
  var countChip = null;
  var searchInput = null;
  var sortSelect = null;
  var categorySelect = null;
  var favoritesButton = null;

  var allItems = [];
  var shown = [];
  var queuedIds = {};
  var status = {};
  var favoritesOnly = false;
  var searchTimer = 0;
  var lastSignature = "";

  function fmt() {
    return window.HUB.fmt;
  }

  /* ------------------------------------------------------------- filtering */

  function matcher(query) {
    var text = String(query || "").trim();
    if (!text) {
      return function () {
        return true;
      };
    }

    /* /pattern/ or /pattern/i -> regex. A malformed pattern falls back to a
       literal search rather than throwing while the user is mid-type. */
    var regexMatch = text.match(/^\/(.*)\/([a-z]*)$/);
    if (regexMatch) {
      try {
        var regex = new RegExp(regexMatch[1], regexMatch[2].indexOf("i") >= 0 ? "i" : "i");
        return function (haystack) {
          return regex.test(haystack);
        };
      } catch (error) {
        /* fall through to substring matching */
      }
    }

    var terms = text.toLowerCase().split(/\s+/).filter(function (term) {
      return term.length > 0;
    });
    return function (haystack) {
      var lower = haystack.toLowerCase();
      for (var i = 0; i < terms.length; i += 1) {
        if (lower.indexOf(terms[i]) < 0) return false;
      }
      return true;
    };
  }

  function haystackFor(item) {
    return [
      item.title,
      item.original_title,
      item.filename,
      item.title_id,
      item.content_id,
      item.category,
      (item.tags || []).join(" ")
    ]
      .filter(Boolean)
      .join(" ");
  }

  function applyFilters() {
    var test = matcher(searchInput ? searchInput.value : "");
    var category = categorySelect ? categorySelect.value : "";
    var sort = sortSelect ? sortSelect.value : "title";

    shown = allItems.filter(function (item) {
      if (favoritesOnly && !item.favorite) return false;
      if (category && String(item.category || "").toLowerCase() !== category.toLowerCase()) return false;
      return test(haystackFor(item));
    });

    shown.sort(function (a, b) {
      if (sort === "size") return (b.size_bytes || 0) - (a.size_bytes || 0);
      if (sort === "recent") return (b.first_seen || 0) - (a.first_seen || 0);
      if (sort === "category") {
        var left = String(a.category || "").toLowerCase();
        var right = String(b.category || "").toLowerCase();
        if (left !== right) return left < right ? -1 : 1;
      }
      return String(a.title || "").toLowerCase().localeCompare(String(b.title || "").toLowerCase());
    });

    /* Pinned titles always lead, whatever the sort. */
    shown.sort(function (a, b) {
      return (a.favorite ? 0 : 1) - (b.favorite ? 0 : 1);
    });

    render();
  }

  /* ------------------------------------------------------------- rendering */

  function artFor(item) {
    var f = fmt();
    var hue = f.hue(item.title_id || item.title || item.filename);
    var art = f.el("div.card__art");

    if (item.cover) {
      var image = f.el("img", { src: item.cover, alt: "", loading: "lazy", decoding: "async" });
      /* If a cover file disappears between scans, fall back silently. */
      image.addEventListener("error", function () {
        if (image.parentNode) image.parentNode.removeChild(image);
        art.insertBefore(placeholder(item, hue), art.firstChild);
      });
      art.appendChild(image);
    } else {
      art.appendChild(placeholder(item, hue));
    }

    var badges = f.el("div.card__badges");
    if (item.category) {
      badges.appendChild(f.el("span.badge", { text: item.category }));
    }
    if (item.source === "url") {
      badges.appendChild(f.el("span.badge.badge--muted", { text: "Link" }));
    }
    if (queuedIds[item.id]) {
      badges.appendChild(f.el("span.badge.badge--accent", { text: queuedIds[item.id] }));
    }
    if (item.favorite) {
      badges.appendChild(f.el("span.badge.badge--accent.badge--pin", { text: "Pinned" }));
    }
    art.appendChild(badges);
    return { art: art, hue: hue };
  }

  function placeholder(item, hue) {
    var f = fmt();
    return f.el(
      "div.card__placeholder",
      { style: { "--art-hue": String(hue) } },
      f.el("span", { text: f.initials(item.title || item.filename) })
    );
  }

  function metaFor(item) {
    var f = fmt();
    var parts = [];
    parts.push(f.el("span", { text: item.source === "url" ? "remote link" : item.size_label || f.bytes(item.size_bytes) }));
    if (item.title_id) parts.push(f.el("span", { text: item.title_id }));
    if (item.version) parts.push(f.el("span", { text: "v" + item.version }));
    if (item.platform && item.platform !== "unknown") parts.push(f.el("span", { text: item.platform.toUpperCase() }));
    return f.el("div.card__meta", null, parts);
  }

  function cardFor(item) {
    var f = fmt();
    var art = artFor(item);

    var card = f.el(
      "button.card",
      {
        type: "button",
        "data-focusable": true,
        "data-nav-group": "library",
        "data-item": item.id,
        "data-focus-id": "item-" + item.id,
        "data-hue": String(art.hue),
        class: queuedIds[item.id] ? "is-queued" : "",
        title: item.filename || item.title
      },
      [
        art.art,
        f.el("div.card__body", null, [
          f.el("div.card__title", { text: item.title || item.filename }),
          metaFor(item)
        ])
      ]
    );

    card.addEventListener("click", function () {
      library.openSheet(item.id);
    });
    return card;
  }

  function render() {
    var f = fmt();
    if (!grid) return;

    /* Skip the DOM work when nothing visible changed -- this runs on every poll. */
    var signature = shown
      .map(function (item) {
        return item.id + ":" + (item.favorite ? 1 : 0) + ":" + (queuedIds[item.id] || "") + ":" + item.title;
      })
      .join("|");
    if (signature === lastSignature) return;
    lastSignature = signature;

    var focusId = null;
    var current = window.HUB.nav.current();
    if (current && current.getAttribute && current.getAttribute("data-focus-id")) {
      focusId = current.getAttribute("data-focus-id");
    }

    f.clear(grid);
    shown.forEach(function (item) {
      grid.appendChild(cardFor(item));
    });

    countChip.textContent =
      shown.length === allItems.length
        ? shown.length + (shown.length === 1 ? " item" : " items")
        : shown.length + " of " + allItems.length;

    var empty = shown.length === 0;
    f.show(grid, !empty);
    f.show(emptyState, empty);
    if (empty) {
      emptyDetail.textContent = allItems.length
        ? "Nothing matches that filter. Clear the search or switch category."
        : "No .pkg files were found in " +
          ((status.directories && status.directories.length
            ? status.directories.join(", ")
            : "the configured folders")) +
          ". Point library.pkg_directories at your package folder in Settings, then rescan.";
    }

    window.HUB.nav.refresh(focusId);
  }

  function refreshCategories(categories) {
    if (!categorySelect) return;
    var existing = categorySelect.value;
    var f = fmt();
    var options = [f.el("option", { value: "", text: "All categories" })];
    (categories || []).forEach(function (name) {
      options.push(f.el("option", { value: name, text: name }));
    });
    f.fill(categorySelect, options);
    categorySelect.value = existing;
  }

  /* --------------------------------------------------------------- actions */

  function queue(itemId, quiet) {
    return window.HUB.api
      .enqueue(itemId)
      .then(function (response) {
        if (!quiet) window.HUB.toast.ok(response.message || "Queued.");
        window.HUB.sfx.done();
        window.HUB.app.pokeTransfers();
        return response;
      })
      .catch(function (error) {
        window.HUB.sfx.error();
        window.HUB.toast.fromError(error, "Could not queue");
      });
  }

  var library = {
    init: function () {
      grid = document.getElementById("library-grid");
      emptyState = document.getElementById("library-empty");
      emptyDetail = document.getElementById("library-empty-detail");
      countChip = document.getElementById("library-count");
      searchInput = document.getElementById("library-search");
      sortSelect = document.getElementById("library-sort");
      categorySelect = document.getElementById("library-category");
      favoritesButton = document.getElementById("library-favorites");

      searchInput.addEventListener("input", function () {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(applyFilters, 90);
      });
      sortSelect.addEventListener("change", applyFilters);
      categorySelect.addEventListener("change", applyFilters);

      favoritesButton.addEventListener("click", function () {
        favoritesOnly = !favoritesOnly;
        favoritesButton.classList.toggle("btn--accent", favoritesOnly);
        window.HUB.sfx.toggle();
        applyFilters();
      });

      document.getElementById("library-rescan").addEventListener("click", function () {
        window.HUB.toast.info("Scanning package folders...");
        window.HUB.api
          .rescan()
          .then(function (response) {
            window.HUB.toast.ok("Found " + response.items + " item(s) in " + response.duration_ms + " ms.");
            return library.load();
          })
          .catch(function (error) {
            window.HUB.toast.fromError(error, "Rescan failed");
          });
      });

      document.getElementById("library-add-url").addEventListener("click", function () {
        window.HUB.modal
          .prompt({
            title: "Add a package link",
            lines: [
              "Paste a direct link to a .pkg. The console fetches it itself, so this hub never downloads or relays the file."
            ],
            label: "URL",
            placeholder: "Direct link to a .pkg file",
            confirmLabel: "Add"
          })
          .then(function (url) {
            if (!url) return null;
            return window.HUB.api.addUrl(url).then(function (response) {
              window.HUB.toast.ok("Added " + response.item.title);
              return library.load();
            });
          })
          .catch(function (error) {
            window.HUB.toast.fromError(error, "Could not add link");
          });
      });

      document.getElementById("library-queue-all").addEventListener("click", function () {
        if (!shown.length) {
          window.HUB.toast.warn("Nothing to queue.");
          return;
        }
        window.HUB.modal
          .confirm({
            title: "Queue " + shown.length + " package(s)?",
            lines: [
              "They install one after another, in the order shown. You can reorder or pause anything from the Queue tab."
            ],
            confirmLabel: "Queue all"
          })
          .then(function (confirmed) {
            if (!confirmed) return null;
            return window.HUB.api
              .enqueue(
                shown.map(function (item) {
                  return item.id;
                })
              )
              .then(function (response) {
                window.HUB.toast.ok(response.message || "Queued.");
                window.HUB.app.pokeTransfers();
              });
          })
          .catch(function (error) {
            window.HUB.toast.fromError(error, "Batch queue failed");
          });
      });

      document.getElementById("library-empty-settings").addEventListener("click", function () {
        window.HUB.app.showSection("settings");
      });
    },

    load: function () {
      return window.HUB.api
        .library({})
        .then(function (response) {
          allItems = response.items || [];
          status = response.status || {};
          refreshCategories(response.categories);
          lastSignature = "";
          applyFilters();
          return response;
        })
        .catch(function (error) {
          window.HUB.toast.fromError(error, "Could not load the library");
        });
    },

    /* Called by the poll loop with the transfers snapshot, to badge queued cards. */
    syncQueueState: function (snapshot) {
      var next = {};
      (snapshot && snapshot.queue ? snapshot.queue : []).forEach(function (entry) {
        if (entry.state === "done" || entry.state === "cancelled") return;
        next[entry.item_id] =
          entry.state === "installing" || entry.state === "starting"
            ? Math.round(entry.progress || 0) + "%"
            : window.HUB.i18n.t("state." + entry.state, entry.state);
      });
      var changed = JSON.stringify(next) !== JSON.stringify(queuedIds);
      queuedIds = next;
      if (changed) {
        lastSignature = "";
        render();
      }
    },

    syncStatus: function (libraryStatus) {
      var previous = status.last_scan;
      status = libraryStatus || status;
      /* Reload the item list when the scanner reports a newer scan. */
      if (status.last_scan && previous && status.last_scan !== previous) library.load();
    },

    itemById: function (itemId) {
      for (var i = 0; i < allItems.length; i += 1) {
        if (allItems[i].id === itemId) return allItems[i];
      }
      return null;
    },

    shownIds: function () {
      return shown.map(function (item) {
        return item.id;
      });
    },

    /* The item sheet: everything you can do with one package. */
    openSheet: function (itemId) {
      var item = library.itemById(itemId);
      if (!item) return;
      var f = fmt();

      var lines = [];
      var details = f.el("dl.kv");
      function row(key, value) {
        if (!value) return;
        details.appendChild(f.el("dt", { text: key }));
        details.appendChild(f.el("dd", { text: String(value) }));
      }
      row("File", item.filename);
      row("Size", item.source === "url" ? "unknown (remote)" : item.size_label);
      row("Title ID", item.title_id);
      row("Content ID", item.content_id);
      row("Version", item.version);
      row("Category", item.category);
      row("Platform", item.platform && item.platform !== "unknown" ? item.platform.toUpperCase() : null);
      row("Metadata from", (item.metadata_source || []).join(", "));
      row("Added", f.dateTime(item.first_seen));
      if (item.source === "url") row("URL", item.url);
      lines.push(details);

      if ((item.warnings || []).length) {
        lines.push(
          f.el("div.callout.callout--warn", null, [
            f.el("strong", { text: "Parser notes" }),
            f.el("span", { text: item.warnings.join(" ") })
          ])
        );
      }

      var actions = [
        { label: "Close", value: null },
        {
          label: item.favorite ? "Unpin" : "Pin to top",
          onClick: function () {
            window.HUB.api
              .setFavorite(item.id, !item.favorite)
              .then(function () {
                return library.load();
              })
              .catch(function (error) {
                window.HUB.toast.fromError(error, "Could not pin");
              });
          },
          value: null
        },
        {
          label: "Rename",
          onClick: function () {
            window.setTimeout(function () {
              library.rename(item);
            }, 10);
          },
          value: null
        },
        {
          label: "Install now",
          variant: "accent",
          onClick: function () {
            queue(item.id);
          },
          value: null
        }
      ];

      if (item.source === "local") {
        actions.splice(3, 0, {
          label: "Delete file",
          variant: "danger",
          onClick: function () {
            window.setTimeout(function () {
              library.deleteFile(item);
            }, 10);
          },
          value: null
        });
      } else {
        actions.splice(3, 0, {
          label: "Remove link",
          onClick: function () {
            window.HUB.api
              .forget(item.id)
              .then(function (response) {
                window.HUB.toast.ok(response.message);
                return library.load();
              })
              .catch(function (error) {
                window.HUB.toast.fromError(error, "Could not remove");
              });
          },
          value: null
        });
      }

      window.HUB.modal.open({
        title: item.title || item.filename,
        lines: lines,
        actions: actions
      });
    },

    rename: function (item) {
      window.HUB.modal
        .prompt({
          title: "Rename display title",
          lines: ["This only changes how the title shows here. The file on disk keeps its name."],
          label: "Title",
          value: item.title || "",
          hint: "Leave empty to go back to " + (item.original_title || item.filename),
          confirmLabel: "Save"
        })
        .then(function (value) {
          if (value === null) return null;
          return window.HUB.api.rename(item.id, value).then(function (response) {
            window.HUB.toast.ok("Renamed to " + response.title);
            return library.load();
          });
        })
        .catch(function (error) {
          window.HUB.toast.fromError(error, "Rename failed");
        });
    },

    deleteFile: function (item) {
      window.HUB.modal
        .confirm({
          title: "Delete this package file?",
          lines: [
            item.filename + " (" + item.size_label + ") will be deleted from " + item.path + ".",
            "This cannot be undone, and it is separate from hiding the item. Only files inside your configured package folders can be deleted."
          ],
          confirmLabel: "Delete file",
          danger: true
        })
        .then(function (confirmed) {
          if (!confirmed) return null;
          return window.HUB.api.deleteFile(item.id).then(function (response) {
            window.HUB.toast.ok(response.message);
            return library.load();
          });
        })
        .catch(function (error) {
          window.HUB.toast.fromError(error, "Delete failed");
        });
    },

    /* Pad shortcuts, routed from gamepad.js when the Library tab is active. */
    handlePadButton: function (name) {
      var current = window.HUB.nav.current();
      var itemId = current && current.getAttribute ? current.getAttribute("data-item") : null;
      if (!itemId) return false;
      var item = library.itemById(itemId);
      if (!item) return false;

      if (name === "triangle") {
        queue(item.id);
        return true;
      }
      if (name === "square") {
        window.HUB.api
          .setFavorite(item.id, !item.favorite)
          .then(function () {
            window.HUB.sfx.toggle();
            return library.load();
          })
          .catch(function (error) {
            window.HUB.toast.fromError(error, "Could not pin");
          });
        return true;
      }
      if (name === "options") {
        library.openSheet(item.id);
        return true;
      }
      return false;
    },

    focusSearch: function () {
      if (searchInput) window.HUB.osk.open(searchInput);
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.library = library;
})(window, document);
