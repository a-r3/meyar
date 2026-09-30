"use strict";
/*
 * MEYAR AI composer submit-guard (progressive; the server remains
 * authoritative — /ui/agent's canonical length validation rejects an empty
 * message regardless of whether this script ran, rendering a recoverable
 * Azerbaijani error in the authenticated workspace). Keeps
 * the send button disabled while the message is empty/whitespace-only,
 * so an empty submission never triggers the browser's own
 * native-language "Please fill out this field" validation popup —
 * see docs/DECISIONS.md D-044.
 */
(function () {
  function init() {
    var textarea = document.getElementById("message");
    var button = document.getElementById("composer-send");
    var form = document.getElementById("agent-composer");
    if (!textarea || !button) return;

    // The server has already committed this submission. Keep rich live
    // cards on screen, but reload/navigation can now use the canonical GET.
    // Durable server submission state is still the replay authority when
    // JavaScript is absent or a browser re-sends the original POST.
    if (form && form.dataset.completedUrl) {
      window.history.replaceState(null, "", form.dataset.completedUrl);
    }

    function sync() {
      button.disabled = textarea.value.trim().length === 0;
    }

    sync();
    textarea.addEventListener("input", sync);
    window.addEventListener("pageshow", sync);
    if (form) form.addEventListener("submit", function () { button.disabled = true; });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
