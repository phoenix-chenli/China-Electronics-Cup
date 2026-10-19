"""Read-only result identity and bounded, observed date spelling feedback."""
from __future__ import annotations
import hashlib
import json
import re
import sqlglot
from sqlglot import exp

def sql_fingerprint(source, sql, parameters):
    try: normalized=sqlglot.parse_one(sql,read='sqlite').sql(dialect='sqlite',pretty=False,comments=False)
    except (sqlglot.errors.ParseError,TypeError):return None
    payload={'source_id':source.get('id'),'sha256':source.get('sha256'),
             'parser_version':source.get('parser_version'),'semantic_revision':source.get('semantic_revision') or source.get('metadata',{}).get('semantic_revision'),
             'sql':normalized,'parameters':parameters}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def _date_key(value):
    if not isinstance(value,str):return None
    m=re.fullmatch(r'(\d{4})(?:年|[-/])(\d{1,2})(?:月|[-/])?(?:(\d{1,2})日?)?',value.strip())
    if not m:return None
    y,month,day=m.groups()
    if not 1<=int(month)<=12 or day is not None and not 1<=int(day)<=31:return None
    return int(y),int(month),int(day) if day else None

def observed_date_feedback(source, sql, parameters, catalog):
    """Suggestions are exact sampled values of the compared physical column.

    Does not change SQL/parameters and never supplies evidence for a claim.
    Only positional '?' direct equality against a known column is recognized.
    """
    try:tree=sqlglot.parse_one(sql,read='sqlite')
    except (sqlglot.errors.ParseError,TypeError):return []
    if len(list(tree.find_all(exp.Select)))!=1:return []
    # Walk a plain AND chain left-to-right, matching SQLite positional order.
    # Generic AST breadth-first traversal can reorder parameters; do not infer
    # positions through OR, functions, subqueries or other placeholder sites.
    where=tree.args.get('where')
    if where is None:return []
    predicates=[]
    def flatten(node):
        if isinstance(node,exp.Paren):return flatten(node.this)
        if isinstance(node,exp.And):return flatten(node.this) and flatten(node.expression)
        if isinstance(node,exp.EQ):predicates.append(node);return True
        return False
    if not flatten(where.this):return []
    placeholders=[node for predicate in predicates for node in (predicate.this,predicate.expression) if isinstance(node,exp.Placeholder)]
    if len(placeholders)!=len(parameters) or len(list(tree.find_all(exp.Placeholder)))!=len(placeholders):return []
    tables={t.alias_or_name:t.name for t in tree.find_all(exp.Table)}
    profiles={(t['name'],p['name']):p for t in catalog.get('tables',[]) for p in t.get('details',{}).get('column_profiles',[])}
    result=[]
    for index,(ph,value) in enumerate(zip(placeholders,parameters)):
        key=_date_key(value); parent=ph.parent
        if not key or not isinstance(parent,exp.EQ):continue
        other=parent.expression if parent.this is ph else parent.this
        if not isinstance(other,exp.Column):continue
        candidates=[(table,p) for (table,col),p in profiles.items() if col==other.name and (not other.table or tables.get(other.table)==table) and table in tables.values()]
        if len(candidates)!=1:continue
        table,profile=candidates[0]
        matches=[v for v in profile.get('distinct_values',[]) if _date_key(v)==key and v!=value]
        if not matches:continue
        result.append({'parameter_index':index,'table':table,'column':other.name,'requested_value':value,
                       'observed_equivalent_spellings':matches[:4],'scope':'bounded_catalog_sample_not_answer_evidence',
                       'source_id':source.get('id'),'sha256':source.get('sha256'),'parser_version':source.get('parser_version'),
                       'guidance':'原查询未改写。核对对象和时期后用真实字段存储格式重新执行；样本不证明全表不存在。'})
    return result[:4]

_DATE_COLUMNS={'period','date','month','时段','时期','日期','月份','年月','统计期'}
_NAME_COLUMNS={'name','名称','站名','姓名','客户名','客户名称','企业名称','设备名称'}

