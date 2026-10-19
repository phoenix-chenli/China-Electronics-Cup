"""Local favorites and per-tab drafts; never modify immutable answer bodies."""
from __future__ import annotations
import json
import re
from .store import now


class ProductStore:
    def __init__(self,store):self.store=store

    def initialize(self):
        with self.store.transaction() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS answer_favorites(
                project_id TEXT NOT NULL,answer_id TEXT NOT NULL REFERENCES answers(id),created_at TEXT NOT NULL,
                PRIMARY KEY(project_id,answer_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS workspace_views(
                project_id TEXT NOT NULL REFERENCES projects(id),client_id TEXT NOT NULL,
                revision INTEGER NOT NULL,updated_at TEXT NOT NULL,body TEXT NOT NULL,
                PRIMARY KEY(project_id,client_id))''')

    def favorite(self,project_id,answer_id,enabled):
        if type(enabled) is not bool:raise ValueError('收藏状态无效')
        with self.store.transaction() as c:
            row=c.execute('SELECT body FROM answers WHERE project_id=? AND id=?',(project_id,answer_id)).fetchone()
            if not row:raise LookupError('回答不属于当前项目')
            if json.loads(row[0])['status'] in ('queued','running'):
                raise ValueError('请等待回答结束后收藏')
            if enabled:c.execute('INSERT OR IGNORE INTO answer_favorites VALUES(?,?,?)',(project_id,answer_id,now()))
            else:c.execute('DELETE FROM answer_favorites WHERE project_id=? AND answer_id=?',(project_id,answer_id))
        return dict(answer_id=answer_id,favorite=enabled)

    def favorites(self,project_id):
        with self.store.transaction() as c:
            return [dict(r) for r in c.execute('SELECT answer_id,created_at FROM answer_favorites WHERE project_id=? ORDER BY created_at DESC,answer_id',(project_id,))]

    @staticmethod
    def _client(client_id):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{8,80}',client_id):raise ValueError('工作标签页标识无效')

    def workspace(self,project_id,client_id):
        self._client(client_id)
        with self.store.transaction() as c:
            row=c.execute('SELECT revision,updated_at,body FROM workspace_views WHERE project_id=? AND client_id=?',(project_id,client_id)).fetchone()
        return dict(revision=row[0],updated_at=row[1],state=json.loads(row[2])) if row else dict(revision=0,updated_at=None,state={})

    def save_workspace(self,project_id,client_id,state,expected_revision):
        self._client(client_id)
        allowed={'draft','source_ids','active_answer_id','selected_source_id','selected_page','scroll_top','branch_id'}
        if not isinstance(state,dict) or set(state)-allowed:raise ValueError('工作区保存字段无效')
        if type(expected_revision) is not int or expected_revision<0:raise ValueError('工作区版本无效')
        if not isinstance(state.get('draft',''),str) or len(state.get('draft',''))>4000:raise ValueError('草稿超过4000字符')
        ids=state.get('source_ids',[])
        from .source_scope import MAX_ANSWER_SOURCES
        if not isinstance(ids,list) or len(ids)>MAX_ANSWER_SOURCES or any(not isinstance(s,str) or not s or len(s)>80 for s in ids) or len(set(ids))!=len(ids):raise ValueError('资料范围无效')
        for field in ('active_answer_id','selected_source_id','branch_id'):
            value=state.get(field)
            if value is not None and (not isinstance(value,str) or not value or len(value)>80):raise ValueError('工作区标识无效')
        for field in ('selected_page','scroll_top'):
            if field in state and (type(state[field]) is not int or not 0<=state[field]<=1000000):raise ValueError('阅读位置无效')
        with self.store.transaction() as c:
            if not c.execute('SELECT 1 FROM projects WHERE id=?',(project_id,)).fetchone():raise LookupError('项目不存在')
            source_refs=ids+([state['selected_source_id']] if state.get('selected_source_id') else [])
            for sid in source_refs:
                if not isinstance(sid,str) or not c.execute('SELECT 1 FROM sources WHERE id=? AND project_id=?',(sid,project_id)).fetchone():
                    raise ValueError('工作区资料不属于当前项目')
            for field in ('active_answer_id','branch_id'):
                aid=state.get(field)
                if aid and (not isinstance(aid,str) or not c.execute('SELECT 1 FROM answers WHERE id=? AND project_id=?',(aid,project_id)).fetchone()):
                    raise ValueError('工作区分支不属于当前项目')
            row=c.execute('SELECT revision FROM workspace_views WHERE project_id=? AND client_id=?',(project_id,client_id)).fetchone()
            current=row[0] if row else 0
            if current!=expected_revision:raise ValueError('工作区版本已更新，未覆盖另一条保存记录')
            stamp=now();revision=current+1
            c.execute('INSERT INTO workspace_views VALUES(?,?,?,?,?) ON CONFLICT(project_id,client_id) DO UPDATE SET revision=excluded.revision,updated_at=excluded.updated_at,body=excluded.body',
                      (project_id,client_id,revision,stamp,json.dumps(state,ensure_ascii=False)))
        return dict(revision=revision,updated_at=stamp,state=state)
