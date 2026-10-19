"""Occurrence-local calendar labels justified by executed SQL filters.

This does not prove business scope, exhaustive results or fact correctness.
Only one explicit half-open ISO date range in a conjunctive WHERE is accepted.
Annual labels additionally require exactly one full calendar year.
"""
from __future__ import annotations
from datetime import date
import re


def _bound_sql(query):
    import sqlglot
    from sqlglot import exp
    from sqlglot.tokens import TokenType
    sql=query.get('sql');parameters=query.get('parameters')
    if not isinstance(sql,str) or not isinstance(parameters,list): return None
    try:
        tokens=[t for t in sqlglot.tokenize(sql,read='sqlite') if t.token_type==TokenType.PLACEHOLDER]
        if len(tokens)!=len(parameters) or any(t.text!='?' or sql[t.end+1:t.end+2].isdigit() for t in tokens): return None
        for token,value in reversed(list(zip(tokens,parameters))):
            if type(value) not in (str,int,float,bool,type(None)): return None
            sql=sql[:token.start]+exp.convert(value).sql(dialect='sqlite')+sql[token.end+1:]
        trees=sqlglot.parse(sql,read='sqlite')
        return trees[0] if len(trees)==1 else None
    except (ValueError,TypeError,sqlglot.errors.SqlglotError):
        return None


def half_open_filter(evidence):
    """Return one strictly proved ISO date range, never execute SQL."""
    from sqlglot import exp
    tree=_bound_sql(evidence.get('query') or {})
    if not isinstance(tree,exp.Select) or len(list(tree.find_all(exp.Select)))!=1: return None
    where=tree.args.get('where')
    if where is None or any(isinstance(n,(exp.Or,exp.Not,exp.Subquery,exp.Exists)) for n in where.walk()): return None
    def flatten(node):
        if isinstance(node,exp.Paren):return flatten(node.this)
        if isinstance(node,exp.And):return flatten(node.this)+flatten(node.expression)
        return [node]
    def key(column):return tuple(p.name.casefold() for p in column.parts)
    predicates=flatten(where.this);columns={key(n) for p in predicates for n in p.find_all(exp.Column)}
    ranges=[]
    for column in columns:
        constraints=[];invalid=False
        for predicate in predicates:
            referenced={key(n) for n in predicate.find_all(exp.Column)}
            if column not in referenced: continue
            if type(predicate) not in (exp.GTE,exp.LT):invalid=True;break
            left,right=predicate.this,predicate.expression
            if not isinstance(left,exp.Column) or key(left)!=column or not isinstance(right,exp.Literal) or not right.is_string:
                invalid=True;break
            raw=right.this
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',raw):invalid=True;break
            try: parsed=date.fromisoformat(raw)
            except ValueError:invalid=True;break
            constraints.append((type(predicate),parsed))
        if invalid:continue
        pairs=set(constraints)
        lower=[v for op,v in pairs if op is exp.GTE]
        upper=[v for op,v in pairs if op is exp.LT]
        if len(pairs)==2 and len(lower)==len(upper)==1 and lower[0]<upper[0]:
            ranges.append({'column':'.'.join(column),'start_inclusive':lower[0].isoformat(),
                           'end_exclusive':upper[0].isoformat()})
    return ranges[0] if len(ranges)==1 else None


def full_year_filter(evidence):
    proved=half_open_filter(evidence)
    if proved is None:return None
    start=date.fromisoformat(proved['start_inclusive']);end=date.fromisoformat(proved['end_exclusive'])
    if start.month==start.day==end.month==end.day==1 and end.year==start.year+1:
        return {'column':proved['column'],'year':start.year,'start_inclusive':proved['start_inclusive'],
                'end_exclusive':proved['end_exclusive']}
    return None


def _column_labels(evidence,proved):
    from sqlglot import exp
    column=proved['column'];labels={column}
    tree=_bound_sql(evidence.get('query')or{})
    if '.' not in column:
        tables=list(tree.find_all(exp.Table)) if tree is not None else []
        if len(tables)==1:
            labels.update(name.casefold()+'.'+column for name in (tables[0].name,tables[0].alias) if name)
    return labels


def _annotation(match,evidence,proved,display_kind):
    return {'kind':'executed_query_time_filter','origin':'executed_sql_where',
            'evidence_id':evidence.get('id'),'source_id':evidence.get('source_id'),**proved,
            'statement_start':match.start(),'statement_end':match.end(),'display_text':match[0],
            'display_kind':display_kind,'scope':'query_filter_label_only_not_business_fact',
            'semantic_review_required':True}


