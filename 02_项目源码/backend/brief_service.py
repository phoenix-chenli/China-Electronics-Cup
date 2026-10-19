"""Sourced, immutable HTML briefs with a stable browser-to-PDF print layout."""
from __future__ import annotations
import hashlib
import html
import json
from pathlib import Path
from .export_service import read_snapshot, ExportError
from .store import identity, now


def esc(value):return html.escape(str(value),quote=True)


def published_sql_snapshot(result):
    """A limited SQL review remains limited when its executed rows are exported."""
    if (result.get('verification') or {}).get('status') != 'limited':return False
    query=result.get('query') or {};columns=result.get('columns');rows=result.get('rows')
    if not isinstance(query.get('sql'),str) or not query['sql'].strip():return False
    if not isinstance(columns,list) or not columns or any(not isinstance(c,str) for c in columns):return False
    if not isinstance(rows,list) or any(not isinstance(row,list) or len(row)!=len(columns) for row in rows):return False
    if type(query.get('returned_rows')) is not int or query['returned_rows']!=len(rows):return False
    if type(query.get('elapsed_ms')) not in (int,float) or query['elapsed_ms']<0:return False
    if not isinstance(query.get('parameters'),(list,dict)) or not isinstance(query.get('used_tables'),list) or not query['used_tables']:return False
    evidence={e.get('id'):e for e in result.get('evidence',[]) if isinstance(e,dict) and e.get('kind')=='table' and not e.get('stale')}
    if any(not any(e.get('table_name')==table for e in evidence.values()) for table in query['used_tables']):return False
    return bool(result.get('claims')) and all(claim.get('evidence_ids') and all(eid in evidence for eid in claim['evidence_ids']) for claim in result['claims'])


def render_brief(snapshot):
    sections=[]
    for index,answer in enumerate(snapshot['answers'],1):
        result=answer['result'];parts=[f'<section><h2>{index}. {esc(answer["question"])}</h2>']
        evidence={e['id']:e for e in result.get('evidence',[]) if e.get('id')}
        for claim in result.get('claims',[]):
            refs=' '.join(f'<a href="#s{index}-{esc(eid)}">[{esc(eid)}]</a>' for eid in claim.get('evidence_ids',[]) if eid in evidence)
            parts.append(f'<p>{esc(claim.get("text","")).replace(chr(10),"<br>")} <span class="ref">{refs}</span></p>')
        columns=result.get('columns');rows=result.get('display_rows') or result.get('rows')
        if columns and rows is not None:
            parts.append('<div class="table-wrap"><table><thead><tr>'+''.join('<th>'+esc(c)+'</th>' for c in columns)+'</tr></thead><tbody>')
            for row in rows:
                parts.append('<tr>'+''.join('<td>'+('—' if v is None else esc(v))+'</td>' for v in row)+'</tr>')
            parts.append('</tbody></table></div>')
            if (result.get('query') or {}).get('truncated'):parts.append('<p class="note">此表保留当时返回行，查询存在截断，请结合原回答中的上限理解。</p>')
        for calc in result.get('calculations',[]):
            parts.append(f'<p class="calculation">{esc(calc.get("expression",""))} = {esc(calc.get("display_value",calc.get("value","")))} {esc(calc.get("unit",""))}</p>')
        parts.append('<h3>来源与核对</h3><ol class="sources">')
        for eid,e in evidence.items():
            location=('第'+str(e['page'])+'页') if e.get('page') else str(e.get('table_name') or '保存的来源范围')
            parts.append(f'<li id="s{index}-{esc(eid)}"><strong>{esc(eid)} · {esc(e.get("source_name",""))}</strong> · {esc(location)}')
            if e.get('quote'):parts.append('<blockquote>'+esc(e['quote'])+'</blockquote>')
            parts.append(f'<span class="digest">原件 SHA256：{esc(e.get("sha256","未记录"))}<br>解析版本：{esc(e.get("parser_version","未记录"))}</span></li>')
        parts.append('</ol>')
        verification=result.get('verification') or {}
        if verification.get('status')=='limited':parts.append('<p class="note">核对范围：只读 SQL 已执行，结果按当时返回行保存；自动关联与口径复核不等同于人工业务核验。</p>')
        if verification.get('checks'):parts.append('<ul class="note">'+''.join('<li>'+esc(check)+'</li>' for check in verification['checks'])+'</ul>')
        if result.get('notes'):parts.append('<p class="note">'+'；'.join(esc(n) for n in result['notes'])+'</p>')
        query=result.get('query') or {}
        if query.get('sql'):
            parts.append('<details><summary>当时执行的查询与参数</summary><pre>'+esc(query['sql'])+'\n'+esc(json.dumps(query.get('parameters'),ensure_ascii=False))+'</pre></details>')
        parts.append(f'<p class="digest">回答编号：{esc(answer["id"])} · 保存时间：{esc(answer["updated_at"])}</p></section>')
        sections.append(''.join(parts))
    css='''@page{size:A4;margin:2.54cm 3.17cm}*{box-sizing:border-box}body{margin:0;color:#263434;background:white;font:16px/1.7 "Noto Serif CJK SC","Songti SC",serif}main{max-width:860px;margin:48px auto;padding:0 24px}h1{font:600 30px/1.3 sans-serif;margin:0 0 16px}h2{font:600 21px/1.5 sans-serif;margin:34px 0 18px}h3{font:600 16px/1.5 sans-serif}p{margin:12px 0}a{color:#476e69;text-decoration:none}.meta,.note{color:#52625e;font-size:14px}.ref{font-size:13px}table{border-collapse:collapse;width:100%;font-size:14px;background:#fff}td,th{border-bottom:1px solid #b8c5c1;padding:9px;text-align:left;background:#fff}th{font-weight:600;border-top:1.5px solid #647772}blockquote{margin:6px 0 12px;padding:0 14px;border-left:2px solid #a9bbb4;white-space:pre-wrap}.digest{font:11px/1.5 monospace;overflow-wrap:anywhere;color:#596662}.sources{padding-left:22px}.sources li{margin:16px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}footer{border-top:1px solid #bdc9c5;margin-top:36px;padding-top:16px;font-size:12px;color:#596662}@media print{main{margin:0;padding:0;max-width:none}body{font-size:12pt;line-height:1.5}h2,h3{break-after:avoid}tr,li,.calculation{break-inside:avoid}a{color:inherit}details{display:block}details>*{display:block}h1{font-size:22pt}.meta,.note{font-size:10pt}.digest{font-size:8pt}}'''
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
            f'<title>{esc(snapshot["title"])}</title><style>{css}</style></head><body><main><h1>{esc(snapshot["title"])}</h1>'
            f'<p class="meta">知衡分析简报 · {esc(snapshot["created_at"])} · {len(snapshot["answers"])} 项已保存分析</p>'
            +''.join(sections)+'<footer>本简报汇集导出时已完成且来源登记版本匹配的回答，不重新运行查询。'
            '来源片段、查询和参数按当时快照保存；后续数据更新不会改写本文件。可在浏览器中打印为 PDF。'
            f'<br>简报编号：{esc(snapshot["id"])}</footer></main></body></html>').encode()


