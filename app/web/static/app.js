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
    const row = e.target.closest?.("tr[data-id]");
    if (e.key === "Enter" && row && (e.target === row || e.target.matches('input[name="ids"]'))) {
      e.preventDefault();
      openJob(row.dataset.id);
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    const open = document.body.dataset.openJob;
    if (open) openJob(open);
  });

  // ---------------------------------------------------------------- bulk selection
  function updateBulk() {
    const picked = document.querySelectorAll('input[name="ids"]:checked').length;
    const bar = $("#bulk");
    if (!bar) return;
    bar.classList.toggle("on", picked > 0);
    $(".bulk-count", bar).textContent = picked ? picked + " selected" : "";
  }

  document.addEventListener("change", (e) => {
    const t = e.target;
    if (t.matches("[data-action=pick-all]")) {
      document.querySelectorAll('input[name="ids"]').forEach((c) => (c.checked = t.checked));
    }
    if (t.matches('input[name="ids"], [data-action=pick-all]')) updateBulk();
    if (t.matches("[data-action=open-view]") && t.value !== null) {
      location.href = "/?" + t.value;
    }
  });
  document.addEventListener("htmx:afterSwap", updateBulk);

  // ---------------------------------------------------------------- saved views
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-action=save-view]")) {
      const name = prompt("Name this view (it saves the current filters and sort):");
      if (!name) return;
      const query = new URLSearchParams(new FormData($("#filters")));
      [...query.keys()].forEach((k) => { if (!query.get(k)) query.delete(k); });
      htmx.ajax("POST", "/views", { target: "#views", swap: "outerHTML", values: { name, query: query.toString() } });
    }
    if (e.target.closest("[data-action=help]")) $("#help")?.showModal();
  });

  // ---------------------------------------------------------------- keyboard
  function rows() { return [...document.querySelectorAll("tr[data-id]")]; }
  function current() { return $("tr.sel") || document.activeElement?.closest?.("tr[data-id]"); }

  function move(step) {
    const all = rows();
    if (!all.length) return;
    const i = all.indexOf(current());
    const next = all[Math.min(Math.max(i + step, 0), all.length - 1)] || all[0];
    document.querySelectorAll("tr.sel").forEach((r) => r.classList.remove("sel"));
    next.classList.add("sel");
    next.focus();
    if (document.body.classList.contains("drawer-open")) openJob(next.dataset.id);
  }

  function setStatus(status) {
    const row = current();
    if (!row) return;
    htmx.ajax("POST", "/job/" + row.dataset.id + "/status", { target: "#drawer", values: { status } })
      .then(() => toast("Marked " + status, "ok"));
  }

  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    const typing = t.closest("textarea, select, [contenteditable], dialog") ||
      (t.matches("input") && !["checkbox", "radio"].includes(t.type));
    if (typing) return;
    const onJobs = !!$("#filters");
    const keys = {
      "?": () => $("#help")?.showModal(),
      "/": () => onJobs && $("#q")?.focus(),
      j: () => onJobs && move(1),
      k: () => onJobs && move(-1),
      x: () => { const c = current()?.querySelector('input[name="ids"]'); if (c) { c.checked = !c.checked; updateBulk(); } },
      i: () => onJobs && setStatus("interested"),
      a: () => onJobs && setStatus("applied"),
      h: () => onJobs && setStatus("hidden"),
    };
    if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
  });

  // keep keyboard focus inside the open job panel
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Tab" || !document.body.classList.contains("drawer-open")) return;
    const focusable = [...$("#drawer").querySelectorAll("a[href], button, input, select, textarea, summary")]
      .filter((el) => !el.disabled && el.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });

  // ---------------------------------------------------------------- tracker drag & drop
  document.addEventListener("dragstart", (e) => {
    const card = e.target.closest?.("[data-drag-id]");
    if (!card) return;
    e.dataTransfer.setData("text/plain", card.dataset.dragId);
    e.dataTransfer.effectAllowed = "move";
    card.classList.add("dragging");
  });
  document.addEventListener("dragend", (e) => e.target.closest?.("[data-drag-id]")?.classList.remove("dragging"));
  document.addEventListener("dragover", (e) => {
    const col = e.target.closest?.("[data-drop-status]");
    if (!col) return;
    e.preventDefault();
    document.querySelectorAll(".drop-target").forEach((c) => c !== col && c.classList.remove("drop-target"));
    col.classList.add("drop-target");
  });
  document.addEventListener("dragleave", (e) => {
    const col = e.target.closest?.("[data-drop-status]");
    if (col && !col.contains(e.relatedTarget)) col.classList.remove("drop-target");
  });
  document.addEventListener("drop", (e) => {
    const col = e.target.closest?.("[data-drop-status]");
    if (!col) return;
    e.preventDefault();
    col.classList.remove("drop-target");
    const id = e.dataTransfer.getData("text/plain");
    if (id) htmx.ajax("POST", "/job/" + id + "/status", { target: "#board", values: { status: col.dataset.dropStatus, view: "board" } });
  });

  // ---------------------------------------------------------------- toasts
  function toast(message, kind = "err") {
    const box = $("#toasts");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.textContent = message;
    box.append(el);
    setTimeout(() => el.remove(), 5000);
  }

  document.addEventListener("htmx:responseError", (e) => {
    const status = e.detail.xhr?.status;
    toast(status === 403 ? "Blocked: that request didn't come from this page." : "Something went wrong (" + status + "). Check the logs.");
  });
  document.addEventListener("htmx:sendError", () => toast("Can't reach Job Scout - is it still running?"));

  window.JobScout = { openJob, closeDrawer, toast };
})();
