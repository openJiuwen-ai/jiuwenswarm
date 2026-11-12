/**
 * Make ``button`` open and close ``menu``. Escape within ``scope`` closes it
 * and gives the focus back to the button; a press outside ``scope`` closes it.
 */
export function dropdown(button: HTMLButtonElement, menu: HTMLElement, scope: HTMLElement): { close: () => void } {
  const show = (open: boolean): void => {
    menu.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
  };
  show(false);
  button.setAttribute('aria-controls', menu.id);
  button.addEventListener('click', () => show(menu.hidden));
  scope.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || menu.hidden) return;
    event.preventDefault();
    event.stopPropagation();
    show(false);
    button.focus();
  });
  document.addEventListener('pointerdown', event => {
    if (!menu.hidden && !scope.contains(event.target as Node)) show(false);
  });
  return { close: () => show(false) };
}
