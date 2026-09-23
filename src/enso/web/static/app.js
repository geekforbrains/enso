// Enso web viewer: progressive enhancement over already-rendered pages.
//
// Every list is complete and usable before this runs. Here, search inputs and selects
// hide rows, sortable headings reorder them, and counts update. Nothing is fetched and no
// stored data is ever inserted as markup; text changes go through textContent and
// visibility through the hidden attribute.
(function () {
  "use strict";

  function text(element, value) {
    if (element) element.textContent = value;
  }

  // -- Filtering ---------------------------------------------------------------

  // A group heading owns every sibling between it and the next heading, which is exactly
  // how a grouped list is written. Read once, so filtering never walks the DOM again.
  function groupsIn(container) {
    return Array.prototype.slice.call(container.querySelectorAll("[data-group]")).map(function (head) {
      var members = [];
      var node = head.nextElementSibling;
      while (node && !node.hasAttribute("data-group")) {
        members.push(node);
        node = node.nextElementSibling;
      }
      return { head: head, members: members, count: head.querySelector("[data-group-count]") };
    });
  }

  // The heading's own count, rewritten from the words the template supplied, so the
  // vocabulary of a page stays in that page rather than in here.
  function countText(element, value) {
    if (!element) return;
    var noun = element.getAttribute(value === 1 ? "data-singular" : "data-plural");
    text(element, noun ? value + " " + noun : String(value));
  }

  function setupFilterable(container) {
    var search = container.querySelector("[data-search]");
    var selects = Array.prototype.slice.call(container.querySelectorAll("select[data-filter]"));
    // Rows are table rows on the file browser and plain list rows everywhere else.
    var rows = Array.prototype.slice.call(container.querySelectorAll("[data-row]"));
    var groups = groupsIn(container);
    var count = container.querySelector("[data-count]");
    var empty = container.querySelector("[data-empty]");

    function apply() {
      var query = search ? search.value.trim().toLowerCase() : "";
      var wanted = selects.map(function (select) {
        return { key: select.getAttribute("data-filter"), value: select.value };
      });
      var visible = 0;
      rows.forEach(function (row) {
        var show = !query || (row.getAttribute("data-text") || "").indexOf(query) !== -1;
        wanted.forEach(function (filter) {
          if (filter.value && row.getAttribute("data-" + filter.key) !== filter.value) show = false;
        });
        row.hidden = !show;
        if (show) visible += 1;
      });
      // A heading over nothing, still counting rows that are no longer there, reads as a
      // bug: it goes when the filter empties its group, and says what is left when it does not.
      groups.forEach(function (group) {
        var shown = group.members.filter(function (member) {
          return !member.hidden;
        }).length;
        group.head.hidden = shown === 0;
        countText(group.count, shown);
      });
      text(count, visible + " of " + rows.length);
      if (empty) empty.hidden = visible !== 0 || rows.length === 0;
    }

    if (search) search.addEventListener("input", apply);
    selects.forEach(function (select) {
      select.addEventListener("change", apply);
    });
    apply();
  }

  // -- Sorting -----------------------------------------------------------------

  function cellValue(row, index) {
    var cell = row.children[index];
    if (!cell) return "";
    var value = cell.getAttribute("data-value");
    return value !== null ? value : cell.textContent.trim();
  }

  function compare(a, b) {
    var numberA = Number(a);
    var numberB = Number(b);
    if (a !== "" && b !== "" && !isNaN(numberA) && !isNaN(numberB)) return numberA - numberB;
    return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
  }

  function setupSortable(table) {
    var headings = Array.prototype.slice.call(table.querySelectorAll("thead th"));
    headings.forEach(function (heading, index) {
      var button = heading.querySelector("button[data-sort]");
      if (!button) return;
      button.addEventListener("click", function () {
        var direction = heading.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
        headings.forEach(function (other) {
          if (other.querySelector("button[data-sort]")) other.setAttribute("aria-sort", "none");
        });
        heading.setAttribute("aria-sort", direction);
        var body = table.tBodies[0];
        if (!body) return;
        var rows = Array.prototype.slice.call(body.rows);
        rows.sort(function (a, b) {
          var result = compare(cellValue(a, index), cellValue(b, index));
          return direction === "ascending" ? result : -result;
        });
        rows.forEach(function (row) {
          body.appendChild(row);
        });
      });
    });
  }

  // -- Forms -------------------------------------------------------------------

  function setupConfirmation(form) {
    var trigger = form.querySelector("[data-confirm-trigger]");
    var fallback = form.querySelector("[data-confirm-fallback]");
    if (!trigger || !fallback) return;
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) event.preventDefault();
    });
    // Only replace the server-rendered confirmation once its submit guard is attached.
    fallback.hidden = true;
    trigger.hidden = false;
  }

  // Selects apply at once. A checkbox refines the form's search, so it reruns only a search
  // that has text; an empty one applies the choice on the next Enter.
  function setupAutosubmit(form) {
    function submit() {
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else form.submit();
    }
    var search = form.querySelector("input[type=search][name]");
    Array.prototype.slice.call(form.querySelectorAll("select[name]")).forEach(function (select) {
      select.addEventListener("change", submit);
    });
    Array.prototype.slice.call(form.querySelectorAll("input[type=checkbox][name]")).forEach(function (box) {
      box.addEventListener("change", function () {
        if (!search || search.value.trim()) submit();
      });
    });
  }

  // An editor warns before leaving with unsaved text, and Cmd/Ctrl+S saves it. A refused
  // save renders text the file does not hold, so that form arrives marked dirty.
  function setupEditor(form) {
    var field = form.querySelector("textarea");
    if (!field) return;
    var saved = form.hasAttribute("data-dirty") ? null : field.value;
    var submitting = false;
    form.addEventListener("submit", function () {
      submitting = true;
    });
    window.addEventListener("beforeunload", function (event) {
      if (submitting || field.value === saved) return;
      event.preventDefault();
      event.returnValue = "";
    });
    field.addEventListener("keydown", function (event) {
      if (!(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey) return;
      if (event.key.toLowerCase() !== "s") return;
      event.preventDefault();
      if (typeof form.requestSubmit === "function") {
        form.requestSubmit();
      } else {
        submitting = true;
        form.submit();
      }
    });
  }

  // -- Mobile navigation -------------------------------------------------------

  function setupMoreMenu(menu) {
    var summary = menu.querySelector("summary");
    if (!summary) return;

    function close(restoreFocus) {
      if (!menu.open) return;
      var focusInside = menu.contains(document.activeElement);
      menu.open = false;
      if (restoreFocus || focusInside) summary.focus();
    }

    summary.setAttribute("aria-expanded", menu.open ? "true" : "false");
    menu.addEventListener("toggle", function () {
      summary.setAttribute("aria-expanded", menu.open ? "true" : "false");
      if (menu.open) {
        var first = menu.querySelector("a");
        if (first) first.focus();
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && menu.open) {
        event.preventDefault();
        close(true);
      }
    });
    document.addEventListener("click", function (event) {
      if (!menu.contains(event.target)) close(false);
    });
    menu.addEventListener("focusout", function (event) {
      if (event.relatedTarget && !menu.contains(event.relatedTarget)) close(false);
    });
  }

  // -- Folder context ----------------------------------------------------------

  function setupFolderContext(panel) {
    var key = "enso.knowledge.folder-open";
    try {
      panel.open = localStorage.getItem(key) === "true";
    } catch (error) {
      // Native disclosure still works when browser storage is unavailable.
    }
    panel.addEventListener("toggle", function () {
      try {
        localStorage.setItem(key, String(panel.open));
      } catch (error) {
        // Remembering the choice is optional; opening the folder is not.
      }
    });
  }

  // -- Keyboard ----------------------------------------------------------------

  // `/` puts the cursor in the page's search field, the way it does on GitHub; the hint
  // drawn inside the field says so. Typing in any field leaves the key alone.
  function setupSearchShortcut() {
    document.addEventListener("keydown", function (event) {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      var target = event.target;
      var typing = target && (target.isContentEditable ||
        ["INPUT", "TEXTAREA", "SELECT"].indexOf(target.tagName) !== -1);
      if (typing) return;
      var search = document.querySelector("input[type=search]");
      if (!search) return;
      event.preventDefault();
      search.focus();
      search.select();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    setupSearchShortcut();
    Array.prototype.slice.call(document.querySelectorAll("[data-folder-context]")).forEach(setupFolderContext);
    Array.prototype.slice.call(document.querySelectorAll("[data-filterable]")).forEach(setupFilterable);
    Array.prototype.slice.call(document.querySelectorAll("table[data-sortable]")).forEach(setupSortable);
    Array.prototype.slice.call(document.querySelectorAll("form[data-autosubmit]")).forEach(setupAutosubmit);
    Array.prototype.slice.call(document.querySelectorAll("form[data-confirm]")).forEach(setupConfirmation);
    Array.prototype.slice.call(document.querySelectorAll("form[data-editor]")).forEach(setupEditor);
    Array.prototype.slice.call(document.querySelectorAll("[data-more-menu]")).forEach(setupMoreMenu);
    Array.prototype.slice.call(document.querySelectorAll("[data-hide-when-enhanced]")).forEach(function (element) {
      element.hidden = true;
    });
  });
})();
