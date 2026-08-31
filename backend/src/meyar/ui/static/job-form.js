"use strict";
/*
 * Vacancy-form kind-aware presentation enhancement (progressive; the
 * server remains the source of truth — meyar.ui.service._parse_criterion_row
 * rejects any stray "min_years" on a non-EXPERIENCE row regardless of
 * whether this script ran). Keeps the "Minimum müddət (il)" control
 * disabled and cleared for every criterion kind except EXPERIENCE, so a
 * stale value typed before switching kind is never submitted.
 */
(function () {
  var EXPERIENCE = "EXPERIENCE";
  var APPLICABLE_PLACEHOLDER = "Minimum müddət (il)";
  var NOT_APPLICABLE_PLACEHOLDER = "Tətbiq olunmur";

  function syncRow(select) {
    var row = select.closest("tr");
    if (!row) return;
    var yearsInput = row.querySelector(".js-min-years");
    if (!yearsInput) return;
    var isExperience = select.value === EXPERIENCE;
    yearsInput.disabled = !isExperience;
    yearsInput.placeholder = isExperience
      ? APPLICABLE_PLACEHOLDER
      : NOT_APPLICABLE_PLACEHOLDER;
    if (!isExperience) {
      yearsInput.value = "";
      yearsInput.setAttribute("aria-disabled", "true");
    } else {
      yearsInput.removeAttribute("aria-disabled");
    }
  }

  function init() {
    var selects = document.querySelectorAll(".js-kind-select");
    for (var i = 0; i < selects.length; i += 1) {
      syncRow(selects[i]);
      selects[i].addEventListener("change", function (event) {
        syncRow(event.target);
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
