"""Explicit offline OCR and evidence-checked document vision providers.

No import, status request, or document operation downloads a model. Public
weights are installed only through install(), and every inference uses the
audited local files. Vision extraction remains pending without independent,
same-region native/OCR observations; model confidence cannot confirm content.
"""
from __future__ import annotations

import base64
import copy
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time
import unicodedata
import uuid
import zipfile

import httpx
from fastapi import HTTPException
from . import connections as c, rag_providers, store as s

VERSION = 'document-ocr-vision-v1'
MODEL_ID = 'ppocrv5-mobile-zh'
MODEL = 'PaddleOCR/PP-OCRv5-mobile-zh'
ENGINE_VERSION = '3.9.0'
MODEL_REVISION = 'RapidAI/RapidOCR-v3.9.0'
LIMITS = {'max_side': 2400, 'max_pixels': 6000000, 'max_input_bytes': 15000000,
          'max_lines': 400, 'max_characters': 24000, 'ocr_timeout_seconds': 120,
          'vision_timeout_seconds': 60, 'max_response_characters': 29999}
FORMULA_REREAD_LIMIT = 2
LOW_SCORE = .8
_SOURCE = 'https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.0/'
# The ONNX digests are the registry in the official 3.9.0 wheel. Dictionary and
# license digests are pinned after downloading the release's public artifacts.
FILES = {
    'det.onnx': ('4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae', 4819576,
                 _SOURCE+'onnx/PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx'),
    'rec.onnx': ('5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5', 16631306,
                 _SOURCE+'onnx/PP-OCRv5/rec/ch_PP-OCRv5_rec_mobile.onnx'),
    'cls.onnx': ('54379ae5174d026780215fc748a7f31910dee36818e63d49e17dc598ecc82df7', 1018508,
                 _SOURCE+'onnx/PP-OCRv5/cls/ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx'),
    'ppocrv5_dict.txt': ('d1979e9f794c464c0d2e0b70a7fe14dd978e9dc644c0e71f14158cdf8342af1b', 74012, _SOURCE+'paddle/PP-OCRv5/rec/ch_PP-OCRv5_rec_mobile/ppocrv5_dict.txt'),
    'LICENSE.txt': ('3840c5c0c61c294264d2dd77b8777be6ddd90121ef4e0e64abcd22edea581d6e', 11376, 'https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/v3.0.3/LICENSE'),
    'RAPIDOCR_LICENSE.txt': ('3e0af25fdd06aa9586ae97adb00ea927ebe5a3805ac77d2d3a81ce5f55693333', 11422, 'https://raw.githubusercontent.com/RapidAI/RapidOCR/v3.9.0/LICENSE'),
}
_lock = threading.RLock()
_verified = None
_engines = {}
_job = {}
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='evidenceflow-document-ocr')
_slot = threading.BoundedSemaphore(1)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def model_dir():
    return Path(os.environ.get('EVIDENCEFLOW_OCR_MODEL_DIR', str(rag_providers.model_dir()))).resolve()/MODEL_ID


def bundled_pack():
    return Path(os.environ.get('EVIDENCEFLOW_OCR_MODEL_PACK',
                              str(s.PROJECT_ROOT/'06_运行环境'/'OCR模型包.zip'))).resolve()


def initialize():
    """Provider has no private configuration table and no startup I/O."""
    return None


