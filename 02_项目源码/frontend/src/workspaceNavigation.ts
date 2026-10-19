export type WorkspaceRoute = {
  project?: string;
  view: 'ask' | 'sources' | 'history';
  answer?: string;
  source?: string;
  evidence?: string;
  evidenceAnswer?: string;
  page?: number;
  settings?: boolean;
};

/** Hash routes also work when the packaged application has no server rewrite rules. */
export function readWorkspaceRoute(hash = window.location.hash): WorkspaceRoute {
  const p = new URLSearchParams(hash.replace(/^#\/?\??/, ''));
  const value = (key: string) => p.get(key) || undefined;
  const page = Number(p.get('page'));
  const view = p.get('view');
  return {
    project: value('project'), view: view === 'sources' || view === 'history' ? view : 'ask',
    answer: value('answer'), source: value('source'), evidence: value('evidence'), evidenceAnswer: value('evidence_answer'),
    page: Number.isSafeInteger(page) && page > 0 ? page : undefined,
    settings: p.get('settings') === '1',
  };
}

export function workspaceHash(route: WorkspaceRoute): string {
  const p = new URLSearchParams();
  if (route.project) p.set('project', route.project);
  p.set('view', route.view);
  if (route.answer) p.set('answer', route.answer);
  if (route.source) {
    p.set('source', route.source);
    if (route.evidence) p.set('evidence', route.evidence);
    if (route.evidenceAnswer) p.set('evidence_answer', route.evidenceAnswer);
    if (route.page) p.set('page', String(route.page));
  }
  if (route.settings) p.set('settings', '1');
  return `#${p.toString()}`;
}

export function readerStateKey(project: string, source: string, evidence?: {id: string; sha256?: string; parser_version?: string; page?: number; start?: number; end?: number; quote?: string; table_name?: string}, owner?: string) {
  return JSON.stringify([project, source, owner, evidence?.id, evidence?.sha256, evidence?.parser_version,evidence?.page,evidence?.start,evidence?.end,evidence?.quote,evidence?.table_name]);
}
