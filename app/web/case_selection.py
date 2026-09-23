"""Browser-side case selection across pages in one admin tab and login session."""

from __future__ import annotations

SCRIPT = """<script>
(function () {
  "use strict";
  const form = document.getElementById("case-batch");
  const boxes = Array.from(form.querySelectorAll('input[type="checkbox"][name="case_ids"]'));
  const count = document.getElementById("case-selected");
  const prefix = "qqbot-case-selection-v2:";
  const limit = __CASE_EXPORT_LIMIT__;
  const filters = JSON.stringify(
    Array.from(form.querySelectorAll('input[data-case-filter]')).map(input => [input.name, input.value])
  );
  // The server supplies a one-way session digest, never a login/CSRF token.
  const key = prefix + form.dataset.selectionSession;
  let chosen = new Set();
  let persistent = true;
  let storage = null;
  try {
    storage = window.sessionStorage;
  } catch (_) {
    persistent = false;
  }

  function showCount() {
    count.textContent = "已勾选 " + chosen.size + " 项（跨页保留）" +
      (persistent ? "" : "；浏览器未允许跨页保存");
  }

  function save() {
    if (persistent) {
      try {
        storage.setItem(key, JSON.stringify({filters: filters, ids: Array.from(chosen)}));
      } catch (_) {
        persistent = false;
      }
    }
    showCount();
  }

  function restore() {
    if (!persistent) return;
    chosen = new Set();
    try {
      if (form.dataset.selectionReset === "1") {
        storage.removeItem(key);
        form.dataset.selectionReset = "0";
        const url = new URL(window.location.href);
        url.searchParams.delete("case_batch_done");
        window.history.replaceState(null, "", url.toString());
      }
      for (let i = storage.length - 1; i >= 0; i--) {
        const oldKey = storage.key(i);
        if (oldKey.startsWith(prefix) && oldKey !== key) storage.removeItem(oldKey);
      }
      const stored = JSON.parse(storage.getItem(key) || "null");
      if (stored && stored.filters === filters && Array.isArray(stored.ids) &&
          stored.ids.length <= limit && stored.ids.every(id => typeof id === "string" && /^[1-9][0-9]*$/.test(id))) {
        chosen = new Set(stored.ids);
      }
    } catch (_) {
      persistent = false;
    }
    boxes.forEach(box => { box.checked = chosen.has(box.value); });
    save();
  }
  restore();
  showCount();
  // Back/forward cache restores the old DOM and closures without rerunning JS.
  window.addEventListener("pageshow", restore);

  form.addEventListener("change", event => {
    if (event.target.name !== "case_ids") return;
    const box = event.target;
    if (box.checked) {
      if (chosen.size >= limit && !chosen.has(box.value)) {
        box.checked = false;
        window.alert("每次最多选择 " + limit + " 个案件");
        return;
      }
      chosen.add(box.value);
    } else {
      chosen.delete(box.value);
    }
    save();
  });

  function selectPage(on) {
    if (on && chosen.size + boxes.filter(box => !chosen.has(box.value)).length > limit) {
      window.alert("每次最多选择 " + limit + " 个案件");
      return;
    }
    boxes.forEach(box => {
      box.checked = on;
      if (on) chosen.add(box.value);
      else chosen.delete(box.value);
    });
    save();
  }
  document.getElementById("case-select-page").addEventListener("click", () => selectPage(true));
  document.getElementById("case-clear-page").addEventListener("click", () => selectPage(false));
  document.getElementById("case-clear-all").addEventListener("click", () => {
    chosen.clear();
    boxes.forEach(box => { box.checked = false; });
    save();
  });

  form.addEventListener("submit", event => {
    form.querySelectorAll('input[data-case-selection-extra]').forEach(input => input.remove());
    if (form.elements.namedItem("scope").value !== "selected") return;
    if (chosen.size === 0) {
      event.preventDefault();
      window.alert("请先勾选案件，或选择当前筛选全部");
      return;
    }
    const current = new Set(boxes.filter(box => box.checked).map(box => box.value));
    chosen.forEach(id => {
      if (current.has(id)) return;
      const hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = "case_ids";
      hidden.value = id;
      hidden.dataset.caseSelectionExtra = "1";
      form.appendChild(hidden);
    });
  });
})();
</script>"""


def script(export_limit: int) -> str:
    return SCRIPT.replace("__CASE_EXPORT_LIMIT__", str(export_limit))
