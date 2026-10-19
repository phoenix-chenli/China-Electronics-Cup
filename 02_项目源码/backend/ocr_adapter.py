"""Explicit local OCR capability selection. No installation or remote POST here."""
from __future__ import annotations
import csv
import io
import json
import platform
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path
from .ocr_native import OCRError, quality_metrics, text_blocks, MAX_INPUT_PIXELS


def capabilities():
    system = platform.system()
    vision = system == 'Darwin' and bool(shutil.which('swift'))
    binary = shutil.which('tesseract')
    languages = []
    if binary:
        try:
            response = subprocess.run([binary,'--list-langs'], capture_output=True, text=True, timeout=3)
            if response.returncode == 0:
                languages = [x.strip() for x in response.stdout.splitlines()[1:] if x.strip()][:100]
        except (OSError,subprocess.TimeoutExpired):
            pass
    usable = [x for x in ('chi_sim','chi_tra','eng') if x in languages]
    selected = 'macos_vision' if vision else 'tesseract' if usable else None
    return {'platform':system,'selected':selected,'available':bool(selected),
            'macos_vision':{'available':vision,'runtime_compilation_required':vision},
            'tesseract':{'available':bool(usable),'languages':usable},
            'note':'本机OCR可用性预检不等于识别质量验收；中文Tesseract需chi_sim或chi_tra语言包。',
            'chinese_available':vision or any(x.startswith('chi_') for x in usable)}


def recognize(image, timeout=35, cancelled=None):
    if platform.system() == 'Darwin':
        from .ocr_native import recognize as native
        if cancelled is None:
            return native(image, timeout=timeout)
        from .reliability import run_cancellable
        with tempfile.TemporaryDirectory(prefix='zhiheng-ocr-page-') as folder:
            path=Path(folder)/'page.png';image.save(path)
            try:
                response=run_cancellable([sys.executable,'-m','backend.ocr_adapter',str(path),str(timeout)],
                    cwd=Path(__file__).resolve().parent.parent,timeout=timeout+1,cancelled=cancelled)
            except subprocess.TimeoutExpired as exc:
                raise OCRError('本地OCR处理超时，未发布部分输出') from exc
            if len(response.stdout)>24_000_000:raise OCRError('本地OCR输出超过预算')
            try:result=json.loads(response.stdout.decode('utf-8').splitlines()[-1])
            except (ValueError,IndexError,UnicodeError) as exc:raise OCRError('本地OCR输出不可读') from exc
            if response.returncode or result.get('error'):raise OCRError(result.get('error','本地OCR失败'))
            return result
    capability = capabilities()
    if capability['selected'] != 'tesseract':
        raise OCRError('此系统未安装可用本地 OCR；请安装 Tesseract 及中文语言包，或使用明确授权的原页视觉读取。原件仍可保留。')
    if image.width * image.height > MAX_INPUT_PIXELS:
        raise OCRError('图片超过本地 OCR 像素预算')
    # TSV preserves actual engine boxes. No inferred cells/tables or corrected digits.
    with tempfile.TemporaryDirectory(prefix='zhiheng-tesseract-') as folder:
        path = Path(folder)/'page.png'; image.convert('RGB').save(path)
        try:
            arguments=[shutil.which('tesseract'),str(path),'stdout','-l',
                '+'.join(capability['tesseract']['languages']),'tsv']
            if cancelled is None:
                response = subprocess.run(arguments,capture_output=True,timeout=max(.1,min(35,timeout)))
            else:
                from .reliability import run_cancellable
                response = run_cancellable(arguments,cancelled=cancelled,timeout=max(.1,min(35,timeout)))
        except subprocess.TimeoutExpired as exc:
            raise OCRError('Tesseract OCR 超时，未发布部分输出') from exc
        if response.returncode or len(response.stdout) > 8_000_000:
            raise OCRError('Tesseract OCR 失败或输出超过预算，未发布部分输出')
        groups = {}
        try:
            for row in csv.DictReader(io.StringIO(response.stdout.decode('utf-8')),delimiter='\t'):
                if row.get('level') != '5' or not row.get('text','').strip(): continue
                key=tuple(row[k] for k in ('page_num','block_num','par_num','line_num'))
                x,y,w,h=map(int,(row['left'],row['top'],row['width'],row['height']))
                if w <= 0 or h <= 0 or x < 0 or y < 0 or x+w > image.width or y+h > image.height:
                    raise ValueError('invalid OCR box')
                groups.setdefault(key,[]).append((row['text'],x,y,x+w,y+h,float(row['conf'])/100))
        except (ValueError,KeyError,UnicodeError) as exc:
            raise OCRError('Tesseract 返回的文字坐标格式无效') from exc
    blocks=[]
    for words in groups.values():
        value=' '.join(w[0] for w in words)
        blocks.append({'text':value,'bbox':[min(w[1] for w in words)/image.width,min(w[2] for w in words)/image.height,
            max(w[3] for w in words)/image.width,max(w[4] for w in words)/image.height],
            'confidence':max(0,min(1,sum(w[5] for w in words)/len(words))),
            'kind':'formula_candidate' if any(c in value for c in '=∑∫√≤≥') else 'text'})
    text,blocks=text_blocks(blocks)
    quality=quality_metrics(image)
    quality.update(ocr_engine='tesseract',ocr_mean_confidence=sum(b['confidence'] for b in blocks)/len(blocks) if blocks else None,
                   rotation_correction=0,preprocessing_applied=False)
    quality['warnings'].append('Tesseract文字与坐标为机器识别候选，未验证业务数值；未推断网格结构。')
    if not capability['chinese_available']:quality['warnings'].append('缺少中文OCR语言包；中文识别可能不完整。')
    return {'text':text,'blocks':blocks,'quality':quality,'extraction':'ocr' if text else 'unreadable','tables':[]}


if __name__=='__main__':
    from PIL import Image
    try:
        with Image.open(sys.argv[1]) as current:
            value=recognize(current,float(sys.argv[2]))
        print(json.dumps(value,ensure_ascii=False))
    except (OCRError,OSError,ValueError) as exc:
        print(json.dumps({'error':str(exc)[:500]},ensure_ascii=False));sys.exit(1)
