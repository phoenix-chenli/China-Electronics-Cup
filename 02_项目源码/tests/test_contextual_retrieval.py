import copy
from backend.contextual_retrieval import contextual_search_query
from backend.retrieval import RetrievalService


def test_raw_inputs_preserved_and_duplicate_not_added():
    result = contextual_search_query('原问题', '查找这一项', '补足单位')
    assert result['original_question'] == '原问题'
    assert result['action_query'] == '查找这一项'
    assert result['completion_focus'] == '补足单位'
    assert result['effective_query'] == '查找这一项\n原问题\n补足单位'
    assert contextual_search_query('原问题', '原问题')['effective_query'] == '原问题'


def test_subquery_target_survives_multi_object_original(tmp_path):
    source = {'id':'s', 'project_id':'p', 'name':'运行案例.pdf', 'sha256':'a'*64,
              'parser_version':'controlled', 'status':'ready', 'metadata':{},
              'pages':[{'number':1,'text':'East River Works pump inspection interval is monthly.'},
                       {'number':2,'text':'West River Works pump inspection interval is weekly.'},
                       {'number':3,'text':'Unrelated South River Works pressure observations.'}]}
    before = copy.deepcopy(source)
    h = RetrievalService(tmp_path,default_mode='lexical').prepare(source,'p')
    original='比较East River Works与West River Works的pump inspection interval。'
    for action, expected in [('West River Works pump inspection interval',2), ('East River Works pump inspection interval',1)]:
        value=contextual_search_query(original,action)
        selected,_=h.select(value['effective_query'],limit=1)
        assert [c.page for c in selected] == [expected]
    assert source == before


def test_explicit_page_scope_cannot_be_expanded(tmp_path):
    source = {'id':'s', 'project_id':'p', 'name':'运行案例.pdf', 'sha256':'b'*64,
              'parser_version':'controlled', 'status':'ready', 'metadata':{},
              'pages':[{'number':1,'text':'East River Works pump inspection interval is monthly.'},
                       {'number':2,'text':'West River Works pump inspection interval is weekly.'}]}
    h=RetrievalService(tmp_path,default_mode='lexical').prepare(source,'p')
    value=contextual_search_query('比较East River Works与West River Works的运行。','West River Works pump inspection interval')
    selected,_=h.select(value['effective_query'],limit=2,pages=[1])
    assert [c.page for c in selected] == [1]
