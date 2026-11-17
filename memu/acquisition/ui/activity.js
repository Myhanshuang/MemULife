import { connector, connectorError, onConnectorProgress } from './connector.js';
import { node, button, timestamp, state, loading } from './dom.js';
const states = { queued: '等待中', running: '接收素材中', processing: '整理记忆中', paused: '已暂停', completed: '已完成', cancelled: '已取消', active: '更新中' };
const attachmentWarnings = { attachment_pending: '部分附件未取回原件', attachment_unparsed: '部分附件已保存原件，正文尚未解析',
  attachment_coverage_unverified: '此来源的附件完整性尚未核验' };

function attachmentNotice(progress = {}) {
  const counts = progress.attachments;
  const parts = counts && Object.values(counts).some(Boolean)
    ? [`附件：已保存 ${counts.available || 0}，未取回 ${counts.pending || 0}，未选入 ${counts.excluded || 0}${counts.referenced ? '，引用 ' + counts.referenced : ''}`] : [];
  for (const warning of progress.warnings || []) if (attachmentWarnings[warning]) parts.push(attachmentWarnings[warning]);
  return parts.length ? node('p', { class: 'setup-hint', role: 'status' }, parts.join('；')) : null;
}

function importFailure(job) {
  if (!job.error_code) return null;
  const browser = job.progress?.browser;
  const phase = { detail: '读取会话', attachments: '下载附件', delivery: '提交素材' }[browser?.phase];
  const details = browser?.diagnostics;
  const message = browser ? connectorError(job.error_code) : job.error_code;
  const lines = [phase, message, details?.stage, details?.http_status ? `HTTP ${details.http_status}` : null].filter(Boolean);
  return node('p', { class: 'error', role: 'status' }, lines.join(' · '));
}

