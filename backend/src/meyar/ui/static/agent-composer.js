"use strict";
/*
 * MEYAR AI composer submit-guard (progressive; the server remains
 * authoritative — /ui/agent's own Form(min_length=1) rejects an empty
 * message regardless of whether this script ran, rendering the existing
 * safe Azerbaijani "Forma məlumatlarını yoxlayın." error page). Keeps
 * the send button disabled while the message is empty/whitespace-only,
 * so an empty submission never triggers the browser's own
 * native-language "Please fill out this field" validation popup —
 * see docs/DECISIONS.md D-044.
 */
(function () {
  function init() {
    var textarea = document.getElementById("message");
    var button = document.getElementById("composer-send");
    if (!textarea || !button) return;

    function sync() {
      button.disabled = textarea.value.trim().length === 0;
    }

    sync();
    textarea.addEventListener("input", sync);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
