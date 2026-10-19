"""Bounded local OCR and geometry. Engine confidence is never an accuracy claim."""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat

from .ocr_preprocess import comparison, enhance, preprocessing_plan

MAX_INPUT_PIXELS = 40_000_000
MAX_OCR_SIDE = 2800
OCR_TIMEOUT = 35
_COMPILE_LOCK = threading.Lock()
CONFIDENCE_NOTE = "OCR 置信度为识别引擎的内部评分，不是识别准确率；数字、符号和低质区域需对照原件。"


class OCRError(ValueError):
    pass


def _binary(timeout=60):
    deadline = time.monotonic() + timeout
    if platform.system() != "Darwin":
        raise OCRError("本地 OCR 需要 macOS Vision；当前系统不可用")
    source = Path(__file__).with_name("native_ocr.swift")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    cache = Path(tempfile.gettempdir()) / f"zhiheng-ocr-{os.getuid()}"
    cache.mkdir(mode=0o700, exist_ok=True)
    target = cache / digest
    if not _COMPILE_LOCK.acquire(timeout=max(0, deadline - time.monotonic())):
        raise OCRError("本地 OCR 初始化超时或不可用，可重试")
    try:
        if not target.exists():
            temporary = target.with_suffix(f".{os.getpid()}.building")
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OCRError("本地 OCR 初始化超时或不可用，可重试")
                completed = subprocess.run(["/usr/bin/swiftc", "-O", str(source), "-o", str(temporary)], capture_output=True, timeout=remaining)
                if completed.returncode:
                    raise OCRError("本地 OCR 编译失败，请检查 Xcode 命令行工具后重试")
                temporary.chmod(0o700)
                temporary.replace(target)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise OCRError("本地 OCR 初始化超时或不可用，可重试") from exc
            finally:
                temporary.unlink(missing_ok=True)
    finally:
        _COMPILE_LOCK.release()
    return target


def normalized_image(image):
    image = ImageOps.exif_transpose(image)
    if "A" in image.getbands() or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        result = Image.alpha_composite(background, rgba).convert("RGB")
        if 'dpi' in image.info:
            result.info['dpi'] = image.info['dpi']
        return result
    return image.convert("RGB")


def inverse_box(box, rotation):
    """Undo a physical PIL counterclockwise rotation; retain original preview geometry."""
    points = []
    for x in (box[0], box[2]):
        for y in (box[1], box[3]):
            points.append({0: (x, y), 90: (1 - y, x), 180: (1 - x, 1 - y), 270: (y, 1 - x)}[rotation])
    return [round(max(0., min(1., v)), 6) for v in (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))]


def quality_metrics(image):
    gray = image.convert("L")
    gray.thumbnail((1400, 1400))
    stats = ImageStat.Stat(gray)
    histogram = gray.histogram()
    count = sum(histogram)
    dark = sum(histogram[:200]) / max(1, count)
    ink_bounds = gray.point(lambda p: 255 if p < 245 else 0).getbbox()
    edge_source = gray.crop(ink_bounds) if ink_bounds else gray
    contrast = ImageStat.Stat(edge_source).stddev[0]
    edges = edge_source.filter(ImageFilter.FIND_EDGES)
    # Remove PIL's artificial border from edge diagnostics.
    if edge_source.width > 4 and edge_source.height > 4:
        edges = edges.crop((2, 2, edge_source.width - 2, edge_source.height - 2))
    sharpness = ImageStat.Stat(edges).var[0]
    extrema = gray.getextrema()
    blank = extrema[1] - extrema[0] < 5
    notices = []
    if blank:
        notices.append("页面近乎单色，未发现可辨文字；请核对原件是否为空白页或扫描遗漏")
    elif contrast < 15:
        notices.append("页面对比度偏低，文字可能漏识别")
    if not blank and sharpness < 45:
        notices.append("页面边缘较模糊，数字和细小符号需对照原件")
    if min(image.size) < 350 and not blank:
        notices.append("图片分辨率偏低，细小文字可能无法辨认")
    return dict(contrast=round(contrast, 2), grayscale_range=extrema[1] - extrema[0],
                sharpness=round(sharpness, 2), dark_pixel_ratio=round(dark, 4), blank=blank, warnings=notices,
                diagnostic_note="对比度、边缘清晰度和空白检测为图像诊断，不代表文字正确率。")


