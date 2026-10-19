"""Regression cases from the 2026-10-19 Windows OCR report."""
import pytest
from PIL import Image, ImageDraw
from backend import ocr_tesseract as t
from backend import ocr_adapter as adapter
from types import SimpleNamespace

HEADER = 'level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'


def record(text, page=1, confidence='90'):
    return f'5\t{page}\t1\t1\t1\t10\t10\t20\t10\t{confidence}\t{text}\n'


def test_quotes_are_literal_and_do_not_swallow_records():
    pages, count = t.parse_tsv((HEADER+record('"')+record('12.50')+record('"abc')).encode(), {1:(100,100)})
    assert count == 3
    assert [w['text'] for w in pages[1]] == ['"','12.50','"abc']
    assert t.line_blocks(pages[1])[0] == '" 12.50 "abc'


@pytest.mark.parametrize('raw', ['', HEADER, HEADER+record('1', confidence='nan'), HEADER+record('1',page=2), HEADER+record('1')+'bad\trow\n'])
def test_invalid_tsv_fails_closed(raw):
    with pytest.raises(ValueError):t.parse_tsv(raw.encode(), {1:(100,100)})


def test_incomplete_batch_rejected_and_real_blank_page_allowed():
    with pytest.raises(ValueError):t.parse_tsv((HEADER+record('1')).encode(), {1:(100,100),2:(100,100)})
    pages, _ = t.parse_tsv((HEADER+'1\t1\t0\t0\t0\t0\t0\t100\t100\t-1\t\n').encode(), {1:(100,100)})
    assert pages == {1:[]}


def test_only_cjk_spaces_are_normalized():
    assert t.join_words(['地','区','生产','总值','1','2','−','16.4','A','B']) == '地区生产总值 1 2 − 16.4 A B'


def grid():
    image=Image.new('RGB',(400,240),'white');draw=ImageDraw.Draw(image)
    for y in (20,80,140,200):draw.line((20,y,380,y),fill='black',width=2)
    for x in (140,260):draw.line((x,20,x,200),fill='black',width=2)
    words=[dict(text=str(i),bbox=box,confidence=.8,line_key=(1,1,1,i)) for i,box in enumerate(
        ([.1,.15,.2,.2],[.4,.15,.5,.2],[.7,.4,.8,.45]),1)]
    return image,words


def test_open_outer_table_has_physical_grid_and_review_boundary():
    image,words=grid();tables=t.table_candidates(image,words)
    assert len(tables)==1
    table=tables[0]
    assert (table['row_count'],table['column_count'])==(3,3)
    assert table['requires_review'] and table['rows'][0][:2]==['1','2']
    assert table['rows'][2]==[None,None,None]
    assert t.table_candidates(Image.new('RGB',image.size,'white'),words)==[]


def test_report_public_sample_grid_geometry_only():
    # Authored anchors exercise image geometry, NOT OCR accuracy or row labels.
    from pathlib import Path
    path=Path(__file__).parents[1]/'samples/石景山区统计公报_第2页原表.png'
    with Image.open(path) as image:
        words=[dict(text='anchor',bbox=[x/image.width,y/image.height,(x+20)/image.width,(y+10)/image.height],
                    confidence=.8,line_key=(1,1,1,i)) for i,(x,y) in enumerate(((170,310),(580,310),(740,355)),1)]
        tables=t.table_candidates(image,words)
    assert len(tables)==1
    assert (tables[0]['row_count'],tables[0]['column_count'])==(22,4)
    assert tables[0]['requires_review']


def test_crop_candidate_never_replaces_decimal_or_sign():
    image,words=grid();tables=t.table_candidates(image,words)
    jobs,truncated=t.cell_crops(image,tables)
    assert not truncated and len(jobs)==3
    original=[list(row) for row in tables[0]['rows']]
    pages={i:[dict(text='-1.2',bbox=[0,0,1,1],confidence=.99,line_key=(i,1,1,1))] for i in range(1,4)}
    assert t.retain_cell_candidates(jobs,pages)==3
    assert tables[0]['rows']==original
    assert jobs[0][1]['text']=='1'
    assert jobs[0][1]['ocr_candidates']==['1','-1.2']
    assert all(cell['requires_review'] for cell in tables[0]['cells'])


