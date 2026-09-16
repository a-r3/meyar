(() => {
  "use strict";
  const heading = document.querySelector("[data-canonical-ranking-url]");
  const canonicalUrl = heading?.dataset.canonicalRankingUrl;
  if (canonicalUrl) {
    window.history.replaceState({}, "", canonicalUrl);
  }
})();
