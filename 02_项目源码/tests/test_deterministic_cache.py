import copy,hashlib,sqlite3,threading
from pathlib import Path
import pytest
from backend.deterministic_cache import DeterministicCache, mechanical_call, _ACTIVE
from backend.sql_engine import execute_sql, SQLExecutionError
from backend.formula_engine import calculate_formula, FormulaError
@pytest.fixture
def source(tmp_path):
    original=tmp_path/'original.txt';original.write_text('甲12元，乙8元。公式 a+b。');db=tmp_path/'source.sqlite'
    with sqlite3.connect(db) as c:c.execute('CREATE TABLE t (amount INTEGER)');c.executemany('INSERT INTO t VALUES (?)',[(12,),(8,)])
    return {'id':'s','project_id':'p','name':'源','sha256':hashlib.sha256(original.read_bytes()).hexdigest(),'parser_version':'v1','original_path':str(original),'data_path':str(db),'pages':[],'tables':[{'name':'t','columns':[{'name':'amount','type':'INTEGER'}]}],'metadata':{},'semantic_revision':'sem1'}
def query(source):return execute_sql(source,'SELECT SUM(amount) AS total FROM t')
def test_hit_deepcopy_checks_current_and_bytes(source,monkeypatch):
    from backend import sql_engine
    cache=DeterministicCache();guards=[];calls=[];original=sql_engine._execute_sql_uncached
    monkeypatch.setattr(sql_engine,'_execute_sql_uncached',lambda *a,**k:(calls.append(1),original(*a,**k))[1])
    with cache.scope('p',[source],is_current=lambda:guards.append(1) or True):
        first=query(source);first['rows'][0][0]=999;second=query(source)
    assert len(calls)==1 and len(guards)==4 and second['rows']==[[20]]
    assert second['cache']['status']=='hit' and second['cache']['fact_verification_cached'] is False and _ACTIVE.get() is None
@pytest.mark.parametrize('change',['project','parser','semantic','metadata','sql','parameters','strategy'])
def test_key_separates_all_scopes(source,monkeypatch,change):
    from backend import sql_engine
    cache=DeterministicCache()
    with cache.scope('p',[source],is_current=lambda:True):assert query(source)['cache']['status']=='miss_stored'
    s=copy.deepcopy(source);p='p';sql='SELECT SUM(amount) AS total FROM t';params=[]
    if change=='project':p='q';s['project_id']=p
    elif change=='parser':s['parser_version']='v2'
    elif change=='semantic':s['semantic_revision']='sem2'
    elif change=='metadata':s['metadata']['table_semantics']=[]
    elif change=='sql':sql='SELECT MAX(amount) FROM t'
    elif change=='parameters':sql='SELECT SUM(amount) FROM t WHERE amount>?';params=[8]
    elif change=='strategy':monkeypatch.setattr(sql_engine,'MAX_RESULTS',199)
    with cache.scope(p,[s],is_current=lambda:True):assert execute_sql(s,sql,params)['cache']['status']=='miss_stored'
def test_data_change_evicts_old(source):
    cache=DeterministicCache()
    with cache.scope('p',[source],is_current=lambda:True):assert query(source)['rows']==[[20]]
    with sqlite3.connect(source['data_path']) as c:c.execute('UPDATE t SET amount=30')
    with cache.scope('p',[source],is_current=lambda:True):
        r=query(source);assert r['rows']==[[60]] and r['cache']['status']=='miss_stored'
    assert len(cache.entries)==1
def test_original_change_blocks_even_unchanged_metadata(source):
    cache=DeterministicCache()
    with cache.scope('p',[source],is_current=lambda:True):query(source)
    Path(source['original_path']).write_text('changed')
    with cache.scope('p',[source],is_current=lambda:True):
        with pytest.raises(ValueError,match='原件摘要'):query(source)
    assert not cache.entries
def test_stale_cancel_and_invalidate(source):
    cache=DeterministicCache()
    with cache.scope('p',[source],is_current=lambda:True):query(source)
    with cache.scope('p',[source],is_current=lambda:False):
        with pytest.raises(ValueError,match='版本'):query(source)
    with cache.scope('p',[source],cancelled=lambda:True,is_current=lambda:True):
        with pytest.raises(InterruptedError):query(source)
    assert _ACTIVE.get() is None;cache.invalidate_source('p','s');assert not cache.entries
@pytest.mark.parametrize('sql',["SELECT date('now')","SELECT datetime('now')","SELECT strftime('%s','now')",'SELECT current_timestamp'])
def test_clocks_bypass(source,sql):
    cache=DeterministicCache()
    with cache.scope('p',[source],is_current=lambda:True):
        assert 'cache' not in execute_sql(source,sql);assert 'cache' not in execute_sql(source,sql)
    assert not cache.entries
def test_errors_random_no_store(source):
    cache=DeterministicCache()
    with cache.scope('p',[source],is_current=lambda:True):
        for sql in ['SELECT absent FROM t','SELECT random()']:
            with pytest.raises(SQLExecutionError):execute_sql(source,sql)
    assert not cache.entries
