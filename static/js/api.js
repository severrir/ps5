/* ===========================================================================
   The one place that talks to the server.

   Every request is same-origin and relative -- there is no base URL to configure
   and no way for a call here to reach anything but this hub. The air-gap audit in
   the Safety tab greps for exactly that property.

   The server's error shape is {ok:false, error, message}; this layer turns it into
   a rejected promise carrying both, so callers can switch on .slug and show
   .message in a toast without parsing anything.
   =========================================================================== */
(function (window) {
  "use strict";

  function ApiError(message, slug, status, payload) {
    this.name = "ApiError";
    this.message = message || "Request failed";
    this.slug = slug || "error";
    this.status = status || 0;
    this.payload = payload || null;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  function request(method, path, body) {
    var options = {
      method: method,
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      cache: "no-store"
    };
    if (body !== undefined && body !== null) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }

    return fetch(path, options).then(function (response) {
      var contentType = response.headers.get("Content-Type") || "";
      var parse = contentType.indexOf("json") >= 0 ? response.json() : response.text();

      return parse.then(
        function (payload) {
          if (typeof payload === "string") {
            if (!response.ok) {
              throw new ApiError(payload || response.statusText, "http_error", response.status, null);
            }
            return { ok: true, raw: payload };
          }
          if (!response.ok || payload.ok === false) {
            throw new ApiError(
              payload.message || response.statusText || "Request failed",
              payload.error || "http_error",
              response.status,
              payload
            );
          }
          return payload;
        },
        function () {
          throw new ApiError("Malformed response from the hub", "bad_response", response.status, null);
        }
      );
    }, function (networkError) {
      /* The console going to sleep, the PC going down, Wi-Fi dropping: all land
         here. Keep the message plain, because it shows up in a toast on a TV. */
      throw new ApiError(
        "Cannot reach the hub (" + (networkError && networkError.message ? networkError.message : "network error") + ")",
        "unreachable",
        0,
        null
      );
    });
  }

  var api = {
    ApiError: ApiError,

    get: function (path) {
      return request("GET", path);
    },
    post: function (path, body) {
      return request("POST", path, body === undefined ? {} : body);
    },
    del: function (path) {
      return request("DELETE", path);
    },

    /* ----------------------------------------------------------- library -- */
    library: function (params) {
      var query = [];
      Object.keys(params || {}).forEach(function (key) {
        var value = params[key];
        if (value === "" || value === null || value === undefined || value === false) return;
        query.push(encodeURIComponent(key) + "=" + encodeURIComponent(String(value)));
      });
      return api.get("/api/library" + (query.length ? "?" + query.join("&") : ""));
    },
    rescan: function () {
      return api.post("/api/library/rescan");
    },
    setFavorite: function (id, favorite) {
      return api.post("/api/library/item/" + encodeURIComponent(id) + "/favorite", { favorite: favorite });
    },
    rename: function (id, title) {
      return api.post("/api/library/item/" + encodeURIComponent(id) + "/rename", { title: title });
    },
    forget: function (id) {
      return api.del("/api/library/item/" + encodeURIComponent(id));
    },
    deleteFile: function (id) {
      return api.post("/api/library/item/" + encodeURIComponent(id) + "/delete-file", { confirm: true });
    },
    addUrl: function (url, title) {
      return api.post("/api/library/url", { url: url, title: title });
    },

    /* --------------------------------------------------------- transfers -- */
    transfers: function () {
      return api.get("/api/transfers");
    },
    enqueue: function (ids) {
      return Array.isArray(ids)
        ? api.post("/api/transfers", { item_ids: ids })
        : api.post("/api/transfers", { item_id: ids });
    },
    transferAction: function (id, action) {
      return api.post("/api/transfers/" + encodeURIComponent(id) + "/" + action);
    },
    moveTransfer: function (id, delta) {
      return api.post("/api/transfers/" + encodeURIComponent(id) + "/move", { delta: delta });
    },
    removeTransfer: function (id) {
      return api.del("/api/transfers/" + encodeURIComponent(id));
    },
    clearFinished: function () {
      return api.post("/api/transfers/clear-finished");
    },
    setThrottle: function (mbps) {
      return api.post("/api/transfers/throttle", { mbps: mbps });
    },
    setConcurrency: function (value) {
      return api.post("/api/transfers/concurrency", { value: value });
    },

    /* ------------------------------------------------------------ system -- */
    state: function () {
      return api.get("/api/system/state");
    },
    settings: function () {
      return api.get("/api/system/settings");
    },
    saveSettings: function (changes) {
      return api.post("/api/system/settings", changes);
    },
    logs: function (after) {
      return api.get("/api/system/logs?after=" + encodeURIComponent(String(after || 0)));
    },
    events: function (limit) {
      return api.get("/api/system/events?limit=" + encodeURIComponent(String(limit || 100)));
    },
    klog: function () {
      return api.get("/api/system/klog");
    },
    bgm: function () {
      return api.get("/api/system/bgm");
    },
    versionCheck: function () {
      return api.get("/api/system/version");
    },
    backup: function () {
      return api.post("/api/system/backup");
    },

    /* ------------------------------------------------------------ safety -- */
    safety: function () {
      return api.get("/api/safety");
    },
    runAudit: function () {
      return api.post("/api/safety/audit");
    },
    runProbe: function () {
      return api.post("/api/safety/probe");
    },
    setProbing: function (enabled) {
      return api.post("/api/safety/probing", { enabled: enabled });
    },
    setChecklist: function (id, checked) {
      return api.post("/api/safety/checklist", { id: id, checked: checked });
    },

    /* -------------------------------------------------------------- wipe -- */
    wipeStatus: function () {
      return api.get("/api/wipe/status");
    },
    safeModeGuide: function () {
      return api.get("/api/wipe/safe-mode");
    },
    setPin: function (pin, currentPin) {
      return api.post("/api/wipe/pin", { pin: pin, current_pin: currentPin });
    },
    wipePlan: function (pin, phrase) {
      return api.post("/api/wipe/plan", { pin: pin, phrase: phrase });
    },
    wipeExecute: function (token) {
      return api.post("/api/wipe/execute", { token: token });
    },

    /* ----------------------------------------------------------- browser -- */
    browserHistory: function () {
      return api.get("/api/browser/history");
    },
    addBrowserHistory: function (url, title, framed) {
      return api.post("/api/browser/history", { url: url, title: title, framed: framed });
    },
    clearBrowserHistory: function () {
      return api.del("/api/browser/history");
    },
    bookmarks: function () {
      return api.get("/api/browser/bookmarks");
    },
    saveBookmarks: function (list) {
      return api.post("/api/browser/bookmarks", { bookmarks: list });
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.api = api;
})(window);