def needs_date_inspection(sql, parameters):
    if any(_date_key(v) for v in parameters):return True
    try:tree=sqlglot.parse_one(sql,read='sqlite')
    except (sqlglot.errors.ParseError,TypeError):return False
    return any(isinstance(p.this,exp.Column) and p.this.name.casefold() in _DATE_COLUMNS
        and isinstance(p.expression,exp.Literal) and p.expression.is_string
        and re.fullmatch(r'%?\d{1,2}月%?',p.expression.this) for p in tree.find_all(exp.Like))

def observed_month_like_feedback(source, sql, catalog):
    """A month-only LIKE gets real storage candidates, not an invented year."""
    try:tree=sqlglot.parse_one(sql,read='sqlite')
    except (sqlglot.errors.ParseError,TypeError):return []
    if len(list(tree.find_all(exp.Select)))!=1:return []
    aliases={t.alias_or_name:t.name for t in tree.find_all(exp.Table)};result=[]
    for predicate in tree.find_all(exp.Like):
        col,value=predicate.this,predicate.expression
        if not isinstance(col,exp.Column) or col.name.casefold() not in _DATE_COLUMNS or not isinstance(value,exp.Literal) or not value.is_string:continue
        match=re.fullmatch(r'%?(\d{1,2})月%?',value.this)
        if not match or not 1<=int(match[1])<=12:continue
        profiles=[(t['name'],p) for t in catalog.get('tables',[]) for p in t.get('details',{}).get('column_profiles',[])
            if p['name']==col.name and t['name'] in aliases.values() and (not col.table or aliases.get(col.table)==t['name'])]
        if len(profiles)!=1:continue
        table,profile=profiles[0]
        matches=[v for v in profile.get('distinct_values',[]) if _date_key(v) and _date_key(v)[1]==int(match[1])]
        if matches:result.append({'table':table,'column':col.name,'requested_like':value.this,
            'observed_same_month_candidates':matches[:4],'year_not_inferred':True,
            'scope':'bounded_catalog_sample_not_answer_evidence','source_id':source.get('id'),
            'sha256':source.get('sha256'),'parser_version':source.get('parser_version'),
            'guidance':'当前LIKE未命中不表示资料缺失。核对用户指定年份与对象后用真实日期格式重新执行，不自动采纳样本年份。'})
    return result[:4]

def needs_entity_inspection(sql):
    if not isinstance(sql,str):return False
    try:tree=sqlglot.parse_one(sql,read='sqlite')
    except (sqlglot.errors.ParseError,TypeError):return False
    return any(isinstance(p.this,exp.Column) and p.this.name.casefold() in _NAME_COLUMNS for p in tree.find_all(exp.EQ))

