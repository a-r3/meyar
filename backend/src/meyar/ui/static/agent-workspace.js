(() => {
  const toggle = document.querySelector('.agent-drawer-toggle');
  const drawer = document.querySelector('.agent-sidebar');
  const closeButton = document.querySelector('.agent-drawer-close');
  const backdrop = document.querySelector('.agent-drawer-backdrop');
  if (!toggle || !drawer || !closeButton || !backdrop) return;

  let priorFocus = null;
  function close() {
    drawer.classList.remove('is-open');
    backdrop.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    if (priorFocus) priorFocus.focus();
  }
  function open() {
    priorFocus = document.activeElement;
    drawer.classList.add('is-open');
    backdrop.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    closeButton.focus();
  }
  toggle.addEventListener('click', () => drawer.classList.contains('is-open') ? close() : open());
  closeButton.addEventListener('click', close);
  backdrop.addEventListener('click', close);
  drawer.addEventListener('click', event => {
    if (event.target.closest('a')) close();
  });
  document.addEventListener('keydown', event => {
    if (!drawer.classList.contains('is-open')) return;
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key !== 'Tab') return;
    const focusable = [...drawer.querySelectorAll('a, button')].filter(node => !node.disabled);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
})();
