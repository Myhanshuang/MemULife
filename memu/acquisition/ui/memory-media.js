import { node, button, size } from './dom.js';

// Shared by list cards and details. No credentials or authenticated URLs enter the DOM.
export function mediaBrowser(client) {
  const abort = new AbortController(), urls = new Set(), players = new Set(), queue = [];
  let active = true, running = 0;
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) {
      observer.unobserve(entry.target); entry.target.loadPreview();
    }
  }, { rootMargin: '100px' });
  function pump() {
    while (active && running < 2 && queue.length) {
      running++;
      queue.shift()().finally(() => { running--; pump(); });
    }
  }
  function gallery(items = [], compact = false) {
    if (!items.length) return null;
    const grid = node('div', { class: 'memory-media-grid', 'aria-label': '附件' });
    for (const item of compact ? items.slice(0, 4) : items) {
      const modality = (item.mime_type || '').split('/')[0];
      const remote = item.kind === 'immich';
      const file = item.kind === 'file';
      const title = item.name || item.ref;
      const status = node('p', { class: 'muted', role: 'status' });
      const view = node('div', { class: 'memory-media-view' });
      const actions = node('div', { class: 'memory-media-actions' });
      let busy = false, ready = false;
      const load = () => {
        if (!active || busy || ready) return;
        busy = true; trigger.disabled = true; status.textContent = '正在读取…';
        queue.push(async () => {
          try {
            const resource = await client(file ? 'memory/file' : 'memory/media', 'POST',
              { ref: item.ref, ...(file ? { file_id: item.file_id } : {}) }, abort.signal);
            if (!active) return;
            let src, tag;
            if (remote) {
              if (resource.open_url) {
                const url = new URL(resource.open_url);
                if (['https:', 'http:'].includes(url.protocol) && !url.username && !url.password) {
                  actions.replaceChildren(node('a', { href: url.href, target: '_blank', rel: 'noopener noreferrer', class: 'button' }, '在 Immich 中查看'));
                }
              }
              if (resource.thumbnail?.startsWith('data:image/jpeg;base64,')) { src = resource.thumbnail; tag = 'img'; }
              else throw new Error('缩略图暂不可用');
            } else {
              tag = { image: 'img', audio: 'audio', video: 'video' }[resource.mime_type?.split('/')[0]];
              if (!file && !tag) throw new Error('浏览器暂不支持此媒体格式。');
              const bytes = Uint8Array.from(atob(resource.data_base64), c => c.charCodeAt(0));
              src = URL.createObjectURL(new Blob([bytes], { type: resource.mime_type })); urls.add(src);
              const download = node('a', { href: src, download: resource.name || title }, '下载附件');
              actions.replaceChildren(download);
              if (file) {
                if (resource.mime_type === 'application/pdf') actions.prepend(node('a', { href: src, target: '_blank', rel: 'noopener' }, '打开附件'));
                ready = true; trigger.hidden = true; status.textContent = ''; download.click(); return;
              }
            }
            const media = node(tag, { src, ...(tag === 'img' ? { alt: title } : { controls: true, preload: 'metadata' }),
              onerror: () => { if (active) status.textContent = '浏览器无法解码此媒体格式。'; } });
            if (tag !== 'img') players.add(media);
            view.replaceChildren(media); ready = true; trigger.hidden = true;
            status.textContent = '';
          } catch (error) {
            if (active && error.name !== 'AbortError') { status.textContent = error.message; trigger.querySelector('.button-label').textContent = '重试'; }
          } finally { busy = false; trigger.disabled = false; }
        });
        pump();
      };
      const trigger = button(file ? '下载附件' : remote || modality === 'image' ? '查看图片' : modality === 'video' ? '加载视频' : '加载音频', load);
      const figure = node('figure', { class: 'memory-media' },
        node('figcaption', {}, node('span', {}, title), !remote && item.size_bytes ? node('small', { class: 'muted' }, size(item.size_bytes)) : null),
        view, trigger, status, actions);
      figure.loadPreview = load;
      grid.append(figure);
      if (!file && (remote || modality === 'image')) observer.observe(figure);
    }
    if (compact && items.length > 4) grid.append(node('p', { class: 'muted' }, `+ ${items.length - 4} 个附件`));
    return grid;
  }
  return { gallery, dispose() {
    active = false; abort.abort(); observer.disconnect(); queue.length = 0;
    for (const player of players) { player.pause(); player.removeAttribute('src'); player.load(); }
    for (const url of urls) URL.revokeObjectURL(url);
    urls.clear(); players.clear();
  } };
}