def text_blocks(blocks):
    """Stable reading order and exact text offsets, without inventing paragraphs."""
    ordered = sorted(blocks, key=lambda b: (round(b["bbox"][1] / .008), b["bbox"][0]))
    parts, offset = [], 0
    for block in ordered:
        text = str(block["text"]).strip()
        if not text:
            continue
        block["text"] = text
        block["start"], block["end"] = offset, offset + len(text)
        block.setdefault("kind", "formula_candidate" if any(s in text for s in ("=", "∑", "∫", "√", "≤", "≥")) else "text")
        parts.append(text)
        offset += len(text) + 1
    return "\n".join(parts), [b for b in ordered if b.get("text")]


def _score(result):
    # This ranks OCR candidates, not correctness. Upright baselines matter when
    # Vision recognizes rotated text with otherwise tied internal confidence.
    return sum(min(len(b["text"].strip()), 100) * float(b["confidence"]) ** 3 *
               (1.0 if b.get("angle") is None or abs(float(b["angle"])) < 35 else .1)
               for b in result.get("blocks", []))


def _adaptive_candidate(image, original, rotation, quality, binary, deadline):
    """At most one extra OCR input under the original page's deadline."""
    audit = quality['preprocessing']
    if not audit['triggers']:
        return original
    started = time.monotonic()
    try:
        if deadline - started < 1:
            audit['status'] = 'skipped_budget'
            return original
        candidate_image = enhance(image.rotate(rotation, expand=True), audit['operations'])
        audit.update(candidate_count=1, input_size=list(candidate_image.size), rotation=rotation)
        with tempfile.TemporaryDirectory(prefix='zhiheng-ocr-enhanced-') as folder:
            path = Path(folder) / 'candidate.png'
            candidate_image.save(path)
            remaining = deadline - time.monotonic()
            if remaining <= .1:
                audit['status'] = 'skipped_budget'
                return original
            audit['extra_ocr_calls'] = 1
            process = subprocess.run([str(binary), str(path)], capture_output=True, timeout=min(8, remaining))
            if process.returncode or len(process.stdout) > 8_000_000:
                raise ValueError('candidate_process')
            results = json.loads(process.stdout)
            if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
                raise ValueError('candidate_shape')
            candidate = results[0]
            if candidate.get('error') or not isinstance(candidate.get('blocks'), list) or len(candidate['blocks']) > 3000:
                raise ValueError('candidate_blocks')
            for block in candidate['blocks']:
                box = block.get('bbox') if isinstance(block, dict) else None
                confidence = block.get('confidence') if isinstance(block, dict) else None
                if (not isinstance(block, dict) or not isinstance(block.get('text'), str)
                        or not isinstance(box, list) or len(box) != 4
                        or any(not isinstance(n, (int, float)) or isinstance(n, bool) or not math.isfinite(n) or not 0 <= n <= 1 for n in box)
                        or box[0] >= box[2] or box[1] >= box[3]
                        or not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
                        or not math.isfinite(confidence) or not 0 <= confidence <= 1
                        or block.get('angle') is not None and (not isinstance(block['angle'], (int, float)) or not math.isfinite(block['angle']))):
                    raise ValueError('candidate_block')
            # Structural comparison validates coordinates/text through the same
            # bounded review representation; malformed optional output is ignored.
            audit.update(comparison(original, candidate, rotation, inverse_box))
            audit.update(status='compared', original_score=round(_score(original), 4),
                         candidate_score=round(_score(candidate), 4))
        if audit['text_disagreement'] or audit['numeric_symbol_disagreement']:
            quality['visual_review_required'] = True
            label = '数字或符号' if audit['numeric_symbol_disagreement'] else '文字'
            quality['warnings'].append(f'自适应增强候选与原 OCR 的{label}不一致；已保留原识别值和候选，须对照原页复核，不可据评分换值')
            return original
        if not audit['truncated'] and _score(candidate) > _score(original) * 1.05:
            audit['selected'] = 'enhanced'
            return candidate
        return original
    except subprocess.TimeoutExpired:
        audit['status'] = 'candidate_timeout'
        quality['warnings'].append('自适应增强候选达到本页剩余耗时上限，已保留原 OCR 结果')
        return original
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError):
        audit['status'] = 'candidate_failed'
        quality['warnings'].append('自适应增强候选未完成或格式无效，已保留原 OCR 结果')
        return original
    finally:
        audit['elapsed_ms'] = round((time.monotonic() - started) * 1000)