def unresolved_name_aggregation(question, sql, findings):
    """Only aggregate collapse of proven same-name different primary keys.

    Ordinary repeated country/category filters and duplicate detail rows do not
    trigger this guard. Explicit plural/all scope or a unique real distinguishing
    label remains under the existing independent semantic review.
    """
    if not isinstance(sql,str):return []
    try:tree=sqlglot.parse_one(sql,read='sqlite')
    except (sqlglot.errors.ParseError,TypeError):return []
    if not any(isinstance(n,(exp.Sum,exp.Avg)) or isinstance(n,exp.Anonymous) and n.name.upper() in ('DECIMAL_SUM','DECIMAL_AVG') for n in tree.walk()):return []
    # Plural listing is not permission to collapse rows. A whole-set total
    # authorizes only the named object in that same affirmative clause.
    clauses=re.split(r'[，,。；;！？!?\n]',question)
    aliases={t.alias_or_name:t.name for t in tree.find_all(exp.Table)}
    group=tree.args.get('group')
    grouped=list(group.expressions) if group is not None else []
    projections=[p.this if isinstance(p,exp.Alias) else p for p in tree.expressions]
    def has_key(nodes,table,key):
        return any(isinstance(n,exp.Column) and n.name==key and
            (aliases.get(n.table)==table if n.table else list(aliases.values()).count(table)==1) for n in nodes)
    issues=[]
    for finding in findings:
        if (finding.get('status')!='checked' or finding.get('column','').casefold() not in _NAME_COLUMNS
            or not isinstance(finding.get('primary_key'),list) or len(finding['primary_key'])!=1
            or finding.get('truncated') or finding.get('matched_rows',0)<2
            or not isinstance(finding.get('value'),str) or finding['value'] not in question):continue
        # A compound key can encode one object's periods or events. It does
        # not establish distinct entities for this narrow mechanical guard.
        object_clauses=[clause for clause in clauses if finding['value'] in clause]
        def authorizes_total(clause):
            plural=re.search(r'(?:全部|所有|两站|两个站|两家|两位|两处|各站|各家)',clause)
            total=re.search(r'(?:合计|总和|总数|汇总|总共)',clause)
            denied=re.search(r'(?:不要|不应|不需要|不是|不得|不|非).{0,4}(?:全部|所有|合计|汇总|总和|两站|各站)',clause)
            return plural and total and not denied
        if any(authorizes_total(clause) for clause in object_clauses):continue
        cols=finding.get('candidate_columns',[]);rows=finding.get('candidates',[])
        if not rows or any(k not in cols for k in finding['primary_key']):continue
        if all(has_key(grouped,finding['table'],key) and has_key(projections,finding['table'],key) for key in finding['primary_key']):
            continue  # Every true object remains separately grouped and displayed.
        indices=[cols.index(k) for k in finding['primary_key']]
        if len({tuple(str(row[i]) for i in indices) for row in rows})<2:continue
        chosen=[]
        for index,row in enumerate(rows):
            labels=[str(v) for c,v in zip(cols,row) if c!=finding['column'] and isinstance(v,str) and len(v)>=2]
            if any(label in clause and not re.search(r'(?:不要|不是|非|不含)'+re.escape(label),clause)
                   for label in labels for clause in object_clauses):chosen.append(index)
        if len(chosen)==1:continue
        issues.append({'table':finding['table'],'name_column':finding['column'],'name':finding['value'],
            'primary_key':finding['primary_key'],'candidate_columns':cols,'candidates':rows,
            'reason':'同名对应多个不同主键，真实用户未选择对象或明确全部合计；不得静默求和'})
    return issues[:3]

def overstrong_empty_claim(text, records):
    """An empty executed filter is not a source-wide absence certificate.

    This applies even when sampling was unavailable, truncated or found no
    spelling candidates. A narrowly described query miss remains permissible.
    Explicitly naming a different cited document keeps that document's own
    supported limitation separate from an unrelated empty table query.
    """
    empty = [r for r in records if (r.get('query_result') or {}).get('rows') == []]
    if not empty:
        return []
    names = {r.get('source_name') for r in records if isinstance(r.get('source_name'), str)}
    clauses = re.split(r'[，,。；;！？!?\n]', text)
    rejected = []
    for record in empty:
        for clause in clauses:
            named = {name for name in names if name and name in clause}
            if named and record.get('source_name') not in named:
                continue
            # Only a finite query statement is exempt, not a later assertion
            # that the source itself lacks records.
            absences = list(re.finditer(r'(?:未找到|没有|未提供|不存在|缺少|查无|无(?=对象|相关|对应|独立|记录|数据|该对象))', clause))
            source_marker = bool(named) or bool(re.search(r'(?:资料|数据库|文件|原文|原件|数据表|表格)', clause))
            record_absence = bool(re.search(r'(?:查无|无|未找到|不存在|没有).{0,45}(?:记录|数据)', clause))
            for absence in absences if source_marker or record_absence else []:
                prefix = clause[:absence.start()]
                if re.search(r'(?:本次|当前|该次|这次|这条|该条)(?:SQL)?查询(?:结果)?(?:中)?$', prefix):
                    continue  # “本次查询未找到资料中的记录” is only a query miss.
                if absence is absences[0] and re.search(r'(?:不能|无法|尚不能|不足以)(?:仅)?(?:据此)?(?:确认|证明|断定|认定|说明)[^，。；]{0,12}$', prefix):
                    continue  # Explicitly withholding a source-absence conclusion.
                if re.search(r'(?:是否|能否)[^，。；]{0,8}$', prefix):
                    continue
                rejected.append(record['id'])
                break
            if record['id'] in rejected:
                break
    return list(dict.fromkeys(rejected))