def _range_annotations(statement,evidence,proved):
    # The ending must explicitly exclude the actual upper bound. An unqualified
    # "start to end" does not establish which endpoint is included.
    pattern=(r'(?<![A-Za-z0-9_.])(?P<start>\d{4}-\d{2}-\d{2})'
             r'(?:\s*[（(](?:含|含当日|包含)[）)])?\s*(?:至|到)\s*'
             r'(?P<end>\d{4}-\d{2}-\d{2})\s*'
             r'(?:之前|以前|[（(](?:不含|不含当日|不包括)[）)])')
    out=[]
    for match in re.finditer(pattern,statement):
        if match['start']!=proved['start_inclusive'] or match['end']!=proved['end_exclusive']:continue
        prefix=statement[max(0,match.start()-80):match.start()]
        suffix=statement[match.end():]
        if re.match(r'\s*(?:以外|之外|外|之后|以后|(?:并|且|同时|但)?\s*(?:含|包含|包括|计入)(?:该|其)?(?:截止|结束|上界|终止)(?:日|日期))',suffix):continue
        if re.search(r'不是|并非|不按|排除|不含|不包括|晚于|大于',prefix):continue
        # An explicitly named date field must match the executed predicate;
        # a table qualifier may be recovered only from a single real SQL table.
        named=re.search(r'(?P<field>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*取\s*$',prefix)
        qualified=re.search(r'(?P<field>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*(?:的)?\s*(?:日期范围为|日期范围|日期为|范围为|期间为)\s*$',prefix)
        if qualified and qualified['field'].casefold() not in _column_labels(evidence,proved):continue
        if named:
            if named['field'].casefold() not in _column_labels(evidence,proved):continue
            if not re.search(r'日期|范围|期间|筛选|统计|查询',prefix[:named.start()]):continue
        elif not re.search(r'(?:日期范围为|日期范围|查询范围为|统计范围为|时间范围为|期间为|查询期间为|日期为|日期取|日期按|统计期间为|从)\s*$',prefix):
            continue
        out.append(_annotation(match,evidence,proved,'half_open_iso_date_range'))
    return out


def time_annotations(statement,evidence):
    interval=half_open_filter(evidence)
    if interval is None:return []
    out=_range_annotations(statement,evidence,interval)
    proved=full_year_filter(evidence)
    if proved is None:return out
    for match in re.finditer(r'(?<![A-Za-z0-9_.])(?P<year>\d{4})\s*年',statement):
        if int(match['year'])!=proved['year']:continue
        prefix=statement[max(0,match.start()-32):match.start()]
        suffix=statement[match.end():]
        if (re.search(r'截至|截止|之前|以后|早于|晚于|不是|并非|不含|排除|不按|未按|不统计|不查询|寿命|年限|期限|年数',prefix)
                or re.match(r'\s*(?:[0-9一二三四五六七八九十]+月|初|末|底|上半年|下半年|第?[一二三四1-4]季度|之前|之后|以前|以后|以来|起|前|后|内|间|及|至|到|[—–~～-])',suffix)):
            continue
        direct=re.search(r'(?:统计|查询|筛选|按|在|范围为|期间为|年度为|年份为)\s*$',prefix)
        leading_authority=re.fullmatch(r'\s*(?:按|根据|依据)[^。！？；;\n]{1,80}[，,:：]\s*',statement[:match.start()])
        # A result narrative can refer back to this actual query without putting
        # "统计" immediately before the year. Accept only a positive clause-level
        # query reference; the SQL still has to prove the same complete year.
        # This masks the year occurrence, never a count/value elsewhere in text.
        query_reference=re.search(
            r'(?:^|[。！？；;，,\n])\s*(?:同一|本次|该)查询(?:结果)?中(?:纳入统计的)?\s*(?:[，,:：]\s*)?$',
            statement[:match.start()])
        if query_reference and re.match(r'\s*(?:以外|之外|外)',suffix):query_reference=None
        if match.start()!=0 and not direct and not leading_authority and not query_reference:continue
        out.append(_annotation(match,evidence,proved,'full_calendar_year'))
    return sorted(out,key=lambda item:(item['statement_start'],item['statement_end']))
