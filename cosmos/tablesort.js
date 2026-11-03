/* Click-to-sort table headers (spreadsheet style) for the COSMOS trackers. */
(function () {
  "use strict";

  var collator;
  try {
    collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
  } catch (e) {
    collator = null;
  }

  function compareText(a, b) {
    if (collator) return collator.compare(a, b);
    return a.toLowerCase().localeCompare(b.toLowerCase());
  }

  function normalize(value) {
    return String(value == null ? "" : value)
      .replace(/\s+/g, " ")
      .trim();
  }

  function isBlank(value) {
    return value === "" || /^[\u2014\u2013-]+$/.test(value);
  }

  /* "none" standing means no requirement: sort it below every numeric value. */
  function isNone(value) {
    return value.toLowerCase() === "none";
  }

  function originalIndex(row) {
    return parseInt(row.getAttribute("data-sort-index"), 10) || 0;
  }

  function sortValue(row, index) {
    var cell = row.cells[index];
    if (!cell) return "";
    var explicit = cell.getAttribute("data-sort");
    if (explicit !== null) return normalize(explicit);
    if (cell.classList.contains("main") && row.dataset.name) return normalize(row.dataset.name);
    return normalize(cell.textContent);
  }

  function sortTable(table, header, direction) {
    var headers = Array.prototype.slice.call(header.parentNode.cells);
    var index = headers.indexOf(header);
    var tbody = table.tBodies[0];
    if (!tbody) return;

    var rows = Array.prototype.slice.call(tbody.rows);
    if (!rows.length) return;

    rows.forEach(function (row, i) {
      if (!row.hasAttribute("data-sort-index")) row.setAttribute("data-sort-index", String(i));
    });

    var multiplier = direction === "desc" ? -1 : 1;

    rows.sort(function (a, b) {
      var av = sortValue(a, index);
      var bv = sortValue(b, index);
      var aBlank = isBlank(av);
      var bBlank = isBlank(bv);

      if (aBlank || bBlank) {
        if (aBlank && bBlank) return originalIndex(a) - originalIndex(b);
        return aBlank ? 1 : -1;
      }

      var aNone = isNone(av);
      var bNone = isNone(bv);
      if (aNone || bNone) {
        if (aNone && bNone) return originalIndex(a) - originalIndex(b);
        return (aNone ? -1 : 1) * multiplier;
      }

      var result = compareText(av, bv);
      if (result === 0) return originalIndex(a) - originalIndex(b);
      return result * multiplier;
    });

    var fragment = document.createDocumentFragment();
    rows.forEach(function (row) {
      fragment.appendChild(row);
    });
    tbody.appendChild(fragment);

    headers.forEach(function (th) {
      if (th === header) {
        th.setAttribute("data-dir", direction);
        th.setAttribute("aria-sort", direction === "asc" ? "ascending" : "descending");
      } else if (th.hasAttribute("data-dir")) {
        th.removeAttribute("data-dir");
        th.setAttribute("aria-sort", "none");
      }
    });
  }

  function makeSortable(header, table) {
    if (header.classList.contains("chk-cell")) return;
    var label = normalize(header.textContent);
    if (!label) return;

    header.classList.add("sortable");
    header.setAttribute("aria-sort", "none");
    header.setAttribute("title", "Sort by " + label);
    header.tabIndex = 0;

    function toggle() {
      var next = header.getAttribute("data-dir") === "asc" ? "desc" : "asc";
      sortTable(table, header, next);
    }

    header.addEventListener("click", toggle);
    header.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
        event.preventDefault();
        toggle();
      }
    });
  }

  var DEFAULT_SORT = "Standing req";

  Array.prototype.forEach.call(document.querySelectorAll("table"), function (table) {
    var head = table.tHead;
    if (!head || !head.rows.length) return;
    var headerRow = head.rows[head.rows.length - 1];
    var defaultHeader = null;
    Array.prototype.forEach.call(headerRow.cells, function (header) {
      makeSortable(header, table);
      if (!defaultHeader && normalize(header.textContent) === DEFAULT_SORT) {
        defaultHeader = header;
      }
    });
    /* Agent tables start with the lowest standing requirement first. */
    if (defaultHeader) sortTable(table, defaultHeader, "asc");
  });
})();
