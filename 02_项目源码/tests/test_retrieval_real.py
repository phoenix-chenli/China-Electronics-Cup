"""Real offline E5 weights; handcrafted retrieval fixtures are explicitly synthetic.

These tests verify semantic retrieval and source anchoring, not LLM answers.
"""
import math
from pathlib import Path

import pytest

from backend.retrieval import LocalE5, RetrievalService, ExactContextReranker

PASSAGES = [
    '员工提出辞职，应提前三十日以书面形式通知人事部门，并办妥工作交接。',
    '消费者网购商品自签收之日起七日内可以无理由退货，定制商品除外。',
    '贷款提前结清须提交书面申请，剩余本金一次归还，不收取违约金。',
    '门诊发生的医疗费用，在扣除起付线后按百分之七十报销。',
    '持居住证的随迁子女可以按实际居住地址申请公办小学入学。',
    '公园允许导盲犬随视障人士入内，其他宠物不得进入室内展馆。',
    '企业应在发现个人信息泄露后立即采取补救措施，并通知受影响的个人。',
    '项目结题后未使用的财政拨款，应当按照原渠道退回，不得挪作奖金。',
    '2023年甲项目营业收入为10万元，统计口径为全公司。',
    '2024年甲项目营业收入为10万元，统计口径仅含本部，不含分公司。',
    '表2 2024年工业投资额。工业固定资产投资为30.7亿元，同比下降7.2%。',
    '注：表2投资额统计范围为计划总投资500万元及以上项目，不含农户。',
    '已购买的高铁车票在列车开车前可以办理改签，具体差额按新票价结算。',
    '租赁合同届满且承租人无欠费和损坏时，出租人须在十个工作日内返还押金。',
    '发生地震时应优先远离玻璃和悬挂物，不要使用电梯。',
    '发票抬头填写单位名称，纳税人识别号必须与工商登记信息一致。',
    '服务器每日执行备份，备份副本异地保存三十天。',
    '水样总磷检测采用钼酸铵分光光度法，空白样与标准曲线应同步测定。',
    '活动门票实名制预约，同一证件每天最多预约一张。',
    '停车场对新能源汽车前两小时免费，超过部分按小时计费。',
] + [f'设备运维附录第{i}节：值班人员需检查管道阀门，并记录设备编号和交班时间。' for i in range(50)] + [
    '末章特别说明：研究数据可在项目结题后通过机构仓库申请获取，须提交用途声明并经过伦理审查。'
]

QUERIES = [
    {'id':'resignation','question':'不想在公司继续上班了，需要事先告知谁并完成什么手续？','gold':[1]},
    {'id':'return','question':'网上买的东西不喜欢，收货后一周内能退吗，有什么例外？','gold':[2]},
    {'id':'early_loan','question':'想把银行欠款一次还清，需要额外交罚款吗？','gold':[3]},
    {'id':'medical','question':'去医院看普通门诊花的钱，超过起付标准能报多少？','gold':[4]},
    {'id':'school','question':'外来务工家庭孩子如何按住址申请上小学？','gold':[5]},
    {'id':'guide_dog','question':'盲人带辅助犬进公园是否允许？','gold':[6]},
    {'id':'privacy','question':'公司客户资料外泄后要采取哪些动作？','gold':[7]},
    {'id':'remaining_grant','question':'科研任务结束还有没花完的政府补助，能用来发奖金吗？','gold':[8]},
    {'id':'same_value_period','question':'2024年甲项目营业收入是多少，统计是否含分公司？','gold':[10]},
    {'id':'footnote','question':'表2的2024年工业投资额与同比变化是多少，统计范围有什么限制？','gold':[11,12]},
    {'id':'deposit','question':'租房到期且没有欠款或损坏，保证金应在多久内退回？','gold':[14]},
    {'id':'tail','question':'科研结束后，怎样取得研究数据，需要办理哪些审核？','gold':[71]},
]


def benchmark_source():
    return {'id':'synthetic-retrieval-v1','project_id':'evaluation','name':'人工构造检索夹具.txt',
            'sha256':'c'*64,'parser_version':'fixture-v1','status':'ready','archived':False,
            'metadata':{},'warnings':['人工构造验收夹具，不是真实业务资料。'],
            'pages':[{'number':i+1,'text':text} for i,text in enumerate(PASSAGES)]}


