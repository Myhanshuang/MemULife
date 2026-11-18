// Keep citation numbers scoped to the answer block that supplied them.
export function perplexityAnswer(markdown: any): string {
  const answer = typeof markdown?.answer === 'string' ? markdown.answer :
    Array.isArray(markdown?.chunks) ? markdown.chunks.filter((v: unknown) => typeof v === 'string').join('') : '';
  if (!answer || !Array.isArray(markdown?.citations)) return answer;
  const sources: string[] = [];
  for (const [index, citation] of markdown.citations.entries()) {
    const url = typeof citation === 'string' ? citation : citation?.url;
    if (typeof url !== 'string' || /\s/.test(url) || !URL.canParse(url)) continue;
    const parsed = new URL(url);
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) continue;
    const title = typeof citation?.title === 'string' ? citation.title.trim() : '';
    sources.push(`[${index + 1}] ${title ? title + ' · ' : ''}${url}`);
  }
  return sources.length ? answer + '\n\n网站来源\n' + sources.join('\n') : answer;
}

// Perplexity timestamps can be ISO strings or Unix seconds/milliseconds.
export function perplexityTime(...candidates: unknown[]): string | null {
  for (const value of candidates) {
    const raw = typeof value === 'number' || typeof value === 'string' ? String(value).trim() : '';
    let millis: number;
    if (/^\d{10}(?:\.\d+)?$/.test(raw)) millis = Number(raw) * 1000;
    else if (/^\d{13}$/.test(raw)) millis = Number(raw);
    else {
      const iso = raw.replace(/^(\d{4}-\d\d-\d\d) /, '$1T').replace(/([+-]\d\d)(\d\d)$/, '$1:$2');
      if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$/i.test(iso)) continue;
      const [year, month, day] = iso.slice(0, 10).split('-').map(Number);
      if (month < 1 || month > 12 || day < 1 || day > new Date(Date.UTC(year, month, 0)).getUTCDate()) continue;
      millis = Date.parse(iso);
    }
    if (Number.isFinite(millis)) return new Date(millis).toISOString();
  }
  return null;
}
