"""Explicit, bounded transports. Secrets never leave the server configuration API."""
from __future__ import annotations
import base64, hashlib, io, json, math, os, re, secrets, sqlite3, threading, time, uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, quote
import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from . import store as s

router=APIRouter(prefix='/api/connections')
lock=threading.RLock()
_model_pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='evidenceflow-model')
_model_slots=threading.BoundedSemaphore(4)
_provider_rate_lock=threading.Lock()
_provider_next_request={}
KINDS={'sqlite','postgresql','mysql','text','vision','embedding','rerank','brave','wikipedia'}
DB_KINDS={'sqlite','postgresql','mysql'}

def initialize():
    with s.conn() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS ef_connections(id TEXT PRIMARY KEY,body TEXT NOT NULL,secret TEXT);
        CREATE TABLE IF NOT EXISTS ef_connection_events(id TEXT PRIMARY KEY,connection_id TEXT,body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ef_import_provenance(table_name TEXT PRIMARY KEY,body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ef_extensions(id TEXT PRIMARY KEY,body TEXT NOT NULL);
        ''')
        for row in c.execute('SELECT id,body FROM ef_extensions').fetchall():
            item=json.loads(row['body'])
            if item.get('status') in {'running','prepared'}:
                item.pop('messages',None)
                item.update(status='failed',error='服务已重启，请重新预览后发送',completed_at=s.now())
                c.execute('UPDATE ef_extensions SET body=? WHERE id=?',(json.dumps(item,ensure_ascii=False),row['id']))

def encode(v):return json.dumps(v,ensure_ascii=False,default=str)
def local_host(host):return host in {'localhost','127.0.0.1','::1'}
def is_local(config):return config['kind']=='sqlite' or (config['kind'] in DB_KINDS and local_host(config.get('host'))) or (config['kind'] in {'text','vision','embedding','rerank'} and local_host(urlsplit(config.get('endpoint','')).hostname))
def local_only():
    with s.conn() as c:
        row=c.execute("SELECT body FROM ef_settings WHERE key='privacy'").fetchone()
    return not row or json.loads(row[0]).get('local_only',True)
def gate(config):
    if not is_local(config) and local_only():raise HTTPException(403,'仅本机处理已开启；如需此连接，请在数据与隐私中允许外部连接')

def cipher():
    from .vault import load_key
    with lock:
        return Fernet(load_key(s.DATA/'.private', Fernet.generate_key))
def get(cid,secret=False):
    with s.conn() as c:row=c.execute('SELECT * FROM ef_connections WHERE id=?',(cid,)).fetchone()
    if not row:raise HTTPException(404,'连接不存在')
    d=json.loads(row['body']);d['has_secret']=bool(row['secret'])
    if secret:
        try:d['_secret']=cipher().decrypt(row['secret'].encode()).decode() if row['secret'] else ''
        except InvalidToken:raise ValueError('凭据无法解密，请重新填写') from None
    return d

def listing():
    with s.conn() as c:rows=c.execute('SELECT body,secret FROM ef_connections ORDER BY rowid').fetchall()
    return [json.loads(r['body'])|{'has_secret':bool(r['secret'])} for r in rows]

def capabilities():
    configs=listing();policy=local_only()
    return {k:any(d['kind']==k and d.get('enabled') and (d.get('last_test') or {}).get('ok') and (not policy or is_local(d)) for d in configs) for k in ['text','vision','embedding','rerank','brave','wikipedia']}

class Strict(BaseModel):model_config=ConfigDict(extra='forbid')
class Config(Strict):
    name:str=Field(min_length=1,max_length=80)
    kind:Literal['sqlite','postgresql','mysql','text','vision','embedding','rerank','brave','wikipedia']
    endpoint:str=Field(default='',max_length=500)
    model:str=Field(default='',max_length=150)
    path:str=Field(default='',max_length=2000)
    host:str=Field(default='',max_length=253)
    port:int=Field(default=5432,ge=1,le=65535)
    database:str=Field(default='',max_length=128)
    username:str=Field(default='',max_length=128)
    secret:str|None=Field(default=None,max_length=4096)
    enabled:bool=False
    timeout:int=Field(default=60,ge=3,le=300)
    expected_revision:int|None=None

def validate(d):
    d['name']=d['name'].strip()
    if not d['name']:raise ValueError('请填写连接名称')
    kind=d['kind']
    if kind in {'text','vision','embedding','rerank'}:
        d['endpoint']=d['endpoint'].rstrip('/');u=urlsplit(d['endpoint'])
        if not u.hostname or u.username or u.password or u.query or u.fragment:raise ValueError('地址不能含凭据、查询参数或片段')
        if u.scheme!='https' and not(u.scheme=='http' and local_host(u.hostname)):raise ValueError('远程模型必须使用 HTTPS；HTTP 仅允许本机')
        if not d['model'].strip():raise ValueError('请填写服务实际提供的模型名称')
    if kind=='sqlite':
        p=Path(d['path']).expanduser()
        if not p.is_absolute() or not p.is_file() or p.suffix.lower() not in {'.db','.sqlite','.sqlite3'}:raise ValueError('请选择存在的 SQLite 文件绝对路径')
        d['path']=str(p.resolve())
    if kind in {'mysql','postgresql'}:
        if not re.fullmatch(r'[A-Za-z0-9.:-]+',d['host']) or not d['database'] or not d['username']:raise ValueError('请填写主机、数据库和只读账户')
    # Remove unrelated fields, preventing stray credentials in unused inputs.
    allowed={'name','kind','enabled','timeout'}|({'endpoint','model'} if kind in {'text','vision','embedding','rerank'} else {'path'} if kind=='sqlite' else {'host','port','database','username'} if kind in DB_KINDS else set())
    return {k:v for k,v in d.items() if k in allowed}

@router.get('')
def list_connections():return {'items':listing(),'local_only':local_only(),'capabilities':capabilities(),'secret_storage':'Windows 当前用户 DPAPI 保护本机密钥；更换电脑或账户后需重新配置' if os.name=='nt' else '服务端加密；本机独立密钥文件，仅当前用户可读'}
@router.post('')
def create(req:Config):return save(str(uuid.uuid4()),req,False)
@router.put('/{cid}')
def update(cid:str,req:Config):return save(cid,req,True)
def save(cid,req,existing):
    with lock:
        old=get(cid) if existing else None
        if old and req.expected_revision!=old['revision']:raise HTTPException(409,'连接已在别处修改，请重新打开')
        config=validate(req.model_dump())
        if old and old['kind']!=config['kind']:raise ValueError('连接类型不可修改，请新建连接')
        if old and old['has_secret'] and req.secret is None and any(old.get(k)!=config.get(k) for k in ['endpoint','host','port','username']):raise ValueError('服务地址或账户已改变，请重新填写或清除凭据')
        changes=not old or any(old.get(k)!=v for k,v in config.items() if k not in {'name','enabled'}) or req.secret is not None
        last_test=None if changes else old.get('last_test')
        if config['enabled'] and (not last_test or not last_test['ok']):raise ValueError('请先保存并测试连接，再启用')
        if config['enabled']:gate(config)
        config.update(id=cid,revision=(old['revision']+1 if old else 1),created_at=old['created_at'] if old else s.now(),updated_at=s.now(),last_test=last_test)
        encrypted=cipher().encrypt(req.secret.encode()).decode() if req.secret else None
        with s.conn() as c:
            if existing and req.secret is None:encrypted=c.execute('SELECT secret FROM ef_connections WHERE id=?',(cid,)).fetchone()[0]
            c.execute('INSERT OR REPLACE INTO ef_connections VALUES (?,?,?)',(cid,encode(config),encrypted))
    return get(cid)
@router.delete('/{cid}')
def delete(cid:str):
    get(cid)
    with s.conn() as c:c.execute('DELETE FROM ef_connections WHERE id=?',(cid,))
    return {'ok':True}

class Enabled(Strict):enabled:bool
@router.patch('/{cid}/enabled')
def enabled(cid:str,req:Enabled):
    with lock:
        d=get(cid)
        if req.enabled:
            gate(d)
            if not d.get('last_test',{} ) or not d['last_test']['ok']:raise ValueError('最近一次连接测试尚未通过')
        d.pop('has_secret',None);d.update(enabled=req.enabled,revision=d['revision']+1,updated_at=s.now())
        with s.conn() as c:c.execute('UPDATE ef_connections SET body=? WHERE id=?',(encode(d),cid))
    return get(cid)

def event(d,action,ok,detail,elapsed):
    body={'id':str(uuid.uuid4()),'connection_id':d['id'],'connection_name':d['name'],'revision':d['revision'],'action':action,'ok':ok,'detail':detail,'duration_ms':round(elapsed*1000),'time':s.now()}
    with s.conn() as c:c.execute('INSERT INTO ef_connection_events VALUES (?,?,?)',(body['id'],d['id'],encode(body)))
    return body
@router.get('/{cid}/events')
def history(cid:str):
    get(cid)
    with s.conn() as c:return [json.loads(r[0]) for r in c.execute('SELECT body FROM ef_connection_events WHERE connection_id=? ORDER BY rowid DESC LIMIT 30',(cid,))]

def request_json(d,url,body=None,headers=None,cancelled=None):
    """One bounded submission; only connection setup can be retried automatically."""
    gate(d)
    if d.get('_required_kind'):
        current=ready(d['id'],{d['_required_kind']})
        if current['revision']!=d['revision']:raise ValueError('连接已改变，请重新准备请求')
    deadline=time.monotonic()+d.get('timeout',60)
    while True:
        if cancelled is not None and cancelled.is_set():raise InterruptedError('查询已取消')
        remaining=deadline-time.monotonic()
        if remaining<=0:raise ValueError('模型请求繁忙，请稍后重试')
        if _model_slots.acquire(timeout=min(.1,remaining)):break
    if cancelled is not None and cancelled.is_set():
        _model_slots.release();raise InterruptedError('查询已取消')
    try:
        future=_model_pool.submit(_request_json_transport,d,url,body,headers,deadline,cancelled)
    except BaseException:
        _model_slots.release();raise
    # Keep the slot until the actual HTTP call ends, including after cancellation.
    future.add_done_callback(lambda _: _model_slots.release())
    while True:
        if cancelled is not None and cancelled.is_set():
            future.cancel();raise InterruptedError('查询已取消')
        remaining=deadline-time.monotonic()
        if remaining<=0:
            future.cancel();raise ValueError('模型请求超过总时限；可重试本轮，已发送的请求未自动重复提交')
        try:
            value=future.result(timeout=min(.1,remaining))
            if cancelled is not None and cancelled.is_set():raise InterruptedError('查询已取消')
            return value
        except FutureTimeout:continue

def _pace_provider(d,deadline,cancelled):
    if urlsplit(d.get('endpoint','')).hostname!='tokenrhythm.studio':return
    account=hashlib.sha256(d.get('_secret','').encode()).hexdigest()
    with _provider_rate_lock:
        start=max(time.monotonic(),_provider_next_request.get(account,0))
        _provider_next_request[account]=start+2
    while time.monotonic()<start:
        if cancelled is not None and cancelled.is_set():raise InterruptedError('查询已取消')
        if time.monotonic()>=deadline:raise ValueError('服务限流等待超过本轮时限，请稍后重试')
        if cancelled is not None:cancelled.wait(min(.1,start-time.monotonic()))
        else:time.sleep(min(.1,max(0,start-time.monotonic())))

def _request_json_transport(d,url,body,headers,deadline,cancelled):
    gate(d)
    if d.get('_required_kind'):
        current=ready(d['id'],{d['_required_kind']})
        if current['revision']!=d['revision']:raise ValueError('连接已改变，请重新准备请求')
    if cancelled is not None and cancelled.is_set():raise InterruptedError('查询已取消')
    _pace_provider(d,deadline,cancelled)
    remaining=deadline-time.monotonic()
    if remaining<=0:raise ValueError('模型请求超过总时限')
    try:
        # HTTPX retries ConnectError/ConnectTimeout before sending the body, never
        # read/write failures or HTTP errors after a possibly billable submission.
        transport=httpx.HTTPTransport(retries=1,trust_env=False)
        with httpx.Client(timeout=httpx.Timeout(remaining,connect=min(5,remaining)),transport=transport,follow_redirects=False,trust_env=False) as client:
            with client.stream('POST' if body is not None else 'GET',url,json=body,headers={'User-Agent':'EvidenceFlow/0.3 (local research tool)',**(headers or {})}) as r:
                if r.is_redirect:raise ValueError('服务要求重定向；请填写最终地址，凭据未转发')
                if r.status_code>=400:
                    detail={401:'凭据无效或已过期',403:'服务拒绝访问，请检查权限或服务访问限制',404:'接口或模型不存在',429:'服务限流或额度不足'}.get(r.status_code,'服务请求失败')
                    raw=bytearray()
                    for part in r.iter_bytes():
                        raw.extend(part[:max(0,65537-len(raw))])
                        if len(raw)>65536:break
                    if len(raw)<=65536:
                        try:
                            error=json.loads(raw);code=error.get('code') or (error.get('error') or {}).get('code')
                            trace=error.get('traceId') or error.get('trace_id')
                            secret=d.get('_secret','')
                            if isinstance(code,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',code) and (not secret or secret not in code):detail+='；错误码 '+code
                            if isinstance(trace,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',trace) and (not secret or secret not in trace):detail+='；请求编号 '+trace
                        except (ValueError,AttributeError,TypeError):pass
                    raise ValueError(detail+f'（HTTP {r.status_code}）')
                data=bytearray()
                for chunk in r.iter_bytes():
                    if cancelled is not None and cancelled.is_set():raise InterruptedError('查询已取消')
                    if time.monotonic()>deadline:raise ValueError('请求超过总时限，请稍后重试')
                    data.extend(chunk)
                    if len(data)>2*1024*1024:raise ValueError('服务响应超过 2 MB')
        return json.loads(data)
    except httpx.TimeoutException:raise ValueError('连接超时；本轮未自动重复提交，可检查网络后重试') from None
    except httpx.RequestError:raise ValueError('无法连接服务，请检查地址、网络和证书；本轮未自动重复提交') from None
    except (json.JSONDecodeError,UnicodeDecodeError):raise ValueError('服务返回的内容不是有效 JSON') from None

def chat(d,messages,max_tokens=1200,cancelled=None,json_mode=False):
    if urlsplit(d['endpoint']).path.rstrip('/').endswith('/apps/anthropic'):
        from . import anthropic_vision
        headers={'anthropic-version':'2023-06-01'}
        if d.get('_secret'):headers['x-api-key']=d['_secret']
        raw=request_json(d,d['endpoint']+'/v1/messages',anthropic_vision.request(messages,d['model'],max_tokens),headers,cancelled=cancelled)
        return anthropic_vision.response(raw,d['model'])
    headers={'Authorization':'Bearer '+d['_secret']} if d.get('_secret') else {}
    body={'model':d['model'],'messages':messages,'stream':False,'max_completion_tokens':max_tokens}
    host=urlsplit(d['endpoint']).hostname
    # DeepSeek shares the output budget with thinking by default, whether it is
    # resold through TokenDance/TokenRhythm or served directly by api.deepseek.com.
    # Our tools and independent evidence checks handle this workflow without it.
    structured_gateway=host in {'tokendance.space','tokenrhythm.studio','api.deepseek.com'}
    if structured_gateway and d['model'].startswith('deepseek-'):
        body.pop('max_completion_tokens')
        body.update(max_tokens=max_tokens,thinking={'type':'disabled'})
    if json_mode and structured_gateway:
        body.update(response_format={'type':'json_object'},temperature=0)
        if d['model'].startswith('qwen3.8-'):body['enable_thinking']=False
    r=request_json(d,d['endpoint']+'/chat/completions',body,headers,cancelled=cancelled)
    try:text=r['choices'][0]['message']['content']
    except (KeyError,IndexError,TypeError):raise ValueError('服务未返回文本回答') from None
    if not isinstance(text,str) or not text.strip():
        if r['choices'][0].get('finish_reason')=='length':raise ValueError('模型在输出预算内未生成回答，请换用其他模型或缩短问题')
        raise ValueError('服务返回空回答，请检查模型状态或换用其他模型')
    return {'text':text[:30000],'model':str(r.get('model',d['model']))[:150],'finish_reason':str(r['choices'][0].get('finish_reason',''))[:40],
        'usage':{k:v for k,v in (r.get('usage') or {}).items() if k in {'prompt_tokens','completion_tokens','total_tokens'} and isinstance(v,int)}}

def _bounded_texts(texts, maximum=32):
    if not isinstance(texts,list) or not 1<=len(texts)<=maximum:raise ValueError('文本批次大小超出限制')
    if any(not isinstance(t,str) or not t.strip() or len(t)>12000 for t in texts):raise ValueError('文本为空或超过单条限制')
    if sum(len(t.encode('utf-8')) for t in texts)>128000:raise ValueError('文本批次超过发送量限制')

def _usage(raw):
    u=raw.get('usage') or {}
    return {k:v for k,v in u.items() if k in {'prompt_tokens','total_tokens'} and isinstance(v,int) and not isinstance(v,bool) and v>=0} if isinstance(u,dict) else {}

def embedding(d,texts,cancelled=None,expected_dimension=None):
    """OpenAI embeddings transport. Validated real responses; never synthesize vectors."""
    _bounded_texts(texts)
    headers={'Authorization':'Bearer '+d['_secret']} if d.get('_secret') else {}
    raw=request_json(d,d['endpoint']+'/embeddings',{'model':d['model'],'input':texts,'encoding_format':'float'},headers,cancelled)
    rows=raw.get('data') if isinstance(raw,dict) else None
    if not isinstance(rows,list) or len(rows)!=len(texts):raise ValueError('嵌入服务返回数量与输入不一致')
    vectors=[None]*len(texts);dimension=None
    for row in rows:
        index=row.get('index') if isinstance(row,dict) else None
        if not isinstance(index,int) or isinstance(index,bool) or not 0<=index<len(texts) or vectors[index] is not None:raise ValueError('嵌入服务返回索引缺失或重复')
        vector=row.get('embedding')
        if not isinstance(vector,list) or not 8<=len(vector)<=4096:raise ValueError('嵌入向量维度无效')
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in vector):raise ValueError('嵌入向量包含无效数值')
        norm=math.hypot(*vector)
        if not math.isfinite(norm) or norm<=1e-12:raise ValueError('嵌入服务返回零向量或无效范数')
        if dimension is None:dimension=len(vector)
        if len(vector)!=dimension or (expected_dimension and len(vector)!=expected_dimension):raise ValueError('嵌入向量维度已改变，请重建索引')
        vectors[index]=[float(v)/norm for v in vector]
    return {'vectors':vectors,'dimension':dimension,'model':str(raw.get('model',d['model']))[:150],'usage':_usage(raw)}

def rerank(d,query,passages,cancelled=None):
    """Common /rerank contract: results[index,relevance_score], in input order."""
    _bounded_texts(passages,64);_bounded_texts([query],1)
    headers={'Authorization':'Bearer '+d['_secret']} if d.get('_secret') else {}
    raw=request_json(d,d['endpoint']+'/rerank',{'model':d['model'],'query':query,'documents':passages,'top_n':len(passages),'return_documents':False},headers,cancelled)
    rows=raw.get('results') if isinstance(raw,dict) else None
    if not isinstance(rows,list) or len(rows)!=len(passages):raise ValueError('重排服务返回数量与候选不一致')
    scores=[None]*len(passages)
    for row in rows:
        index=row.get('index') if isinstance(row,dict) else None;score=row.get('relevance_score') if isinstance(row,dict) else None
        if not isinstance(index,int) or isinstance(index,bool) or not 0<=index<len(scores) or scores[index] is not None:raise ValueError('重排服务返回索引缺失或重复')
        if isinstance(score,bool) or not isinstance(score,(float,int)) or not math.isfinite(score):raise ValueError('重排服务返回无效分数')
        scores[index]=float(score)
    return {'scores':scores,'model':str(raw.get('model',d['model']))[:150],'usage':_usage(raw)}

def vision_probe():
    from PIL import Image,ImageDraw
    colors=[('RED','#e32020'),('BLUE','#174ced'),('GREEN','#159c38')];secrets.SystemRandom().shuffle(colors)
    im=Image.new('RGB',(360,160),'white');draw=ImageDraw.Draw(im)
    for i,(_,color) in enumerate(colors):draw.rectangle((15+i*120,25,105+i*120,135),fill=color)
    b=io.BytesIO();im.save(b,format='PNG')
    return b.getvalue(),','.join(x[0] for x in colors)

def search(d,q):
    if d['kind']=='brave':
        if not d.get('_secret'):raise ValueError('请填写搜索 API 密钥')
        raw=request_json(d,'https://api.search.brave.com/res/v1/web/search?q='+quote(q)+'&count=5',headers={'X-Subscription-Token':d['_secret']})
        rows=raw.get('web',{}).get('results',[])
        items=[{'title':x.get('title',''),'url':x.get('url',''),'snippet':x.get('description','')} for x in rows]
    else:
        raw=request_json(d,'https://zh.wikipedia.org/w/api.php?action=query&list=search&srsearch='+quote(q)+'&srlimit=5&format=json')
        if 'error' in raw:raise ValueError('百科检索服务返回错误')
        items=[{'title':x['title'],'url':'https://zh.wikipedia.org/?curid='+str(x['pageid']),'snippet':x.get('snippet','')} for x in raw.get('query',{}).get('search',[])]
    import html
    for x in items:x['title']=html.unescape(re.sub('<[^>]*>','',x['title']))[:500];x['snippet']=html.unescape(re.sub('<[^>]*>','',x['snippet']))[:3000]
    return [x for x in items if urlsplit(x['url']).scheme in {'http','https'} and not urlsplit(x['url']).username][:5]

@contextmanager
def database(d):
    gate(d);c=None
    try:
        if d['kind']=='sqlite':
            c=sqlite3.connect(Path(d['path']).as_uri()+'?mode=ro',uri=True,timeout=3)
            c.execute('PRAGMA query_only=ON');c.execute('PRAGMA trusted_schema=OFF')
            deadline=time.monotonic()+d['timeout'];c.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
        elif d['kind']=='postgresql':
            import psycopg
            c=psycopg.connect(host=d['host'],port=d['port'],dbname=d['database'],user=d['username'],password=d.get('_secret',''),connect_timeout=5,sslmode='prefer' if is_local(d) else 'verify-full',options=f"-c default_transaction_read_only=on -c statement_timeout={d['timeout']*1000}")
            c.execute('SET TRANSACTION READ ONLY')
        elif d['kind']=='mysql':
            import pymysql
            c=pymysql.connect(host=d['host'],port=d['port'],database=d['database'],user=d['username'],password=d.get('_secret',''),connect_timeout=5,read_timeout=d['timeout'],write_timeout=5,ssl=None if is_local(d) else {'check_hostname':True},autocommit=False)
            with c.cursor() as cur:cur.execute('SET TRANSACTION READ ONLY');cur.execute('START TRANSACTION READ ONLY')
        else:raise ValueError('请选择数据库连接')
        yield c
    except HTTPException:raise
    except (ValueError,FileNotFoundError):raise
    except Exception:raise ValueError('数据库读取失败，请检查连接、只读权限和表结构；未执行写入') from None
    finally:
        if c:c.close()

def rows(c,sql,params=(),limit=20001):
    cur=c.cursor()
    try:
        cur.execute(sql,params)
        cols=[x[0] for x in cur.description]
        values=[];size=0
        while len(values)<limit:
            batch=cur.fetchmany(min(256,limit-len(values)))
            if not batch:break
            size+=len(encode(batch).encode())
            if size>20*1024*1024:raise ValueError('返回数据超过 20 MB，请缩小表范围')
            values.extend(batch)
        def scalar(v):
            if v is None or isinstance(v,(str,int,float)):return v
            if isinstance(v,bytes):return '[二进制 '+str(len(v))+' 字节]'
            return str(v)
        return {'columns':cols,'rows':[{k:scalar(v) for k,v in zip(cols,row)} for row in values]}
    finally:cur.close()

def table_list(d,c):
    if d['kind']=='sqlite':return [{'schema':'main','name':r['name']} for r in rows(c,"SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",limit=1001)['rows']]
    if d['kind']=='postgresql':sql="SELECT table_schema AS schema,table_name AS name FROM information_schema.tables WHERE table_type='BASE TABLE' AND table_schema NOT IN ('pg_catalog','information_schema') ORDER BY table_schema,table_name"
    else:sql="SELECT table_schema AS `schema`,table_name AS name FROM information_schema.tables WHERE table_type='BASE TABLE' AND table_schema=DATABASE() ORDER BY table_name"
    return rows(c,sql,limit=1001)['rows']

def identifier(d,schema,name):
    quoted=lambda v:'`'+v.replace('`','``')+'`' if d['kind']=='mysql' else s.quoted(v)
    return quoted(schema)+'.'+quoted(name)
class Table(Strict):
    schema_name:str=Field(default='main',max_length=255)
    name:str=Field(min_length=1,max_length=255)
class Tables(Strict):
    tables:list[Table]=Field(min_length=1,max_length=10)

def ready(cid,kind=None):
    d=get(cid,True);gate(d)
    if kind and d['kind'] not in kind:raise ValueError('连接类型与操作不匹配')
    if not d.get('enabled') or not d.get('last_test') or not d['last_test']['ok']:raise ValueError('请先测试并启用连接')
    return d


def import_metadata(snapshot, mapping, column_mapping=None):
    """Retain source keys without inventing local constraints or parent tables."""
    source=snapshot['source'];by_name={t['qualified_name']:t for t in snapshot['tables']}
    column_mapping=column_mapping or {}
    def columns(key, values):
        aliases=column_mapping.get(key,{})
        return [aliases.get(value,value) for value in values]
    result={}
    for original,local in mapping.items():
        table=by_name[original]
        body={'original_table':{'schema':table['schema'],'name':table['name']},
              'origin':{'connection_id':source.get('connection_id'),'connection_revision':source.get('revision'),
                        'fingerprint':source['fingerprint'],'fingerprint_scope':'schema_snapshot'},
              'primary_key':columns(original,table.get('primary_key',[])),
              'unique_keys':[{'name':key.get('name','source-unique'),'columns':columns(original,key['columns'])}
                             for key in table.get('unique_keys',[])],
              'foreign_keys':[],'unresolved_foreign_keys':[]}
        for foreign in table.get('foreign_keys',[]):
            parent=foreign.get('referenced_schema',table['schema'])+'.'+foreign['referenced_table']
            targets=foreign.get('referenced_columns') or []
            if (not targets or any(value is None for value in targets)) and parent in by_name:
                targets=by_name[parent].get('primary_key',[])
            relation={'id':foreign.get('id','source-fk'),'columns':columns(original,foreign['columns']),
                      'referenced_schema':'main','referenced_table':mapping.get(parent,foreign['referenced_table']),
                      'referenced_columns':columns(parent,targets)}
            if parent in mapping and targets and all(targets):body['foreign_keys'].append(relation)
            else:
                relation['referenced_schema']=foreign.get('referenced_schema',table['schema'])
                body['unresolved_foreign_keys'].append(relation)
        result[local]=body
    return result

@router.post('/{cid}/test')
def test(cid:str):
    d=get(cid,True);start=time.monotonic();ok=False;detail='';probe={}
    try:
        gate(d)
        if d['kind'] in DB_KINDS:
            with database(d) as c:count=len(table_list(d,c))
            detail=f'只读连接通过；发现 {count} 张表'
        elif d['kind']=='text':
            a,b=secrets.randbelow(800)+100,secrets.randbelow(800)+100
            result=chat(d,[{'role':'user','content':f'计算 {a}+{b}。只返回整数，不要其他内容。'}])
            if result['text'].strip()!=str(a+b):raise ValueError('文本探针未通过：返回值不符合计算结果')
            detail='文本实际调用通过；算术探针正确，不代表业务准确率'
        elif d['kind']=='vision':
            png,expected=vision_probe()
            result=chat(d,[{'role':'user','content':[{'type':'text','text':'Read the three squares left to right. Return only their English color names RED, BLUE or GREEN, comma separated.'},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(png).decode()}}]}])
            if result['text'].replace(' ','').strip().upper()!=expected:raise ValueError('视觉探针未通过：颜色顺序与测试图不符')
            detail='图像实际调用通过；颜色探针正确，不代表 OCR 或页面核验已通过'
        elif d['kind']=='embedding':
            result=embedding(d,['测试用语：汽车售后保修条款','测试用语：车辆维修保修规定','测试用语：番茄炒蛋烹饪方法'])
            if len({tuple(v) for v in result['vectors']})!=3:raise ValueError('嵌入探针未通过：不同输入返回相同向量')
            probe={'dimension':result['dimension'],'provider_model':result['model']}
            detail=f"嵌入实际调用通过；{result['dimension']} 维、数量及有限数值验证，不代表中文检索质量"
        elif d['kind']=='rerank':
            result=rerank(d,'测试问题：车辆保修期限是多久？',['测试条款：车辆保修期限为三年。','测试文本：番茄炒蛋需要鸡蛋和番茄。'])
            if result['scores'][0]<=result['scores'][1]:raise ValueError('重排探针未通过：相关条款未排在无关文本之前')
            probe={'provider_model':result['model']}
            detail='重排实际调用通过；候选索引、有限分数与测试顺序验证，不代表业务准确率'
        else:
            found=search(d,'SQLite')
            if not found:raise ValueError('检索探针没有返回可打开的来源')
            detail=f'检索实际调用通过；收到 {len(found)} 条来源'
        ok=True
    except HTTPException as e:detail=str(e.detail)
    except (ValueError,OSError) as e:detail=str(e)[:250]
    ev=event(d,'test',ok,detail,time.monotonic()-start)
    with lock:
        latest=get(cid)
        if latest['revision']!=d['revision']:raise HTTPException(409,'测试期间配置已改变，结果不用于新配置，请重新测试')
        latest.pop('has_secret',None);latest['last_test']={**{k:ev[k] for k in ['ok','detail','duration_ms','time']},**probe}
        if not ok:latest['enabled']=False
        with s.conn() as c:c.execute('UPDATE ef_connections SET body=? WHERE id=?',(encode(latest),cid))
    return get(cid)

@router.get('/{cid}/tables')
def tables(cid:str):
    d=ready(cid,DB_KINDS)
    with database(d) as c:items=table_list(d,c)
    return {'items':items[:1000],'truncated':len(items)>1000}
@router.post('/{cid}/preview')
def preview(cid:str,req:Table):
    d=ready(cid,DB_KINDS)
    with database(d) as c:
        if {'schema':req.schema_name,'name':req.name} not in table_list(d,c):raise ValueError('表不在当前连接中')
        data=rows(c,'SELECT * FROM '+identifier(d,req.schema_name,req.name)+' LIMIT 20',limit=20)
    return {**data,'table':req.model_dump(),'preview_limit':20,'read_only':True,'time':s.now()}
@router.post('/{cid}/import')
def import_tables(cid:str,req:Tables):
    from . import database_query
    d=ready(cid,DB_KINDS);prepared=[];start=time.monotonic();stamp=s.now()
    if len({(t.schema_name,t.name) for t in req.tables})!=len(req.tables):raise ValueError('不能重复导入同一张表')
    selected=[t.schema_name+'.'+t.name for t in req.tables]
    snapshot=database_query.inspect(cid,selected)
    if snapshot['source']['revision']!=d['revision']:raise ValueError('数据库连接已改变，请重新选择导入来源')
    mapping={}
    with database(d) as c:
        if d['kind']=='sqlite':c.execute('BEGIN')
        allowed=table_list(d,c)
        for t in req.tables:
            if {'schema':t.schema_name,'name':t.name} not in allowed:raise ValueError('选中的表已不存在')
            data=rows(c,'SELECT * FROM '+identifier(d,t.schema_name,t.name)+' LIMIT 20001')
            if len(data['rows'])>20000:raise ValueError('单表超过 2 万行，请在数据库建立较小的分析表后重试')
            if len(set(data['columns']))!=len(data['columns']) or not data['columns']:raise ValueError('字段为空或重复')
            if len(encode(data).encode())>20*1024*1024:raise ValueError('单表超过 20 MB，请缩小范围')
            name='import_'+(re.sub(r'\W+','_',t.name)[:40] or 'table')+'_'+uuid.uuid4().hex[:8]
            provenance={'connection_id':cid,'connection_name':d['name'],'kind':d['kind'],'schema':t.schema_name,'source_table':t.name,'imported_at':stamp,'row_count':len(data['rows']),'sha256':hashlib.sha256(encode(data).encode()).hexdigest(),'read_only_source':True}
            mapping[t.schema_name+'.'+t.name]=name
            prepared.append((name,data,provenance))
    current=database_query.inspect(cid,selected)
    if current['source']['revision']!=d['revision'] or current['source']['fingerprint']!=snapshot['source']['fingerprint']:
        raise ValueError('导入期间数据库连接或结构已改变；未提交快照，请重试')
    metadata=import_metadata(snapshot,mapping)
    c=sqlite3.connect(s.BUSINESS_DB,timeout=8)
    try:
        c.execute('ATTACH DATABASE ? AS catalog',(str(s.APP_DB),));c.execute('BEGIN IMMEDIATE')
        latest=c.execute('SELECT body FROM catalog.ef_connections WHERE id=?',(cid,)).fetchone()
        if not latest or json.loads(latest[0])['revision']!=d['revision']:
            raise ValueError('导入期间数据库连接已改变；未提交快照，请重试')
        c.execute('CREATE TABLE IF NOT EXISTS catalog.ef_database_import_metadata(name TEXT PRIMARY KEY,body TEXT NOT NULL)')
        for name,data,provenance in prepared:
            columns=data['columns'];types=[]
            for key in columns:
                values=[r[key] for r in data['rows'] if r[key] is not None]
                types.append('INTEGER' if values and all(isinstance(v,int) for v in values) else 'REAL' if values and all(isinstance(v,(int,float)) for v in values) else 'TEXT')
            c.execute('CREATE TABLE '+s.quoted(name)+' ('+', '.join(s.quoted(k)+' '+typ for k,typ in zip(columns,types))+')')
            c.executemany('INSERT INTO '+s.quoted(name)+' VALUES ('+','.join('?' for _ in columns)+')',[tuple(r[k] for k in columns) for r in data['rows']])
            c.execute('INSERT INTO catalog.table_catalog VALUES (?,?,?,0)',(name,d['name']+' / '+provenance['source_table'],stamp))
            c.execute('INSERT INTO catalog.ef_import_provenance VALUES (?,?)',(name,encode(provenance)))
            c.execute('INSERT INTO catalog.ef_database_import_metadata VALUES (?,?)',(name,encode(metadata[name])))
        c.commit()
    except sqlite3.Error:
        c.rollback();raise ValueError('本地导入提交失败；数据表、目录与关系元数据均已回滚，请重试') from None
    except Exception:c.rollback();raise
    finally:c.close()
    event(d,'import',True,f'导入 {len(prepared)} 张表的本地快照',time.monotonic()-start)
    return {'tables':[n for n,_,_ in prepared],'provenance':[p for _,_,p in prepared],'message':'已导入本地快照并保留主键、唯一键及同批表关系；可在主问答设置选择本机工作库进行模型驱动的数据库问数。'}
