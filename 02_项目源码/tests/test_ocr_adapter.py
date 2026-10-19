from types import SimpleNamespace
import pytest
from PIL import Image
from backend import ocr_adapter as o


def test_nonmac_missing_ocr_is_preflight_visible(monkeypatch):
    monkeypatch.setattr(o.platform,'system',lambda:'Windows')
    monkeypatch.setattr(o.shutil,'which',lambda name:None)
    assert not o.capabilities()['available']
    with pytest.raises(o.OCRError,match='未安装'):o.recognize(Image.new('RGB',(100,100)))


def test_tesseract_real_tsv_boxes_and_no_fake_table(monkeypatch):
    monkeypatch.setattr(o.platform,'system',lambda:'Linux')
    monkeypatch.setattr(o.shutil,'which',lambda name:'/local/tesseract' if name=='tesseract' else None)
    tsv='level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t10\t20\t30\t10\t90\t12.50\n'
    def run(args,**kw):
        return SimpleNamespace(returncode=0,stdout='List of available languages (2):\nchi_sim\neng\n' if '--list-langs' in args else tsv.encode())
    monkeypatch.setattr(o.subprocess,'run',run)
    r=o.recognize(Image.new('RGB',(100,100)))
    assert r['text']=='12.50' and r['blocks'][0]['bbox']==[.1,.2,.4,.3]
    assert r['blocks'][0]['confidence']==.9 and r['tables']==[]
    assert r['quality']['ocr_engine']=='tesseract'


def test_tesseract_malformed_geometry_is_rejected(monkeypatch):
    monkeypatch.setattr(o.platform,'system',lambda:'Linux')
    monkeypatch.setattr(o,'capabilities',lambda:{'selected':'tesseract','tesseract':{'languages':['eng']},'chinese_available':False})
    monkeypatch.setattr(o.shutil,'which',lambda n:'/local/tesseract')
    raw=b'level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t90\t0\t40\t10\t99\t123\n'
    monkeypatch.setattr(o.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=raw))
    with pytest.raises(o.OCRError,match='坐标'):o.recognize(Image.new('RGB',(100,100)))


@pytest.mark.parametrize('recognized_word', ['"', '"quoted', '12"50'])
def test_tesseract_literal_quotes_never_consume_following_tsv_records(monkeypatch, recognized_word):
    monkeypatch.setattr(o.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(o, 'capabilities', lambda: {
        'selected': 'tesseract', 'tesseract': {'languages': ['chi_sim', 'eng']},
        'chinese_available': True,
    })
    monkeypatch.setattr(o.shutil, 'which', lambda name: '/local/tesseract')
    header = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
    raw = (header +
           f'5\t1\t1\t1\t1\t1\t10\t10\t20\t10\t90\t{recognized_word}\n' +
           '5\t1\t1\t1\t2\t1\t10\t30\t20\t10\t95\t7.30\n').encode()
    monkeypatch.setattr(o.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=raw))
    result = o.recognize(Image.new('RGB', (100, 100)))
    assert len(result['blocks']) == 2
    assert result['blocks'][0]['text'] == recognized_word
    assert result['blocks'][1]['text'] == '7.30'
    assert '\t' not in result['text']
    assert result['extraction'] == 'ocr'
