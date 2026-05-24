// Tab switcher.
//
// Active tab is persisted to localStorage so a page refresh / hard
// reload keeps the operator on whichever tab they were viewing.
// Falls back to the default tab ("tonight") on first visit or if the
// saved tab doesn't exist anymore.

const STORAGE_KEY = "atlas.activeTab";
const DEFAULT_TAB = "tonight";

function _setActive(name, handlers) {
  const tabs = document.querySelectorAll("#tabs .tab");
  const panels = document.querySelectorAll("main .panel");
  let matched = false;
  tabs.forEach((t) => {
    const isMe = t.dataset.tab === name;
    if (isMe) matched = true;
    t.classList.toggle("active", isMe);
  });
  panels.forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${name}`));
  if (matched) {
    try {
      localStorage.setItem(STORAGE_KEY, name);
    } catch (_) {
      // localStorage may be disabled (private browsing, etc.) —
      // fall through; the click-to-switch still works for the
      // current session.
    }
    if (handlers && handlers[name]) {
      try {
        handlers[name](window.atlas?.api);
      } catch (e) {
        console.error(`tab handler ${name} threw:`, e);
      }
    }
  }
  return matched;
}

export function initTabs(handlers) {
  const tabs = document.querySelectorAll("#tabs .tab");
  tabs.forEach((btn) => {
    btn.addEventListener("click", () => {
      _setActive(btn.dataset.tab, handlers);
    });
  });

  // Restore last-active tab on initial load.
  let want = DEFAULT_TAB;
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) want = saved;
  } catch (_) {
    // ignore
  }
  // _setActive returns false if the saved tab no longer exists
  // (e.g. removed in a build) — fall back to default.
  if (!_setActive(want, handlers) && want !== DEFAULT_TAB) {
    _setActive(DEFAULT_TAB, handlers);
  }
}
