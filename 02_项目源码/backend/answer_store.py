"""Persistent answer jobs and immutable result snapshots, separate from drafts."""
from __future__ import annotations
import json
import hashlib
from .store import identity, now
from .source_snapshot import snapshot, scope_matches
from .answer_contract import initial_fields, prepare_result
from . import answer_events
from .conversation_state import build_initial, finalize_state, validate_patch

FINAL = {'completed','needs_clarification','insufficient_evidence','failed','cancelled'}
MAX_LATE_AUDITS = 128
MAX_LATE_AUDIT_CHARACTERS = 1024 * 1024
class AnswerStore:
    def __init__(self, store): self.store=store
    def initialize(self):
        with self.store.transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS answers(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),request_id TEXT NOT NULL,body TEXT NOT NULL,UNIQUE(project_id,request_id))')
            c.execute('CREATE INDEX IF NOT EXISTS answers_project ON answers(project_id)')
            answer_events.initialize(c)
            for row in c.execute('SELECT id,body FROM answers').fetchall():
                item=json.loads(row['body'])
                if item['status'] in ('queued','running'):
                    item.update(status='failed',stage='处理已中断',error='上次服务中断，未自动重复调用模型，请重新提问。',updated_at=now())
                    c.execute('UPDATE answers SET body=? WHERE id=?',(json.dumps(item,ensure_ascii=False),row['id']))
                    answer_events.append(c, item, kind='interrupted')
    def get(self, project_id, answer_id):
        with self.store.transaction() as c:
            r=c.execute('SELECT body FROM answers WHERE id=? AND project_id=?',(answer_id,project_id)).fetchone()
            return json.loads(r[0]) if r else None
    def listing(self, project_id):
        with self.store.transaction() as c:
            rows=[json.loads(r[0]) for r in c.execute('SELECT body FROM answers WHERE project_id=? ORDER BY rowid DESC LIMIT 50',(project_id,))]
        return rows
    def events(self, project_id, answer_id, after=0, limit=200):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('事件分页参数无效')
        with self.store.transaction() as c:
            return [json.loads(r[0]) for r in c.execute(
                'SELECT body FROM answer_events WHERE project_id=? AND answer_id=? AND sequence>? ORDER BY sequence LIMIT ?',
                (project_id,answer_id,after,limit))]
    def index(self, project_id, before=None, limit=50, conversation_id=None, search=''):
        if type(limit) is not int or not 1<=limit<=100 or (before is not None and (type(before) is not int or before<1)):
            raise ValueError('会话索引分页参数无效')
        clauses=['project_id=?'];params=[project_id]
        if before is not None:clauses.append('rowid<?');params.append(before)
        if conversation_id:
            clauses.append("json_extract(body,'$.conversation_id')=?");params.append(conversation_id)
        if search:
            clauses.append("json_extract(body,'$.question') LIKE ? ESCAPE '\\'")
            params.append('%'+search.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
        with self.store.transaction() as c:
            rows=c.execute('SELECT rowid,body FROM answers WHERE '+' AND '.join(clauses)+' ORDER BY rowid DESC LIMIT ?',[*params,limit+1]).fetchall()
        items=[]
        for row in rows[:limit]:
            item=json.loads(row[1])
            items.append({key:item.get(key) for key in ('id','question','status','stage','created_at','updated_at',
                'conversation_id','parent_answer_id','turn_index','source_ids','context_revision')})
        return dict(items=items,next_before=rows[limit-1][0] if len(rows)>limit else None,has_more=len(rows)>limit)
    def conversation(self, project_id, answer_id):
        """Walk one immutable branch; never mix sibling replies or other projects."""
        chain=[];seen=set()
        while answer_id:
            if answer_id in seen or len(chain)>=1000:raise ValueError('会话超过1000轮或历史不完整，请从索引选择分支')
            seen.add(answer_id)
            item=self.get(project_id,answer_id)
            if not item:raise ValueError('上文不存在，请开始新问题')
            chain.append(item);answer_id=item.get('parent_answer_id')
        return list(reversed(chain))

    def create(self, project_id, source, question, mode, table_name, request_id, models, parent_answer_id=None, sources=None, context_patch=None, expected_context_revision=None):
        context_patch=validate_patch(context_patch)
        sources = sources or [source]
        source_ids = [s['id'] for s in sources]
        snapshots = [snapshot(s) for s in sources]
        with self.store.transaction() as c:
            old=c.execute('SELECT body FROM answers WHERE project_id=? AND request_id=?',(project_id,request_id)).fetchone()
            if old:
                item=json.loads(old[0])
                if any(item.get(k)!=v for k,v in {'source_id':source['id'],'question':question,'mode':mode,'table_name':table_name,'parent_answer_id':parent_answer_id}.items()): raise ValueError('同一请求编号的内容发生变化，请重新提问')
                if set(item.get('source_ids', [item['source_id']])) != set(source_ids): raise ValueError('同一请求编号的资料范围发生变化，请重新提问')
                if item.get('context_patch',[])!=context_patch or item.get('expected_context_revision')!=expected_context_revision:
                    raise ValueError('同一请求编号的口径调整发生变化，请重新提问')
                return item,False
            active=[json.loads(r[0]) for r in c.execute('SELECT body FROM answers')]
            if sum(a['status'] in ('queued','running') for a in active)>=4: raise ValueError('已有4个问答任务正在处理，请稍后再试')
            stamp=now()
            parent=None
            if parent_answer_id:
                row=c.execute('SELECT body FROM answers WHERE project_id=? AND id=?',(project_id,parent_answer_id)).fetchone()
                if not row:raise ValueError('上文不属于当前项目')
                parent=json.loads(row[0])
                if parent['status'] not in ('completed','needs_clarification','insufficient_evidence'):raise ValueError('请等待上文处理结束，或开始新问题')
                if any(parent.get(k)!=v for k,v in {'source_id':source['id'],'mode':mode,'table_name':table_name,'source_sha256':source['sha256'],'parser_version':source['parser_version']}.items()):raise ValueError('资料范围或版本已变化，请开始新问题')
                if set(parent.get('source_ids', [parent['source_id']])) != set(source_ids) or (parent.get('sources') and not scope_matches(parent['sources'], snapshots)):
                    raise ValueError('资料范围或解析内容已变化，请开始新问题')
                if parent.get('turn_index',1)>=1000:raise ValueError('本会话已达1000轮，请开始新问题')
            if expected_context_revision is not None and (not parent or parent.get('context_revision')!=expected_context_revision):
                raise ValueError('当前分支口径已变化，请刷新后再调整')
            if context_patch and parent and expected_context_revision is None:
                raise ValueError('调整已有口径需要提供所见的上下文版本')
            item=dict(id=identity(),project_id=project_id,request_id=request_id,question=question,source_id=source['id'],source_name=source['name'],source_sha256=source['sha256'],parser_version=source['parser_version'],mode=mode,table_name=table_name,status='queued',stage='等待处理',created_at=stamp,updated_at=stamp,elapsed_ms=0,models=models,error=None,warnings=source['warnings'],result=None)
            item.update(parent_answer_id=parent_answer_id,conversation_id=(parent.get('conversation_id') or parent['id']) if parent else item['id'],turn_index=parent.get('turn_index',1)+1 if parent else 1,normalized_question=None,intent=None)
            item.update(source_ids=source_ids, sources=snapshots)
            item.update(initial_fields(snapshots))
            item.update(context_patch=context_patch,expected_context_revision=expected_context_revision)
            item['context_state']=build_initial(item['id'],question,snapshots,parent,context_patch)
            item['context_revision']=item['context_state']['revision']
            c.execute('INSERT INTO answers(id,project_id,request_id,body) VALUES(?,?,?,?)',(item['id'],project_id,request_id,json.dumps(item,ensure_ascii=False)))
            answer_events.append(c,item,kind='created')
        return item,True
    def update(self,project_id,answer_id,**changes):
        with self.store.transaction() as c:
            row=c.execute('SELECT body FROM answers WHERE id=? AND project_id=?',(answer_id,project_id)).fetchone()
            if not row:return None
            item=json.loads(row[0])
            if item['status'] in FINAL:return item
            before=dict(item)
            if isinstance(changes.get('result'),dict):
                changes['result']=prepare_result(changes['result'],changes.get('status',item['status']))
            item.update(changes,updated_at=now())
            if item['status'] in FINAL and item.get('context_state'):
                item['context_state']=finalize_state(item,item['status'],item.get('result') or {})
                item['context_revision']=item['context_state']['revision']
            c.execute('UPDATE answers SET body=? WHERE id=?',(json.dumps(item,ensure_ascii=False),answer_id))
            if item['status']!=before['status'] or item['stage']!=before['stage']:
                answer_events.append(c,item,before)
            return item

    def append_terminal_audit(self, project_id, answer_id, diagnostics, *, destination='query_diagnostics'):
        """Append a detached, privacy-projected callback snapshot after cancel/fail.

        This internal diagnostic-only API cannot update an answer/result. Its
        dedicated top-level field is preserved by public_record/JSON export;
        even a null result, the original error, timestamps and lifecycle events
        remain exactly as recorded. The ordinary terminal update guard stays
        intact. Atomic read/deduplicate/append also handles concurrent workers.
        """
        if destination not in ('diagnostics', 'query_diagnostics') or not isinstance(diagnostics, dict):
            raise ValueError('终态审计必须是指定诊断类型的对象')
        from .audit_redaction import redact_diagnostics
        diagnostics = redact_diagnostics(diagnostics)
        encoded = json.dumps({'destination': destination, 'data': diagnostics},
                             ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        detached = json.loads(encoded)
        with self.store.transaction() as c:
            row = c.execute('SELECT body FROM answers WHERE id=? AND project_id=?', (answer_id, project_id)).fetchone()
            if not row:
                return None
            item = json.loads(row[0])
            if item.get('id') != answer_id or item.get('project_id') != project_id:
                return None
            if item.get('status') not in ('cancelled', 'failed'):
                return item
            # This is an additive field introduced for late diagnostics. Do not
            # overwrite unexpected legacy metadata, even if it has this name.
            audit = item.setdefault('diagnostics', {})
            if not isinstance(audit, dict):
                return item
            records = audit.setdefault('late_received_audits', [])
            if not isinstance(records, list):
                return item
            if any(isinstance(record, dict) and record.get('sha256') == digest for record in records):
                return item
            if len(records) >= MAX_LATE_AUDITS:
                audit['late_received_audits_limit_reached'] = True
            else:
                record = {'sha256': digest, 'destination': destination,
                          'publication_status': 'diagnostic_only', 'terminal_status': item['status'],
                          'characters': len(encoded)}
                # Never evict the original failure or a previous received
                # snapshot to admit a later callback. Explicitly disclose the
                # storage limit; it grants no model retry or success status.
                size = len(json.dumps(records, ensure_ascii=False))
                if size + len(encoded) > MAX_LATE_AUDIT_CHARACTERS:
                    record.update(data_omitted=True, omission_reason='terminal_audit_storage_budget')
                else:
                    record['data'] = detached['data']
                records.append(record)
            c.execute('UPDATE answers SET body=? WHERE id=? AND project_id=?',
                      (json.dumps(item, ensure_ascii=False), answer_id, project_id))
            return item
