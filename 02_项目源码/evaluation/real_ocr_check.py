"""Reproduce the frozen real-page OCR check without overwriting its evidence.

Run from the project root: python -m evaluation.real_ocr_check --validate-only
No model API is used. Images are derived only from the frozen protocol's PDFs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / '评测材料/复杂文档专项/真实OCR'
CONDITIONS = ('original', 'rotate90', 'downsample50')
sys.path.insert(0, str(ROOT))
os.environ.setdefault('OMP_NUM_THREADS', '1')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def normalize(text):
    text = unicodedata.normalize('NFKC', text).casefold()
    text = text.translate(str.maketrans({'–': '-', '—': '-', '−': '-'}))
    return re.sub(r'\s+', '', text)


def contains(anchor, text):
    if re.fullmatch(r'\d+(?:\.\d+)?', anchor):
        raw = unicodedata.normalize('NFKC', text)
        return re.search(r'(?<![\d.])' + re.escape(anchor) + r'(?![\d.])', raw) is not None
    return normalize(anchor) in normalize(text)


def validate(manifest_path, protocol_path):
    """Validate all source bytes/pages/anchors without rendering or doing OCR."""
    import pymupdf
    manifest_path, protocol_path = manifest_path.resolve(), protocol_path.resolve()
    checksum_path = protocol_path.with_suffix('.sha256')
    if digest(protocol_path) != checksum_path.read_text(encoding='utf-8').split()[0]:
        raise ValueError('冻结协议摘要不一致')
    manifest, protocol = load(manifest_path), load(protocol_path)
    rows = manifest['files']
    by_id = {row['id']: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError('资料清单包含重复ID')
    if (not protocol['pages'] or len(protocol['pages']) != protocol['page_count']
            or any(len(p['anchors']) != protocol['anchors_per_page'] for p in protocol['pages'])):
        raise ValueError('页数或锚点数量与冻结协议不一致')
    if tuple(protocol['conditions']) != CONDITIONS:
        raise ValueError('冻结条件集合或顺序不一致')
    sources, observations = {}, []
    for page in protocol['pages']:
        identity, number = page['source_id'], page['physical_page']
        row = by_id[identity]
        original = (manifest_path.parent / row['file']).resolve()
        if not original.is_relative_to(manifest_path.parent):
            raise ValueError('原件路径必须位于资料清单目录内')
        if identity not in sources:
            actual = digest(original)
            if actual != row['sha256'] or actual != page['source_sha256']:
                raise ValueError('原件摘要不一致：' + identity)
            sources[identity] = original
        if page['source_sha256'] != row['sha256']:
            raise ValueError('冻结页的来源摘要与清单不一致')
        with pymupdf.open(original) as pdf:
            if pdf.page_count != row['pages'] or not 1 <= number <= pdf.page_count:
                raise ValueError('物理页数不一致：' + identity)
            native = pdf[number - 1].get_text()
            missing = [a['text'] for a in page['anchors'] if not contains(a['text'], native)]
            if missing:
                raise ValueError(f'原件文字层缺少冻结锚点：{identity}/{number}/{missing}')
        observations.append({'source_id': identity, 'physical_page': number,
                             'source_sha256': row['sha256'], 'anchors': len(page['anchors'])})
    if len(sources) != protocol['independent_originals']:
        raise ValueError('独立原件数与冻结协议不一致')
    return protocol, sources, {'status': 'inputs_valid', 'pages': observations,
        'protocol_sha256': digest(protocol_path), 'manifest_sha256': digest(manifest_path),
        'manifest_matches_original_freeze': digest(manifest_path) == protocol.get('original_manifest_sha256', protocol['manifest_sha256']),
        'manifest_matches_current_scope': digest(manifest_path) == protocol['manifest_sha256'],
        'model_chat_api_calls': 0, 'ocr_calls': 0}


def choose_output(path):
    if path is None:
        return Path(tempfile.mkdtemp(prefix='zhiheng-real-ocr-'))
    path = path.resolve()
    if path == EVIDENCE.resolve() or path.is_relative_to(EVIDENCE.resolve()):
        raise ValueError('不能向冻结证据目录写入重跑结果')
    # An existing empty directory is also refused, avoiding accidental evidence reuse.
    path.mkdir(parents=True, exist_ok=False)
    return path


def run(protocol, sources, validation, output):
    import pymupdf
    from PIL import Image
    from backend import parsers

    result_dir, image_dir = output / '结果', output / '输入图片'
    result_dir.mkdir(); image_dir.mkdir()
    write(output / '输入校验.json', validation)
    records, started = [], time.monotonic()
    for page in protocol['pages']:
        stem = f"{page['source_id']}_p{page['physical_page']:03}"
        original = image_dir / (stem + '_original.png')
        with pymupdf.open(sources[page['source_id']]) as pdf:
            pdf_page = pdf[page['physical_page'] - 1]
            scale = 2000 / max(pdf_page.rect.width, pdf_page.rect.height)
            pdf_page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(original)
        with Image.open(original) as image:
            image.transpose(Image.Transpose.ROTATE_90).save(image_dir / (stem + '_rotate90.png'))
            image.resize((image.width // 2, image.height // 2), Image.Resampling.LANCZOS).save(image_dir / (stem + '_downsample50.png'))
        for condition in CONDITIONS:
            identity = stem + '_' + condition
            path = image_dir / (identity + '.png')
            actual_hash = digest(path)
            t = time.monotonic()
            record = {'source_id': page['source_id'], 'source_sha256': page['source_sha256'],
                'physical_page': page['physical_page'], 'condition': condition,
                'image_sha256': actual_hash,
                'image_matches_original_freeze': actual_hash == page['images'][condition]['sha256'],
                'status': 'failed', 'denominator': len(page['anchors']), 'hits': 0,
                'model_chat_api_calls': 0}
            try:
                parsed = parsers.parse(path, 'png', result_dir / (identity + '.sqlite'), lambda *a: None)
                if parsed.get('data_path'):
                    parsed['data_path'] = '结果/' + Path(parsed['data_path']).name
                write(result_dir / (identity + '.json'), parsed)
                text = parsed['pages'][0]['text']
                checks = [{'anchor': a['text'], 'found': contains(a['text'], text)} for a in page['anchors']]
                quality = parsed['pages'][0]['quality']
                record.update(status='parsed', hits=sum(r['found'] for r in checks), anchors=checks,
                    text_characters=len(text), table_count=len(parsed['tables']),
                    rotation_correction=quality.get('rotation_correction'), quality=quality,
                    result_file='结果/' + identity + '.json')
            except Exception as exc:
                record.update(error_type=type(exc).__name__, error=str(exc)[:1000],
                    anchors=[{'anchor': a['text'], 'found': False} for a in page['anchors']])
            record['elapsed_seconds'] = round(time.monotonic() - t, 3)
            with Image.open(path) as image:
                record['width'], record['height'] = image.size
            records.append(record)
            write(output / '真实OCR专项结果.json', {'status': 'running', 'results': records, 'model_chat_api_calls': 0})
            print(json.dumps({k: record[k] for k in ('source_id', 'physical_page', 'condition', 'status', 'hits', 'denominator')}, ensure_ascii=False), flush=True)
    grouped = {condition: {'pages': len(rows), 'hits': sum(r['hits'] for r in rows),
        'denominator': sum(r['denominator'] for r in rows),
        'failed_pages': sum(r['status'] != 'parsed' for r in rows),
        'elapsed_seconds': round(sum(r['elapsed_seconds'] for r in rows), 3)}
        for condition in CONDITIONS for rows in [[r for r in records if r['condition'] == condition]]}
    summary = {'status': 'completed', 'parser_version': parsers.PARSER_VERSION,
        'frozen_protocol_sha256': validation['protocol_sha256'], 'independent_originals': len(sources),
        'original_pages': len(protocol['pages']), 'derived_images_counted_as_documents': False, 'workers': 1,
        'results': records, 'conditions': grouped, 'model_chat_api_calls': 0,
        'elapsed_seconds': round(time.monotonic() - started, 3),
        'boundary': protocol['boundary'], 'normalization': protocol['normalization'],
        'rerun_note': '独立重跑；未覆盖随包冻结结果。渲染器或OCR环境变化会影响图摘要和识别结果。'}
    write(output / '真实OCR专项结果.json', summary)
    print(json.dumps({'output': str(output), 'conditions': grouped}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description='按冻结协议离线复现真实页面OCR专项；默认写新临时目录，单worker，零模型API调用。')
    parser.add_argument('--manifest', type=Path, default=ROOT / '知识库样本/真实业务文档/资料清单.json', help='原件资料清单')
    parser.add_argument('--protocol', type=Path, default=EVIDENCE / '冻结锚点.json', help='便携冻结锚点协议，旁边须有.sha256')
    parser.add_argument('--output', type=Path, help='必须尚不存在；省略则新建系统临时目录')
    parser.add_argument('--validate-only', action='store_true', help='只核验原件摘要、页数和锚点，不渲染、不OCR、不创建输出目录')
    args = parser.parse_args()
    try:
        protocol, sources, validation = validate(args.manifest, args.protocol)
        if args.validate_only:
            print(json.dumps(validation, ensure_ascii=False, indent=2))
        else:
            run(protocol, sources, validation, choose_output(args.output))
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f'输入或运行条件错误：{exc}\n')


if __name__ == '__main__':
    main()
