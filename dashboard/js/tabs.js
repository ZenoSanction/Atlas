// Tab switcher.
export function initTabs(handlers) {
  const tabs = document.querySelectorAll("#tabs .tab");
  const panels = document.querySelectorAll("main .panel");
  tabs.forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.tab;
      tabs.forEach((t) => t.classList.toggle("active", t === btn));
      panels.forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${name}`));
      if (handlers[name]) handlers[name](window.atlas.api);
    });
  });
}
