"""Conservative roles for already-published values, not new business inference."""
import math
import re
import sqlglot
from sqlglot import exp


def identifier_label(value):
    return bool(re.search(r'(?:^id$|_id$|Id$|ID$|编号|编码|标识|序号|代码|邮编|电话|日期|时间|姓名|名称)',str(value)))


def grounded_numeric_metric(metric,claims):
    if not isinstance(metric,str) or identifier_label(metric):return False
    pattern=re.escape(metric)+r'(?:为|是|[:：]|约|等于|达到|共|总计|\s){0,8}[-+]?\d'
    return any(re.search(pattern,c['text']) for c in claims)


def measure_indexes(answer,capabilities,trusted_slots):
    """Only numeric result columns with a checked metric role can become axes.

    A physical PK/FK or identifier remains an identifier under an alias/cast.
    COUNT(identifier) is a count measure. Existing SQL aggregate semantics were
    reviewed by the answer engine; this helper never proposes joins/dimensions.
    Ambiguous AST/source mapping stays unknown. Non-SQL result tables require
    an exact independently-reviewed metric label, not numeric-looking IDs.
    """
    result=answer.get('result') or {};columns=result.get('columns');rows=result.get('rows')
    if not isinstance(columns,list) or not columns or len(columns)>64 or not isinstance(rows,list) or not rows:return []
    if any(not isinstance(r,list) or len(r)!=len(columns) for r in rows):return []
    numeric=[]
    for index in range(len(columns)):
        values=[r[index] for r in rows if r[index] is not None]
        if values and all(type(v) is int or type(v) is float and math.isfinite(v)
                          or isinstance(v,str) and re.fullmatch(r'-?\d+(?:\.\d+)?',v) for v in values):numeric.append(index)
    query=result.get('query') or {};sql=query.get('sql')
    metric=trusted_slots.get('metric',{}).get('value')
    if not sql:
        return [i for i in numeric if columns[i]==metric and not identifier_label(columns[i])]
    try:
        tree=sqlglot.parse_one(sql,read='sqlite')
        if not isinstance(tree,exp.Select) or len(tree.expressions)!=len(columns):return []
    except (sqlglot.errors.ParseError,TypeError,ValueError):return []
    scopes=answer.get('source_revisions') or answer.get('sources') or []
    catalogs=[]
    for catalog in capabilities.get('catalogs',[])[:16]:
        if not isinstance(catalog,dict):continue
        recorded=catalog.get('source_snapshot')
        if isinstance(recorded,dict) and any(isinstance(s,dict) and all(recorded.get(k)==s.get(k)
            for k in ('id','sha256','parser_version','revision','semantic_revision')) for s in scopes):catalogs.append(catalog)
    tables={t['name']:t for c in catalogs for t in c.get('tables',[]) if isinstance(t,dict) and isinstance(t.get('name'),str)}
    direct=[tree.args['from_'].this] if tree.args.get('from_') else []
    direct += [j.this for j in tree.args.get('joins',[])]
    aliases={t.alias_or_name:t.name for t in direct if isinstance(t,exp.Table)}
    foreign_keys={(r.get('table'),name) for c in catalogs for r in c.get('relationships',[])
                  if isinstance(r,dict) for name in r.get('columns',[]) if isinstance(name,str)}
    def field(column):
        names=[aliases[column.table]] if column.table in aliases else list(aliases.values()) if not column.table else []
        matches=[(name,c) for name in names for c in tables.get(name,{}).get('columns',[]) if c.get('name')==column.name]
        return matches[0] if len(matches)==1 else None
    accepted=[]
    for index in numeric:
        expression=tree.expressions[index]
        if isinstance(expression,exp.Alias):expression=expression.this
        refs=list(expression.find_all(exp.Column));counts=list(expression.find_all(exp.Count))
        computed=bool(list(expression.find_all(exp.AggFunc)) or list(expression.find_all(exp.Cast))
                      or any(f.name.upper() in {'DECIMAL_SUM','DECIMAL_AVG'} for f in expression.find_all(exp.Anonymous)))
        if not refs and not counts:continue
        valid=True
        for ref in refs:
            if ref.find_ancestor(exp.Count) is not None:continue
            match=field(ref)
            if not match:valid=False;break
            table,column=match
            if (identifier_label(column['name']) or column.get('primary_key_position')
                    or column['name'] in tables[table].get('primary_key',[])
                    or (table,column['name']) in foreign_keys
                    or any(k in str(column.get('type','')).upper() for k in ('DATE','TIME','BOOL'))):valid=False;break
            if not computed and not any(k in str(column.get('type','')).upper() for k in ('INT','REAL','DOUBLE','FLOAT','NUMERIC','DECIMAL')):
                valid=False;break
        if valid and (counts or not identifier_label(columns[index])):accepted.append(index)
    return accepted