export function activity(client, sourceNames, followable = new Set()) {
  const root = node('section', { class: 'activity-page' }), jobs = node('div'), subscriptions = node('div');
  const notice = node('p', { class: 'notice error', role: 'status', hidden: true });
  let timer, live = true, generation = 0, pending = false;
  const abort = new AbortController();
  const reload = button('刷新', () => { if (!pending) void load(); }, { icon: 'refresh' });
  root.append(node('div', { class: 'page-heading' }, node('h1', {}, '导入记录'), reload), notice,
    node('h2', {}, '任务'), jobs, node('h2', {}, '持续更新'), subscriptions);
  const call = async (path, body) => {
    if (!live || pending) return;
    clearTimeout(timer); timer = null; generation++; pending = true; root.inert = true; root.setAttribute('aria-busy', 'true');
    try { await client(path, 'POST', body, abort.signal); if (live) await load(); }
    catch (e) { if (live && e.name !== 'AbortError') { notice.textContent = e.message; notice.hidden = false; } }
    finally {
      pending = false; root.inert = false; root.removeAttribute('aria-busy'); reload.disabled = false;
      if (live && !timer) timer = setTimeout(load, 150000);
    }
  };
  async function load() {
    if (!live) return;
    const version = ++generation;
    clearTimeout(timer); timer = null; reload.disabled = true;
    if (!jobs.childElementCount) loading(jobs);
    try {
      const [j, s] = await Promise.all([client('imports/jobs', 'GET', undefined, abort.signal), client('imports/subscriptions', 'GET', undefined, abort.signal)]);
      if (!live || version !== generation) return;
      notice.hidden = true; jobs.replaceChildren(); subscriptions.replaceChildren();
      for (const job of j.jobs) {
        const actions = node('div', { class: 'actions' });
        if (!job.subscription_id && ['queued', 'running', 'processing', 'paused'].includes(job.state)) {
          const action = job.state === 'paused' ? 'resume' : 'pause';
          actions.append(button(action === 'resume' ? '继续' : '暂停', () => call(`imports/jobs/${job.job_id}`, { action })),
            button('取消', () => { if (confirm('取消此导入任务？已导入内容会保留。')) void call(`imports/jobs/${job.job_id}`, { action: 'cancel' }); }));
        }
        if (job.state === 'completed' && !job.subscription_id && followable.has(job.source_id) && !s.subscriptions.some(p => p.seed_job_id === job.job_id && p.state !== 'cancelled')) {
          const interval = node('select', { 'aria-label': '更新间隔' }, ...[[900, '每 15 分钟'], [3600, '每小时'], [86400, '每天']].map(([v, label]) => node('option', { value: v }, label)));
          actions.append(interval, button('持续更新', () => call('imports/subscriptions', { job_id: job.job_id, interval_seconds: Number(interval.value) })));
        }
        const details = node('div', { class: 'import-details', hidden: true });
        const toggle = button('查看详情', async () => {
          if (!details.hidden) { details.hidden = true; toggle.textContent = '查看详情'; return; }
          details.hidden = false; toggle.textContent = '收起详情'; loading(details);
          try {
            const value = await client(`imports/jobs/${job.job_id}`, 'GET', undefined, abort.signal);
            if (!live) return;
            details.replaceChildren(node('h3', {}, '导入范围'));
            const attachments = attachmentNotice(value.progress);
            if (attachments) details.append(attachments);
            const failure = importFailure(value);
            if (failure) details.append(failure);
            for (const item of value.items || []) {
              const scope = value.selection.messages?.[item.id];
              details.append(node('div', { class: 'import-item' }, node('strong', {}, item.title || item.name || item.id),
                node('span', {}, scope ? `${Object.keys(scope).length} 条消息` : '完整范围')));
            }
            const records = value.memory?.result?.results || [];
            if (records.length) {
              details.append(node('h3', {}, '记忆内容'));
              for (const record of records) details.append(node('details', { class: 'import-memory' },
                node('summary', {}, `${record.kind === 'unit' ? '记忆' : record.kind === 'source' ? '原始材料' : record.kind === 'media' ? '附件原件' : '摘要'} · ${record.ref}`),
                node('pre', {}, record.body || ''), record.truncated ? node('span', {}, '内容已截断') : null));
            }
            if (value.memory_unavailable || value.memory?.is_error) details.append(node('p', { class: 'error' }, '记忆内容暂不可用'));
          } catch (error) { if (live) state(details, error.message); }
        });
        actions.append(toggle);
        if (job.state === 'queued' && !job.progress?.received?.length) actions.append(button('立即读取', async () => {
          try { await connector('sync', { source_id: job.source_id }); notice.textContent = '已通知连接器'; notice.hidden = false; }
          catch (error) { notice.textContent = error.message; notice.hidden = false; }
        }));
        jobs.append(node('article', { class: 'activity-row' }, node('div', {}, node('strong', {}, sourceNames.get(job.source_id) || job.source_id),
          node('small', {}, `${job.selection.scopes.length} 项范围`), attachmentNotice(job.progress), importFailure(job)),
          node('span', { role: 'status', class: 'status-badge', 'data-state': job.stage === 'browser_error' ? 'paused' : job.state }, job.stage === 'browser_error' ? '浏览器读取失败' : job.stage === 'awaiting_browser' ? '等待浏览器读取' : states[job.state]), node('time', {}, timestamp(job.updated_at)), actions), details);
      }
      for (const sub of s.subscriptions) {
        const actions = node('div', { class: 'actions' });
        if (sub.state !== 'cancelled') actions.append(button(sub.state === 'active' ? '暂停' : '继续', () => call(`imports/subscriptions/${sub.subscription_id}`, { action: sub.state === 'active' ? 'pause' : 'resume' })),
          button('取消', () => { if (confirm('取消持续更新？已导入内容会保留。')) void call(`imports/subscriptions/${sub.subscription_id}`, { action: 'cancel' }); }));
        subscriptions.append(node('article', { class: 'activity-row' }, node('div', {}, node('strong', {}, sourceNames.get(sub.source_id) || sub.source_id),
          node('small', {}, `${sub.scope === 'source' ? '持续关注整个来源 · 含附件 · ' : ''}间隔 ${Math.round(sub.interval_seconds / 60)} 分钟`), sub.error_code ? node('p', { class: 'error' }, sub.error_code) : null),
          node('span', { role: 'status', class: 'status-badge', 'data-state': sub.state }, states[sub.state]), node('time', {}, timestamp(sub.last_success_at)), actions));
      }
      if (!j.jobs.length) state(jobs, '暂无导入任务');
      if (!s.subscriptions.length) state(subscriptions, '暂无持续更新');
    } catch (e) { if (live && version === generation && e.name !== 'AbortError') { notice.hidden = false; notice.textContent = e.message; } }
    finally { if (version === generation) { reload.disabled = pending; if (live) timer = setTimeout(load, 150000); } }
  }
  const stopProgress = onConnectorProgress(() => { if (!pending) void load(); });
  void load(); return { element: root, dispose() { stopProgress(); live = false; clearTimeout(timer); abort.abort(); } };
}
