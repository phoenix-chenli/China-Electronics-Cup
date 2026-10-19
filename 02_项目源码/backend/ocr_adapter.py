"""Explicit local OCR capability selection. No installation or remote POST here."""
from __future__ import annotations
import csv
import json
import platform
import shutil
import subprocess
import tempfile
import sys
import math
import time
from pathlib import Path
from .ocr_native import OCRError, quality_metrics, MAX_INPUT_PIXELS


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
    started = time.monotonic()
    if not isinstance(timeout, (int,float)) or not math.isfinite(timeout) or timeout <= 0:
        raise OCRError('Tesseract OCR 时间预算无效')
    deadline = started + min(35,timeout)
    capability = capabilities()
    if capability['selected'] != 'tesseract':
        raise OCRError('此系统未安装可用本地 OCR；请安装 Tesseract 及中文语言包，或使用明确授权的原页视觉读取。原件仍可保留。')
    if image.width * image.height > MAX_INPUT_PIXELS:
        raise OCRError('图片超过本地 OCR 像素预算')
    from .ocr_tesseract import input_dpi, parse_tsv, line_blocks, table_candidates, table_text_blocks, cell_crops, retain_cell_candidates
    from .ocr_orientation import reading_quality, choose_orientation, remap_result
    dpi = input_dpi(image)
    quality = quality_metrics(image)
    audit = {'status':'not_applicable','extra_ocr_calls':0,'crop_count':0,
             'max_crops':80,'max_crop_pixels':12_000_000,'numeric_values_replaced':False}
    quality.update(ocr_engine='tesseract', rotation_correction=0, preprocessing_applied=False,
                   input_dpi=list(dpi) if dpi else None, dpi_source='image_metadata' if dpi else 'engine_default',
                   text_normalization='cjk_interword_spaces', cell_review=audit)
    orientation = {'status':'not_started', 'selected_rotation':None, 'attempts':[],
                   'ambiguous':False, 'budget_exhausted':False, 'max_candidates':4,
                   'coordinate_frame':'original_image', 'reading_order':'corrected_image',
                   'diagnostic_note':'方向和引擎评分仅用于选择机器候选，不证明内容正确。'}
    quality['orientation'] = orientation
    def execute(arguments, limit):
        remaining = min(limit, deadline-time.monotonic())
        if remaining <= .05:
            raise subprocess.TimeoutExpired(arguments, max(0,remaining))
        if cancelled is None:
            response = subprocess.run(arguments,capture_output=True,timeout=remaining)
        else:
            from .reliability import run_cancellable
            response = run_cancellable(arguments,cancelled=cancelled,timeout=remaining)
        error = getattr(response,'stderr',b'')
        if isinstance(error,bytes):error=error.decode('utf-8',errors='replace')
        if ('read_params_file' in error and ('tsv' in error or "Can't open" in error)):
            raise OCRError('Tesseract TSV 配置缺失或无效；请检查 tessdata 的 configs/tsv，未当作空白页')
        if response.returncode or len(response.stdout)>8_000_000:
            raise OCRError('Tesseract OCR 失败或输出超过预算，未发布部分输出')
        return response
    with tempfile.TemporaryDirectory(prefix='zhiheng-tesseract-') as folder:
        path = Path(folder)/'page.png'
        image.convert('RGB').save(path, **({'dpi':dpi} if dpi else {}))
        arguments=[shutil.which('tesseract'),str(path),'stdout','-l',
            '+'.join(capability['tesseract']['languages'])]
        if dpi:arguments.extend(['--dpi',str(dpi[0])])
        arguments.append('tsv')
        primary_started=time.monotonic()
        try:
            response=execute(arguments,35)
        except InterruptedError:
            raise
        except subprocess.TimeoutExpired as exc:
            raise OCRError('Tesseract OCR 超时，未发布部分输出') from exc
        except OSError as exc:
            raise OCRError('Tesseract OCR 无法启动，请检查本机安装与执行权限') from exc
        try:
            pages, records=parse_tsv(response.stdout,{1:image.size})
        except (ValueError,KeyError,TypeError,UnicodeError,csv.Error) as exc:
            raise OCRError('Tesseract 返回的 TSV 文字坐标格式无效或为空，请检查语言及 TSV 配置') from exc
        first = dict(rotation=0, words=pages[1], records=records, quality=reading_quality(pages[1],image.size))
        candidates = [first]
        orientation['attempts'].append(dict(rotation=0,status='completed',elapsed_ms=round((time.monotonic()-primary_started)*1000),**first['quality']))
        if not first['quality']['strong'] and not quality['blank']:
            for rotation in (90,270,180):
                if deadline-time.monotonic()<1:
                    orientation['budget_exhausted']=True
                    break
                rotated=image.rotate(rotation,expand=True)
                rotated_dpi=dpi[::-1] if dpi and rotation in (90,270) else dpi
                rotated_path=Path(folder)/f'rotation-{rotation}.png'
                rotated.convert('RGB').save(rotated_path,**({'dpi':rotated_dpi} if rotated_dpi else {}))
                command=arguments.copy();command[1]=str(rotated_path)
                if rotated_dpi:command[command.index('--dpi')+1]=str(rotated_dpi[0])
                candidate_started=time.monotonic()
                try:
                    result=execute(command,8)
                    observed,count=parse_tsv(result.stdout,{1:rotated.size})
                    candidate=dict(rotation=rotation,words=observed[1],records=count,
                                   quality=reading_quality(observed[1],rotated.size))
                    candidates.append(candidate)
                    orientation['attempts'].append(dict(rotation=rotation,status='completed',elapsed_ms=round((time.monotonic()-candidate_started)*1000),**candidate['quality']))
                except InterruptedError:
                    raise
                except (OCRError,OSError,subprocess.TimeoutExpired,ValueError,TypeError,KeyError,UnicodeError,csv.Error) as exc:
                    orientation['attempts'].append(dict(rotation=rotation,status='timeout' if isinstance(exc,subprocess.TimeoutExpired) else 'failed',
                        elapsed_ms=round((time.monotonic()-candidate_started)*1000),error_type=type(exc).__name__))
                    if deadline-time.monotonic()<1:orientation['budget_exhausted']=True
        selected,ambiguous=choose_orientation(candidates)
        orientation['ambiguous']=ambiguous
        if selected is None:
            orientation['status']='blank' if quality['blank'] and not pages[1] else 'unreadable'
            quality.update(visual_review_required=not quality['blank'],ocr_mean_confidence=None,
                           elapsed_ms=round((time.monotonic()-started)*1000),
                           rejected_text_characters=sum(c['quality']['text_characters'] for c in candidates))
            if orientation['status']!='blank':
                quality['warnings'].append('方向候选均未达到文字可用条件；低质文字、坐标和表格未作为正文发布，请核对原图或重试')
            return dict(text='',blocks=[],tables=[],quality=quality,extraction='unreadable')
        rotation=selected['rotation'];words=selected['words'];text,blocks=line_blocks(words)
        orientation.update(status='corrected' if rotation else 'kept_original',selected_rotation=rotation)
        quality.update(rotation_correction=rotation,tsv_records=selected['records'],word_observations=len(words))
        if rotation:quality['warnings'].append(f'本地OCR按逆时针 {rotation}° 纠正方向；文字与单元格定位已映射回原图，仍须核验')
        if ambiguous:
            quality['visual_review_required']=True
            quality['warnings'].append('存在接近的可读方向候选；保留确定性选择，方向及内容仍需对照原图')
        if orientation['budget_exhausted']:quality['warnings'].append('部分方向候选受时间预算限制未完成；所选机器结果仍须核验')
        corrected=image.rotate(rotation,expand=True) if rotation else image
        tables=table_candidates(corrected,words) if deadline-time.monotonic()>.1 else []
        if tables:
            text,blocks=table_text_blocks(corrected,words,tables)
            quality['visual_review_required']=True
            jobs,truncated=cell_crops(corrected,tables)
            audit.update(status='skipped_budget',crop_count=len(jobs),truncated=truncated)
            if jobs and deadline-time.monotonic()>=1:
                paths=[]
                for i,(_,_,crop) in enumerate(jobs):
                    dest=Path(folder)/f'cell-{i}.png';crop.save(dest,dpi=(300,300));paths.append(str(dest))
                listing=Path(folder)/'cells.txt';listing.write_text('\n'.join(paths)+'\n',encoding='utf-8')
                batch=[arguments[0],str(listing),'stdout','-l',arguments[4],'--psm','6','--dpi','300','tsv']
                try:
                    audit['extra_ocr_calls']=1
                    result=execute(batch,15)
                    candidates,_=parse_tsv(result.stdout,{i:crop.size for i,(_,_,crop) in enumerate(jobs,1)})
                    differences=retain_cell_candidates(jobs,candidates)
                    audit.update(status='compared',disagreement_cells=differences)
                    if differences:quality['warnings'].append(f'{differences} 个单元格复读存在差异；原值保留，候选须对照原图复核，小数点和负号未自动补写')
                except InterruptedError:
                    raise
                except (OCRError,OSError,subprocess.TimeoutExpired,ValueError,TypeError,KeyError,UnicodeError,csv.Error):
                    audit['status']='candidate_failed'
                    quality['warnings'].append('表格局部复读未完整完成；保留整页候选，不以失败推断空单元格')
            if truncated:quality['warnings'].append('逐格复读达到数量或像素预算，未复读单元格仍须核验')
            quality['warnings'].append('表格依据真实格线或注明来源的词框布局生成待复核候选；未确认表头、行名、数值和单位')
        else:
            quality['warnings'].append('未取得可靠表格网格；仅保留文字，不表示原件没有表格')
        remap_result(blocks,tables,rotation)
    quality.update(ocr_mean_confidence=sum(b['confidence'] for b in blocks)/len(blocks) if blocks else None,
                   elapsed_ms=round((time.monotonic()-started)*1000))
    quality['warnings'].append('Tesseract文字与坐标为机器识别候选，未验证业务数值。')
    if not capability['chinese_available']:quality['warnings'].append('缺少中文OCR语言包；中文识别可能不完整。')
    return {'text':text,'blocks':blocks,'quality':quality,'extraction':'ocr' if text else 'unreadable','tables':tables}


if __name__=='__main__':
    from PIL import Image
    try:
        with Image.open(sys.argv[1]) as current:
            value=recognize(current,float(sys.argv[2]))
        print(json.dumps(value,ensure_ascii=False))
    except (OCRError,OSError,ValueError) as exc:
        print(json.dumps({'error':str(exc)[:500]},ensure_ascii=False));sys.exit(1)
