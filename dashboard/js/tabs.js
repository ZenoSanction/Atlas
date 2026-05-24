// Tab switcher.
//
// Initial tab visibility is handled BEFORE this module runs:
//   * Inline script in <head> reads localStorage and sets
//     <html data-active-tab="…">
//   * CSS in atlas.css uses that attribute to show only the matching
//     panel; every other panel is display:none from first paint.
//
// So this module only needs to:
//   * Wire click handlers
//   * On click, update <html data-active-tab> + localStorage + active
//     button class + call the tab's handler
//   * On init, mark the active button + call the active tab's handler
//     so the initial data fetch happens
//
// No JS-driven panel show/hide. No snap-back. Refresh stays on the
// same tab cleanly with no flash through Tonight.

const STORAGE_KEY = "atlas.activeTab";
const DEFAULT_TAB = "tonight";

function _activateButton(name) {
  const tabs = document.querySelectorAll("#tabs .tab");
  tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
}

function _persist(name) {
  try {
    localStorage.setItem(STORAGE_KEY, name);
  } catch (_) {
    // localStorage disabled (private browsing, etc.) — no-op
  }
}

function _setActive(name, handlers) {
  document.documentElement.setAttribute("data-active-tab", name);
  _persist(name);
  _activateButton(name);
  if (handlers && handlers[name]) {
    try {
      handlers[name](window.atlas?.api);
    } catch (e) {
      console.error(`tab handler ${name} threw:`, e);
    }
  }
}

export function initTabs(handlers) {
  const tabs = document.querySelectorAll("#tabs .tab");
  tabs.forEach((btn) => {
    btn.addEventListener("click", () => {
      _setActive(btn.dataset.tab, handlers);
    });
  });

  // Initial: the inline <head> script already set the correct
  // data-active-tab + CSS already painted the right panel. We just
  // need to mark the active button and call the tab's handler so
  // data loads.
  let active = document.documentElement.getAttribute("data-active-tab")
                || DEFAULT_TAB;
  // Verify the active tab actually exists as a button; if not, fall
  // back. (Defensive — covers the edge case where localStorage holds
  // a stale tab name from an older build.)
  if (!document.querySelector(`#tabs .tab[data-tab="${active}"]`)) {
    active = DEFAULT_TAB;
    document.documentElement.setAttribute("data-active-tab", active);
  }
  _activateButton(active);
  if (handlers && handlers[active]) {
    try {
      handlers[active](window.atlas?.api);
    } catch (e) {
      console.error(`initial tab handler ${active} threw:`, e);
    }
  }
}
