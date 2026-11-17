import { icon } from './icons.js';

// Only static application markup opts in; no source text is interpreted as SVG.
for (const slot of document.querySelectorAll('[data-icon]')) slot.replaceChildren(icon(slot.dataset.icon));
