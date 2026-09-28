// Job Scout UI behaviour. No inline handlers anywhere, so the Content-Security-Policy can
// forbid inline script. Everything is event delegation, so it survives htmx swaps.
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);

  // ---------------------------------------------------------------- drawer
  function openJob(id) {
    const drawer = $("#drawer");
    if (!drawer) return;
    htmx.ajax("GET", "/job/" + id, { target: "#drawer" }).then(() => {
      document.body.classList.add("drawer-open");
      $(".drawer-head .close", drawer)?.focus();
    });
    document.querySelectorAll("tr.sel").forEach((r) => r.classList.remove("sel"));
    document.querySelector('tr[data-id="' + id + '"]')?.classList.add("sel");
    const u = new URL(location);
    u.searchParams.set("job", id);
    history.replaceState(null, "", u);
  }

  function closeDrawer() {
    if (!document.body.classList.contains("drawer-open")) return;
    document.body.classList.remove("drawer-open");
    const u = new URL(location);
    u.searchParams.delete("job");
    history.replaceState(null, "", u);
    $("tr.sel")?.focus();
  }

  function sortBy(col) {
    const s = $("#sort"), d = $("#dir");
    const ascDefault = ["distance", "company", "title"].includes(col);
    if (s.value === col) d.value = (d.value || (ascDefault ? "asc" : "desc")) === "desc" ? "asc" : "desc";
    else { s.value = col; d.value = ""; }
    htmx.trigger("#filters", "change");
  }

  function copyText(id, btn) {
    navigator.clipboard.writeText(document.getElementById(id).value).then(() => {
      btn.textContent = "Copied";
      setTimeout(() => (btn.textContent = "Copy"), 1500);
    });
  }

  document.addEventListener("click", (e) => {
    const t = e.target;
    const sortTh = t.closest("th[data-sort]");
    if (sortTh) return sortBy(sortTh.dataset.sort);
    if (t.closest("[data-action=close-drawer], .scrim")) return closeDrawer();
    const copy = t.closest("[data-copy]");
    if (copy) return copyText(copy.dataset.copy, copy);
    const row = t.closest("tr[data-id]");
    if (row && !t.closest("a, button, input, select, label")) return openJob(row.dataset.id);
  });

  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset?.confirm;
    if (msg && !confirm(msg)) e.preventDefault();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") return closeDrawer();
    if (e.key === "Enter" && e.target.matches("tr[data-id]")) openJob(e.target.dataset.id);
  });

  document.addEventListener("DOMContentLoaded", () => {
    const open = document.body.dataset.openJob;
    if (open) openJob(open);
  });

  window.JobScout = { openJob, closeDrawer };
})();
