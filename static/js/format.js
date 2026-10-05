/* ===========================================================================
   Formatting helpers and tiny DOM builders.

   Mirrors server/pkg_parser.format_size so a size never looks different depending
   on which side rendered it.

   HUB.fmt.el() is used everywhere instead of innerHTML with interpolated data:
   package titles come from filenames and parsed headers, and a filename is
   attacker-controlled enough to be worth never injecting as markup.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var UNITS = ["B", "KB", "MB", "GB", "TB"];

  var fmt = {
    bytes: function (value) {
      var n = Number(value) || 0;
      if (n < 0) n = 0;
      for (var i = 0; i < UNITS.length; i += 1) {
        if (n < 1024 || i === UNITS.length - 1) {
          return (i <= 1 ? Math.round(n) : n.toFixed(2)) + " " + UNITS[i];
        }
        n /= 1024;
      }
      return n + " B";
    },

    rate: function (bytesPerSecond) {
      var n = Number(bytesPerSecond) || 0;
      if (n <= 0) return "";
      return fmt.bytes(n) + "/s";
    },

    percent: function (value) {
      var n = Number(value) || 0;
      return (n < 10 ? n.toFixed(1) : Math.round(n)) + "%";
    },

    duration: function (seconds) {
      var s = Math.max(0, Math.round(Number(seconds) || 0));
      if (s < 60) return s + "s";
      var m = Math.floor(s / 60);
      if (m < 60) return m + "m " + (s % 60) + "s";
      var h = Math.floor(m / 60);
      return h + "h " + (m % 60) + "m";
    },

    clock: function (timestamp) {
      if (!timestamp) return "";
      var date = new Date(Number(timestamp) * 1000);
      return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    },

    dateTime: function (timestamp) {
      if (!timestamp) return "";
      var date = new Date(Number(timestamp) * 1000);
      return date.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + fmt.clock(timestamp);
    },

    relative: function (timestamp) {
      if (!timestamp) return "never";
      var delta = Date.now() / 1000 - Number(timestamp);
      if (delta < 5) return "just now";
      if (delta < 90) return Math.round(delta) + "s ago";
      if (delta < 5400) return Math.round(delta / 60) + "m ago";
      if (delta < 172800) return Math.round(delta / 3600) + "h ago";
      return Math.round(delta / 86400) + "d ago";
    },

    /* Stable hue from a string, for the generated cover placeholders and the
       ambient accent. Same title always gets the same colour. */
    hue: function (text) {
      var str = String(text || "");
      var hash = 0;
      for (var i = 0; i < str.length; i += 1) {
        hash = (hash * 31 + str.charCodeAt(i)) % 100000;
      }
      /* Bias away from muddy yellows/greens so placeholders stay in the
         PlayStation register: blues, violets, teals, the occasional magenta. */
      var palette = [198, 212, 224, 238, 252, 268, 284, 300, 316, 186, 174];
      return palette[hash % palette.length];
    },

    initials: function (title) {
      var words = String(title || "?")
        .replace(/[^\w\s]/g, " ")
        .split(/\s+/)
        .filter(function (word) {
          return word.length > 0;
        });
      if (words.length === 0) return "?";
      if (words.length === 1) return words[0].substring(0, 2).toUpperCase();
      return (words[0][0] + words[1][0]).toUpperCase();
    },

    /* ------------------------------------------------------------------ DOM */

    /* el("div.card", {attrs}, [children|string]) */
    el: function (spec, attrs, children) {
      var parts = String(spec).split(".");
      var tag = parts.shift() || "div";
      var node = document.createElement(tag);
      if (parts.length) node.className = parts.join(" ");

      if (attrs) {
        Object.keys(attrs).forEach(function (key) {
          var value = attrs[key];
          if (value === null || value === undefined || value === false) return;
          if (key === "text") {
            node.textContent = String(value);
          } else if (key === "class") {
            node.className = node.className ? node.className + " " + value : String(value);
          } else if (key === "dataset") {
            Object.keys(value).forEach(function (dataKey) {
              node.setAttribute("data-" + dataKey, String(value[dataKey]));
            });
          } else if (key === "on") {
            Object.keys(value).forEach(function (event) {
              node.addEventListener(event, value[event]);
            });
          } else if (key === "style") {
            /* Set through CSSOM, which the CSP permits (unlike a style attribute
               written into markup). */
            Object.keys(value).forEach(function (prop) {
              node.style.setProperty(prop, String(value[prop]));
            });
          } else if (value === true) {
            node.setAttribute(key, "");
          } else {
            node.setAttribute(key, String(value));
          }
        });
      }

      if (children !== undefined && children !== null) {
        var list = Array.isArray(children) ? children : [children];
        list.forEach(function (child) {
          if (child === null || child === undefined || child === false) return;
          node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
        });
      }
      return node;
    },

    /* An <svg><use> reference into the inline sprite. */
    icon: function (name, size) {
      var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      if (size) {
        svg.setAttribute("width", String(size));
        svg.setAttribute("height", String(size));
      }
      var use = document.createElementNS("http://www.w3.org/2000/svg", "use");
      use.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href", "#i-" + name);
      svg.appendChild(use);
      return svg;
    },

    clear: function (node) {
      while (node && node.firstChild) node.removeChild(node.firstChild);
      return node;
    },

    /* Replace a container's children in one go. */
    fill: function (node, children) {
      if (!node) return node;
      fmt.clear(node);
      var list = Array.isArray(children) ? children : [children];
      list.forEach(function (child) {
        if (child === null || child === undefined || child === false) return;
        node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
      });
      return node;
    },

    show: function (node, visible) {
      if (!node) return;
      if (visible) node.classList.remove("hidden");
      else node.classList.add("hidden");
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.fmt = fmt;
})(window, document);