class BriefService:
    def __init__(self,store):self.store=store;self.root=store.root/'briefs'
    def initialize(self):
        self.root.mkdir(parents=True,exist_ok=True)
        with self.store.transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS briefs(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),title TEXT NOT NULL,created_at TEXT NOT NULL,body TEXT NOT NULL,html_sha256 TEXT NOT NULL)')
    def create(self,project_id,title,answer_ids):
        if not isinstance(title,str) or not title.strip() or len(title)>120:raise ValueError('简报标题为1至120字符')
        if not isinstance(answer_ids,list) or not 1<=len(answer_ids)<=20 or any(not isinstance(a,str) or not a or len(a)>80 for a in answer_ids) or len(set(answer_ids))!=len(answer_ids):raise ValueError('请选择1至20条不同的已完成分析')
        # Hold a writer reservation while collecting read-only snapshots. All
        # selected answers and source versions belong to one unchanged state.
        with self.store.transaction() as connection:
            return self._create_locked(connection,project_id,title,answer_ids)
    def _create_locked(self,connection,project_id,title,answer_ids):
        answers=[];metadata={}
        for aid in answer_ids:
            payload=read_snapshot(self.store.path,project_id,aid)
            answer=payload['answers'][0];result=answer.get('result') or {}
            if answer['status']!='completed' or not result.get('claims') or not ((result.get('verification') or {}).get('status')=='passed' or published_sql_snapshot(result)):
                raise ValueError('仅可汇集已完成且有核对记录的事实或已执行查询')
            if payload['record_metadata'][aid]['freshness']['scope_stale']:raise ValueError('所选回答来源或业务定义已过期，请先重新分析')
            answers.append(answer);metadata[aid]=payload['record_metadata'][aid]
        brief=dict(schema_version='zhiheng.brief.v1',id=identity(),project_id=project_id,title=title.strip(),created_at=now(),answers=answers,record_metadata=metadata)
        data=render_brief(brief);sha=hashlib.sha256(data).hexdigest();target=self.root/(brief['id']+'.html')
        target.write_bytes(data)
        try:
            connection.execute('INSERT INTO briefs VALUES(?,?,?,?,?,?)',(brief['id'],project_id,brief['title'],brief['created_at'],json.dumps(brief,ensure_ascii=False),sha))
        except Exception:target.unlink(missing_ok=True);raise
        return dict(id=brief['id'],title=brief['title'],created_at=brief['created_at'],answer_ids=answer_ids,html_sha256=sha)
    def listing(self,project_id):
        with self.store.transaction() as c:
            return [dict(r) for r in c.execute('SELECT id,title,created_at,html_sha256 FROM briefs WHERE project_id=? ORDER BY created_at DESC',(project_id,))]
    def get(self,project_id,brief_id,format='html'):
        if format not in ('html','json'):raise ValueError('简报格式仅支持html/json')
        with self.store.transaction() as c:
            row=c.execute('SELECT body,html_sha256 FROM briefs WHERE id=? AND project_id=?',(brief_id,project_id)).fetchone()
        if not row:raise LookupError('简报不存在或不属于当前项目')
        if format=='json':return row[0].encode(), 'application/json'
        data=(self.root/(brief_id+'.html')).read_bytes()
        if hashlib.sha256(data).hexdigest()!=row[1]:raise ValueError('简报文件摘要不符，未提供变更后的内容')
        return data,'text/html'