def configure(monkeypatch, run):
    monkeypatch.setattr(adapter.platform,'system',lambda:'Windows')
    monkeypatch.setattr(adapter,'capabilities',lambda:{'selected':'tesseract','tesseract':{'languages':['chi_sim','eng']},'chinese_available':True})
    monkeypatch.setattr(adapter.shutil,'which',lambda _: 'tesseract')
    monkeypatch.setattr(adapter.subprocess,'run',run)


def test_dpi_survives_temporary_png_and_engine_arguments(monkeypatch):
    def run(args,**kwargs):
        assert args[args.index('--dpi')+1]=='137'
        with Image.open(args[1]) as current:assert current.info['dpi'][0]==pytest.approx(137,abs=.1)
        return SimpleNamespace(returncode=0,stdout=(HEADER+record('12.50')).encode(),stderr=b'')
    configure(monkeypatch,run)
    image=Image.new('RGB',(100,100));image.info['dpi']=(136.83,136.83)
    result=adapter.recognize(image)
    assert result['quality']['input_dpi']==[137,137]


@pytest.mark.parametrize('stdout,stderr',[(b'',b''),(b'',b'read_params_file: Can\'t open tsv')])
def test_success_exit_without_tsv_is_not_blank_success(monkeypatch,stdout,stderr):
    configure(monkeypatch,lambda *a,**kw:SimpleNamespace(returncode=0,stdout=stdout,stderr=stderr))
    with pytest.raises(adapter.OCRError):adapter.recognize(Image.new('RGB',(100,100)))


def test_failed_crop_batch_retains_page_result(monkeypatch):
    image,words=grid()
    raw=HEADER+''.join(f'5\t1\t1\t1\t{i}\t{round(w["bbox"][0]*400)}\t{round(w["bbox"][1]*240)}\t40\t12\t90\t{w["text"]}\n' for i,w in enumerate(words,1))
    calls=[]
    def run(args,**kw):
        calls.append((args,kw['timeout']))
        return SimpleNamespace(returncode=0,stdout=raw.encode() if len(calls)==1 else b'',stderr=b'')
    configure(monkeypatch,run)
    result=adapter.recognize(image)
    assert len(calls)==2 and calls[1][1]<=15
    assert result['tables'][0]['rows'][0][:2]==['1','2']
    assert result['quality']['cell_review']['status']=='candidate_failed'


def test_parse_image_materializes_pending_review_table_without_changing_original(tmp_path,monkeypatch):
    import hashlib
    import sqlite3
    from backend import parsers
    image,words=grid();path=tmp_path/'scan.png';image.save(path,dpi=(137,137))
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    text,blocks=t.line_blocks(words)
    from backend.ocr_native import quality_metrics
    def recognize(current,**kwargs):
        assert t.input_dpi(current)==(137,137)
        return dict(text=text,blocks=blocks,quality=quality_metrics(current),extraction='ocr',tables=t.table_candidates(current,words))
    monkeypatch.setattr(adapter,'recognize',recognize)
    result=parsers.parse_image(path,tmp_path/'table.sqlite',lambda *a:None)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    assert result['metadata']['table_regions'][0]['requires_review']
    assert result['metadata']['table_semantics'][0]['requires_review']
    with sqlite3.connect(result['data_path']) as db:
        assert db.execute('SELECT * FROM "图片表1"').fetchall()[0]==('1','2',None)


def test_transparent_normalization_preserves_dpi():
    from backend.ocr_native import normalized_image
    image=Image.new('RGBA',(100,100));image.info['dpi']=(137,137)
    assert t.input_dpi(normalized_image(image))==(137,137)


def test_cancelled_crop_propagates(monkeypatch):
    from backend import reliability
    image,_=grid()
    configure(monkeypatch,lambda *a,**kw:None)
    def cancelled(*args,**kwargs):raise InterruptedError('cancelled')
    monkeypatch.setattr(reliability,'run_cancellable',cancelled)
    with pytest.raises(InterruptedError):adapter.recognize(image,cancelled=lambda:True)
