/** Original, decorative SVG glyphs. Source names remain the accessible labels. */
const paths = {
  layers: ['M12 3 2 8l10 5 10-5-10-5Z', 'm2 12 10 5 10-5', 'm2 17 10 5 10-5'],
  grid: ['M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z'],
  history: ['M3 11a9 9 0 1 1 2.6 7.4', 'M3 4v7h7', 'M12 7v5l3 2'],
  search: ['M21 21l-5-5', 'M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z'],
  refresh: ['M20 7a8 8 0 0 0-14-2L3 8', 'M3 3v5h5', 'M4 17a8 8 0 0 0 14 2l3-3', 'M21 21v-5h-5'],
  edit: ['m15 4 5 5', 'm4 16 12-12a2 2 0 0 1 4 4L8 20l-5 1 1-5Z'],
  arrow: ['M5 12h14', 'm13 6 6 6-6 6'],
  chevron: ['m9 5 7 7-7 7'],
  check: ['m5 12 4 4L19 6'],
  close: ['m6 6 12 12', 'M18 6 6 18'],
  trash: ['M3 6h18M9 6V3h6v3', 'm5 6 1 15h12l1-15', 'M10 10v7M14 10v7'],
  plus: ['M12 5v14M5 12h14'],
  pause: ['M8 5v14M16 5v14'],
  play: ['m8 4 12 8-12 8V4Z'],
  list: ['M9 6h12M9 12h12M9 18h12M3 6h.01M3 12h.01M3 18h.01'],
  shield: ['m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z', 'm8 12 3 3 5-6'],
  inbox: ['M4 4h16l2 11v5H2v-5L4 4Z', 'M2 15h6l2 3h4l2-3h6'],
  file: ['M14 2H5v20h14V7l-5-5Z', 'M14 2v6h5M8 12h8M8 16h6'],
  folder: ['M3 6h7l2 2h9v12H3V6Z', 'M3 6V4h7l2 2h7'],
  files: ['M3 6h6l3 3h9v12H3V6Z', 'M5 6V3h10l4 4v2'],
  database: ['M20 5c0 2-3.6 3-8 3s-8-1-8-3 3.6-3 8-3 8 1 8 3Z', 'M4 5v14c0 2 3.6 3 8 3s8-1 8-3V5', 'M4 12c0 2 3.6 3 8 3s8-1 8-3'],
  chat: ['M21 11a9 9 0 0 1-9 9H3l2-5a9 9 0 1 1 16-4Z', 'M8 10h8M8 14h5'],
  qq: ['M8 8V6a4 4 0 0 1 8 0v2c3 3 4 6 3 8l-2-1c0 4-2 5-5 5s-5-1-5-5l-2 1c-1-2 0-5 3-8Z', 'M9 7h.01M15 7h.01M10 10h4M8 21H5M16 21h3'],
  wechat: ['M15 12a5 5 0 0 1-5 5H3l1-3a6 6 0 1 1 11-2Z', 'M14 7a6 6 0 0 1 7 9l1 4h-7a6 6 0 0 1-5-3', 'M7 10h.01M11 10h.01M17 13h.01'],
  chatgpt: ['M12 3a5 5 0 0 1 8 5 5 5 0 0 1 0 8 5 5 0 0 1-8 5 5 5 0 0 1-8-5 5 5 0 0 1 0-8 5 5 0 0 1 8-5Z', 'm12 6 6 3v7l-6 3-6-3V9l6-3Z', 'm6 9 6 3 6-3M12 12v7'],
  claude: ['M12 2v20M2 12h20M5 5l14 14M5 19 19 5M8 2l8 20M2 8l20 8M2 16l20-8M8 22l8-20'],
  gemini: ['M12 2c1.5 7 3 8.5 10 10-7 1.5-8.5 3-10 10C10.5 15 9 13.5 2 12c7-1.5 8.5-3 10-10Z'],
  deepseek: ['M3 9c4-5 12-3 14 3l4-4v7c-3 7-17 8-19-3', 'M3 9c3 5 9 7 14 3M7 9h.01', 'm11 6 1-3 4 4'],
  perplexity: ['M12 2v20M5 4l14 12M19 4 5 16', 'M5 4v16l7-6 7 6V4', 'M2 8h20v9H2V8Z'],
  grok: ['M20 3 6 17M11 4a8 8 0 1 0 9 9', 'm13 13 7 7M4 20l7-2'],
  notion: ['m4 3 4-1 13 2v17l-4 1H4V3Z', 'm4 3 13 2 4-1M17 5v17', 'M7 17V8l7 10V9M6 8h3M12 9h3M6 17h3'],
  feishu: ['m3 4 18 5-8 4L3 4Z', 'm3 4 4 16 6-7M7 20l14-11'],
  immich: ['M12 12C3 5 9-2 12 5c3-7 9 0 0 7Z', 'M12 12c9-7 14 1 6 3 7 3-1 10-6-3Z', 'M12 12c0 11-10 10-7 3-7 2-5-8 7-3Z'],
  'local-ai': ['M3 4h18v16H3V4Z', 'm6 8 4 4-4 4M13 16h5'],
  screen: ['M2 3h20v14H2V3ZM8 21h8M12 17v4', 'm10 7 5 3-5 3V7Z'],
  logout: ['M9 4H3v16h6M9 12h13', 'm17 7 5 5-5 5'],
  lock: ['M6 10h12v11H6V10ZM8 10V6a4 4 0 0 1 8 0v4M12 14v3'],
};

export function icon(name, className = '') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  for (const [key, value] of Object.entries({ viewBox: '0 0 24 24', width: '20', height: '20', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.65', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false', class: `icon ${className}` })) svg.setAttribute(key, value);
  for (const d of paths[name] || paths.file) {
    const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', d); svg.append(path);
  }
  return svg;
}

export const sourceIcon = slug => icon(slug.replace('ai/', '') || 'grid', 'source-icon');
