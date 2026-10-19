import type { AnswerEvidence, Page } from './types';

export type TextMatch = { page: number; start: number; end: number };

/** Explicit navigation wins over a saved position, while nonexistent pages never become selected. */
export function resolveReaderPage(pages: Pick<Page, 'number'>[], requested?: number, saved?: number | null, citation?: number): number | null {
  return [requested,saved,citation].find(value => pages.some(page => page.number === value)) ?? pages[0]?.number ?? null;
}

/** Citation offsets are Unicode code points, never JS UTF-16 offsets. */
export function exactCitationSpan(page: Pick<Page, 'number' | 'text'>, evidence?: Pick<AnswerEvidence, 'page' | 'quote' | 'start' | 'end'>): [number, number] | null {
  if (!evidence || evidence.page !== page.number || !evidence.quote) return null;
  const chars = Array.from(page.text);
  if (Number.isInteger(evidence.start) && Number.isInteger(evidence.end)) {
    const start = evidence.start!, end = evidence.end!;
    return start >= 0 && end > start && end <= chars.length && chars.slice(start, end).join('') === evidence.quote ? [start, end] : null;
  }
  const index = page.text.indexOf(evidence.quote);
  if (index < 0 || page.text.indexOf(evidence.quote, index + 1) >= 0) return null;
  const start = Array.from(page.text.slice(0, index)).length;
  return [start, start + Array.from(evidence.quote).length];
}

/** Literal, case-insensitive lookup in parsed text. Does not claim to search image pixels. */
export function findTextMatches(pages: Pick<Page, 'number' | 'text'>[], query: string): TextMatch[] {
  if (!query.trim()) return [];
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const matcher = new RegExp(escaped, 'giu');
  const matches: TextMatch[] = [];
  for (const page of pages) {
    matcher.lastIndex = 0;
    let utf16Offset = 0, codePointOffset = 0;
    for (const match of page.text.matchAll(matcher)) {
      const start = codePointOffset + Array.from(page.text.slice(utf16Offset, match.index)).length;
      const end = start + Array.from(match[0]).length;
      matches.push({ page: page.number, start, end });
      utf16Offset = match.index + match[0].length;
      codePointOffset = end;
    }
  }
  return matches;
}

export const readerCellText = (value: unknown): string => value === null || value === undefined ? 'NULL' : typeof value === 'object' ? JSON.stringify(value) : String(value);

/** Escape CSV and neutralize spreadsheet formula interpretation; leave genuine numbers numeric. */
export function readerCsv(columns: string[], rows: unknown[][]): string {
  const cell = (value: unknown): string => {
    let text = value === null || value === undefined ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);
    if (typeof value !== 'number' && /^[\s]*[=+\-@\t\r]/u.test(text)) text = `'${text}`;
    return `"${text.replaceAll('"', '""')}"`;
  };
  return '\uFEFF' + [columns, ...rows].map(row => row.map(cell).join(',')).join('\r\n');
}
