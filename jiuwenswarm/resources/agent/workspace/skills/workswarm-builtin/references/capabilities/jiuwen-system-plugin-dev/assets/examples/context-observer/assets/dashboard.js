'use strict';
const motion = document.getElementById('motion');
const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
function setMotion(enabled) {
  document.documentElement.classList.toggle('motion-off', !enabled);
  motion.setAttribute('aria-pressed', String(enabled));
  motion.querySelector('span').textContent = enabled ? 'ON' : 'OFF';
}
setMotion(!reduced.matches);
motion.addEventListener('click', () => setMotion(motion.getAttribute('aria-pressed') !== 'true'));
reduced.addEventListener('change', event => setMotion(!event.matches));
for (const card of document.querySelectorAll('.glass')) {
  card.addEventListener('pointermove', event => {
    if (document.documentElement.classList.contains('motion-off')) return;
    const rect = card.getBoundingClientRect();
    card.style.setProperty('--mx', `${event.clientX - rect.left}px`);
    card.style.setProperty('--my', `${event.clientY - rect.top}px`);
  });
}
