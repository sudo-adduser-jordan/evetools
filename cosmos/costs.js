(function () {
  "use strict";

  var bar = document.querySelector("[data-cost-section]");
  if (!bar) return;

  var slug = (window.location.pathname.split("/").pop() || "")
    .replace(/-cosmos\.html$/, "")
    .replace(/\.html$/, "");
  if (!slug) return;

  var valueEl = bar.querySelector(".cost-value");
  var metaEl = bar.querySelector(".cost-meta");

  function formatIsk(value) {
    if (!isFinite(value) || value <= 0) return "\u2014";
    if (value >= 1e9) return (value / 1e9).toFixed(2) + "b isk";
    if (value >= 1e6) return (value / 1e6).toFixed(2) + "m isk";
    if (value >= 1e3) return (value / 1e3).toFixed(1) + "k isk";
    return Math.round(value).toLocaleString("en-US") + " isk";
  }

  function formatDate(value) {
    if (!value) return "";
    var date = new Date(value);
    if (isNaN(date.getTime())) return "";
    return date.toISOString().slice(0, 10);
  }

  fetch("data/costs.json", { cache: "no-store" })
    .then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    })
    .then(function (data) {
      var line = data && data.lines && data.lines[slug];
      var section = line && line.shopping;
      if (!section) {
        if (metaEl) metaEl.textContent = "No price data for this line";
        return;
      }

      var total = Number(section.total || 0);
      var priced = Number(section.pricedItems || 0);
      var count = Number(section.totalItems || 0);
      var unpriced = Array.isArray(section.unpriced) ? section.unpriced : [];
      var complete = unpriced.length === 0 && (count === 0 || priced >= count);

      if (valueEl) {
        valueEl.textContent = complete && priced > 0 ? formatIsk(total) : "\u2014";
        valueEl.title = complete && priced > 0 ? Math.round(total).toLocaleString("en-US") + " isk" : "";
      }

      if (metaEl) {
        var bits = [];
        var updated = formatDate(section.updated || data.generatedAt);
        if (updated) bits.push("updated " + updated);
        metaEl.textContent = bits.join(" \u00b7 ");
        var sourceTitle =
          "Estimated from Fuzzwork Jita 4-4 market aggregates and EVE Ref public contracts.";
        if (unpriced.length) {
          sourceTitle +=
            " Unpriced: " + unpriced.slice(0, 12).join(", ") +
            (unpriced.length > 12 ? " +" + (unpriced.length - 12) + " more" : "");
        }
        metaEl.title = sourceTitle;
      }

      bar.classList.toggle("partial", !complete);
    })
    .catch(function () {
      if (valueEl) valueEl.textContent = "\u2014";
      if (metaEl) metaEl.textContent = "Price data unavailable";
    });
})();
