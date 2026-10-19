"""Limited SQL exports preserve their actual review scope, never promote trust."""
import copy
import pytest
from backend.brief_service import published_sql_snapshot,render_brief


def result():
    return {'verification':{'status':'limited','checks':['只读查询已执行']},
            'columns':['国家','合计'],'rows':[['甲','0'],['乙',None]],
            'query':{'sql':'SELECT country, total FROM totals','parameters':[],
                     'returned_rows':2,'elapsed_ms':2,'used_tables':['totals']},
            'claims':[{'id':'C1','text':'查询返回两行。','evidence_ids':['E1']}],
            'evidence':[{'id':'E1','kind':'table','table_name':'totals','source_name':'示例',
                         'sha256':'a'*64,'parser_version':'v1','stale':False}]}


def test_executed_sql_snapshot_preserves_limited_boundary():
    r=result();before=copy.deepcopy(r)
    assert published_sql_snapshot(r)
    text=render_brief({'id':'brief','title':'查询','created_at':'2026-10-18','answers':[
        {'id':'answer','question':'查询合计','updated_at':'2026-10-18','result':r}]}).decode()
    assert '不等同于人工业务核验' in text and '只读查询已执行' in text
    assert r==before and r['verification']['status']=='limited'


@pytest.mark.parametrize('mutation',[
    lambda r:r['query'].pop('returned_rows'),
    lambda r:r['query'].update(returned_rows=3),
    lambda r:r['query'].update(used_tables=['other']),
    lambda r:r['query'].pop('sql'),
    lambda r:r['query'].update(elapsed_ms=-1),
    lambda r:r['query'].update(returned_rows=True),
    lambda r:r['evidence'][0].update(stale=True),
    lambda r:r['claims'][0].update(evidence_ids=['absent']),
    lambda r:r['rows'].append(['wrong-width']),
    lambda r:r['verification'].update(status='failed'),
])
def test_candidates_malformed_or_unbound_snapshots_cannot_be_exported(mutation):
    r=result();mutation(r);assert not published_sql_snapshot(r)
