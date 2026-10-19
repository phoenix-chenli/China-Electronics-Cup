"""Source-local table evidence is a candidate, never an inferred SQL fact."""
import hashlib

from PIL import Image,ImageDraw

from backend import ocr_adapter,ocr_tesseract,parsers,table_context
from backend.ocr_native import quality_metrics


def fixture():
    image=Image.new('RGB',(1200,1600),'white')
    ImageDraw.Draw(image).text((30,20),'authored layout fixture',fill='black')
    observations=[]
    for r,row in enumerate([['地区','本期','上期'],['北区','-2.75','1.00'],['南区','0.00','4.50'],['东区','7.25','6.50']]):
        for c,text in enumerate(row):
            x=[.1,.48,.76][c];y=.1+r*.065
            observations.append(dict(text=text,bbox=[x,y,x+.1,y+.025],confidence=.94,line_key=(1,1,1,r+1)))
    return image,observations


def test_unruled_import_keeps_exact_source_cells_but_never_creates_sql_table(tmp_path,monkeypatch):
    image,words=fixture();path=tmp_path/'layout.png';image.save(path)
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    def recognize(current,**kwargs):
        tables=ocr_tesseract.table_candidates(current,words)
        assert len(tables)==1 and tables[0]['extraction']=='ocr_layout'
        text,blocks=ocr_tesseract.table_text_blocks(current,words,tables)
        return dict(text=text,blocks=blocks,tables=tables,quality=quality_metrics(current),extraction='ocr')
    monkeypatch.setattr(ocr_adapter,'recognize',recognize)
    destination=tmp_path/'tables.sqlite'
    result=parsers.parse_image(path,destination,lambda *a:None)
    assert result['tables']==[] and result['data_path'] is None and not destination.exists()
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    table=result['metadata']['table_regions'][0]
    assert table['sql_eligible'] is False and table['requires_review']
    current=result['pages'][0]
    for cell in table['cells']:
        assert current['text'][cell['start']:cell['end']]==cell['text']
    source=dict(result,id='fixture',format='png')
    selected=next(c for c in table['cells'] if c['row']==2 and c['col']==2)
    context=table_context.context_for_span(source,1,selected['start'],selected['end'])
    assert context['has_table'] and context['requires_review'] and not context.get('unresolved_structure')
    projection=context['tables'][0]
    assert projection['selected_cell_ids']==['R2C2']
    assert projection['sql_eligible'] is False
    assert next(c for c in projection['cells'] if c['cell_id']=='R2C2')['text']=='-2.75'


def test_cell_block_binding_does_not_locate_repeated_numbers_by_text():
    image,words=fixture()
    for word in words:
        if word['bbox'][0]>.4 and word['bbox'][1]>.15:word['text']='7.00'
    tables=ocr_tesseract.table_candidates(image,words)
    text,blocks=ocr_tesseract.table_text_blocks(image,words,tables)
    observed=[c for c in tables[0]['cells'] if c.get('text')=='7.00']
    assert len(observed)==6 and len({c['start'] for c in observed})==6
    assert all(text[c['start']:c['end']]=='7.00' for c in observed)
    assert all(text[b['start']:b['end']]==b['text'] for b in blocks)