def _verify(root=None):
    global _verified
    root = root or model_dir()
    identity = []
    for name, (digest, size, _) in FILES.items():
        path = root/name
        if not digest or not size:
            raise ValueError('OCR 模型清单尚未完成审定')
        if not path.is_file() or path.is_symlink():
            raise ValueError('本机 OCR 模型未安装或不完整，请主动安装模型包')
        stat = path.stat()
        if stat.st_size != size:
            raise ValueError('OCR 模型文件大小不符，请重新安装')
        identity.append((name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    key = (str(root), tuple(identity))
    # Windows can retain identical size and timestamps for quick in-place writes.
    # Rehash even when the stat key matches; cached metadata is not proof of bytes.
    for name, (digest, _, _) in FILES.items():
        if s.digest(root/name) != digest:
            raise ValueError('OCR 模型 SHA256 不符，请重新安装')
    with _lock:
        _verified = key
    return key


def describe():
    provider = {'ready': False, 'local': True, 'mode': 'local', 'provider_id': MODEL_ID,
                'model': MODEL, 'model_revision': MODEL_REVISION, 'engine': 'rapidocr',
                'engine_version': ENGINE_VERSION, 'execution_provider': 'CPUExecutionProvider',
                'fingerprint': None, 'reason': '', 'error': None, 'location_precision': 'line'}
    try:
        actual = importlib.metadata.version('rapidocr')
        ort = importlib.metadata.version('onnxruntime')
        if actual != ENGINE_VERSION:
            raise ValueError('需要固定的 rapidocr '+ENGINE_VERSION+' 依赖')
        provider['onnxruntime_version'] = ort
        provider['fingerprint'] = _hash({'version': VERSION, 'engine': actual, 'onnxruntime': ort,
                                         'model': MODEL, 'revision': MODEL_REVISION, 'files': FILES,
                                         'score_threshold': LOW_SCORE, 'limits': LIMITS})
        _verify()
        provider['ready'] = True
    except (ValueError, OSError, importlib.metadata.PackageNotFoundError) as exc:
        provider.update(reason=str(exc), error=type(exc).__name__)
    return provider


def configuration(body=None):
    if body is not None:
        raise ValueError('OCR 使用审定固定模型；解析模式和连接请在任务中选择')
    options = []
    for d in c.listing():
        if d.get('kind') != 'vision':
            continue
        available = bool(d.get('enabled') and (d.get('last_test') or {}).get('ok')
                         and (not c.local_only() or c.is_local(d)))
        options.append({'id': d['id'], 'name': d['name'], 'model': d.get('model', ''),
                        'revision': d['revision'], 'local': c.is_local(d), 'ready': available,
                        'enabled': bool(d.get('enabled')), 'tested': bool((d.get('last_test') or {}).get('ok'))})
    return {'version': VERSION, 'read_only': True, 'local_ocr': describe(),
            'ocr_model': MODEL, 'model_id': MODEL_ID, 'limits': dict(LIMITS),
            'vision_connections': options, 'local_only': c.local_only(),
            'rotations': [0, 90, 180, 270], 'visual_schema': visual_schema()}


def models_status():
    installed = False; error = ''
    try:
        _verify(); installed = True
    except (ValueError, OSError) as exc:
        error = str(exc)
    with _lock:
        job = dict(_job)
    provider = describe()
    return {'items': [{'id': MODEL_ID, 'name': MODEL, 'role': 'ocr', 'installed': installed,
                       'verified': installed, 'size_bytes': sum(v[1] for v in FILES.values()),
                       'total_bytes': sum(v[1] for v in FILES.values()), 'license': 'Apache-2.0',
                       'source': MODEL_REVISION, 'revision': MODEL_REVISION,
                       'status': job.get('status') or ('completed' if installed else 'not_installed'),
                       'downloaded_bytes': job.get('downloaded_bytes', 0),
                       'error': job.get('error') or error, 'bundled': bundled_pack().is_file()}],
            'provider': provider, 'local_ocr': provider}


def _set_job(**values):
    with _lock:
        _job.update(values)


def _copy(source, destination, digest, size, done):
    h = hashlib.sha256(); count = 0; started = time.monotonic()
    with destination.open('wb') as output:
        for chunk in iter(lambda: source.read(1024*1024), b''):
            count += len(chunk)
            if count > size or time.monotonic()-started > 180:
                raise ValueError('OCR 公开模型文件超过大小或下载时限')
            h.update(chunk); output.write(chunk)
            _set_job(downloaded_bytes=done+count)
    if count != size or h.hexdigest() != digest:
        raise ValueError('OCR 模型文件大小或 SHA256 不符，未安装')
    return done+count


def _remove_stage(path, root):
    # Native Path operations only; resolve every recursive deletion boundary.
    resolved = path.resolve()
    if resolved.parent == root.resolve() and path.name.startswith(('.ocr-install-', '.ocr-old-')):
        shutil.rmtree(path, ignore_errors=True)


def _install(download):
    global _verified
    destination = model_dir(); root = destination.parent
    root.mkdir(parents=True, exist_ok=True)
    stage = root/('.ocr-install-'+uuid.uuid4().hex); stage.mkdir()
    done = 0; old = None
    _set_job(status='running', downloaded_bytes=0, error='', started_at=s.now())
    try:
        archive = bundled_pack()
        if archive.is_file():
            if archive.is_symlink():
                raise ValueError('OCR 模型包不能为符号链接')
            with zipfile.ZipFile(archive) as pack:
                names = pack.namelist()
                expected = {MODEL_ID+'/'+name for name in FILES}
                if len(names) != len(set(names)) or set(names) != expected:
                    raise ValueError('OCR 模型包文件清单不符或含重复条目')
                for name, (digest, size, _) in FILES.items():
                    info = pack.getinfo(MODEL_ID+'/'+name)
                    if info.file_size != size:
                        raise ValueError('OCR 模型包大小不符')
                    with pack.open(info) as source:
                        done = _copy(source, stage/name, digest, size, done)
        else:
            if not download:
                raise ValueError('缺少随包 OCR 模型，请主动同意公开模型下载')
            _set_job(status='downloading')
            with httpx.Client(timeout=httpx.Timeout(45, connect=10), follow_redirects=True, trust_env=False) as client:
                for name, (digest, size, url) in FILES.items():
                    with client.stream('GET', url) as response:
                        if response.status_code != 200:
                            raise ValueError(f'OCR 公开模型下载失败（HTTP {response.status_code}）')
                        class Reader:
                            def __init__(self):
                                self.chunks = response.iter_bytes(1024*1024)
                            def read(self, _):
                                return next(self.chunks, b'')
                        done = _copy(Reader(), stage/name, digest, size, done)
        _verify(stage)
        (stage/'manifest.json').write_text(c.encode({'id': MODEL_ID, 'model': MODEL,
            'revision': MODEL_REVISION, 'license': 'Apache-2.0', 'installed_at': s.now(),
            'files': {name: {'sha256': v[0], 'size_bytes': v[1], 'source': v[2]} for name, v in FILES.items()}}), encoding='utf-8')
        with _lock:
            if destination.exists():
                if destination.is_symlink():
                    raise ValueError('OCR 安装目录不能为符号链接')
                old = root/('.ocr-old-'+uuid.uuid4().hex); destination.rename(old)
            try:
                stage.rename(destination)
            except Exception:
                if old is not None:
                    old.rename(destination)
                raise
            _verified = None; _engines.clear()
        _verify()
        if old is not None:
            _remove_stage(old, root)
        _set_job(status='completed', downloaded_bytes=done, error='', completed_at=s.now())
    except Exception as exc:
        _set_job(status='failed', error=str(exc)[:300], completed_at=s.now())
    finally:
        if stage.exists():
            _remove_stage(stage, root)


def install(accepted: bool):
    if type(accepted) is not bool:
        raise ValueError('安装确认必须是布尔值')
    if not bundled_pack().is_file() and not accepted:
        raise ValueError('下载公开 OCR 模型需要主动确认；安装过程不发送文档')
    with _lock:
        if _job.get('status') not in {'pending', 'running', 'downloading'}:
            _job.clear(); _job.update(status='pending', downloaded_bytes=0, error='')
            threading.Thread(target=_install, args=(accepted,), daemon=True,
                             name='document-model-install').start()
    return models_status()


def _check(cancelled=None, deadline=None):
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError('文档解析已取消')
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError('文档模型调用超过总时限')


def _image(raw):
    from PIL import Image, UnidentifiedImageError
    if not isinstance(raw, bytes) or not raw or len(raw) > LIMITS['max_input_bytes']:
        raise ValueError('图片为空或超过 15 MB 限制')
    try:
        with Image.open(io.BytesIO(raw)) as im:
            width, height = im.size
            if not width or not height or max(width, height) > LIMITS['max_side'] or width*height > LIMITS['max_pixels']:
                raise ValueError('图片超过 2400 边长或 6 MP，请减小页面渲染尺寸')
            if getattr(im, 'n_frames', 1) != 1:
                raise ValueError('不支持多帧图片，请逐页提交')
            if im.format not in {'PNG', 'JPEG', 'WEBP', 'BMP', 'TIFF'}:
                raise ValueError('不支持的图片格式')
            im.load()
            return im.convert('RGB')
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError('图片无法解码') from None


class _GuardedSession:
    """Pass ORT RunOptions to the real session so cancellation terminates CPU work."""
    def __init__(self, session):
        self.session = session
        self.context = None
    def __getattr__(self, name):
        return getattr(self.session, name)
    def run(self, outputs, inputs, run_options=None):
        cancelled, deadline, options = self.context
        _check(cancelled, deadline)
        value = self.session.run(outputs, inputs, run_options=options)
        _check(cancelled, deadline)
        return value


def _load(provider):
    _verify()
    key = (str(model_dir()), provider['fingerprint'])
    with _lock:
        if key in _engines:
            return _engines[key]
        from rapidocr import RapidOCR, EngineType, ModelType, OCRVersion
        params = {'Global.text_score': 0.0, 'Global.max_side_len': 2400,
                  'Global.return_word_box': False, 'Global.return_single_char_box': False,
                  'Global.model_root_dir': str(model_dir()), 'Global.log_level': 'error',
                  'EngineConfig.onnxruntime.intra_op_num_threads': 2,
                  'EngineConfig.onnxruntime.inter_op_num_threads': 1,
                  'EngineConfig.onnxruntime.use_cuda': False, 'EngineConfig.onnxruntime.use_dml': False,
                  'EngineConfig.onnxruntime.use_cann': False, 'EngineConfig.onnxruntime.use_coreml': False,
                  'Det.limit_type': 'max', 'Det.limit_side_len': 2400, 'Det.max_candidates': 20000,
                  'Rec.rec_keys_path': str(model_dir()/'ppocrv5_dict.txt')}
        for role in ('Det', 'Rec', 'Cls'):
            params.update({role+'.model_path': str(model_dir()/(role.lower()+'.onnx')),
                           role+'.engine_type': EngineType.ONNXRUNTIME,
                           role+'.model_type': ModelType.MOBILE,
                           role+'.ocr_version': OCRVersion.PPOCRV5})
        engine = RapidOCR(params=params)
        sessions = []
        for part in (engine.text_det, engine.text_cls, engine.text_rec):
            session = part.session.session
            if session.get_providers() != ['CPUExecutionProvider']:
                raise ValueError('OCR 模型必须仅使用本机 CPU 推理')
            guard = _GuardedSession(session)
            part.session.session = guard; sessions.append(guard)
        # Embedded ONNX alphabet is what RapidOCR actually decodes. Match it to
        # the audited external alphabet as well; supplying a path alone is not enough.
        embedded = engine.text_rec.session.get_character_list()
        external = (model_dir()/'ppocrv5_dict.txt').read_text(encoding='utf-8').splitlines()
        if embedded != external:
            raise ValueError('OCR 模型内置字典与审定中文字符字典不一致')
        detect = engine.detect_and_crop
        def bounded_detect(*args, **kwargs):
            crops, result = detect(*args, **kwargs)
            if len(crops) > LIMITS['max_lines']:
                raise ValueError('OCR 超过 400 行，请缩小页面范围；未截断内容')
            return crops, result
        engine.detect_and_crop = bounded_detect
        _engines[key] = (engine, sessions)
        return _engines[key]


def _still_current(provider):
    latest = describe()
    if not latest['ready'] or latest['fingerprint'] != provider['fingerprint']:
        raise ValueError('OCR 模型或依赖身份已改变，请重新准备解析')


def _original_point(x, y, width, height, rotation):
    if rotation == 90:
        return [y/width, 1-x/height]
    if rotation == 180:
        return [1-x/width, 1-y/height]
    if rotation == 270:
        return [1-y/width, x/height]
    return [x/width, y/height]


def ocr(raw: bytes, rotation: int = 0, cancelled=None, deadline=None):
    if type(rotation) is not int or rotation not in {0, 90, 180, 270}:
        raise ValueError('页面旋转必须为 0/90/180/270 度')
    started = time.monotonic()
    deadline = min(deadline if deadline is not None else started+LIMITS['ocr_timeout_seconds'], started+LIMITS['ocr_timeout_seconds'])
    _check(cancelled, deadline)
    provider = describe()
    if not provider['ready']:
        raise ValueError(provider['reason'])
    image = _image(raw); width, height = image.size
    from PIL import Image
    if rotation:
        image = image.transpose({90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                                 270: Image.Transpose.ROTATE_90}[rotation])
    import numpy as np
    # RapidOCR ndarray input is BGR, while PIL's decoded raster is RGB.
    pixels = np.asarray(image)[:, :, ::-1].copy()
    import onnxruntime as ort
    options = ort.RunOptions()
    while not _slot.acquire(timeout=.05):
        _check(cancelled, deadline)
    def infer():
        try:
            _check(cancelled, deadline); _still_current(provider)
            engine, sessions = _load(provider)
            for session in sessions:
                session.context = (cancelled, deadline, options)
            result = engine(pixels, return_word_box=False, return_single_char_box=False)
            _check(cancelled, deadline); _still_current(provider)
            return result
        except (ValueError, InterruptedError, TimeoutError):
            raise
        except Exception as exc:
            _check(cancelled, deadline)
            raise ValueError('本机 OCR 推理失败：'+type(exc).__name__) from None
    future = _pool.submit(infer)
    future.add_done_callback(lambda _: _slot.release())
    try:
        while True:
            _check(cancelled, deadline)
            try:
                result = future.result(timeout=.05); break
            except FutureTimeout:
                if future.done():
                    raise
    except (InterruptedError, TimeoutError):
        options.terminate = True; future.cancel(); raise
    _check(cancelled, deadline); _still_current(provider)
    boxes = result.boxes
    texts = result.txts
    scores = result.scores
    if texts is None:
        texts = (); boxes = (); scores = ()
    if boxes is None or scores is None or not len(boxes) == len(texts) == len(scores):
        raise ValueError('OCR 输出文本、坐标与分数数量不一致')
    if len(texts) > LIMITS['max_lines'] or sum(len(t) for t in texts if isinstance(t, str)) > LIMITS['max_characters']:
        raise ValueError('OCR 输出超过行数或文字限制；未截断内容')
    lines = []; issues = []
    rw, rh = image.size
    for index, (text, corners, score) in enumerate(zip(texts, boxes, scores)):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('OCR 输出含空行、非法文本或超长行')
        if isinstance(score, (bool, np.bool_)) or not isinstance(score, (int, float, np.integer, np.floating)):
            raise ValueError('OCR 输出置信度类型无效')
        score = float(score)
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError('OCR 输出含无效置信度')
        array = np.asarray(corners)
        if array.shape != (4, 2) or array.dtype.kind not in {'f', 'i', 'u'}:
            raise ValueError('OCR 输出不是四点行框')
        polygon = []
        for x, y in corners:
            x, y = float(x), float(y)
            if not math.isfinite(x+y) or not -1e-5 <= x <= rw+1e-5 or not -1e-5 <= y <= rh+1e-5:
                raise ValueError('OCR 行坐标越界或非有限')
            px, py = _original_point(min(rw, max(0, x)), min(rh, max(0, y)), width, height, rotation)
            polygon.append([round(px, 8), round(py, 8)])
        box = [min(p[0] for p in polygon), min(p[1] for p in polygon),
               max(p[0] for p in polygon), max(p[1] for p in polygon)]
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError('OCR 行坐标面积无效')
        status = 'pending' if score < LOW_SCORE else 'confirmed'
        lines.append({'text': text, 'box': box, 'polygon': polygon, 'confidence': score,
                      'status': status, 'method': 'ocr', 'line_id': index, 'location_precision': 'line'})
        if status == 'pending':
            issues.append({'code': 'low_ocr_score', 'message': 'OCR 低分文字需要复核', 'box': box, 'line_id': index})
    if not lines:
        issues.append({'code': 'no_text', 'message': '本机 OCR 未识别到文字，请检查页面清晰度与旋转'})
    duration = round((time.monotonic()-started)*1000)
    provenance = {'method': 'ocr', 'model': MODEL, 'fingerprint': provider['fingerprint'],
                  'engine_version': ENGINE_VERSION, 'model_revision': MODEL_REVISION,
                  'input_image_sha256': hashlib.sha256(raw).hexdigest(), 'rotation': rotation,
                  'width': width, 'height': height, 'duration_ms': duration, 'local': True,
                  'location_precision': 'line', 'confidence_scope': 'uncalibrated_recognition_score'}
    _check(cancelled, deadline); _still_current(provider)
    return {'lines': lines, 'issues': issues, 'method': 'ocr', 'model': MODEL,
            'fingerprint': provider['fingerprint'], 'duration_ms': duration,
            'rotation': rotation, 'input_image_sha256': provenance['input_image_sha256'],
            'provenance': provenance, 'provider': provider, 'width': width, 'height': height}


def visual_schema():
    from . import visual_formula
    return {'blocks': [{'text': 'string', 'box': '[x0,y0,x1,y1] in 0..1', 'kind': 'text|title|caption|header|footer|list|note'}],
            'formulas': [visual_formula.schema()],
            'tables': [{'title': 'string', 'box': '[x0,y0,x1,y1]', 'rows': 'integer', 'columns': 'integer',
                        'cells': [{'row': 'zero-based integer', 'column': 'zero-based integer', 'rowspan': 'integer >=1',
                                   'colspan': 'integer >=1', 'text': 'string; empty is distinct from zero',
                                   'box': '[x0,y0,x1,y1]', 'role': 'column_header|row_header|data|header|body|stub|corner|unknown'}]}],
            'charts': [{'title': 'string', 'kind': 'bar|line|pie|scatter|area|stacked_bar|stacked_area|donut|other',
                        'box': '[x0,y0,x1,y1]', 'x_label': 'string', 'y_label': 'string', 'unit': 'string',
                        'series': [{'name': 'string', 'axis': 'primary|secondary|unknown', 'color': 'string',
                                    'points': [{'label': 'string', 'value': 'string or null', 'printed_text': 'string',
                                                'box': '[x0,y0,x1,y1]', 'value_source': 'printed|estimated|unknown'}]}]}],
            'issues': [{'code': 'string', 'message': 'string', 'box': 'optional [x0,y0,x1,y1]'}],
            'provenance': {'notes': 'optional string'}}


def _object(value, required, optional=()):
    if not isinstance(value, dict) or set(value)-set(required)-set(optional) or set(required)-set(value):
        raise ValueError('视觉 JSON 字段缺失或包含未知字段')
    return value


def _text(value, maximum=2000):
    if not isinstance(value, str) or len(value) > maximum or any(ord(ch) < 32 and ch not in '\n\t\r' for ch in value):
        raise ValueError('视觉 JSON 文本类型、长度或控制字符无效')
    return value


def _integer(value, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError('视觉 JSON 整数类型或范围无效')
    return value


def _box(value, parent=None):
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError('视觉区域必须是四个归一化坐标')
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in value):
        raise ValueError('视觉区域坐标越界、非有限或类型无效')
    box = [float(v) for v in value]
    if box[0] >= box[2] or box[1] >= box[3]:
        raise ValueError('视觉区域面积无效')
    if parent and (box[0] < parent[0] or box[1] < parent[1] or box[2] > parent[2] or box[3] > parent[3]):
        raise ValueError('视觉子区域不在父区域内')
    return box


def _list(value, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError('视觉 JSON 列表类型或数量无效')
    return value


def _strict_json(text):
    if not isinstance(text, str) or not text.strip() or len(text) > LIMITS['max_response_characters']:
        raise ValueError('视觉响应为空、超限或已被传输截断')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('视觉 JSON 包含重复字段')
            result[key] = value
        return result
    def invalid(_):
        raise ValueError('视觉 JSON 含非有限数值')
    try:
        body = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    except (json.JSONDecodeError, RecursionError):
        raise ValueError('视觉响应不是严格 JSON 对象') from None
    return _object(body, {'blocks', 'tables', 'charts', 'issues', 'provenance'}, {'formulas'})


def _canonical(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value).replace('−', '-').replace('–', '-'))


_NUMBERS = re.compile(r'(?<![\d.])[+-]?(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)(?:%|‰)?(?![\d.])')


def _numeric_tokens(text):
    # Keep spaces/newlines: stripping them would merge adjacent labels such as
    # "2026 120" into the invented number "2026120".
    return _NUMBERS.findall(unicodedata.normalize('NFKC', text).replace('−', '-').replace('–', '-'))


def _observations(lines):
    if not isinstance(lines, list) or len(lines) > 6000:
        raise ValueError('独立文字观测列表无效或超限')
    result = []; total = 0
    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            raise ValueError('独立文字观测无效')
        text = _text(line.get('text'), 4000); box = _box(line.get('box', line.get('bbox')))
        total += len(text)
        if total > 48000:
            raise ValueError('独立文字观测超过总文字限制')
        score = line.get('confidence', 1)
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError('独立文字观测置信度无效')
        if text.strip() and line.get('status') != 'pending' and score >= LOW_SCORE:
            identity = line.get('line_id', index)
            if type(identity) not in (str, int) or (isinstance(identity, str) and len(identity) > 100):
                raise ValueError('独立文字观测身份无效')
            result.append({'text': text, 'box': box, 'id': identity})
    return result


def _evidence(text, box, observed):
    """Use only observations mostly within this region, never the whole page."""
    selected = []
    for line in observed:
        b = line['box']
        overlap = max(0, min(box[2], b[2])-max(box[0], b[0]))*max(0, min(box[3], b[3])-max(box[1], b[1]))
        if overlap/((b[2]-b[0])*(b[3]-b[1])) >= .8:
            selected.append(line)
    selected.sort(key=lambda line: (round(line['box'][1], 3), line['box'][0]))
    target = _canonical(text)
    source_text = '\n'.join(line['text'] for line in selected)
    material = _canonical(source_text)
    if not target or not material:
        return {'status': 'pending', 'verification': 'no_same_region_observation', 'evidence_lines': []}
    numbers = _numeric_tokens(text)
    present = _numeric_tokens(source_text)
    supported = target in material and all(number in present for number in numbers)
    return {'status': 'confirmed' if supported else 'pending',
            'verification': 'same_region_observation' if supported else 'observation_conflict',
            'evidence_lines': [line['id'] for line in selected] if supported else []}


def _chart_association(point, observed):
    """Confirm only a complete, local observed category/value label pair.

    Separate axis labels and value labels need further geometric validation;
    merely finding both somewhere in a chart cannot prove their association.
    OCR supplies line geometry, so we never invent character boxes to pair them.
    """
    label = _canonical(point['label']); printed = _canonical(point['printed_text'])
    value = _canonical(point['value'] or '')
    if not label or not printed or not value:
        return {'status': 'pending', 'verification': 'unverified_chart_association', 'evidence_lines': []}
    pairs = {label+value, value+label}
    # A line containing several categories/values cannot establish a pair.
    if printed not in pairs | {value}:
        return {'status': 'pending', 'verification': 'unverified_chart_association', 'evidence_lines': []}
    for line in observed:
        if '\n' in line['text'] or '\r' in line['text']:
            continue
        b = line['box']; box = point['box']
        overlap = max(0, min(box[2], b[2])-max(box[0], b[0]))*max(0, min(box[3], b[3])-max(box[1], b[1]))
        if overlap/((b[2]-b[0])*(b[3]-b[1])) < .8:
            continue
        material = _canonical(line['text'])
        if material in pairs and all(n in _numeric_tokens(line['text']) for n in _numeric_tokens(point['printed_text'])):
            return {'status': 'confirmed', 'verification': 'same_local_observed_label_pair', 'evidence_lines': [line['id']]}
    return {'status': 'pending', 'verification': 'unverified_chart_association', 'evidence_lines': []}


def _validate_visual(body, observed):
    blocks = []; tables = []; charts = []; issues = []; confirmed = []
    from . import visual_formula
    formulas = []
    for formula in _list(body.get('formulas', []), 20):
        _object(formula, {'box', 'tree'}, {'coordinate_space'})
        item = visual_formula.parse(formula['tree'])
        space=formula.get('coordinate_space','normalized')
        if space not in {'normalized','normalized_1000'}:raise ValueError('公式坐标空间无效')
        box=formula['box']
        if space=='normalized_1000':
            if not isinstance(box,list) or len(box)!=4 or any(type(x) not in (int,float) or not math.isfinite(x) or not 0<=x<=1000 for x in box):raise ValueError('公式归一化千分坐标无效')
            box=[x/1000 for x in box]
        item['box'] = _box(box)
        item['coordinate_space']='normalized'
        item['model_box']=formula['box'];item['model_coordinate_space']=space
        formulas.append(item)
        issues.append({'code':'formula_structure_incomplete', 'message':item['reason'], 'box':item['box']})
    def verify(item, text, box):
        verdict = _evidence(text, box, observed); item.update(verdict)
        if verdict['status'] == 'confirmed':
            confirmed.append(text)
        else:
            issues.append({'code': verdict['verification'], 'message': '视觉提取文字缺少同区域独立观测或与观测不一致，待复核', 'box': box})
    for block in _list(body['blocks'], 200):
        _object(block, {'text', 'box', 'kind'})
        item = {'text': _text(block['text'], 4000), 'box': _box(block['box']), 'kind': _text(block['kind'], 30)}
        if item['kind'] not in {'text', 'title', 'caption', 'header', 'footer', 'list', 'note'}:
            raise ValueError('视觉文字块类型无效')
        verify(item, item['text'], item['box']); blocks.append(item)
    for table in _list(body['tables'], 20):
        _object(table, {'title', 'box', 'rows', 'columns', 'cells'})
        item = {'title': _text(table['title'], 300), 'box': _box(table['box']),
                'rows': _integer(table['rows'], 1, 100), 'columns': _integer(table['columns'], 1, 50), 'cells': []}
        if item['rows']*item['columns'] > 2000:
            raise ValueError('视觉表格网格超过 2000 格')
        occupied = set()
        for cell in _list(table['cells'], 1000):
            _object(cell, {'row', 'column', 'rowspan', 'colspan', 'text', 'box', 'role'})
            row = _integer(cell['row'], 0, item['rows']-1); column = _integer(cell['column'], 0, item['columns']-1)
            rowspan = _integer(cell['rowspan'], 1, item['rows']); colspan = _integer(cell['colspan'], 1, item['columns'])
            if row+rowspan > item['rows'] or column+colspan > item['columns']:
                raise ValueError('视觉表格单元格跨度越界')
            covered = {(r, col) for r in range(row, row+rowspan) for col in range(column, column+colspan)}
            if covered & occupied:
                raise ValueError('视觉表格网格重叠或单元格重复')
            occupied |= covered
            role = _text(cell['role'], 30)
            if role not in {'column_header', 'row_header', 'data', 'header', 'body', 'stub', 'corner', 'unknown'}:
                raise ValueError('视觉表格单元格角色无效')
            value = {'row': row, 'column': column, 'rowspan': rowspan, 'colspan': colspan,
                     'text': _text(cell['text'], 2000), 'box': _box(cell['box'], item['box']), 'role': role}
            for previous in item['cells']:
                a, b = value['box'], previous['box']
                area = max(0, min(a[2], b[2])-max(a[0], b[0]))*max(0, min(a[3], b[3])-max(a[1], b[1]))
                if area > 1e-8:
                    raise ValueError('视觉表格单元格区域重叠，与独立网格冲突')
            verify(value, value['text'], value['box']); item['cells'].append(value)
        if len(occupied) != item['rows']*item['columns']:
            raise ValueError('视觉表格缺少网格单元格；空白格必须明确保留')
        title_verdict = _evidence(item['title'], item['box'], observed)
        item['title_status'] = title_verdict['status'] if item['title'] else 'pending'
        item['status'] = 'confirmed' if all(cell['status'] == 'confirmed' for cell in item['cells']) and (not item['title'] or title_verdict['status'] == 'confirmed') else 'pending'
        item['structure_status'] = 'schema_valid'; tables.append(item)
    for chart in _list(body['charts'], 20):
        _object(chart, {'title', 'kind', 'box', 'x_label', 'y_label', 'unit', 'series'})
        item = {key: _text(chart[key], 300) for key in ('title', 'kind', 'x_label', 'y_label', 'unit')}
        item['box'] = _box(chart['box']); item['series'] = []
        if item['kind'] not in {'bar', 'line', 'pie', 'scatter', 'area', 'stacked_bar', 'stacked_area', 'donut', 'other'}:
            raise ValueError('视觉图表类型无效')
        item['metadata_status'] = {key: _evidence(item[key], item['box'], observed)['status']
                                   for key in ('title', 'x_label', 'y_label', 'unit')}
        for series in _list(chart['series'], 30):
            _object(series, {'name', 'axis', 'color', 'points'})
            series_out = {key: _text(series[key], 100) for key in ('name', 'axis', 'color')}
            if series_out['axis'] not in {'primary', 'secondary', 'unknown'}:
                raise ValueError('视觉图表系列轴身份无效')
            series_out['name_status'] = _evidence(series_out['name'], item['box'], observed)['status']
            series_out['points'] = []
            for point in _list(series['points'], 200):
                _object(point, {'label', 'value', 'printed_text', 'box', 'value_source'})
                value = point['value']
                if value is not None:
                    value = _text(value, 100)
                    if not value.strip():
                        raise ValueError('不可读图点必须为 null，不能为空值字符串')
                point_out = {'label': _text(point['label'], 300), 'value': value,
                             'printed_text': _text(point['printed_text'], 400),
                             'box': _box(point['box'], item['box']), 'value_source': _text(point['value_source'], 30)}
                if point_out['value_source'] not in {'printed', 'estimated', 'unknown'}:
                    raise ValueError('视觉图点数值来源无效')
                verdict = _evidence(point_out['printed_text'], point_out['box'], observed)
                number_supported = value is not None and bool(_NUMBERS.fullmatch(unicodedata.normalize('NFKC', value).strip()))
                number_supported &= value is not None and _canonical(value) in _canonical(point_out['printed_text'])
                if value is not None and _numeric_tokens(value):
                    number_supported &= all(n in _numeric_tokens(point_out['printed_text']) for n in _numeric_tokens(value))
                association = _chart_association(point_out, observed)
                point_out['association_status'] = association['status']
                metadata_supported = series_out['name_status'] == 'confirmed' and series_out['axis'] != 'unknown'
                metadata_supported &= not item['title'] or item['metadata_status']['title'] == 'confirmed'
                metadata_supported &= bool(item['unit']) and item['metadata_status']['unit'] == 'confirmed'
                if item['kind'] not in {'pie', 'donut'}:
                    metadata_supported &= bool(item['y_label']) and item['metadata_status']['y_label'] == 'confirmed'
                    metadata_supported &= bool(item['x_label']) and item['metadata_status']['x_label'] == 'confirmed'
                if point_out['value_source'] != 'printed' or not number_supported or verdict['status'] != 'confirmed' or association['status'] != 'confirmed' or not metadata_supported:
                    verdict = {'status': 'pending', 'verification': 'estimated_or_unobserved_chart_value', 'evidence_lines': []}
                    issues.append({'code': verdict['verification'], 'message': '图点没有可核对的印刷数值和类别；估读不进入确定性内容', 'box': point_out['box']})
                else:
                    confirmed.append(point_out['printed_text'])
                point_out.update(verdict); series_out['points'].append(point_out)
            series_out['status'] = 'confirmed' if series_out['name_status'] == 'confirmed' and series_out['points'] and all(p['status'] == 'confirmed' for p in series_out['points']) else 'pending'
            item['series'].append(series_out)
        item['status'] = 'confirmed' if item['series'] and all(v['status'] == 'confirmed' for v in item['series']) else 'pending'
        charts.append(item)
    for issue in _list(body['issues'], 100):
        _object(issue, {'code', 'message'}, {'box'})
        item = {'code': _text(issue['code'], 80), 'message': _text(issue['message'], 500)}
        if 'box' in issue:
            item['box'] = _box(issue['box'])
        issues.append(item)
    _object(body['provenance'], set(), {'notes'})
    notes = _text(body['provenance'].get('notes', ''), 1000)
    if sum(len(t) for t in confirmed) > LIMITS['max_characters']:
        raise ValueError('视觉确定性文字超过总文字限制')
    return {'blocks': blocks, 'tables': tables, 'charts': charts, 'formulas': formulas, 'issues': issues,
            'confirmed_text': '\n'.join(dict.fromkeys(confirmed)), 'model_notes': notes}


def _vision_current(connection_id, revision):
    if type(revision) is not int or revision < 1:
        raise ValueError('视觉连接预期版本无效')
    provider = c.ready(connection_id, {'vision'})
    if provider['revision'] != revision:
        raise ValueError('视觉连接已改变，请重新预览')
    return provider


def _visual_call(provider, messages, raw, purpose, cancelled, deadline, trace=None, max_tokens=5500):
    _check(cancelled,deadline)
    _vision_current(provider['id'],provider['revision'])
    provider={**provider,'timeout':max(.01,min(provider['timeout'],deadline-time.monotonic()))}
    event={'id':uuid.uuid4().hex,'method':'visual_model','purpose':purpose,'status':'running',
           'started_at':s.now(),'duration_ms':None,'provider_id':provider['id'],
           'model':provider['model'],'input_image_sha256':hashlib.sha256(raw).hexdigest(),'sent_bytes':len(raw)}
    def emit():
        if trace:trace(copy.deepcopy(event))
    started=time.monotonic();emit()
    try:
        response=c.chat(provider,messages,max_tokens=max_tokens,cancelled=cancelled)
        event.update(status='completed',actual_model=response['model'],usage=response.get('usage',{}))
        return response,event
    except (InterruptedError,TimeoutError) as exc:
        event.update(status='cancelled' if isinstance(exc,InterruptedError) else 'failed',error=type(exc).__name__);raise
    except Exception as exc:
        detail=str(exc)
        if provider.get('_secret'):detail=detail.replace(provider['_secret'],'[REDACTED]')
        event.update(status='failed',error=detail[:250]);raise
    finally:
        event.update(duration_ms=round((time.monotonic()-started)*1000),finished_at=s.now());emit()


def _reread_visual_formulas(raw, result, provider, cancelled, deadline, trace):
    from . import visual_formula
    from PIL import Image
    records=[]
    with Image.open(io.BytesIO(raw)) as opened:image=opened.convert('RGB')
    width,height=image.size
    for index,formula in enumerate(result['formulas']):
        kinds=formula['structure_audit']['kinds']
        complex_math=(not formula['structure_complete'] or kinds.get('large_operator',0)>0 or
                      kinds.get('fraction',0)>1 or kinds.get('scripts',0)>1)
        if not complex_math:continue
        if len(records)>=FORMULA_REREAD_LIMIT:break
        if deadline-time.monotonic()<10:
            records.append({'formula_index':index,'status':'not_run_budget','duration_ms':0});break
        _check(cancelled,deadline)
        b=formula['box']
        # A model box can end at a fraction bar and omit a bound's descender.
        # Preserve a margin relative to formula height, not only page height.
        padx=max(.01,.04*(b[2]-b[0]));pady=max(.025,.30*(b[3]-b[1]))
        rect=(max(0,int((b[0]-padx)*width)),max(0,int((b[1]-pady)*height)),
              min(width,int((b[2]+padx)*width)+1),min(height,int((b[3]+pady)*height)+1))
        crop=image.crop(rect);scale=max(1,min(4,1400/max(crop.size)))
        crop=crop.resize((max(1,round(crop.width*scale)),max(1,round(crop.height*scale))))
        buffer=io.BytesIO();crop.save(buffer,format='PNG');payload=buffer.getvalue()
        prompt=('仅识别此公式放大图，返回一个严格JSON对象，不要代码围栏或额外说明。'
                '顶层仅允许blocks,tables,charts,formulas,issues,provenance六个字段。'
                'blocks,tables,charts,issues均为[]，provenance必须为{}；formulas恰含一个公式。'
                '不能求解、补全、纠正常见写法或把上下标拆为正文。'
                '模糊/裁切内容必须unknown；不存在的脚标/上下限才用null。公式示例：'+
                json.dumps(visual_formula.schema(),ensure_ascii=False)+visual_formula.instructions())
        geometry=visual_formula.geometry_hints(payload) if kinds.get('large_operator',0) else {'available':False}
        if geometry.get('pairs'):
            prompt+=('Local measured ink positions: '+json.dumps(geometry,ensure_ascii=False)+
                     'Use these only to inspect raised/lowered glyphs relative to their adjacent base, not to infer character identities. '
                     'If the identity or association is unclear, keep unknown instead of using familiar notation. '
                     'These hints are input only: never copy them into provenance or add output fields.')
        prompt+='Return raw JSON only, no markdown fences; provenance must be {}.'
        messages=[{'role':'user','content':[{'type':'text','text':prompt},
            {'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(payload).decode()}}]}]
        record={'formula_index':index,'source_region_pixels':list(rect),'source_image_sha256':hashlib.sha256(raw).hexdigest(),
                'input_image_sha256':hashlib.sha256(payload).hexdigest(),'sent_bytes':len(payload),'scale':scale,'geometry_hints':geometry}
        # A failed re-read must not erase a contradiction already visible locally.
        screened_tree,source_audit=visual_formula.audit_geometry(formula['tree'],geometry)
        if source_audit['conflicts']:
            screened=copy.deepcopy(formula);screened.update(visual_formula.parse(screened_tree))
            screened.update(page_candidate={k:copy.deepcopy(formula[k]) for k in
                ('latex','tree','box','structure_audit','structure_complete')},geometry_audit=source_audit,
                reason='本机像素位置与模型上下标结构冲突，已保留未知节点，须对照原图核验')
            result['formulas'][index]=screened
        record['source_geometry_audit']=source_audit
        started=time.monotonic()
        try:
            _image(payload)
            response,call=_visual_call(provider,messages,payload,'formula_zoom',cancelled,deadline,trace,max_tokens=3500)
            record.update(call_id=call['id'],usage=response.get('usage',{}),actual_model=response['model'],raw_response=response['text'])
            candidates=_validate_visual(_strict_json(response['text']),[])['formulas']
            if len(candidates)!=1:raise ValueError('局部复读未返回唯一完整公式候选')
            candidate=candidates[0];relative=candidate['box']
            audited_tree,geometry_audit=visual_formula.audit_geometry(candidate['tree'],geometry)
            if geometry_audit['conflicts']:
                candidate.update(visual_formula.parse(audited_tree))
            candidate['geometry_audit']=geometry_audit
            candidate['box']=[(rect[0]+relative[0]*(rect[2]-rect[0]))/width,(rect[1]+relative[1]*(rect[3]-rect[1]))/height,
                              (rect[0]+relative[2]*(rect[2]-rect[0]))/width,(rect[1]+relative[3]*(rect[3]-rect[1]))/height]
            original={k:copy.deepcopy(formula[k]) for k in ('latex','tree','box','structure_audit','structure_complete')}
            candidate['page_candidate']=original
            candidate['zoom_verification']='same_model_consistency' if _canonical(candidate['latex'])==_canonical(formula['latex']) else 'same_model_disagreement'
            candidate['zoom_candidate_box']=relative;candidate['zoom_source_region_pixels']=list(rect)
            candidate['reason']=('本机像素位置与模型上下标结构冲突，已保留未知节点，须对照原图核验'
                if geometry_audit['conflicts'] else '局部放大公式候选仍须对照原图确认；同模型一致或分歧都不证明正确')
            result['formulas'][index]=candidate
            record.update(status='completed',candidate_latex=candidate['latex'],verification=candidate['zoom_verification'])
        except (InterruptedError,TimeoutError):raise
        except (ValueError,HTTPException) as exc:
            _check(cancelled,deadline)
            record.update(status='failed',error=str(exc)[:250])
        finally:
            record['duration_ms']=round((time.monotonic()-started)*1000);records.append(record)
    result['formula_rereads']=records
    return result


def visual(raw, connection_id, expected_revision, observed_lines, cancelled=None, deadline=None, trace=None):
    started = time.monotonic()
    calls=[]
    def record_call(event):
        current=next((item for item in calls if item['id']==event['id']),None)
        if current is None:calls.append(copy.deepcopy(event))
        else:current.update(copy.deepcopy(event))
        if trace:trace(event)
    deadline = min(deadline if deadline is not None else started+LIMITS['vision_timeout_seconds'], started+LIMITS['vision_timeout_seconds'])
    _check(cancelled, deadline)
    provider = _vision_current(connection_id, expected_revision)
    image = _image(raw); observed = _observations(observed_lines)
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as original:
        mime={'PNG':'image/png','JPEG':'image/jpeg','WEBP':'image/webp'}.get(original.format)
    if mime is None:raise ValueError('视觉发送仅支持 PNG、JPEG、WEBP；请先转换图片并重新预览')
    schema = json.dumps(visual_schema(), ensure_ascii=False)
    prompt = ('只提取图片中可见的文字、表格网格、图表和二维公式。返回一个严格 JSON 对象，不要代码围栏或额外说明。'
              '坐标必须为原图归一化 [x0,y0,x1,y1]，范围0到1。只返回schema列出的字段。'
              '表格从0开始索引，合并格只列一次，所有网格含空白格必须被覆盖，不要按合计推算数字。'
              '图点不可读的value为null；value必须是字符串，印刷标签printed、像素估读estimated、不可读unknown。'
              '不要把估读当印刷标签，不生成原因或预测。provenance仅允许notes字段。schema: '+schema)
    prompt += ('公式放入formulas，不要拆入blocks。逐个保留分子、分母、根次数、上下标、求和/积分上下限和括号；'
               '不求解、不补全、不用领域知识猜测。tree只用上述节点和原图符号，不生成LaTeX命令。'
               '脚标/上下限确实不存在时才填null；模糊、裁切或无法判断时必须放unknown节点并说明原因。')
    from . import visual_formula
    prompt += visual_formula.instructions()
    messages = [{'role': 'user', 'content': [{'type': 'text', 'text': prompt},
                 {'type': 'image_url', 'image_url': {'url': 'data:'+mime+';base64,'+base64.b64encode(raw).decode()}}]}]
    # Independently re-read privacy/enabled/test/revision immediately before and
    # after sending. Existing transport additionally checks _required_kind.
    provider = _vision_current(connection_id, expected_revision)
    provider = {**provider, '_required_kind': 'vision',
                'timeout': max(.01, min(provider['timeout'], deadline-time.monotonic()))}
    _check(cancelled, deadline)
    try:
        response,primary_call = _visual_call(provider,messages,raw,'page',cancelled,deadline,record_call)
    except ValueError:
        _check(cancelled, deadline); raise
    _check(cancelled, deadline); _vision_current(connection_id, expected_revision)
    body = _strict_json(response['text'])
    result = _validate_visual(body, observed)
    result = _reread_visual_formulas(raw,result,provider,cancelled,deadline,record_call)
    _check(cancelled, deadline); _vision_current(connection_id, expected_revision)
    duration = round((time.monotonic()-started)*1000)
    usage = {key: value for key, value in response.get('usage', {}).items()
             if key in {'prompt_tokens', 'completion_tokens', 'total_tokens'} and type(value) is int and value >= 0}
    public_provider = {'provider_id': connection_id, 'connection_id': connection_id,
                       'connection_revision': expected_revision, 'model': provider['model'],
                       'actual_model': response['model'], 'local': c.is_local(provider), 'method': 'visual'}
    public_provider['fingerprint'] = _hash({'version': VERSION, **public_provider})
    provenance = {**public_provider, 'input_image_sha256': hashlib.sha256(raw).hexdigest(),
                  'sent_image_sha256': hashlib.sha256(raw).hexdigest(), 'usage': usage,
                  'duration_ms': duration, 'sent_image_bytes': len(raw),
                  'sent_bytes': len(raw), 'byte_scope': 'frozen_image_bytes_before_base64_transport',
                  'width': image.width, 'height': image.height, 'observation_count': len(observed),
                  'validation': 'schema_and_same_region_observation', 'model_notes': result.pop('model_notes')}
    rereads=result['formula_rereads']
    total_usage={key:sum(call.get('usage',{}).get(key,0) for call in calls if type(call.get('usage',{}).get(key)) is int)
                 for key in ('prompt_tokens','completion_tokens','total_tokens')}
    provenance.update(formula_reread_limit=FORMULA_REREAD_LIMIT,
        image_submission_attempts=len(calls),total_prepared_image_bytes=sum(call['sent_bytes'] for call in calls),
        formula_rereads=copy.deepcopy(rereads),primary_call_id=primary_call['id'],model_calls=calls,total_model_usage=total_usage)
    return {**result, 'provenance': provenance, 'provider': public_provider,
            'raw_response': response['text'], 'model': response['model'], 'method': 'visual',
            'input_image_sha256': provenance['input_image_sha256'], 'duration_ms': duration, 'usage': usage}
