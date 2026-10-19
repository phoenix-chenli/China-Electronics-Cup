"""Narrow deterministic user/AST contracts, never an answer-key oracle.

Unknown natural language stays unknown. Checks bind literal request fragments,
DDL keys and real SQL structure; actual counterexamples use the same readonly
executor limits and never supply reference answers to a model.
"""
from __future__ import annotations
import json
import re
import hashlib
import sqlglot
from sqlglot import exp


def fingerprint(sql, parameters):
    try:sql=sqlglot.parse_one(sql,read='sqlite').sql(dialect='sqlite',normalize=True,comments=False)
    except (ValueError,TypeError,sqlglot.errors.ParseError):sql=str(sql)
    return hashlib.sha256(json.dumps([sql,parameters],ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def explicit_output_contract(question):
    """Only a closed, short literal enumeration after 输出/返回 … punctuation.

    Do not interpret free-form '列明细', examples, optional items or an ellipsis
    as an exhaustive projection contract. Suffix unit notes do not add columns.
    """
    if not isinstance(question,str):return None
    candidates=[]
    for match in re.finditer(r'(?:只?输出|只?返回)\s*',question):
        tail=re.split(r'[。；;\n？?]',question[match.end():],maxsplit=1)[0]
        if re.search(r'输出|返回',tail):continue
        text=re.split(r'[，,]',tail,maxsplit=1)[0].strip()
        text=re.sub(r'[（(](?:均[^）)]*|分别[^）)]*)[）)]$','',text).strip()
        # An explicit separator enumeration only. '最早、最晚两列' is exact.
        text=re.sub(r'(?:这?\s*[二三四五六七八九十\d]+列)(?:输出)?$','',text).strip()
        if re.search(r'例如|等(?:列|字段)?$|至少|可选|任选|包括|包含|…|\.\.\.|和.*或|或',text):continue
        parts=[p.strip() for p in re.split(r'、|，|,|和|及',text) if p.strip()]
        if any(re.search(r'(?:[一二三四五六七八九十两]|\d+)(?:项值|项指标|个指标|个字段|列数据)',p) for p in parts):continue
        singleton=bool(re.match(r'只(?:输出|返回)',match.group(0))) and len(parts)==1 and len(parts[0])<=16 and not re.search(r'的|满足|符合',parts[0])
        if not (2<=len(parts)<=12 or singleton) or any(len(p)>24 or re.search(r'[=×*/<>]|按.+排序|保留|不把|不要',p) for p in parts):continue
        candidates.append({'basis':'literal_user_enumeration','span':[match.start(),match.end()+len(text)],
                           'quote':question[match.start():match.end()+len(text)],'columns':parts,'expected_count':len(parts)})
    return candidates[-1] if len(candidates)==1 else None


def _mapping(select,catalog):
    tables={t['name']:t for t in catalog.get('tables',[])}
    direct=[select.args['from_'].this] if select.args.get('from_') else []
    direct += [j.this for j in select.args.get('joins',[])]
    aliases={t.alias_or_name:t.name for t in direct if isinstance(t,exp.Table) and t.name in tables}
    def resolve(col):
        if not isinstance(col,exp.Column):return None
        names=[aliases[col.table]] if col.table in aliases else list(aliases.values()) if not col.table else []
        values=[(name,c) for name in names for c in tables[name].get('columns',[]) if c['name']==col.name]
        return values[0] if len(values)==1 else None
    return tables,aliases,resolve


def _nonnull_user_basis(question,column,aliases=()):
    # Only a literal physical field name (or a separately confirmed semantic
    # alias, if supplied by a future caller) can authorize a filter. A SQL alias
    # chosen by the same planner is not independent user grounding.
    for label in [column,*aliases]:
        if not label:continue
        escaped=re.escape(label)
        patterns=[escaped+r'\s*(?:字段|列|值)?\s*(?:不是\s*NULL|非\s*NULL|不为\s*NULL|IS\s+NOT\s+NULL|不为空|非空|已填写|有值)',
                  r'(?:非空的?|已填写的?|有值的?)\s*'+escaped,
                  r'(?:排除|过滤掉|去掉)\s*'+escaped+r'\s*(?:为|是)?\s*NULL']
        for pattern in patterns:
            for match in re.finditer(pattern,question,re.I):
                prefix=question[max(0,match.start()-8):match.start()]
                if re.search(r'不要|不得|不能|禁止|不应',prefix):continue
                return match.group(0)
    return None


def _redundant_nonnull(select,node):
    where=select.args.get('where')
    if not where or not isinstance(where.this,exp.And):return False
    target=node.this
    def strict_value(value):
        while isinstance(value,(exp.Paren,exp.Cast)):value=value.this
        return isinstance(value,exp.Column) and value==target
    for part in where.this.flatten():
        if part is node.parent:continue
        if isinstance(part,(exp.EQ,exp.NEQ,exp.GT,exp.GTE,exp.LT,exp.LTE,exp.Like)):
            if strict_value(part.this) or strict_value(part.expression):return True
        if isinstance(part,exp.Between) and strict_value(part.this):return True
    return False


def adjudicate_nonnull_basis(mechanical,check,question,history=()):
    """A detailed independent field-to-exact-user-quote check, not five booleans.

    No quote is synthesized from a normalized question or SQL alias. Bare
    'same unit', a new model interpretation or an unrelated generic quote
    cannot authorize dropping unknown source rows.
    """
    raw=check.get('nonnull_filter_basis') if isinstance(check,dict) else None
    supplied=raw if isinstance(raw,list) and len(raw)<=16 else []
    originals=[question]+[t['original_question'] for t in history if isinstance(t,dict) and isinstance(t.get('original_question'),str)]
    accepted=[];remaining=[]
    for issue in mechanical['issues']:
        found=None
        if issue['code']=='unrequested_nonnull_filter':
            for item in supplied:
                if not isinstance(item,dict) or item.get('supported') is not True or item.get('table')!=issue['table'] or item.get('column')!=issue['column']:continue
                quote=item.get('user_quote')
                if not isinstance(quote,str) or not 2<=len(quote)<=120 or not any(quote in q for q in originals):continue
                if re.search(r'不要|不能|不得|禁止',quote):continue
                if not re.search(r'非空|非\s*NULL|不为\s*NULL|不是\s*NULL|不为空|已填写|未.{1,16}(?:不计入|不进分母|不计)|(?:排除|剔除).{0,12}(?:未知|缺失|NULL)',quote,re.I):continue
                if re.search(r'取消|撤销|移除|保留.*NULL|不要过滤|全部数据',question) and quote not in question:continue
                found={'table':item['table'],'column':item['column'],'user_quote':quote,
                       'basis':'received_independent_field_review_of_exact_user_quote'};break
        if found:accepted.append(found)
        else:remaining.append(issue)
    mechanical['accepted_nonnull_basis']=accepted
    mechanical['unresolved_issues']=remaining
    return remaining


def _global_none_requested(question):
    # A nested existence request ('at least one child that never ...') must not
    # become global absence of the displayed parent. Ambiguous wording stays
    # with independent semantic review, not this deterministic block.
    strong=r'完全没有|完全未|从未|从没|没有任何|never|no\s+.+\s+at\s+all'
    for clause in re.split(r'[，。；;？?]',question):
        match=re.search(strong,clause,re.I)
        if not match:continue
        before=clause[:match.start()]
        if re.search(r'不把|不是|并非|不代表|不等于|不要|不得',before):continue
        if re.search(r'(?:至少有|至少存在|存在|有一|有某|有部分|有.{0,4}[笔条项个])[^，。；;？?]{0,32}$',before):continue
        if re.search(r'(?:at\s+least\s+one|some|any)\s+.{0,40}$',before,re.I):continue
        return True
    return False


def mechanical_contract(sql,parameters,question,catalog,source,execute,cancelled=lambda:False,*,actual_columns=None,history=()):
    tree=sqlglot.parse_one(sql,read='sqlite')
    result={'version':'sql-mechanical-contract-1','issues':[],'output_contract':explicit_output_contract(question),
            'null_filters':[],'quantifier_checks':[],'counterexamples':[],
            'boundary':'Narrow literal request and AST/DDL checks. Unknown meanings still require independent review.'}
    def issue(code,**detail):result['issues'].append({'code':code,**detail})
    output=result['output_contract']
    actual_count=len(actual_columns) if isinstance(actual_columns,list) else len(tree.expressions) if isinstance(tree,exp.Select) and not any(p.is_star for p in tree.expressions) else None
    if output and actual_count is not None and actual_count!=output['expected_count']:
        issue('explicit_output_column_count',expected=output['expected_count'],actual=actual_count,user_quote=output['quote'])
    tables={t['name']:t for t in catalog.get('tables',[])}
    selects=list(tree.find_all(exp.Select))
    if len(selects)>16:
        issue('mechanical_contract_budget_exceeded',select_count=len(selects));result['truncated']=True;return result
    for select in selects:
        _,aliases,resolve=_mapping(select,catalog)
        where=select.args.get('where')
        for node in where.find_all(exp.Is) if where else []:
            if node.find_ancestor(exp.Select) is not select or not isinstance(node.expression,exp.Null) or not isinstance(node.parent,exp.Not):continue
            match=resolve(node.this)
            if not match:
                # Derived/ambiguous NULL filtering is not implicitly authorized.
                predicate=node.parent.sql(dialect='sqlite')
                issue('unresolved_nonnull_filter',predicate=predicate);continue
            table,column=match;name=column['name']
            basis=_nonnull_user_basis(question,name)
            if not basis and name not in question and not re.search(r'取消|撤销|去掉|移除|保留|不过滤|不要过滤|全部|所有',question):
                for turn in reversed(list(history)[-30:]):
                    original=turn.get('original_question') if isinstance(turn,dict) else None
                    if isinstance(original,str) and name in original:
                        matched=_nonnull_user_basis(original,name)
                        if matched:basis={'original_question':original,'quote':matched,'answer_id':turn.get('answer_id') or turn.get('id')}
                        break
            redundant=_redundant_nonnull(select,node)
            entry={'table':table,'column':name,'predicate':node.parent.sql(dialect='sqlite'),'user_basis':basis,'declared_nonnullable':column.get('nullable') is False,'entailed_by_other_conjunct':redundant}
            result['null_filters'].append(entry)
            if not basis and column.get('nullable') is not False and not redundant:
                issue('unrequested_nonnull_filter',**entry)
        # This global none check is activated only by an explicit strong
        # quantifier. Ordinary requests for individual unpaid orders are not
        # equated with a customer's complete absence of payments.
        if not _global_none_requested(question):continue
        projected=[]
        for projection in select.expressions:
            p=projection.this if isinstance(projection,exp.Alias) else projection
            if isinstance(p,exp.Column) and (match:=resolve(p)):
                projected.append((p,match[0],match[1]['name']))
        antis=[(e,e.parent,'NOT EXISTS') for e in select.find_all(exp.Exists)
               if e.find_ancestor(exp.Select) is select and isinstance(e.parent,exp.Not)]
        # LEFT JOIN ... WHERE right_primary_key IS NULL is another common
        # anti-join spelling. Bind it to the actual ON condition and same DDL.
        for node in where.find_all(exp.Is) if where else []:
            if node.find_ancestor(exp.Select) is not select or isinstance(node.parent,exp.Not) or not isinstance(node.expression,exp.Null) or not isinstance(node.this,exp.Column):continue
            match=resolve(node.this)
            if not match or node.this.name not in tables[match[0]].get('primary_key',[]):continue
            for join in select.args.get('joins',[]):
                if str(join.side).upper()!='LEFT' or not isinstance(join.this,exp.Table) or join.this.alias_or_name!=node.this.table or not join.args.get('on'):continue
                inner=exp.select('1').from_(join.this.copy()).where(join.args['on'].copy())
                antis.append((exp.Exists(this=inner),node,'LEFT JOIN NULL'))
        for exists,negative_predicate,spelling in antis:
            inner=exists.this
            if not isinstance(inner,exp.Select):continue
            local_direct=[inner.args['from_'].this] if inner.args.get('from_') else []
            local_direct += [j.this for j in inner.args.get('joins',[])]
            local_aliases={t.alias_or_name for t in local_direct if isinstance(t,exp.Table)}
            correlations=[]
            for col in inner.find_all(exp.Column):
                if col.find_ancestor(exp.Select) is not inner or not col.table or col.table in local_aliases:continue
                if (match:=resolve(col)) and (col,match[0],match[1]['name']) not in correlations:correlations.append((col,match[0],match[1]['name']))
            for _,lower,lower_key in correlations:
                if lower_key not in tables[lower].get('primary_key',[]):continue
                for projection,entity_table,entity_column in projected:
                    if entity_table==lower and entity_column==lower_key:continue
                    links=[]
                    for rel in catalog.get('relationships',[]):
                        if rel.get('table')!=lower or len(rel.get('columns',[]))!=1 or len(rel.get('referenced_columns',[]))!=1:continue
                        fk=rel['columns'][0]
                        if ((entity_table==lower and entity_column==fk)
                            or (entity_table==rel.get('referenced_table') and entity_column==rel['referenced_columns'][0])):links.append(rel)
                    for link in links:
                        # A declared unique FK cannot have both a positive and
                        # negative child record; no one-to-many grain mismatch.
                        unique=[k.get('columns',[]) for k in tables[lower].get('unique_keys',[]) if isinstance(k,dict)]
                        if link['columns'] in unique or link['columns']==tables[lower].get('primary_key'):continue
                        record={'entity_projection':projection.sql(dialect='sqlite'),'lower_table':lower,'lower_key':lower_key,
                                'declared_entity_fk':link['columns'],'referenced_entity':link.get('referenced_table'),
                                'predicate':negative_predicate.sql(dialect='sqlite'),'anti_join_form':spelling}
                        if record in result['quantifier_checks']:continue
                        result['quantifier_checks'].append(record)
                        issue('global_none_correlated_at_child_grain',**record)
                        # Keep other conjuncts exactly; do not transform OR or
                        # aggregate/window queries into purported counterexamples.
                        conjuncts=list(where.this.flatten()) if where and isinstance(where.this,exp.And) else [where.this] if where else []
                        if any(isinstance(c,exp.Or) for c in conjuncts) or select.args.get('group') or any(p.find(exp.AggFunc) or p.find(exp.Window) for p in select.expressions):continue
                        kept=[c.copy() for c in conjuncts if c is not negative_predicate]
                        if len(kept)==len(conjuncts):continue
                        probe=select.copy();probe.set('distinct',None);probe.set('order',None);probe.set('limit',None);probe.set('offset',None)
                        probe.set('expressions',[projection.copy(),exp.Count(this=exp.Star()).as_('row_count')])
                        probe.set('where',exp.Where(this=exp.and_(*kept)) if kept else None)
                        probe.set('group',exp.Group(expressions=[projection.copy()]))
                        positive=exp.Max(this=exp.Case(ifs=[exp.If(this=exists.copy(),true=exp.Literal.number(1))],default=exp.Literal.number(0)))
                        negative=exp.Max(this=exp.Case(ifs=[exp.If(this=exp.Not(this=exists.copy()),true=exp.Literal.number(1))],default=exp.Literal.number(0)))
                        probe.set('having',exp.Having(this=exp.and_(exp.EQ(this=positive,expression=exp.Literal.number(1)),exp.EQ(this=negative,expression=exp.Literal.number(1)))))
                        probe.set('limit',exp.Limit(expression=exp.Literal.number(6)))
                        # Parameter reordering/count could change meaning. Use
                        # only literal queries here; parameterized variants still
                        # receive the structural block, not invented probe rows.
                        if parameters or len(result['counterexamples'])>=2:continue
                        if cancelled():raise InterruptedError('已取消')
                        from .sql_engine import SQLExecutionError
                        try:
                            data=execute(source,probe.sql(dialect='sqlite'),[],cancelled)
                            result['counterexamples'].append({'basis':'actual_readonly_query','sql':data['sql'],'parameters':[],
                                'columns':data['columns'],'rows':data['rows'],'truncated':data.get('truncated',False),
                                'meaning':'These entities have both a related and an unrelated child record; DISTINCT on child-level absence does not mean entity-level absence.'})
                        except SQLExecutionError as exc:
                            result['counterexamples'].append({'basis':'probe_failed_not_evidence','error_code':exc.code})
    if len(json.dumps(result,ensure_ascii=False))>24000:
        return {'version':result['version'],'issues':[{'code':'mechanical_contract_budget_exceeded'}],
                'truncated':True,'original_issue_count':len(result['issues']),
                'boundary':'Mechanical contract exceeded24000 characters; no incomplete contract may authorize publication.'}
    return result
