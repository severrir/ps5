/* ===========================================================================
   Spatial focus engine.

   Everything reachable carries data-focusable. Movement is geometric rather than
   index-based: from the focused element's centre, candidates are scored by distance
   along the pressed axis plus a penalty for drifting off it. That means the grid,
   the rail, the queue rows and the on-screen keyboard all navigate correctly
   without anyone maintaining a coordinate table, and a layout change cannot
   silently break the D-pad.

   Scopes keep focus trapped where it belongs: opening the keyboard or a modal
   pushes a scope, closing it pops back and restores the element you came from.
   =========================================================================== */
(function (window, document) {
  "use strict";

  var CROSS_PENALTY = 1.9; /* how much drifting off-axis costs */
  var OVERLAP_BONUS = 0.55; /* reward candidates that line up with the current one */

  var scopes = [];
  var focused = null;
  var lastPointerFocus = null;

  function fmt() {
    return window.HUB.fmt;
  }

  function scopeRoot() {
    if (scopes.length) return scopes[scopes.length - 1].root;
    return document.getElementById("app");
  }

  function isVisible(el) {
    if (!el || el.disabled) return false;
    if (el.classList.contains("hidden")) return false;
    if (el.getAttribute("aria-hidden") === "true") return false;
    /* offsetParent is null for display:none subtrees, which covers inactive
       sections and the closed keyboard without reading computed styles. */
    if (el.offsetParent === null && el.tagName !== "BODY") return false;
    var rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  }

  function candidates() {
    var root = scopeRoot();
    if (!root) return [];
    var nodes = root.querySelectorAll("[data-focusable]");
    var list = [];
    for (var i = 0; i < nodes.length; i += 1) {
      if (isVisible(nodes[i])) list.push(nodes[i]);
    }
    return list;
  }

  function centre(el) {
    var rect = el.getBoundingClientRect();
    return {
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
      left: rect.left,
      right: rect.right,
      top: rect.top,
      bottom: rect.bottom,
      width: rect.width,
      height: rect.height
    };
  }

  function overlap(a, b, axis) {
    if (axis === "x") {
      return Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left)) / Math.max(1, Math.min(a.width, b.width));
    }
    return Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)) / Math.max(1, Math.min(a.height, b.height));
  }

  function score(from, to, direction) {
    var primary;
    var cross;
    var lined;

    if (direction === "left" || direction === "right") {
      primary = direction === "right" ? to.x - from.x : from.x - to.x;
      cross = Math.abs(to.y - from.y);
      lined = overlap(from, to, "y");
    } else {
      primary = direction === "down" ? to.y - from.y : from.y - to.y;
      cross = Math.abs(to.x - from.x);
      lined = overlap(from, to, "x");
    }

    /* Must actually be in the pressed direction, by a margin that ignores
       sub-pixel layout noise. */
    if (primary < 4) return Infinity;
    return primary + cross * CROSS_PENALTY - lined * primary * OVERLAP_BONUS;
  }

  function setAmbient(el) {
    var node = el;
    while (node && node !== document.body) {
      if (node.getAttribute && node.getAttribute("data-hue")) {
        document.documentElement.style.setProperty("--ambient-hue", node.getAttribute("data-hue"));
        return;
      }
      node = node.parentNode;
    }
    document.documentElement.style.setProperty("--ambient-hue", "212");
  }

  var nav = {
    /* --------------------------------------------------------------- focus */

    current: function () {
      return focused && isVisible(focused) ? focused : null;
    },

    focus: function (el, options) {
      var settings = options || {};
      if (!el || !isVisible(el)) return false;

      if (focused && focused !== el) focused.classList.remove("is-focused");
      focused = el;
      el.classList.add("is-focused");

      if (el.scrollIntoView) {
        try {
          el.scrollIntoView({ block: "nearest", inline: "nearest" });
        } catch (error) {
          el.scrollIntoView(false);
        }
      }
      setAmbient(el);

      /* Give real inputs the caret too, so a USB keyboard keeps working. */
      if (settings.dom !== false && el.focus) {
        try {
          el.focus({ preventScroll: true });
        } catch (error) {
          el.focus();
        }
      }
      if (settings.silent !== true) window.HUB.sfx.move();
      return true;
    },

    blur: function () {
      if (focused) focused.classList.remove("is-focused");
      focused = null;
    },

    focusFirst: function (container, options) {
      var root = container || scopeRoot();
      if (!root) return false;
      var nodes = root.querySelectorAll("[data-focusable]");
      for (var i = 0; i < nodes.length; i += 1) {
        if (isVisible(nodes[i])) return nav.focus(nodes[i], options || { silent: true });
      }
      return false;
    },

    focusGroup: function (groupName, options) {
      var root = scopeRoot();
      if (!root) return false;
      var nodes = root.querySelectorAll('[data-nav-group="' + groupName + '"]');
      for (var i = 0; i < nodes.length; i += 1) {
        if (isVisible(nodes[i])) return nav.focus(nodes[i], options || { silent: true });
      }
      return false;
    },

    /* ---------------------------------------------------------- movement  */

    move: function (direction) {
      var list = candidates();
      if (!list.length) return false;

      var currentEl = nav.current();
      if (!currentEl) {
        return nav.focus(list[0], { silent: true });
      }

      var from = centre(currentEl);
      var best = null;
      var bestScore = Infinity;

      for (var i = 0; i < list.length; i += 1) {
        if (list[i] === currentEl) continue;
        var value = score(from, centre(list[i]), direction);
        if (value < bestScore) {
          bestScore = value;
          best = list[i];
        }
      }

      if (best) return nav.focus(best);

      /* Nothing strictly along the axis. For horizontal presses at the end of a
         row, step to the nearest element on the adjacent row instead of dead-
         ending, which is what makes a wrapped grid feel right. */
      if (direction === "left" || direction === "right") {
        var wrapped = null;
        var wrappedScore = Infinity;
        for (var j = 0; j < list.length; j += 1) {
          if (list[j] === currentEl) continue;
          var to = centre(list[j]);
          var sameRow = overlap(from, to, "y") > 0.4;
          if (sameRow) continue;
          var vertical = direction === "right" ? to.y - from.y : from.y - to.y;
          if (vertical < 4) continue;
          var candidateScore = vertical + Math.abs(to.x - (direction === "right" ? 0 : window.innerWidth));
          if (candidateScore < wrappedScore) {
            wrappedScore = candidateScore;
            wrapped = list[j];
          }
        }
        if (wrapped) return nav.focus(wrapped);
      }
      return false;
    },

    /* Page-sized jump, used by L2/R2. */
    page: function (direction) {
      var scroller = nav.scroller();
      if (!scroller) return false;
      var delta = scroller.clientHeight * 0.82 * (direction === "down" ? 1 : -1);
      scroller.scrollTop += delta;
      /* Re-anchor focus to something now on screen so the next D-pad press makes
         sense relative to what the user is looking at. */
      var list = candidates();
      var viewTop = scroller.getBoundingClientRect().top;
      var best = null;
      var bestDistance = Infinity;
      for (var i = 0; i < list.length; i += 1) {
        var rect = list[i].getBoundingClientRect();
        var distance = Math.abs(rect.top - viewTop - 40);
        if (rect.bottom > viewTop && distance < bestDistance) {
          bestDistance = distance;
          best = list[i];
        }
      }
      if (best) nav.focus(best, { silent: true });
      return true;
    },

    scroller: function () {
      var section = document.querySelector(".section.is-active .scroll");
      return section || null;
    },

    /* -------------------------------------------------------- activation  */

    activate: function () {
      var el = nav.current();
      if (!el) return false;

      var tag = el.tagName;
      var type = (el.getAttribute("type") || "").toLowerCase();

      /* Text-ish fields open the on-screen keyboard rather than relying on the
         console's own IME, which is awkward to reach with a pad. */
      if ((tag === "INPUT" && type !== "range" && type !== "checkbox") || tag === "TEXTAREA") {
        window.HUB.osk.open(el);
        return true;
      }
      if (tag === "SELECT") {
        /* Cycle instead of opening a native dropdown: native select popups are
           unreliable to drive with a D-pad in this browser. */
        el.selectedIndex = (el.selectedIndex + 1) % Math.max(1, el.options.length);
        el.dispatchEvent(new Event("change", { bubbles: true }));
        window.HUB.sfx.toggle();
        return true;
      }
      window.HUB.sfx.select();
      el.click();
      return true;
    },

    /* ------------------------------------------------------------ scopes  */

    pushScope: function (root) {
      scopes.push({ root: root, previous: focused });
      return root;
    },

    popScope: function () {
      var scope = scopes.pop();
      if (scope && scope.previous && isVisible(scope.previous)) {
        nav.focus(scope.previous, { silent: true });
      } else {
        nav.focusFirst(null, { silent: true });
      }
      return scope;
    },

    scopeDepth: function () {
      return scopes.length;
    },

    /* Called after a re-render: keep focus if the element survived, otherwise
       land on the nearest thing to where it was. */
    refresh: function (preferredId) {
      if (focused && isVisible(focused) && document.contains(focused)) {
        focused.classList.add("is-focused");
        return;
      }
      if (preferredId) {
        var preferred = document.querySelector('[data-focus-id="' + preferredId + '"]');
        if (preferred && isVisible(preferred)) {
          nav.focus(preferred, { silent: true });
          return;
        }
      }
      focused = null;
    },

    init: function () {
      /* Mouse and touch still work; hovering simply moves the focus ring so the
         two input models never disagree about what is selected. */
      document.addEventListener(
        "mouseover",
        function (event) {
          var target = event.target && event.target.closest ? event.target.closest("[data-focusable]") : null;
          if (target && target !== focused && isVisible(target)) {
            lastPointerFocus = target;
            if (focused) focused.classList.remove("is-focused");
            focused = target;
            target.classList.add("is-focused");
            setAmbient(target);
          }
        },
        true
      );

      document.addEventListener(
        "focusin",
        function (event) {
          var target = event.target && event.target.closest ? event.target.closest("[data-focusable]") : null;
          if (target && target !== focused) {
            if (focused) focused.classList.remove("is-focused");
            focused = target;
            target.classList.add("is-focused");
            setAmbient(target);
          }
        },
        true
      );
    }
  };

  window.HUB = window.HUB || {};
  window.HUB.nav = nav;
})(window, document);
