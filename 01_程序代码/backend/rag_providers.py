"""Pinned, local CPU RAG models and separately tested bounded remote transports.

Installing an audited public model is explicit. Importing this module never
downloads weights, builds an index, or sends user documents to a provider.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Literal
import uuid
import zipfile

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
import httpx
import numpy as np
from . import connections as c, store as s

router = APIRouter(prefix='/api/rag')
_lock = threading.RLock()
_sessions = {}
_tokenizers = {}
_verified = {}
_jobs = {}
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='evidenceflow-rag-cpu')
_slots = threading.BoundedSemaphore(2)
QUERY_PREFIX = '为这个句子生成表示以用于检索相关文章：'
PROVIDER_VERSION = 'rag-onnx-cls-v1'
DEFAULT = {'revision': 1, 'embedding_mode': 'local', 'embedding_connection_id': None,
           'rerank_mode': 'local', 'rerank_connection_id': None,
           'embedding_model': 'BAAI/bge-small-zh-v1.5',
           'rerank_model': 'BAAI/bge-reranker-base', 'remote_index_allowed': False}

# Model weights are BAAI models converted/quantized by Xenova. These are not
# claimed to be numerically equivalent to the original full-precision weights.
REGISTRY = {
 'bge-small-zh-v1.5': {
  'role': 'embedding', 'model': 'BAAI/bge-small-zh-v1.5', 'source': 'Xenova/bge-small-zh-v1.5',
  'revision': '75c43b069aac4d136ba6bc1122f995fedcfd2781', 'dimension': 512, 'max_tokens': 512,
  'files': {
   'model.onnx': ('15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc', 24010842, 'onnx/model_quantized.onnx'),
   'tokenizer.json': ('48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26', 439125, 'tokenizer.json'),
   'config.json': ('d4193ead3a810fd694fa8a31d7fc72fbaebc0668b603e398734bf2f6538ff42f', 716, 'config.json'),
   'MODEL_CARD.md': ('cb22054df1a98f7ee9d5b281d023e67c27cc316e1733baf03fc994d95c3048ab', 1057, 'README.md'),
   'LICENSE.txt': ('587a673933425dbc36ec61268d3b954051b2d3ef3c9b322ede357976055ffdd5', 1065, 'license')
  }},
 'bge-reranker-base': {
  'role': 'rerank', 'model': 'BAAI/bge-reranker-base', 'source': 'Xenova/bge-reranker-base',
  'revision': '280bcc27a84e0b898c251e06fddb25171bd9b101', 'dimension': None, 'max_tokens': 512,
  'files': {
   'model.onnx': ('dd98f3e67837d23210a6b7550c08cced4f61845b940ac45be3565840a10f3244', 279301077, 'onnx/model_quantized.onnx'),
   'tokenizer.json': ('48564c5c7d3fa64d85d95e65414a542385f88b0f128fd8d4163fd7a57f2be05c', 17098079, 'tokenizer.json'),
   'config.json': ('b6575b9d5be20d6747417c8e20c5a0db1636356e0b6d422d7244c628423c4d4c', 782, 'config.json'),
   'MODEL_CARD.md': ('2ccb3f72b5eb6205316c0b9dde944d6f944ec4a87b85706ce07ef9d9d02b4690', 1135, 'README.md'),
   'LICENSE.txt': ('587a673933425dbc36ec61268d3b954051b2d3ef3c9b322ede357976055ffdd5', 1065, 'license')
  }}
}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def model_dir():
    return Path(os.environ.get('EVIDENCEFLOW_RAG_MODEL_DIR', str(s.DATA/'models'))).resolve()


def bundled_pack():
    return Path(os.environ.get('EVIDENCEFLOW_RAG_MODEL_PACK', str(s.PROJECT_ROOT/'06_运行环境'/'RAG模型包.zip'))).resolve()


def initialize():
    with s.conn() as db:
        db.execute('CREATE TABLE IF NOT EXISTS ef_rag_provider_settings(key TEXT PRIMARY KEY,body TEXT NOT NULL)')
        db.execute('INSERT OR IGNORE INTO ef_rag_provider_settings VALUES (?,?)', ('providers', c.encode(DEFAULT)))


@router.get('/config')
def config():
    initialize()
    with s.conn() as db:
        row = db.execute("SELECT body FROM ef_rag_provider_settings WHERE key='providers'").fetchone()
    return {**DEFAULT, **json.loads(row[0])}


class ConfigRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)
    embedding_mode: Literal['local', 'connection', 'disabled'] = 'local'
    embedding_connection_id: str | None = Field(default=None, max_length=100)
    rerank_mode: Literal['local', 'connection', 'disabled'] = 'local'
    rerank_connection_id: str | None = Field(default=None, max_length=100)
    embedding_model: Literal['BAAI/bge-small-zh-v1.5'] = 'BAAI/bge-small-zh-v1.5'
    rerank_model: Literal['BAAI/bge-reranker-base'] = 'BAAI/bge-reranker-base'
    remote_index_allowed: bool = False


@router.put('/config')
def save_config(req: ConfigRequest):
    with _lock:
        old = config()
        if req.expected_revision != old['revision']:
            raise HTTPException(409, 'RAG 配置已改变，请刷新后再保存')
        body = req.model_dump(exclude={'expected_revision'})
        for role in ('embedding', 'rerank'):
            cid = body[role+'_connection_id']
            if body[role+'_mode'] == 'connection':
                if not cid: raise ValueError('请选择已测试并启用的 '+role+' 连接')
                c.ready(cid, {role})
            else:
                body[role+'_connection_id'] = None
        body.update(revision=old['revision']+1, updated_at=s.now())
        with s.conn() as db:
            db.execute("UPDATE ef_rag_provider_settings SET body=? WHERE key='providers'", (c.encode(body),))
    return config()


def _verify(name, path=None):
    spec = REGISTRY[name]; root = path or (model_dir()/name)
    identity = []
    for filename, (_, size, _) in spec['files'].items():
        p = root/filename
        if not p.is_file() or p.is_symlink(): raise ValueError('本地模型未安装或文件不完整，请安装审定模型包')
        stat = p.stat()
        if stat.st_size != size: raise ValueError('本地模型文件大小不符，请重新安装模型包')
        identity.append((filename, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    key = (str(root), tuple(identity))
    with _lock:
        if _verified.get(name) == key: return spec
    for filename, (digest, _, _) in spec['files'].items():
        if s.digest(root/filename) != digest: raise ValueError('本地模型文件摘要不符，请重新安装模型包')
    with _lock: _verified[name] = key
    return spec


def describe(role='embedding'):
    if role not in {'embedding', 'rerank'}: raise ValueError('未知 RAG 模型类型')
    cfg = config(); mode = cfg[role+'_mode']
    base = {'role': role, 'ready': False, 'local': mode != 'connection',
            'revision': cfg['revision'], 'dimension': None, 'max_tokens': 512,
            'fingerprint': None, 'provider_id': None, 'model': cfg[role+'_model'], 'mode': mode}
    try:
        if mode == 'disabled': raise ValueError('此 RAG 能力已关闭')
        if mode == 'local':
            name = cfg[role+'_model'].split('/')[-1]; spec = REGISTRY[name]
            base.update(provider_id=name, dimension=spec['dimension'], precision='q8',
                        onnx_source=spec['source'], model_revision=spec['revision'], local=True,
                        fingerprint=_hash({'version': PROVIDER_VERSION, 'model': spec['model'], 'source': spec['source'],
                                           'revision': spec['revision'], 'files': spec['files'], 'pooling': 'CLS' if role=='embedding' else 'logits',
                                           'normalize': role=='embedding', 'query_prefix': QUERY_PREFIX if role=='embedding' else ''}))
            _verify(name)
            if importlib.util.find_spec('onnxruntime') is None or importlib.util.find_spec('tokenizers') is None:
                raise ValueError('本地 ONNX 推理依赖尚未就绪')
        else:
            cid = cfg[role+'_connection_id']
            if not cid: raise ValueError('尚未选择 '+role+' 连接')
            d = c.ready(cid, {role}); last = d['last_test']
            base.update(provider_id=cid, local=c.is_local(d), model=d['model'], connection_revision=d['revision'],
                        dimension=last.get('dimension') if role=='embedding' else None,
                        fingerprint=_hash({'provider_id':cid, 'revision':d['revision'], 'endpoint':d['endpoint'],
                                           'model':d['model'], 'actual_model':last.get('provider_model'),
                                           'dimension':last.get('dimension'), 'version': PROVIDER_VERSION}))
            if role=='embedding' and not base['dimension']: raise ValueError('嵌入连接没有有效维度，请重新测试')
        base.update(ready=True, reason='', error=None)
    except (ValueError, OSError, HTTPException) as exc:
        base.update(reason=str(exc.detail) if isinstance(exc, HTTPException) else str(exc), error=type(exc).__name__)
    return base


def capabilities():
    return {role: describe(role) for role in ('embedding', 'rerank')}


def _check(cancelled=None, deadline=None):
    if cancelled is not None and cancelled.is_set(): raise InterruptedError('查询已取消')
    if deadline is not None and time.monotonic() >= deadline: raise TimeoutError('RAG 模型调用超过总时限')


def _snapshot(role, expected_fingerprint=None):
    provider = describe(role)
    if not provider['ready']: raise ValueError(provider['reason'])
    if expected_fingerprint and provider['fingerprint'] != expected_fingerprint:
        raise ValueError('RAG 模型身份已改变，请重建索引或重新准备查询')
    return provider


def _still_current(provider):
    current = describe(provider['role'])
    if not current['ready'] or current['revision'] != provider['revision'] or current['fingerprint'] != provider['fingerprint']:
        raise ValueError('RAG 配置、模型或连接已改变，请重新准备查询')


def _load(name):
    spec = _verify(name); root = model_dir()/name
    key = (str(root), name, spec['revision'], spec['files']['model.onnx'][0])
    with _lock:
        if key not in _sessions:
            from tokenizers import Tokenizer
            import onnxruntime as ort
            tok = Tokenizer.from_file(str(root/'tokenizer.json'))
            tok.no_truncation()
            pad = '[PAD]' if spec['role']=='embedding' else '<pad>'
            pad_id = tok.token_to_id(pad)
            if pad_id is None: raise ValueError('模型分词器缺少 padding 标记')
            tok.enable_padding(pad_id=pad_id, pad_token=pad)
            options = ort.SessionOptions(); options.intra_op_num_threads=2; options.inter_op_num_threads=1
            options.execution_mode=ort.ExecutionMode.ORT_SEQUENTIAL
            sess = ort.InferenceSession(str(root/'model.onnx'), sess_options=options, providers=['CPUExecutionProvider'])
            _sessions[key] = (tok, sess)
        return _sessions[key]


def token_count(text, role='embedding', strict=False):
    if not isinstance(text, str): raise ValueError('分词输入必须是文字')
    try:
        cfg=config(); name=cfg[role+'_model'].split('/')[-1]; _verify(name)
        from tokenizers import Tokenizer
        key=(str(model_dir()),name,REGISTRY[name]['files']['tokenizer.json'][0])
        with _lock:
            if key not in _tokenizers:
                tok=Tokenizer.from_file(str(model_dir()/name/'tokenizer.json'))
                tok.no_truncation();tok.no_padding();_tokenizers[key]=tok
            tok=_tokenizers[key]
        return len(tok.encode(text, add_special_tokens=False).ids)
    except (ValueError, OSError, ImportError) as exc:
        if strict:
            raise ValueError('真实模型分词器不可用：'+type(exc).__name__) from None
        # Conservatively bound offline chunks. This is explicitly not model
        # tokenizer output; inference later checks actual encoded lengths.
        return max(1, len(text.encode('utf-8')))


def _local(role, inputs, provider, cancelled, deadline):
    """Two CPU slots; cancellation terminates ONNX rather than releasing a busy slot."""
    _check(cancelled, deadline)
    import onnxruntime as ort
    while not _slots.acquire(timeout=.1): _check(cancelled, deadline)
    options = ort.RunOptions()
    def infer():
        try:
            _still_current(provider)
            tok, sess = _load(provider['provider_id']); names = {v.name for v in sess.get_inputs()}
            values=[]; batch=8 if role=='embedding' else 4
            for start in range(0,len(inputs),batch):
                _check(cancelled,deadline); _still_current(provider)
                encoded=tok.encode_batch(inputs[start:start+batch], add_special_tokens=True)
                if any(len(v.ids)>provider['max_tokens'] for v in encoded):
                    raise ValueError('原文或问题超过本地模型 token 上限；请使用有界分窗，未静默截断')
                feed={'input_ids':np.asarray([v.ids for v in encoded],dtype=np.int64),
                      'attention_mask':np.asarray([v.attention_mask for v in encoded],dtype=np.int64),
                      'token_type_ids':np.asarray([v.type_ids for v in encoded],dtype=np.int64)}
                output=sess.run(None,{k:v for k,v in feed.items() if k in names},run_options=options)[0]
                if role=='embedding':
                    array=np.asarray(output,dtype=np.float32)
                    if array.ndim==3: array=array[:,0,:]
                    if array.ndim!=2 or array.shape!=(len(encoded),provider['dimension']): raise ValueError('本地嵌入输出维度与审定模型不符')
                    norm=np.linalg.norm(array,axis=1,keepdims=True)
                    if not np.isfinite(array).all() or not np.isfinite(norm).all() or (norm<=1e-12).any(): raise ValueError('本地嵌入输出无效')
                    values.extend((array/norm).tolist())
                else:
                    array=np.asarray(output,dtype=np.float64)
                    if array.size!=len(encoded) or not np.isfinite(array).all(): raise ValueError('本地重排输出数量或分数无效')
                    # Preserve actual logits. These scores are not calibrated
                    # probabilities of a correct or sufficiently sourced answer.
                    values.extend(array.reshape(-1).tolist())
            _check(cancelled,deadline); _still_current(provider)
            return values
        except (ValueError, InterruptedError, TimeoutError): raise
        except Exception as exc:
            _check(cancelled,deadline)
            raise ValueError('本地 ONNX 推理失败：'+type(exc).__name__) from None
    future=_pool.submit(infer);future.add_done_callback(lambda _: _slots.release())
    while True:
        try:
            _check(cancelled,deadline)
        except (InterruptedError,ValueError,TimeoutError):
            options.terminate=True;future.cancel();raise
        try:
            return future.result(timeout=.1)
        except FutureTimeout:
            # concurrent.futures.TimeoutError aliases builtin TimeoutError;
            # an expired inference deadline must not become an endless poll.
            if future.done():
                options.terminate=True
                raise
        except (InterruptedError,ValueError):
            options.terminate=True;future.cancel();raise


def _remote(provider):
    d=c.ready(provider['provider_id'],{provider['role']})
    if d['revision']!=provider['connection_revision']: raise ValueError('连接已改变，请重新准备请求')
    return {**d,'_required_kind':provider['role']}


def embed(texts, cancelled=None, expected_fingerprint=None, purpose='query', remote_index_opt_in=False, deadline=None):
    c._bounded_texts(texts,32)
    if purpose not in {'query','index'}: raise ValueError('未知嵌入用途')
    started=time.monotonic();deadline=min(deadline or started+60,started+60)
    _check(cancelled,deadline);provider=_snapshot('embedding',expected_fingerprint)
    if provider['mode']=='connection':
        if purpose=='index' and not provider['local']:
            if not config()['remote_index_allowed'] or not remote_index_opt_in:
                raise ValueError('远程批量索引尚未经过文档发送预览确认，未发送切片')
        d=_remote(provider);_check(cancelled,deadline)
        d['timeout']=max(.01,min(d['timeout'],deadline-time.monotonic()))
        try: result=c.embedding(d,texts,cancelled,provider['dimension'])
        except ValueError:
            _check(cancelled,deadline)
            raise
    else:
        inputs=[QUERY_PREFIX+t if purpose=='query' else t for t in texts]
        result={'vectors':_local('embedding',inputs,provider,cancelled,deadline),'usage':{}}
    _check(cancelled,deadline);_still_current(provider)
    return {**result,'provider':provider,'duration_ms':round((time.monotonic()-started)*1000),'sent_bytes':sum(len(t.encode('utf-8')) for t in texts) if not provider['local'] else 0}


def rerank(query, passages, cancelled=None, expected_fingerprint=None, deadline=None):
    c._bounded_texts(passages,64);c._bounded_texts([query],1)
    started=time.monotonic();deadline=min(deadline or started+60,started+60)
    _check(cancelled,deadline);provider=_snapshot('rerank',expected_fingerprint)
    if provider['mode']=='connection':
        d=_remote(provider);_check(cancelled,deadline)
        d['timeout']=max(.01,min(d['timeout'],deadline-time.monotonic()))
        try: result=c.rerank(d,query,passages,cancelled)
        except ValueError:
            _check(cancelled,deadline)
            raise
    else:
        result={'scores':_local('rerank',[(query,t) for t in passages],provider,cancelled,deadline),'usage':{}}
    _check(cancelled,deadline);_still_current(provider)
    return {**result,'provider':provider,'duration_ms':round((time.monotonic()-started)*1000),'sent_bytes':len(query.encode('utf-8'))+sum(len(t.encode('utf-8')) for t in passages) if not provider['local'] else 0}


@router.get('/models')
def models():
    items=[]
    for name,spec in REGISTRY.items():
        installed=False;error=''
        try: _verify(name);installed=True
        except (ValueError,OSError) as exc: error=str(exc)
        with _lock: job=dict(_jobs.get(name) or {})
        items.append({'id':name,'name':spec['model'],'role':spec['role'],'installed':installed,'verified':installed,
                      'size_bytes':sum(v[1] for v in spec['files'].values()),'license':'MIT','precision':'q8',
                      'source':spec['source'],'revision':spec['revision'],'error':job.get('error') or error,
                      'status':job.get('status') or ('completed' if installed else 'not_installed'),
                      'downloaded_bytes':job.get('downloaded_bytes',0),'total_bytes':sum(v[1] for v in spec['files'].values()),
                      'bundled':bundled_pack().is_file()})
    return {'items':items,'providers':capabilities()}


class InstallRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    model_id:Literal['bge-small-zh-v1.5','bge-reranker-base']
    confirm_download:bool=False


def _model_url(spec,source):
    if source=='license': return 'https://raw.githubusercontent.com/FlagOpen/FlagEmbedding/master/LICENSE'
    return f"https://huggingface.co/{spec['source']}/resolve/{spec['revision']}/{source}"


def _set_job(name,**values):
    with _lock: _jobs.setdefault(name,{}).update(values)


def _copy_checked(source,dest,digest,size,name,done):
    h=hashlib.sha256();n=0
    with dest.open('wb') as out:
        for chunk in iter(lambda:source.read(1024*1024),b''):
            n+=len(chunk)
            if n>size: raise ValueError('模型包文件超过审定大小')
            h.update(chunk);out.write(chunk);_set_job(name,downloaded_bytes=done+n)
    if n!=size or h.hexdigest()!=digest: raise ValueError('模型包文件大小或 SHA256 不符，未安装')
    return done+n


def _install(name,download):
    spec=REGISTRY[name];root=model_dir();root.mkdir(parents=True,exist_ok=True)
    stage=root/('.install-'+uuid.uuid4().hex);stage.mkdir();done=0
    _set_job(name,status='running',downloaded_bytes=0,error='',started_at=s.now())
    try:
        archive=bundled_pack()
        if archive.is_file():
            with zipfile.ZipFile(archive) as pack:
                names=pack.namelist()
                if len(names)!=len(set(names)): raise ValueError('模型包包含重复文件')
                for filename,(digest,size,_) in spec['files'].items():
                    member=name+'/'+filename
                    info=pack.getinfo(member)
                    if info.file_size!=size: raise ValueError('模型包内容大小与审定清单不符')
                    with pack.open(info) as source: done=_copy_checked(source,stage/filename,digest,size,name,done)
        else:
            if not download: raise ValueError('没有随包模型，请确认公开模型下载后再安装')
            _set_job(name,status='downloading')
            with httpx.Client(timeout=httpx.Timeout(45,connect=10),follow_redirects=True,trust_env=False) as client:
                for filename,(digest,size,source_name) in spec['files'].items():
                    url=_model_url(spec,source_name)
                    # Fixed public URLs, no credentials, and no user content.
                    with client.stream('GET',url) as response:
                        if response.status_code!=200: raise ValueError(f'公开模型下载失败（HTTP {response.status_code}）')
                        h=hashlib.sha256();n=0;started=time.monotonic()
                        with (stage/filename).open('wb') as out:
                            for chunk in response.iter_bytes(1024*1024):
                                n+=len(chunk)
                                if n>size or time.monotonic()-started>900: raise ValueError('公开模型下载超过大小或耗时限制')
                                h.update(chunk);out.write(chunk);_set_job(name,downloaded_bytes=done+n)
                        if n!=size or h.hexdigest()!=digest: raise ValueError('下载模型的 SHA256 不符，未安装')
                        done+=n
        _verify(name,stage)
        manifest={'id':name,'model':spec['model'],'onnx_source':spec['source'],'revision':spec['revision'],
                  'precision':'q8','license':'MIT','installed_at':s.now(),'files':{k:{'sha256':v[0],'size':v[1]} for k,v in spec['files'].items()}}
        (stage/'manifest.json').write_text(c.encode(manifest),encoding='utf-8')
        dest=root/name;old=None
        if dest.exists():
            old=root/('.old-'+uuid.uuid4().hex);dest.rename(old)
        stage.rename(dest)
        if old and old.resolve().is_relative_to(root.resolve()): shutil.rmtree(old)
        with _lock: _verified.pop(name,None)
        _verify(name)
        _set_job(name,status='completed',downloaded_bytes=done,completed_at=s.now(),error='')
    except Exception as exc:
        _set_job(name,status='failed',error=str(exc)[:300],completed_at=s.now())
    finally:
        if stage.exists() and stage.resolve().is_relative_to(root.resolve()): shutil.rmtree(stage,ignore_errors=True)


@router.post('/models/install')
def install_model(req:InstallRequest):
    name=req.model_id
    if not bundled_pack().is_file() and not req.confirm_download:
        raise ValueError('缺少随包模型；下载公开模型需要主动确认，不会发送文档')
    with _lock:
        if (_jobs.get(name) or {}).get('status') in {'pending','running','downloading'}:
            return {'model_id':name,**_jobs[name]}
        _jobs[name]={'status':'pending','downloaded_bytes':0,'error':''}
        threading.Thread(target=_install,args=(name,req.confirm_download),daemon=True,name='rag-model-install').start()
    return {'model_id':name,'status':'pending'}
