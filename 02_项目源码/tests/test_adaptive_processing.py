import copy
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw

from backend import adaptive_processing as a
from backend import parsers, ocr_adapter
from backend.ocr_native import text_blocks


def block(text='原生金额12.50元', box=(.1,.2,.7,.25), **extra):
    return dict(text=text, bbox=list(box), kind='text', confidence=None, **extra)


def extracted(blocks=None, images=None, tables=None):
    blocks = [block()] if blocks is None else blocks
    images = images or []; tables = tables or []
    return dict(blocks=blocks, tables=tables, has_image=bool(images), page_rotation=0,
                warnings=[], page_features=a.geometry(blocks, images, tables))


def quality(**extra):
    return dict(blank=False, contrast=50., sharpness=100., warnings=[], **extra)


def test_union_duplicate_and_overlapping_placements():
    assert a.union_area([[0,0,1,1],[0,0,1,1]]) == 1
    assert a.union_area([[0,0,.6,1],[.4,0,1,1]]) == 1
    assert a.union_area([[.1,.1,.3,.3],[.6,.6,.8,.8]]) == pytest.approx(.08)


def test_invalid_geometry_cannot_supply_coverage():
    assert a.union_area([[0,0,2,1],[0,0,0,1],[0,0,float('nan'),1]]) == 0


def test_ordinary_native_no_ocr_and_input_immutable():
    raw=extracted();saved=copy.deepcopy(raw)
    p=a.plan(raw,'原生金额12.50元',quality())
    assert p['requested_route']=='native_only' and not p['executed']
    assert raw==saved


@pytest.mark.parametrize('change,expected',[
    ('image','substantial_embedded_image'),
    ('formula','native_formula_structure_check'),
    ('table','native_table_relationship_check'),
    ('low_contrast','measured_low_contrast'),
    ('low_sharpness','measured_low_sharpness'),
    ('columns','measured_two_column_candidate'),
])
def test_measured_triggers_select_comparison_not_replacement(change, expected):
    raw=extracted();q=quality()
    if change=='image':raw=extracted(images=[[.1,.4,.9,.8]])
    if change=='formula':raw['page_features']['formula_candidate_count']=1
    if change=='table':raw['page_features']['native_table_count']=1
    if change=='columns':raw['page_features']['multi_column_candidate']=True
    if change=='low_contrast':q['contrast']=10
    if change=='low_sharpness':q['sharpness']=20
    p=a.plan(raw,'原生金额12.50元',q)
    assert p['requested_route']=='native_with_ocr_comparison'
    assert expected in p['reasons']


def test_blank_skips_even_when_image_present():
    q=quality();q['blank']=True
    assert a.plan(extracted(blocks=[],images=[[0,0,1,1]]),'',q)['requested_route']=='blank_no_ocr'


def test_scanned_page_no_native_primary_ocr():
    assert a.plan(extracted(blocks=[],images=[[0,0,1,1]]),'',quality())['requested_route']=='ocr_primary'


@pytest.mark.parametrize('text,box,basis',[
    ('9',(.48,.94,.52,.97),'marginal_native_over_page_image'),
    ('原件标题',(.1,.13,.7,.17),'isolated_header_over_page_image'),
    ('CONFIDENTIAL',(.4,.49,.6,.51),'explicit_watermark_over_page_image'),
])
def test_sparse_scan_exception_requires_actual_geometry(text,box,basis):
    p=a.plan(extracted(blocks=[block(text,box)],images=[[0,0,1,1]]),text,quality())
    assert p['requested_route']=='ocr_primary' and p['native_primary_exception']==basis


def test_short_business_text_in_middle_not_demoted_to_watermark():
    p=a.plan(extracted(images=[[0,0,1,1]]),'原生金额12.50元',quality())
    assert p['requested_route']=='native_with_ocr_comparison'


def test_truncated_geometry_cannot_claim_sparse_scan_exception():
    raw=extracted(blocks=[block('9',(.48,.94,.52,.97))],images=[[0,0,1,1]])
    raw['page_features']['geometry_truncated']=True
    assert a.plan(raw,'9',quality())['requested_route']=='native_with_ocr_comparison'


def ocr_result(text='OCR金额12.80元',box=(.1,.2,.7,.25)):
    text,blocks=text_blocks([block(text,box)])
    if blocks:blocks[0]['confidence']=.42
    return dict(text=text,blocks=blocks,extraction='ocr',quality=quality(),tables=[])


def test_difference_keeps_values_and_confidence_without_conflict_claim():
    raw=extracted(images=[[0,0,1,1]]);p=a.plan(raw,'原生金额12.50元',quality())
    c=a.comparison('原生金额12.50元',raw['blocks'],ocr_result(),p)
    assert c['numeric_token_disagreement'] and c['requires_original_page_review']
    assert '12.50' in c['native_text'] and '12.80' in c['ocr_text']
    assert c['selected']=='native' and c['observations'][0]['confidence']==.42
    assert c['evidence_status']=='unverified_ocr_candidate'