def recognize(image, *, timeout=OCR_TIMEOUT):
    """Four orientations, then at most one quality-triggered bounded candidate."""
    started = time.monotonic()
    deadline = started + timeout
    if image.width * image.height > MAX_INPUT_PIXELS:
        raise OCRError("图片超过 4,000 万像素，请缩小后导入")
    image = normalized_image(image)
    quality = quality_metrics(image)
    quality.update(rotation_correction=0, ocr_mean_confidence=None, confidence_note=CONFIDENCE_NOTE,
                   preprocessing=preprocessing_plan(image, quality), visual_review_required=False)
    if quality["blank"]:
        quality['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        return dict(text="", blocks=[], quality=quality, extraction="unreadable", tables=[])
    image.thumbnail((MAX_OCR_SIDE, MAX_OCR_SIDE))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OCRError("页面 OCR 超时，可裁剪复杂区域或降低分辨率后重试")
    binary = _binary(timeout=min(60, remaining))
    with tempfile.TemporaryDirectory(prefix="zhiheng-ocr-") as folder:
        paths = []
        for rotation in (0, 90, 180, 270):
            path = Path(folder) / f"{rotation}.png"
            image.rotate(rotation, expand=True).save(path)
            paths.append(str(path))
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OCRError("页面 OCR 超时，可裁剪复杂区域或降低分辨率后重试")
            process = subprocess.run([str(binary), *paths], capture_output=True, timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise OCRError("页面 OCR 超时，可裁剪复杂区域或降低分辨率后重试") from exc
        if process.returncode or len(process.stdout) > 8_000_000:
            raise OCRError("页面 OCR 未完成，可重试")
        try:
            results = json.loads(process.stdout)
            if not isinstance(results, list) or len(results) != 4 or any(not isinstance(r, dict) for r in results):
                raise ValueError('orientations')
        except (ValueError, TypeError) as exc:
            raise OCRError("页面 OCR 返回无效结果，可重试") from exc
    winner = max(range(len(results)), key=lambda i: (_score(results[i]), -i))
    selected = results[winner]
    if selected.get("error"):
        raise OCRError("页面 OCR 失败，可重试")
    rotation = (0, 90, 180, 270)[winner]
    selected = _adaptive_candidate(image, selected, rotation, quality, binary, deadline)
    corrected_blocks = selected.get("blocks", [])
    blocks = []
    for block in corrected_blocks:
        blocks.append({"text": block["text"], "bbox": inverse_box(block["bbox"], rotation), "confidence": round(float(block["confidence"]), 4), "kind": "formula_candidate" if any(s in block["text"] for s in ("=", "∑", "∫", "√", "≤", "≥")) else "text"})
    # Reading order belongs to the corrected image, then geometry maps to original.
    corrected_order = sorted(range(len(corrected_blocks)), key=lambda i: (round(corrected_blocks[i]["bbox"][1] / .008), corrected_blocks[i]["bbox"][0]))
    ordered = [blocks[i] for i in corrected_order]
    text, offset = [], 0
    for block in ordered:
        block["text"] = block["text"].strip()
        block["start"], block["end"] = offset, offset + len(block["text"])
        text.append(block["text"])
        offset += len(block["text"]) + 1
    if rotation:
        quality["warnings"].append(f"识别时旋转 {rotation}°；原件和定位坐标保持原有方向")
    angles = [float(b["angle"]) for b in corrected_blocks if b.get("angle") is not None and len(b["text"]) >= 4 and abs(float(b["angle"])) < 30]
    skew = statistics.median(angles) if angles else None
    if skew is not None and abs(skew) > 2:
        quality["warnings"].append(f"文字行倾斜约 {abs(skew):.1f}°，表格边界和符号需复核")
    confidence = statistics.mean(b["confidence"] for b in blocks) if blocks else None
    low_confidence = sum(b["confidence"] < .65 for b in blocks)
    if low_confidence:
        quality["warnings"].append(f"{low_confidence} 处文字的 OCR 置信度偏低，请对照原件核对数字、符号和名称")
    if not blocks:
        quality["warnings"].append("未识别到可用文字，请核对原件或重新扫描后重试")
    quality.update(low_confidence_blocks=low_confidence, rotation_correction=rotation, skew_degrees=round(skew, 2) if skew is not None else None,
                   ocr_mean_confidence=round(confidence, 4) if confidence is not None else None, elapsed_ms=round((time.monotonic() - started) * 1000))
    corrected_image = image.rotate(rotation, expand=True)
    tables = detect_grid(corrected_image, corrected_blocks, rotation)
    if tables:
        tables, table_notices = _recognize_cells(corrected_image, tables, binary, deadline, rotation)
        quality["warnings"].extend(table_notices)
    quality["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return dict(text="\n".join(text), blocks=ordered, quality=quality, extraction="ocr" if blocks else "unreadable", tables=tables)


def _recognize_cells(image, tables, binary, deadline, rotation):
    """Independent crop recognition after actual grid detection; retain disagreements."""
    warnings, accepted, jobs = [], [], []
    if deadline - time.monotonic() <= .1:
        return [], ["扫描表格逐格复核达到耗时上限，已仅保留页面文字"]
    for table in tables:
        if len(jobs) + len(table["cells"]) > 120:
            warnings.append("扫描表格逐格复核超过每页 120 格上限；未完成逐格复核的表格仅保留页面文字")
            continue
        accepted.append(table)
        for cell in table["cells"]:
            # Stored geometry is original-preview space; recover corrected crop space.
            box = inverse_box(cell["bbox"], (360 - rotation) % 360)
            coords = [int(box[0] * image.width) + 2, int(box[1] * image.height) + 2,
                      int(box[2] * image.width) - 2, int(box[3] * image.height) - 2]
            if coords[2] <= coords[0] or coords[3] <= coords[1]:
                continue
            crop = image.crop(coords)
            # Enlarging is only a recognition input; source evidence stays unchanged.
            factor = min(3, 110 / max(1, crop.height), 900 / max(1, crop.width))
            if factor > 1:
                crop = crop.resize((round(crop.width * factor), round(crop.height * factor)), Image.Resampling.LANCZOS)
            crop = ImageOps.expand(crop, border=10, fill="white")
            jobs.append((table, cell, crop))
    if not jobs:
        return [], warnings
    if deadline - time.monotonic() <= .1:
        return [], warnings + ["扫描表格逐格复核达到耗时上限，已仅保留页面文字"]
    with tempfile.TemporaryDirectory(prefix="zhiheng-cell-ocr-") as folder:
        paths = []
        for index, (_, _, crop) in enumerate(jobs):
            path = Path(folder) / f"{index}.png"
            crop.save(path)
            paths.append(str(path))
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return [], warnings + ["扫描表格逐格复核达到耗时上限，已仅保留页面文字"]
            process = subprocess.run([str(binary), *paths], capture_output=True, timeout=remaining)
            results = json.loads(process.stdout) if process.returncode == 0 and len(process.stdout) < 8_000_000 else []
        except (subprocess.TimeoutExpired, ValueError):
            results = []
    if len(results) != len(jobs):
        return [], warnings + ["扫描表格逐格 OCR 未完成，已仅保留页面文字；可重试"]
    rejected, differences = set(), 0
    for (table, cell, _), result in zip(jobs, results):
        original = cell["text"]
        if result.get("error"):
            rejected.add(id(table))
            continue
        parts = sorted(result.get("blocks", []), key=lambda b: (round(b["bbox"][1] / .04), b["bbox"][0]))
        value = "\n".join(b["text"].strip() for b in parts).strip() or None
        if not value and original:
            if table.get("_requires_cell_ocr"):
                rejected.add(id(table))
            cell["verification"] = "cell_ocr_empty"
            continue
        cell["verification"] = "cell_ocr"
        if original and value and "".join(original.split()) != "".join(value.split()):
            differences += 1
            cell["ocr_disagreement"] = True
            cell["ocr_candidates"] = [original, value]
        cell["text"] = value
        cell["confidence"] = round(min(b["confidence"] for b in parts), 4) if parts else None
        table["rows"][cell["row"] - 1][cell["col"] - 1] = value
    if differences:
        warnings.append(f"{differences} 个单元格的全页与逐格 OCR 结果不一致，已保留两种识别值；计算前需对照原件确认")
    if rejected:
        warnings.append("部分扫描表格的逐格 OCR 不完整，已仅保留页面文字")
    for table in accepted:
        table.pop("_requires_cell_ocr", None)
        table["verification"] = "cell_ocr"
    return [table for table in accepted if id(table) not in rejected], warnings


def _bands(values, minimum):
    bands = []
    for index, value in enumerate(values):
        if value >= minimum:
            if bands and index <= bands[-1][-1] + 4:
                bands[-1].append(index)
            else:
                bands.append([index])
    return [round(statistics.mean(b)) for b in bands if len(b) <= 14]


def detect_grid(image, blocks, rotation=0, *, allow_open_outer=False):
    """Conservative closed rules, including light rules on colored table backgrounds."""
    gray = image.convert("L")
    gray.thumbnail((1400, 1400))
    direct = _detect_grid_mask(gray, blocks, rotation, allow_open_outer=allow_open_outer)
    if direct:
        for table in direct:
            table.setdefault("grid_detection", "dark_rules")
        return direct
    # Edges must still form long, closed intersecting rules. Text alignment alone
    # cannot create a grid. This preserves white separators in colored tables.
    edges = gray.filter(ImageFilter.FIND_EDGES).point(lambda p: 0 if p >= 12 else 255)
    indirect = _detect_grid_mask(edges, blocks, rotation, allow_open_outer=allow_open_outer)
    for table in indirect:
        table.setdefault("grid_detection", "intensity_edges")
    return indirect


def _detect_grid_mask(gray, blocks, rotation, *, allow_open_outer=False):
    width, height = gray.size
    pixels = gray.load()
    # Long horizontal ink runs identify actual drawn rules, not text alignment.
    runs = []
    for y in range(height):
        start = None
        longest = (0, 0)
        gap = 0
        for x in range(width):
            if pixels[x, y] < 160:
                if start is None:
                    start = x
                gap = 0
            elif start is not None:
                gap += 1
                if gap > 4:
                    if x - start - gap > longest[1] - longest[0]:
                        longest = (start, x - gap)
                    start = None
        if start is not None and width - start - gap > longest[1] - longest[0]:
            longest = (start, width - gap)
        if longest[1] - longest[0] >= width * .3:
            runs.append((y, *longest))
    if len(runs) < 3:
        return []
    groups = []
    for y, left, right in runs:
        existing = next((g for g in groups if abs(g[0][1] - left) < 8 and abs(g[0][2] - right) < 8), None)
        if existing is None:
            groups.append([(y, left, right)])
        else:
            existing.append((y, left, right))
    output = []
    for group in groups[:12]:
        ymask = [0] * height
        for y, _, _ in group:
            ymask[y] = 1
        ys = _bands(ymask, 1)
        if not 3 <= len(ys) <= 100:
            continue
        top, bottom = ys[0], ys[-1]
        if bottom - top < 35:
            continue
        left = round(statistics.median(r[1] for r in group))
        right = round(statistics.median(r[2] for r in group))
        coverage = [sum(pixels[x, y] < 160 for y in range(top, bottom + 1)) for x in range(width)]
        xs = [x for x in _bands(coverage, (bottom - top + 1) * .83) if left - 5 <= x <= right + 5]
        open_outer = False
        if allow_open_outer and len(xs) >= 2:
            # Statistical tables often omit the outside vertical border. The
            # actual common horizontal endpoints provide its extent; internal
            # separators must still be drawn through every row, not inferred
            # from aligned numbers or expected column counts.
            open_outer = abs(xs[0]-left) > 8 or abs(xs[-1]-right) > 8
            if abs(xs[0]-left) > 8:xs.insert(0,left)
            if abs(xs[-1]-right) > 8:xs.append(right)
            if any(max(sum(pixels[xx, y] < 160 for y in range(ys[r], ys[r+1]+1))
                           for xx in range(max(0,x-2), min(width,x+3))) < .83*(ys[r+1]-ys[r]+1)
                   for x in xs[1:-1] for r in range(len(ys)-1)):
                continue
        if not 3 <= len(xs) <= 40 or abs(xs[0] - left) > 8 or abs(xs[-1] - right) > 8:
            continue
        # Every cell must have closed edges; merged/broken scan grids are not fabricated.
        if any(max(sum(pixels[x, yy] < 160 for x in range(xs[0], xs[-1] + 1))
                       for yy in range(max(0, y - 2), min(height, y + 3))) < .83 * (xs[-1] - xs[0] + 1) for y in ys):
            continue
        assigned = {}
        ambiguous = False
        for block in blocks:
            b = block["bbox"]
            cx, cy = (b[0] + b[2]) * width / 2, (b[1] + b[3]) * height / 2
            if not xs[0] < cx < xs[-1] or not ys[0] < cy < ys[-1]:
                continue
            row = next(i for i in range(len(ys) - 1) if ys[i] <= cy < ys[i + 1])
            col = next(i for i in range(len(xs) - 1) if xs[i] <= cx < xs[i + 1])
            if b[0] * width < xs[col] - 4 or b[2] * width > xs[col + 1] + 4 or b[1] * height < ys[row] - 4 or b[3] * height > ys[row + 1] + 4:
                ambiguous = True
            assigned.setdefault((row, col), []).append(block)
        if len(assigned) < 3:
            continue
        cells, rows = [], []
        for r in range(len(ys) - 1):
            values = []
            for c in range(len(xs) - 1):
                content = sorted(assigned.get((r, c), []), key=lambda b: (b["bbox"][1], b["bbox"][0]))
                text = "\n".join(b["text"] for b in content) or None
                values.append(text)
                cells.append(dict(row=r + 1, col=c + 1, text=text, bbox=inverse_box([xs[c] / width, ys[r] / height, xs[c + 1] / width, ys[r + 1] / height], rotation), rowspan=1, colspan=1, covered_by=None,
                                  confidence=round(min(b["confidence"] for b in content), 4) if content else None))
            rows.append(values)
        output.append(dict(bbox=inverse_box([xs[0] / width, ys[0] / height, xs[-1] / width, ys[-1] / height], rotation), extraction="ocr_grid", rows=rows, row_count=len(rows), column_count=len(xs) - 1, cells=cells, header_names=[], has_merged_cells=False, data_row_offset=0, requires_review=True, _requires_cell_ocr=ambiguous))
        if open_outer:output[-1]['grid_detection']='horizontal_rules_open_outer'
    return output[:8]
