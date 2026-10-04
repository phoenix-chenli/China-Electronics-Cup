"""Local settings/help and RAM-only temporary conversations. No external clients."""
from __future__ import annotations
import copy
from decimal import Decimal
import json
import secrets
import threading
import time
from typing import Literal
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from . import store as s, engine

router = APIRouter(prefix='/api')

def initialize():
    with s.conn() as c:
        c.execute('CREATE TABLE IF NOT EXISTS ef_settings(key TEXT PRIMARY KEY,body TEXT NOT NULL)')
        c.execute('INSERT OR IGNORE INTO ef_settings VALUES (?,?)', ('privacy', json.dumps({'local_only': True})))

def require_local():
    from .connections import local_only
    if local_only():raise HTTPException(403, '仅本机处理已开启')

@router.get('/settings')
def settings():
    from . import connections
    caps=connections.capabilities()
    return {'local_only': connections.local_only(), 'can_disable_local_only': True,
            'external_services': [{'id':x['id'],'name':x['name'],'enabled':x['enabled']} for x in connections.listing() if not connections.is_local(x)],
            'text_model': caps['text'], 'vision_model': caps['vision'], 'ocr': False,
            'storage': {'database': str(s.APP_DB), 'documents': str(s.DOCS), 'uploads': str(s.UPLOADS)},
            'temporary': {'persistent': False, 'timeout_seconds': 1800, 'uploads': False},
            'boundary': '临时问答仅查询现有数据，不调用外部服务、不保存分析；关闭即丢弃，意外断连最多保留 30 分钟内存。'}

class Privacy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    local_only: bool

@router.patch('/settings/privacy')
def privacy(req: Privacy):
    with s.conn() as db:db.execute('UPDATE ef_settings SET body=? WHERE key=?',(json.dumps({'local_only':req.local_only}),'privacy'))
    return settings()

class HelpRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str = Field(min_length=1, max_length=2000)
    tone: Literal['concise', 'friendly'] = 'concise'
    page: str = ''
    project_id: str = 'default'
    session_id: str | None = None
    run_id: str | None = None
    claim_id: str | None = None
    evidence_id: str | None = None
    scenario_id: str | None = None
    preferences: dict = Field(default_factory=dict)

@router.post('/assistant/help')
def help_answer(req: HelpRequest):
    result = None
    with s.conn() as c:
        if not c.execute('SELECT 1 FROM ef_projects WHERE id=?',(req.project_id,)).fetchone(): raise HTTPException(404,'项目不存在')
        if req.session_id:
            if not c.execute('SELECT 1 FROM ef_sessions WHERE id=? AND project_id=?',(req.session_id,req.project_id)).fetchone(): raise ValueError('会话不属于当前项目')
            if req.run_id:
                matches = [json.loads(r[0]) for r in c.execute('SELECT result_json FROM ef_messages WHERE session_id=? AND result_json IS NOT NULL',(req.session_id,))]
                result = next((r for r in matches if r['run_id']==req.run_id), None)
                if not result: raise ValueError('结果不属于当前会话')
    if result and req.evidence_id and not any(e['id']==req.evidence_id for e in result['evidence']): raise ValueError('来源不属于当前结果')
    q=req.question.strip(); action=None; state='read'; kind='help'; evidence=[]; text=''
    if req.scenario_id and any(x in q for x in ['情景','参数','奖金','变化','这个结果']):
        from .scenarios import body
        calculation=body('ef_scenario_calculations',req.scenario_id)
        baseline=body('ef_scenario_baselines',calculation['baseline_id'])
        if baseline['project_id']!=req.project_id or (req.session_id and baseline['session_id']!=req.session_id): raise ValueError('情景不属于当前会话')
        lines=[]
        for parameter in calculation['parameters']:
            value=Decimal(parameter['value'])
            label=(f'{value * 100:,.4f}'.rstrip('0').rstrip('.')+'%') if parameter['unit']=='ratio' else f'{value:,.2f} 元'
            lines.append(parameter['label']+'：'+label+('（用户假设）' if parameter['assumption'] else ''))
        text='\n'.join(lines)+f"\n基准奖金：{Decimal(calculation['baseline_value']):,.2f} 元；情景奖金：{Decimal(calculation['value']):,.2f} 元；变化：{Decimal(calculation['delta']):+,.2f} 元。"
        text+='\n'+calculation['contribution_note']+'这不是独立因果贡献。'
        if baseline['data_version']!=s.data_version():text+='\n来源数据已变化；以上为原方案快照，请刷新后再使用。'
        source_ids={eid for p in calculation['parameters'] for eid in p.get('evidence_ids',[])}
        evidence=[e for e in calculation['evidence'] if e['id'] in source_ids];kind='context';action='scenario';state='explain_left'
    elif result and any(x in q for x in ['这条结论','解释结果','当前结果','这个结果','为什么','这些来源']):
        claim=next((c for c in result['claims'] if c['id']==req.claim_id),None) if req.claim_id else next((c for c in result['claims'] if not req.evidence_id or req.evidence_id in c['evidence_ids']),None)
        if req.claim_id and not claim: raise ValueError('结论不属于当前结果')
        if claim:
            evidence=[e for e in result['evidence'] if e['id'] in claim['evidence_ids']]
            text=claim['text']+'\n'+('可打开这条结论绑定的来源。' if evidence else '这条结论没有直接来源，请先补充依据。')
            state='explain_right' if evidence else 'clarify';kind='context'
        else:text='当前结果还没有结论。';state='clarify'
    elif any(x in q for x in ['来源','证据']):
        text='点击结论后的来源编号，可查看同版本文档、原文位置或实际 SQL。精简只收起片段，详细会显示版本与参数，来源数量不变。';action='evidence';state='explain_right'
    elif any(x in q for x in ['导入','上传','文件格式']):
        text='在“数据”选择“导入”。支持 CSV、Excel、SQLite、PDF、文本和图片；扫描件与图片只保留原件，文字识别未配置。';action='data'
    elif any(x in q for x in ['临时','隐私','本机','外发']):
        text='普通分析保存在本机 SQLite，文件保存在本机目录。临时问答只使用内存，不写入历史；关闭或刷新会退出。外部调用由“仅本机处理”开关控制；临时问答始终只在本机查询。';action='settings'
    elif any(x in q for x in ['模型','联网','能做什么']):
        text='主问答可在模型连接页选择自动、本地或指定模型。已测试并启用的文本模型可规划只读业务查询、文档检索、计算和来源核验；模型补充与联网检索仍可单独预览调用。OCR 仍未启用，临时问答只在本机处理。';action='settings'
    elif any(x in q for x in ['主题','深色','字号','字体','密度','动画','配置','设置']):
        p=req.preferences
        text='当前主题：'+{'light':'浅色','dark':'深色','system':'跟随系统'}.get(p.get('theme'),'浅色')+'；字号：'+str(p.get('fontSize',14))+'px。修改外观立即生效。关闭动画仍可提问；系统减少动态效果优先。';action='settings'
    elif any(x in q for x in ['你好','您好','在吗']):text='你好，我是证流。';state='welcome'
    else:
        return {'kind':'query','text':'可在当前分析中查询。','state':'think','context':{'project_id':req.project_id,'session_id':req.session_id}}
    if req.tone=='friendly':text='我来帮你看看。'+text
    return {'kind':kind,'text':text,'state':state,'action':action,'evidence':evidence,
            'context':{'project_id':req.project_id,'session_id':req.session_id,'run_id':req.run_id,'claim_id':req.claim_id,'evidence_id':req.evidence_id,'scenario_id':req.scenario_id},'mode':'local-help'}

class TemporaryRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    question: str=Field(min_length=1,max_length=2000)
    confirmed: dict=Field(default_factory=dict)
    inherit_context: bool=True

class TemporaryStore:
    def __init__(self): self.sessions={}; self.lock=threading.RLock()
    def cleanup(self):
        for token,item in list(self.sessions.items()):
            if item['expires']<time.monotonic(): self.close(token)
    def create(self):
        with self.lock:
            self.cleanup()
            if len(self.sessions)>=32: raise HTTPException(429,'临时会话已满，请关闭未使用的窗口')
            token=secrets.token_urlsafe(32)
            self.sessions[token]={'previous':{},'stop':threading.Event(),'busy':False,'expires':time.monotonic()+1800}
            timer=threading.Timer(1800,self.close,args=(token,));timer.daemon=True;self.sessions[token]['timer']=timer;timer.start()
            return token
    def close(self,token):
        with self.lock:
            item=self.sessions.pop(token,None)
            if item:
                item['stop'].set();item['timer'].cancel()
    def query(self,token,req):
        with self.lock:
            self.cleanup();item=self.sessions.get(token)
            if not item:raise HTTPException(410,'临时会话已结束，请重新打开')
            if item['busy']:raise HTTPException(409,'当前临时查询尚未完成')
            item['busy']=True
            previous=copy.deepcopy(item['previous']) if req.inherit_context else {}
        try:
            with s.query_cancellation(item['stop']):
                result=engine.execute(req.question,previous,req.confirmed,cancelled=item['stop'],persist=False)
            with self.lock:
                if token not in self.sessions:raise HTTPException(410,'临时会话已结束')
                item['previous']=result['query_spec']
            # These results intentionally have no persistent run / save action.
            result['actions']=[];result['temporary']=True
            return result
        except InterruptedError:raise HTTPException(410,'临时会话已结束') from None
        finally:item['busy']=False

temporary=TemporaryStore()
@router.post('/temporary/sessions')
def temporary_start(response:Response):
    response.headers['Cache-Control']='no-store'
    return {'id':temporary.create(),'persistent':False}
@router.post('/temporary/{token}/query')
def temporary_query(token:str,req:TemporaryRequest,response:Response):
    response.headers['Cache-Control']='no-store'
    return temporary.query(token,req)
@router.post('/temporary/{token}/close')
def temporary_close(token:str):temporary.close(token);return {'ok':True}

@router.get('/definitions/archived')
def archived_definitions():
    with s.conn() as c:return [json.loads(r['body'])|{'id':r['id']} for r in c.execute('SELECT * FROM definitions WHERE deleted=1')]
@router.post('/definitions/{did}/restore')
def restore_definition(did:str):
    with s.conn() as c:row=c.execute('SELECT body FROM definitions WHERE id=? AND deleted=1',(did,)).fetchone()
    if not row:raise HTTPException(404,'回收记录不存在')
    return s.save_definition(json.loads(row[0]))