def test_missing_guard_wal_bypass(source):
    cache=DeterministicCache()
    with cache.scope('p',[source]):assert 'cache' not in query(source)
    Path(source['data_path']+'-wal').touch()
    with cache.scope('p',[source],is_current=lambda:True):assert 'cache' not in query(source)
    assert not cache.entries
def formula_inputs(source):
    e={'E1':{'id':'E1','kind':'text','source_id':'s','sha256':source['sha256'],'parser_version':'v1','quote':'甲12元，乙8元。公式 a+b。'}}
    b={'a':{'evidence_id':'E1','quote':'12元','unit':'元'},'b':{'evidence_id':'E1','quote':'8元','unit':'元'}}
    return e,b
def test_formula_proof_reexecutes_only_arithmetic_cached(source,monkeypatch):
    from backend import formula_engine
    e,b=formula_inputs(source);cache=DeterministicCache();proofs=[];math=[];resolve=formula_engine._resolve_binding;evaluate=formula_engine._evaluate_resolved
    monkeypatch.setattr(formula_engine,'_resolve_binding',lambda *a,**k:(proofs.append(1),resolve(*a,**k))[1])
    monkeypatch.setattr(formula_engine,'_evaluate_resolved',lambda *a,**k:(math.append(1),evaluate(*a,**k))[1])
    with cache.scope('p',[source],is_current=lambda:True):
        r=calculate_formula('a+b',b,e,'元',user_formula='a+b');r['parameters'][0]['value']='999'
        r=calculate_formula('a+b',b,e,'元',user_formula='a+b')
        assert r['value']=='20' and r['cache']['status']=='hit' and r['parameters'][0]['value']!='999'
        assert len(proofs)==4 and len(math)==1
        bad=copy.deepcopy(b);bad['a']['quote']='13元'
        with pytest.raises(FormulaError):calculate_formula('a+b',bad,e,'元',user_formula='a+b')
        with pytest.raises(FormulaError):calculate_formula('a+b',b,e,'kg',user_formula='a+b')
    assert len(cache.entries)==1
def test_formula_unit_precision_key(source):
    e,b=formula_inputs(source);cache=DeterministicCache()
    with cache.scope('p',[source],is_current=lambda:True):
        a=calculate_formula('a+b',b,e,'元',user_formula='a+b');z=calculate_formula('a+b',b,e,'万元',precision=4,user_formula='a+b')
        assert a['value']=='20' and z['value']=='0.002' and a['cache']['status']==z['cache']['status']=='miss_stored'
    assert len(cache.entries)==2
def test_lru_and_finally_reset(source):
    cache=DeterministicCache(max_entries=1,max_bytes=1000)
    with pytest.raises(RuntimeError):
        with cache.scope('p',[source],is_current=lambda:True):
            mechanical_call('fixture',{'n':1},lambda:{'rows':[1]});mechanical_call('fixture',{'n':2},lambda:{'rows':[2]});assert len(cache.entries)==1
            raise RuntimeError('stop')
    assert _ACTIVE.get() is None
def test_parallel_projects_context_isolated(source):
    cache=DeterministicCache();barrier=threading.Barrier(2);results=[]
    def worker(p):
        with cache.scope(p,[{**source,'project_id':p}],is_current=lambda:True):
            barrier.wait();results.append(mechanical_call('fixture',{},lambda:p)[0])
        assert _ACTIVE.get() is None
    ts=[threading.Thread(target=worker,args=(p,)) for p in ['p','q']]
    for t in ts:t.start()
    for t in ts:t.join()
    assert set(results)=={'p','q'} and len(cache.entries)==2

def test_mutation_during_compute_blocks_publish_and_does_not_store(source):
    cache=DeterministicCache()
    def mutate():
        with sqlite3.connect(source['data_path']) as c:c.execute('UPDATE t SET amount=99')
        return {'value':20}
    with cache.scope('p',[source],is_current=lambda:True):
        with pytest.raises(ValueError,match='执行期间'):
            mechanical_call('fixture',{},mutate)
    assert not cache.entries

def test_cancel_after_arithmetic_never_stores_result(source):
    cache=DeterministicCache();cancelled=[False]
    def compute():cancelled[0]=True;return 20
    with cache.scope('p',[source],cancelled=lambda:cancelled[0],is_current=lambda:True):
        with pytest.raises(InterruptedError):mechanical_call('fixture',{},compute)
    assert not cache.entries

def test_same_sql_different_parameter_values_miss(source):
    cache=DeterministicCache();sql='SELECT SUM(amount) FROM t WHERE amount>?'
    with cache.scope('p',[source],is_current=lambda:True):
        a=execute_sql(source,sql,[0]);b=execute_sql(source,sql,[8]);c=execute_sql(source,sql,[0])
        assert a['rows']==[[20]] and b['rows']==[[12]] and c['rows']==[[20]]
        assert [r['cache']['status'] for r in (a,b,c)]==['miss_stored','miss_stored','hit']
