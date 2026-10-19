"""Offline multilingual E5 inference. Model weights are explicit local artifacts.

No network downloads, lexical vectors or silently truncated token windows are
used here. Query and passage prefixes, masked mean pooling and L2 normalization
follow the original model card. Long passages cover every tokenizer window.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path


class EmbeddingError(RuntimeError):
    pass


def check_cancelled(cancelled):
    if cancelled():
        raise InterruptedError('检索已停止')


class LocalE5:
    dimension = 384

    def __init__(self, model_dir=None, *, threads=2):
        self.model_dir = Path(model_dir) if model_dir else Path(__file__).resolve().parents[2] / 'models' / 'multilingual-e5-small'
        self.threads = threads
        self._lock = threading.RLock()
        self._session = None
        self._tokenizer = None
        try:
            self.manifest = json.loads((self.model_dir / 'manifest.json').read_text())
            if self.manifest['dimension'] != self.dimension:
                raise ValueError('dimension')
            weight_sha = self.manifest['files']['onnx/model_quantized.onnx']['sha256']
            self.identity = {'model': self.manifest['model'], 'revision': self.manifest['revision'],
                             'weights_sha256': weight_sha, 'dimension': self.dimension,
                             'tokenizer_sha256': self.manifest['files']['tokenizer.json']['sha256'],
                             'algorithm': 'e5-prefix-mean-l2-all-windows512-stride64-v1', 'license': self.manifest['license']}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError('本地向量模型清单缺失或无效') from exc

    def _load(self):
        if self._session is not None:
            return
        try:
            import onnxruntime as ort
            ort.disable_telemetry_events()
            from tokenizers import Tokenizer
            for name in ('tokenizer.json', 'onnx/model_quantized.onnx'):
                with (self.model_dir / name).open('rb') as handle:
                    actual = hashlib.file_digest(handle, 'sha256').hexdigest()
                if actual != self.manifest['files'][name]['sha256']:
                    raise EmbeddingError('本地向量模型校验失败')
            options = ort.SessionOptions()
            options.intra_op_num_threads = self.threads
            options.inter_op_num_threads = 1
            options.log_severity_level = 3
            self._session = ort.InferenceSession(str(self.model_dir / 'onnx/model_quantized.onnx'),
                sess_options=options, providers=['CPUExecutionProvider'])
            self._tokenizer = Tokenizer.from_file(str(self.model_dir / 'tokenizer.json'))
            self._tokenizer.enable_truncation(max_length=512, stride=64)
        except EmbeddingError:
            raise
        except Exception as exc:
            self._session = None
            raise EmbeddingError('本地向量模型未能加载；请检查离线依赖与权重') from exc

    def embed(self, texts, *, kind='passage', cancelled=lambda: False):
        if kind not in ('passage', 'query'):
            raise ValueError('embedding kind')
        # Tokenizer/session are shared across jobs; a short bounded inference
        # window is the cancellation granularity, not a whole document.
        with self._lock:
            check_cancelled(cancelled)
            self._load()
            import numpy as np
            vectors = []
            for text in texts:
                check_cancelled(cancelled)
                encoded = self._tokenizer.encode(kind + ': ' + text)
                windows = [encoded, *encoded.overflowing]
                pooled = []
                for window in windows:
                    check_cancelled(cancelled)
                    feeds = {'input_ids': np.asarray([window.ids], dtype=np.int64),
                             'attention_mask': np.asarray([window.attention_mask], dtype=np.int64),
                             'token_type_ids': np.asarray([window.type_ids], dtype=np.int64)}
                    feeds = {i.name: feeds[i.name] for i in self._session.get_inputs()}
                    try:
                        output = self._session.run(None, feeds)[0]
                    except Exception as exc:
                        raise EmbeddingError('本地向量计算失败') from exc
                    mask = np.asarray(window.attention_mask, dtype=np.float32)
                    pooled.append((output[0] * mask[:, None]).sum(axis=0) / max(mask.sum(), 1))
                vector = np.asarray(pooled, dtype=np.float32).mean(axis=0)
                norm = float(np.linalg.norm(vector))
                if len(vector) != self.dimension or not np.isfinite(vector).all() or norm <= 0:
                    raise EmbeddingError('本地向量输出维度或数值无效')
                vectors.append((vector / norm).tolist())
            check_cancelled(cancelled)
            return vectors