@pytest.fixture(scope='module')
def real_model():
    directory=Path(__file__).resolve().parents[1]/'models'/'multilingual-e5-small'
    if not (directory/'onnx'/'model_quantized.onnx').is_file():
        pytest.skip('真实离线权重缺失；该跳过不得算真实模型通过')
    return LocalE5(directory)


def test_real_multilingual_vectors_have_dimension_normalization_and_semantic_signal(real_model):
    vectors=real_model.embed(['员工辞职需提前书面通知人事。','服务器备份副本存放异地。'])
    query=real_model.embed(['离开公司前找哪个部门办手续？'],kind='query')[0]
    assert len(query)==384
    assert abs(sum(v*v for v in query)-1)<1e-5
    assert sum(a*b for a,b in zip(query,vectors[0])) > sum(a*b for a,b in zip(query,vectors[1]))


def test_real_hybrid_retrieves_tail_and_period_preserves_exact_source(tmp_path,real_model):
    document=benchmark_source()
    handle=RetrievalService(tmp_path,real_model,reranker=ExactContextReranker()).prepare(document,'evaluation')
    for question,gold in [('科研结束后，怎样取得研究数据，需要办理哪些审核？',71),
                          ('2024年甲项目营业收入是多少，统计是否含分公司？',10)]:
        chunks,_=handle.select(question,limit=5,mode='rerank')
        assert gold in [c.page for c in chunks]
        assert all(document['pages'][c.page-1]['text'][c.start:c.end]==c.text for c in chunks)
        assert handle.audit()['searches'][-1]['vector_rows_scanned']==len(PASSAGES)


def test_real_long_passage_covers_overflow_windows_instead_of_truncating(real_model):
    # We inspect actual tokenizer overflow and invoke inference for the entire
    # passage. This establishes window coverage, not recall of every tail fact.
    real_model._load()
    text='设备运维检查记录。'*150+'员工辞职应提前通知人事。'
    encoded=real_model._tokenizer.encode('passage: '+text)
    assert encoded.overflowing
    vector=real_model.embed([text])[0]
    assert len(vector)==384 and all(math.isfinite(v) for v in vector)


def test_real_embedding_through_api_reuses_persisted_index_with_fake_answer_models(tmp_path,real_model):
    """Actual model/index/API, explicitly fake generation and independent verifier."""
    import uuid
    from fastapi.testclient import TestClient
    from backend.main import create_app
    from test_answers_api import configure,FakeGatewayFactory,project,upload,submit,wait_terminal
    runtime=tmp_path/'runtime';configure(runtime)
    gateway=FakeGatewayFactory()
    factory=lambda directory:RetrievalService(directory,real_model)
    app=create_app(data_dir=runtime,samples_dir=tmp_path/'samples',enable_workers=False,
                   answer_gateway_factory=gateway,retrieval_factory=factory)
    with TestClient(app) as client:
        pid=project(client)
        document=upload(client,pid,'facts.txt','2024年地区生产总值1312.9亿元，比上年增长6.5%。'.encode())
        body={'question':'地区生产总值是多少？','source_id':document['source']['id'],'mode':'document','request_id':str(uuid.uuid4())}
        first=wait_terminal(client,pid,submit(client,pid,body)['id'])
        assert first['status']=='completed',first
        audit=first['result']['retrieval_audit']
        assert audit['mode']=='hybrid' and audit['cache_hit'] is False
        assert audit['version']['embedding']['model']=='Xenova/multilingual-e5-small'
        assert audit['searches'][0]['vector_rows_scanned']>=1
    restarted=create_app(data_dir=runtime,samples_dir=tmp_path/'samples',enable_workers=False,
                         answer_gateway_factory=gateway,retrieval_factory=factory)
    with TestClient(restarted) as client:
        second=wait_terminal(client,pid,submit(client,pid,{**body,'request_id':str(uuid.uuid4())})['id'])
        assert second['status']=='completed',second
        assert second['result']['retrieval_audit']['cache_hit'] is True
        assert second['result']['retrieval_audit']['index_generation']==audit['index_generation']
        assert second['result']['evidence'][0]['quote']=='地区生产总值1312.9亿元'