def test_large_observations_bounded_with_explicit_truncation():
    r=ocr_result();r['text']='x'*50000;r['blocks']=[block('x'*1000,(.1,.2,.7,.25)) for _ in range(500)]
    c=a.comparison('native',[],r,{'requested_route':'native_with_ocr_comparison'})
    assert c['truncated'] and len(c['ocr_text'])<=18000
    assert sum(len(b['text']) for b in c['observations'])<=24000


def test_numeric_excerpt_truncation_is_explicit():
    text=' '.join(str(i) for i in range(300))
    c=a.comparison(text,[],ocr_result(text),{'requested_route':'native_with_ocr_comparison'})
    assert c['numeric_tokens_truncated'] and len(c['native_numeric_tokens'])==256
    assert not c['numeric_token_disagreement']


def mock_pdf(monkeypatch, raw, *, ocr=None, cancelled=None):
    calls=[]
    def worker(path,index=None,image_path=None,*args,**kwargs):
        if index is None:return {'page_count':1,'outline':[]}
        im=Image.new('RGB',(600,800),'white');d=ImageDraw.Draw(im)
        for y in range(80,600,30):d.text((50,y),'Measured page 12.50 native and image',fill='black')
        im.save(image_path);return copy.deepcopy(raw)
    def recognize(image,timeout,**kw):
        calls.append(timeout)
        if isinstance(ocr,BaseException):raise ocr
        return copy.deepcopy(ocr or ocr_result())
    monkeypatch.setattr(parsers,'_pdf_worker',worker)
    monkeypatch.setattr(ocr_adapter,'recognize',recognize)
    # Keep explicit route trigger deterministic; OCR quality remains actual return.
    monkeypatch.setattr('backend.ocr_native.quality_metrics',lambda im:quality())
    return calls


def test_parser_native_offsets_and_grid_unchanged_after_disagreement(tmp_path,monkeypatch):
    raw=extracted(images=[[.1,.4,.9,.8]])
    calls=mock_pdf(monkeypatch,raw)
    result=parsers.parse_pdf(tmp_path/'fake.pdf',lambda *a:None)
    p=result['pages'][0]
    assert p['text']=='原生金额12.50元' and p['extraction']=='native'
    assert p['text'][p['blocks'][0]['start']:p['blocks'][0]['end']]==p['blocks'][0]['text']
    assert len(calls)==1 and p['quality']['visual_review_required']
    assert p['chart_observations'][0]['text']=='OCR金额12.80元'
    assert p['chart_observations'][0]['confidence']==.42
    route=result['metadata']['document_profiles']['processing_plan'][0]
    assert route['executed'] and route['verification_status']=='not_verified'


def test_parser_page_number_scan_selects_ocr_but_preserves_native(tmp_path,monkeypatch):
    raw=extracted(blocks=[block('9',(.48,.94,.52,.97))],images=[[0,0,1,1]])
    mock_pdf(monkeypatch,raw,ocr=ocr_result('正文金额12.50元，另有页码9'))
    result=parsers.parse_pdf(tmp_path/'fake.pdf',lambda *a:None);p=result['pages'][0]
    assert p['extraction']=='ocr' and p['text'].startswith('正文')
    assert p['ocr_comparison']['native_text']=='9'
    assert p['ocr_comparison']['native_blocks'][0]['text']=='9'
    assert p['adaptive_processing']['native_primary_exception']=='marginal_native_over_page_image'


def test_parser_ordinary_native_does_not_call_ocr(tmp_path,monkeypatch):
    calls=mock_pdf(monkeypatch,extracted())
    p=parsers.parse_pdf(tmp_path/'fake.pdf',lambda *a:None)['pages'][0]
    assert not calls and p['text']=='原生金额12.50元'
    assert p['adaptive_processing']['executed'] and not p['adaptive_processing']['ocr_attempted']


def test_limit_is_explicit_and_not_cached_as_completed(tmp_path,monkeypatch):
    path=tmp_path/'fake.pdf';path.write_bytes(b'fixture')
    calls=mock_pdf(monkeypatch,extracted(images=[[0,0,1,1]]));monkeypatch.setattr(parsers,'MAX_OCR_PAGES',0)
    r=parsers.parse_pdf(path,lambda *a:None,checkpoint_dir=tmp_path/'cache')
    assert not calls and r['pages'][0]['text']=='原生金额12.50元'
    assert not r['pages'][0]['adaptive_processing']['executed']
    assert r['metadata']['import_batches']['pending_pages']==[1]


def test_failed_ocr_retains_native_and_pending(tmp_path,monkeypatch):
    from backend.ocr_native import OCRError
    path=tmp_path/'fake.pdf';path.write_bytes(b'fixture')
    mock_pdf(monkeypatch,extracted(images=[[0,0,1,1]]),ocr=OCRError('local OCR unavailable'))
    r=parsers.parse_pdf(path,lambda *a:None,checkpoint_dir=tmp_path/'cache')
    assert r['pages'][0]['text']=='原生金额12.50元'
    assert r['pages'][0]['adaptive_processing']['status']=='ocr_failed'
    assert r['metadata']['import_batches']['pending_pages']==[1]


