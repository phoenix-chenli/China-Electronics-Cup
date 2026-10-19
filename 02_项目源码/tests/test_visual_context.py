import base64,io
from pathlib import Path
import pytest
from PIL import Image
from backend.visual_context import page_content

def source(tmp_path):
    p=tmp_path/'original.png';Image.new('RGB',(2400,1200),'white').save(p)
    return dict(id='s',name='fixture',format='png',original_path=str(p),pages=[{'number':1}])

def test_original_bound_crop_and_not_modified(tmp_path):
    s=source(tmp_path);before=Path(s['original_path']).read_bytes();parts=page_content(s,1,[0,0,0.5,1])
    assert parts[0]['type']=='text'
    image=Image.open(io.BytesIO(base64.b64decode(parts[1]['image_url']['url'].split(',')[1])))
    assert image.size==(1200,1200) and Path(s['original_path']).read_bytes()==before

@pytest.mark.parametrize('page,bbox',[(0,None),(True,None),(2,None),(1,[0,0,2,1]),(1,[1,0,0,1]),(1,[0,0,float('nan'),1])])
def test_invalid_page_and_region_rejected(tmp_path,page,bbox):
    with pytest.raises(ValueError):page_content(source(tmp_path),page,bbox)

def test_unsupported_source(tmp_path):
    s=source(tmp_path);s['format']='txt'
    with pytest.raises(ValueError):page_content(s,1)


def test_transparent_black_text_keeps_same_white_background_as_parser(tmp_path):
    from PIL import ImageDraw
    from backend.ocr_native import normalized_image
    p=tmp_path/'transparent.png'
    original=Image.new('RGBA',(300,150),(0,0,0,0))
    ImageDraw.Draw(original).rectangle((50,45,130,95),fill=(0,0,0,255))
    original.save(p)
    s=dict(id='transparent',name=p.name,format='png',original_path=str(p),pages=[{'number':1}])
    before=p.read_bytes()
    parts=page_content(s,1)
    image=Image.open(io.BytesIO(base64.b64decode(parts[1]['image_url']['url'].split(',')[1]))).convert('RGB')
    parsed=normalized_image(original)
    assert image.getpixel((0,0)) == parsed.getpixel((0,0)) == (255,255,255)
    assert image.getpixel((90,70)) == parsed.getpixel((90,70)) == (0,0,0)
    assert p.read_bytes()==before
