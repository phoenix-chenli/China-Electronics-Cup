"""Offline OCR behind a verified model pack. Recognition is never faked.

Pipeline: detection finds text boxes, recognition decodes them, and the result
becomes a new parse version with page-relative boxes so citations keep working.

Two rules this module enforces:

* Weights come only from a pack whose files match the operator-supplied manifest
  SHA-256. This module never downloads anything and never invents a hash.
* When no verified pack is installed, every caller keeps the honest
  "no extractable text, needs recognition" state. Re-reading a text layer is
  never reported as OCR.

Running the models needs ``onnxruntime``, which the portable runtime already
ships for retrieval. The per-pixel work lives here; the weights come from the
pack, so `recognize` stays untested against real weights until one is installed.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import threading
import zipfile
from pathlib import Path

from . import store as s

PROVIDER_VERSION = "ocr-ons-v1"
DETECTION_ROLE = "detection"
RECOGNITION_ROLE = "recognition"
REQUIRED_ROLES = (DETECTION_ROLE, RECOGNITION_ROLE)
ADAPTERS = ("dbnet_ctc",)
MAX_PART_BYTES = 512 * 1024 * 1024
MAX_PIXELS = 12_000_000

# Detection: pixels above this probability are text. Boxes narrower or shorter
# than BOX_MIN_PIXELS are dropped as noise.
DETECT_THRESHOLD = 0.30
BOX_MIN_PIXELS = 2
BOX_MIN_SCORE = 0.30
# A recognized line below this mean probability is kept but flagged, so the
# page can be marked "needs review" instead of presented as reliable.
LINE_MIN_SCORE = 0.60
RECOGNITION_HEIGHT = 48
RECOGNITION_WIDTH = 320

_lock = threading.RLock()
_sessions = {}


def model_dir():
    return Path(os.environ.get("EVIDENCEFLOW_OCR_MODEL_DIR", str(s.DATA / "models" / "ocr"))).resolve()


def pack_path():
    return Path(os.environ.get("EVIDENCEFLOW_OCR_MODEL_PACK", str(s.PROJECT_ROOT / "06_运行环境" / "OCR模型包.zip"))).resolve()


def manifest_path():
    return Path(os.environ.get("EVIDENCEFLOW_OCR_MANIFEST", str(s.PROJECT_ROOT / "06_运行环境" / "OCR模型包.manifest.json"))).resolve()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def runtime_available():
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False, "本机未提供 onnxruntime，无法执行本地识别"
    return True, ""


def manifest():
    """Read and validate the operator-supplied manifest. Returns (data, error)."""
    path = manifest_path()
    if not path.is_file():
        return None, "未提供 OCR 模型清单（06_运行环境/OCR模型包.manifest.json）"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "OCR 模型清单无法读取或不是有效 JSON"
    if not isinstance(data, dict):
        return None, "OCR 模型清单格式不正确"
    adapter = data.get("adapter")
    if adapter not in ADAPTERS:
        return None, "OCR 模型清单声明的适配器未实现：" + str(adapter)
    files = data.get("files")
    if not isinstance(files, list) or not files:
        return None, "OCR 模型清单缺少文件摘要列表"
    roles = set()
    for item in files:
        if not isinstance(item, dict):
            return None, "OCR 模型清单的文件项格式不正确"
        name, sha = item.get("path"), item.get("sha256")
        if not isinstance(name, str) or not name or Path(name).name != name:
            return None, "OCR 模型清单包含非法文件名"
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            return None, "OCR 模型清单包含无效 SHA-256：" + str(name)
        role = item.get("role")
        if role in REQUIRED_ROLES:
            roles.add(role)
    missing = [r for r in REQUIRED_ROLES if r not in roles]
    if missing:
        return None, "OCR 模型清单缺少必需角色：" + "、".join(missing)
    if not data.get("license"):
        return None, "OCR 模型清单未标注许可证"
    return data, ""


def verify_pack():
    """Check the archive digest before any byte of it is trusted."""
    data, error = manifest()
    if error:
        return None, error
    pack = pack_path()
    if not pack.is_file():
        return None, "未找到 OCR 模型包（06_运行环境/OCR模型包.zip）"
    expected = (data.get("pack_sha256") or "").strip().lower()
    if len(expected) != 64:
        return None, "OCR 模型清单未提供模型包 SHA-256"
    actual = digest(pack)
    if actual != expected:
        return None, "OCR 模型包摘要与清单不一致，已停止安装"
    return data, ""


def installed():
    marker = model_dir() / ".complete"
    if not marker.is_file():
        return None
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return recorded if isinstance(recorded, dict) else None


def capabilities():
    """Report exactly why OCR is or is not usable. Never optimistic."""
    state = installed()
    ok, reason = runtime_available()
    base = {"provider": PROVIDER_VERSION, "ready": False, "adapter": None, "pack_sha256": None,
            "installed_at": None, "models": [], "license": None, "reason": ""}
    if state:
        base.update(adapter=state.get("adapter"), pack_sha256=state.get("pack_sha256"),
                    installed_at=state.get("installed_at"),
                    models=[{"path": m["path"], "role": m.get("role"), "sha256": m["sha256"]}
                            for m in state.get("files", [])],
                    license=state.get("license"))
    if not ok:
        base["reason"] = reason
        return base
    if not state:
        _, error = verify_pack()
        base["reason"] = error or "OCR 模型尚未安装，扫描页保持待复核"
        return base
    base["ready"] = True
    return base


def install():
    """Verify, then extract into the user cache. Refuses on any mismatch."""
    with _lock:
        data, error = verify_pack()
        if error:
            raise ValueError(error)
        target = model_dir()
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / ("ocr-install-" + hashlib.sha256(os.urandom(16)).hexdigest()[:12])
        try:
            with zipfile.ZipFile(pack_path()) as archive:
                for item in archive.infolist():
                    if item.filename.startswith(("/", "\\")) or ".." in Path(item.filename).parts:
                        raise ValueError("OCR 模型包包含非法路径")
                    if item.file_size > MAX_PART_BYTES:
                        raise ValueError("OCR 模型包包含过大的文件")
                archive.extractall(staging)
            for item in data["files"]:
                candidate = staging / item["path"]
                if not candidate.is_file():
                    raise ValueError("OCR 模型包缺少文件：" + item["path"])
                if digest(candidate) != item["sha256"]:
                    raise ValueError("OCR 模型文件摘要不一致：" + item["path"])
            record = {"provider": PROVIDER_VERSION, "adapter": data["adapter"],
                      "pack_sha256": digest(pack_path()), "license": data["license"],
                      "installed_at": s.now(), "files": data["files"]}
            (staging / ".complete").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            if target.exists():
                shutil.rmtree(target)
            staging.rename(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        with _lock:
            _sessions.clear()
        return capabilities()


def uninstall():
    with _lock:
        _sessions.clear()
        target = model_dir()
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    return capabilities()


def _session(role):
    """Lazily open an onnxruntime session for one role."""
    state = installed()
    if not state:
        raise ValueError("OCR 模型尚未安装")
    with _lock:
        if role in _sessions:
            return _sessions[role]
        import onnxruntime
        item = next((m for m in state.get("files", []) if m.get("role") == role), None)
        if item is None:
            raise ValueError("OCR 模型缺少角色：" + role)
        path = model_dir() / item["path"]
        options = onnxruntime.SessionOptions()
        # Local single-user service: keep inference single-threaded and predictable.
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        _sessions[role] = onnxruntime.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        return _sessions[role]


def _dictionary():
    state = installed() or {}
    item = next((m for m in state.get("files", []) if m.get("role") == "dictionary"), None)
    if item is None:
        return []
    text = (model_dir() / item["path"]).read_text(encoding="utf-8")
    return [line.rstrip("\r\n") for line in text.splitlines()]


def _runs(row):
    """Row-major runs of True pixels, used for label-free connected components."""
    import numpy as np
    padded = np.concatenate(([False], row, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return list(zip(edges[::2], edges[1::2]))


def _components(mask):
    """Two-pass run-length connected components: returns (labels, count)."""
    import numpy as np
    height, width = mask.shape
    labels = np.zeros((height, width), dtype=np.int32)
    parent = [0]

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left, right):
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    previous = []
    for y in range(height):
        current = _runs(mask[y])
        for start, end in current:
            matches = [label for (pstart, pend, label) in previous if pstart < end and start < pend]
            if matches:
                label = min(matches)
                for other in matches:
                    union(label, other)
            else:
                parent.append(len(parent))
                label = len(parent) - 1
            labels[y, start:end] = label
        previous = [(start, end, int(labels[y, start])) for start, end in current]
    if len(parent) <= 1:
        return labels, 0
    for y in range(height):
        row = labels[y]
        active = np.flatnonzero(row)
        if active.size:
            row[active] = [find(int(row[x])) for x in active]
    roots = {int(v) for v in np.unique(labels)} - {0}
    return labels, len(roots)


def _boxes(mask):
    """Group text pixels into boxes with a confidence from mask coverage."""
    import numpy as np
    labels, count = _components(mask)
    boxes = []
    for label in range(1, count + 1):
        ys, xs = np.nonzero(labels == label)
        if ys.size < BOX_MIN_PIXELS:
            continue
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
        if (x1 - x0) < BOX_MIN_PIXELS or (y1 - y0) < BOX_MIN_PIXELS:
            continue
        boxes.append((x0, y0, x1, y1))
    boxes.sort(key=lambda b: (b[1] // 12, b[0]))
    return boxes, labels


def _decode(logits, dictionary, blank=0):
    """Greedy CTC decode: collapse repeats, drop blanks, map via the dictionary."""
    import numpy as np
    best = logits.argmax(axis=1)
    scores = logits.max(axis=1)
    text, confidence, previous = [], [], -1
    for index, token in enumerate(best):
        token = int(token)
        if token != previous and token != blank:
            char = dictionary[token - 1] if 0 < token <= len(dictionary) else ""
            if char:
                text.append(char)
                confidence.append(float(scores[index]))
        previous = token
    return "".join(text), (sum(confidence) / len(confidence) if confidence else 0.0)


def _adapter_dbnet_ctc(image_bytes, sessions):
    """DBNet-style probability map plus CRNN/CTC recognition."""
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as raw:
        image = raw.convert("RGB")
    width, height = image.size
    if width * height > MAX_PIXELS:
        scale = (MAX_PIXELS / (width * height)) ** 0.5
        width, height = max(1, int(width * scale)), max(1, int(height * scale))
        image = image.resize((width, height))
    array = np.asarray(image, dtype=np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    tensor = ((array - mean) / std).transpose(2, 0, 1)[None].astype(np.float32)

    detection = sessions["detection"]
    inputs = detection.get_inputs()[0].name
    output = np.asarray(detection.run(None, {inputs: tensor})[0]).squeeze()
    if output.shape[0] != height or output.shape[1] != width:
        from PIL import Image as _Image
        output = np.asarray(_Image.fromarray(output.astype(np.float32)).resize((width, height)))
    if output.max() > 1.0 or output.min() < 0.0:
        output = 1.0 / (1.0 + np.exp(-output))
    mask = output > DETECT_THRESHOLD
    boxes, labels = _boxes(mask)

    recognition = sessions.get("recognition")
    if recognition is None or not boxes:
        return []
    dictionary = sessions.get("dictionary") or []
    if not dictionary:
        return []
    name = recognition.get_inputs()[0].name
    lines = []
    for (x0, y0, x1, y1) in boxes:
        patch = image.crop((max(0, x0 - 2), max(0, y0 - 2), min(width, x1 + 2), min(height, y1 + 2)))
        if patch.width < 2 or patch.height < 2:
            continue
        ratio = patch.width / patch.height
        target_w = max(8, min(RECOGNITION_WIDTH, int(round(RECOGNITION_HEIGHT * ratio))))
        patch = patch.resize((target_w, RECOGNITION_HEIGHT))
        grey = np.asarray(patch.convert("L"), dtype=np.float32) / 255.0
        grey = (grey - 0.5) / 0.5
        logits = np.asarray(recognition.run(None, {name: grey[None, None].astype(np.float32)})[0])
        logits = logits.reshape(-1, logits.shape[-1])
        text, score = _decode(logits, dictionary)
        if not text.strip():
            continue
        coverage = float((labels[y0:y1, x0:x1] > 0).mean()) if y1 > y0 else 0.0
        lines.append({"text": text, "score": round(min(score, DETECT_THRESHOLD + coverage), 4),
                      "box": [round(x0 / width, 6), round(y0 / height, 6),
                              round(x1 / width, 6), round(y1 / height, 6)]})
    return lines


def _adapter(name):
    if name == "dbnet_ctc":
        return _adapter_dbnet_ctc
    raise ValueError("OCR 适配器未实现：" + str(name))


def recognize_page(image_bytes):
    """Recognize a page and shape it like a parse page. None when not installed.

    Character offsets inside a line are exact; per-character boxes are spread
    across the recognized line box, which callers record as ``box_precision``
    so line geometry is never presented as character geometry.
    """
    if not capabilities().get("ready"):
        return None
    result = recognize(image_bytes)
    text, blocks, index = "", [], 0
    for line in result["lines"]:
        content = line["text"].strip()
        if not content:
            continue
        box, chars, span = line["box"], [], max(1, len(content))
        for offset, character in enumerate(content):
            left = box[0] + (box[2] - box[0]) * offset / span
            right = box[0] + (box[2] - box[0]) * (offset + 1) / span
            chars.append({"start": len(text) + offset, "end": len(text) + offset + len(character),
                          "text": character, "box": [left, box[1], right, box[3]]})
        start = len(text)
        text += content
        blocks.append({"id": f"p1-b{index}", "start": start, "end": len(text), "text": content,
                       "kind": "text", "level": 0, "title_path": "", "box": box, "chars": chars})
        index += 1
        text += "\n"
    return {"text": text, "blocks": blocks, "line_count": result["line_count"],
            "mean_score": result["mean_score"], "low_confidence": result["low_confidence"],
            "info": {"provider": result["provider"], "adapter": result["adapter"],
                     "pack_sha256": result["pack_sha256"], "line_count": result["line_count"],
                     "mean_score": result["mean_score"], "low_confidence": result["low_confidence"],
                     "box_precision": "line", "verified": False}}


def recognize(image_bytes):
    """Run the installed adapter. Raises when no verified pack is available."""
    state = installed()
    if not state:
        _, error = verify_pack()
        raise ValueError(error or "OCR 模型尚未安装，未执行识别")
    ok, reason = runtime_available()
    if not ok:
        raise ValueError(reason)
    sessions = {"detection": _session(DETECTION_ROLE)}
    if any(m.get("role") == RECOGNITION_ROLE for m in state.get("files", [])):
        sessions["recognition"] = _session(RECOGNITION_ROLE)
    sessions["dictionary"] = _dictionary()
    lines = _adapter(state["adapter"])(image_bytes, sessions)
    return {"provider": PROVIDER_VERSION, "adapter": state["adapter"], "pack_sha256": state["pack_sha256"],
            "lines": lines, "line_count": len(lines),
            "low_confidence": sum(1 for line in lines if line["score"] < LINE_MIN_SCORE),
            "mean_score": round(sum(line["score"] for line in lines) / len(lines), 4) if lines else None,
            "verified": False}