def test_cancel_late_ocr_does_not_publish_or_checkpoint(tmp_path,monkeypatch):
    path=tmp_path/'fake.pdf';path.write_bytes(b'fixture');state={'cancel':False}
    mock_pdf(monkeypatch,extracted(images=[[0,0,1,1]]))
    def late(*args,**kw):state['cancel']=True;return ocr_result()
    monkeypatch.setattr(ocr_adapter,'recognize',late)
    with pytest.raises(InterruptedError):
        parsers.parse_pdf(path,lambda *a:None,checkpoint_dir=tmp_path/'cache',cancelled=lambda:state['cancel'])
    assert not list((tmp_path/'cache').rglob('*.json'))


def test_checkpoint_reuses_completed_route_and_version_invalidates(tmp_path,monkeypatch):
    path=tmp_path/'fake.pdf';path.write_bytes(b'fixture')
    calls=mock_pdf(monkeypatch,extracted(images=[[0,0,1,1]]))
    one=parsers.parse_pdf(path,lambda *a:None,checkpoint_dir=tmp_path/'cache')
    two=parsers.parse_pdf(path,lambda *a:None,checkpoint_dir=tmp_path/'cache')
    assert len(calls)==1 and two['metadata']['import_batches']['reused_pages']==[1]
    assert one['pages']==two['pages']
    monkeypatch.setattr(parsers,'PARSER_VERSION','test-new-parser-version')
    three=parsers.parse_pdf(path,lambda *a:None,checkpoint_dir=tmp_path/'cache')
    assert len(calls)==2 and three['metadata']['import_batches']['reused_pages']==[]


def test_real_pdf_geometry_rotation_and_duplicate_images(tmp_path):
    from backend.pdf_extract import extract
    img=tmp_path/'image.png';Image.new('RGB',(50,50),'blue').save(img)
    path=tmp_path/'page.pdf'
    with pymupdf.open() as d:
        p=d.new_page(width=600,height=800)
        p.insert_text((30,60),'Actual native text')
        xref=p.insert_image(pymupdf.Rect(60,400,540,720),filename=str(img))
        p.insert_image(pymupdf.Rect(60,400,540,720),xref=xref)
        p.set_rotation(90);d.save(path)
    result=extract(path,0,tmp_path/'preview.png');f=result['page_features']
    assert f['image_bbox_coverage']==pytest.approx(.32)
    assert f['bbox_source']=='pymupdf_original_page_geometry'
    assert all(a.valid_box(b) for b in f['image_boxes'])


def test_total_time_expired_after_render_does_not_call_ocr(tmp_path,monkeypatch):
    calls=mock_pdf(monkeypatch,extracted(images=[[0,0,1,1]]))
    worker=parsers._pdf_worker;clock={'value':0.}
    def rendering(*args,**kw):
        result=worker(*args,**kw)
        if len(args)>1 and args[1] is not None:clock['value']=2.
        return result
    monkeypatch.setattr(parsers,'_pdf_worker',rendering)
    monkeypatch.setattr(parsers.time,'monotonic',lambda:clock['value'])
    monkeypatch.setattr(parsers,'MAX_DOCUMENT_SECONDS',1)
    r=parsers.parse_pdf(tmp_path/'fake.pdf',lambda *args:None)
    assert not calls and r['pages'][0]['text']=='原生金额12.50元'
    assert not r['pages'][0]['adaptive_processing']['ocr_attempted']
    assert r['metadata']['import_batches']['pending_pages']==[1]


def test_character_limit_prevents_primary_ocr_partial_publication(tmp_path,monkeypatch):
    mock_pdf(monkeypatch,extracted(blocks=[],images=[[0,0,1,1]]),ocr=ocr_result('真实OCR正文超过预算'))
    monkeypatch.setattr(parsers,'MAX_TEXT',3)
    r=parsers.parse_pdf(tmp_path/'fake.pdf',lambda *args:None)
    assert r['pages']==[] and r['metadata']['quality_summary']['processing_truncated']
    assert r['metadata']['import_batches']['pending_pages']==[1]


def test_empty_primary_ocr_does_not_cache_page_number_as_complete(tmp_path,monkeypatch):
    path=tmp_path/'fake.pdf';path.write_bytes(b'fixture')
    raw=extracted(blocks=[block('9',(.48,.94,.52,.97))],images=[[0,0,1,1]])
    calls=mock_pdf(monkeypatch,raw,ocr=ocr_result(''))
    first=parsers.parse_pdf(path,lambda *a:None,checkpoint_dir=tmp_path/'cache')
    assert first['pages'][0]['text']=='9'
    assert first['pages'][0]['adaptive_processing']['status']=='ocr_primary_empty'
    assert first['metadata']['import_batches']['pending_pages']==[1]
    # Failed observations persist separately, never as completed-page cache.
    assert not list((tmp_path/'cache').rglob('[0-9][0-9][0-9][0-9][0-9][0-9].json'))
    assert len(list((tmp_path/'cache').rglob('*.pending.json'))) == 1
    second=parsers.parse_pdf(path,lambda *a:None,checkpoint_dir=tmp_path/'cache')
    assert len(calls)==2 and not second['metadata']['import_batches']['reused_pages']
