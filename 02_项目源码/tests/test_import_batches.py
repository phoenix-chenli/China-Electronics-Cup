import copy
import json
from pathlib import Path
import pytest
from PIL import Image
from backend import parsers
from backend.import_batches import PageCheckpoints


def test_checkpoint_corruption_source_and_parser_revision_are_isolated(tmp_path):
    original=tmp_path/'original';original.write_bytes(b'original A')
    cache=PageCheckpoints(tmp_path/'cache',original,'v1')
    page={'number':1,'text':'Exact 12.50','extraction':'native'}
    assert cache.save(page,[])
    assert cache.load(1)['page']==page
    assert PageCheckpoints(tmp_path/'cache',original,'v2').load(1) is None
    original.write_bytes(b'original B')
    assert PageCheckpoints(tmp_path/'cache',original,'v1').load(1) is None
    path=next(cache.root.glob('*.json'));value=json.loads(path.read_text());value['body']['page']['text']='changed'
    path.write_text(json.dumps(value));assert cache.load(1) is None


def pdf_fixture(monkeypatch,tmp_path,count=3):
    original=tmp_path/'original.pdf';original.write_bytes(b'bounded fixture, mock page decoder')
    calls=[]
    def worker(path,index=None,image_path=None,timeout=None,**kw):
        if index is None:return {'page_count':count,'outline':[]}
        calls.append(index+1)
        Image.new('RGB',(300,400),'white').save(image_path)
        return {'blocks':[{'text':f'Original page {index+1} value 12.50','bbox':[.1,.1,.8,.2],'confidence':None,'kind':'text'}],
                'tables':[],'has_image':False,'page_rotation':0,'warnings':[],'printed_page_label':str(index+1)}
    monkeypatch.setattr(parsers,'_pdf_worker',worker)
    return original,calls


def test_interruption_resumes_completed_pages_without_reextracting_or_changing_offsets(tmp_path,monkeypatch):
    original,calls=pdf_fixture(monkeypatch,tmp_path)
    cache=tmp_path/'source-checkpoints';state={'cancel':False}
    def progress(stage,value,message):
        if len(calls)==1:state['cancel']=True
    with pytest.raises(InterruptedError):
        parsers.parse(original,'pdf',tmp_path/'first.sqlite',progress,checkpoint_dir=cache,cancelled=lambda:state['cancel'])
    assert calls==[1]
    result=parsers.parse(original,'pdf',tmp_path/'retry.sqlite',lambda *a:None,checkpoint_dir=cache)
    assert calls==[1,2,3]
    assert result['metadata']['import_batches']['reused_pages']==[1]
    assert result['metadata']['import_batches']['status']=='complete'
    for page in result['pages']:
        for block in page['blocks']:assert page['text'][block['start']:block['end']]==block['text']


def test_failed_page_is_not_cached_and_retry_recovers_only_failed_page(tmp_path,monkeypatch):
    original,calls=pdf_fixture(monkeypatch,tmp_path)
    worker=parsers._pdf_worker;failed=[False]
    def flaky(path,index=None,*args,**kwargs):
        if index==1 and not failed[0]:
            failed[0]=True;raise parsers.ParseError('fixture page timeout')
        return worker(path,index,*args,**kwargs)
    monkeypatch.setattr(parsers,'_pdf_worker',flaky)
    cache=tmp_path/'checkpoints'
    first=parsers.parse(original,'pdf',tmp_path/'first.sqlite',lambda *a:None,checkpoint_dir=cache)
    assert first['metadata']['import_batches']['pending_pages']==[2]
    second=parsers.parse(original,'pdf',tmp_path/'second.sqlite',lambda *a:None,checkpoint_dir=cache)
    assert second['metadata']['import_batches']['pending_pages']==[]
    assert second['metadata']['import_batches']['reused_pages']==[1,3]
    assert calls==[1,3,2]


def test_ocr_budget_pages_remain_pending_and_later_batch_progresses(tmp_path,monkeypatch):
    from backend import ocr_adapter
    original,calls=pdf_fixture(monkeypatch,tmp_path)
    worker=parsers._pdf_worker
    def scan(path,index=None,*args,**kwargs):
        item=worker(path,index,*args,**kwargs)
        if index is not None:item.update(blocks=[],has_image=True)
        return item
    monkeypatch.setattr(parsers,'_pdf_worker',scan)
    monkeypatch.setattr(parsers,'MAX_OCR_PAGES',1)
    monkeypatch.setattr('backend.ocr_native.quality_metrics',lambda image:{'blank':False,'warnings':[]})
    monkeypatch.setattr(ocr_adapter,'recognize',lambda image,timeout,**kw:{'text':'原字12.50','blocks':[{'text':'原字12.50','start':0,'end':7,'bbox':[0,0,1,1],'kind':'text','confidence':.9}],
                      'extraction':'ocr','quality':{'warnings':[],'blank':False},'tables':[]})
    cache=tmp_path/'checkpoints'
    result=parsers.parse(original,'pdf',tmp_path/'a.sqlite',lambda *a:None,checkpoint_dir=cache)
    assert result['metadata']['import_batches']['pending_pages']==[2,3]
    result=parsers.parse(original,'pdf',tmp_path/'b.sqlite',lambda *a:None,checkpoint_dir=cache)
    assert result['metadata']['import_batches']['pending_pages']==[3]
    result=parsers.parse(original,'pdf',tmp_path/'c.sqlite',lambda *a:None,checkpoint_dir=cache)
    assert result['metadata']['import_batches']['status']=='complete'
    assert result['metadata']['import_batches']['reused_pages']==[1,2]
