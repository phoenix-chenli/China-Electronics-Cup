"""Real, offline OCR on reproducible authored diagnostics; no cloud or secrets.

Run with the bundled runtime environment, --output pointing to an external
temporary directory. Requires the explicitly installed, verified OCR pack.
These authored development/validation images are not an independent benchmark.
"""
from pathlib import Path
import argparse,copy,hashlib,io,json,os,sys,time,unicodedata

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'01_程序代码/启动与打包'),str(ROOT/'01_程序代码')]
from runtime_bootstrap import activate
activate()
import numpy as np
from PIL import Image,ImageDraw,ImageFont,ImageFilter
from backend import document_providers as provider,page_preprocessing as prep


def norm(text):return ''.join(unicodedata.normalize('NFKC',text).split()).casefold()


def metrics(lines, expected):
    found=[norm(line['text']) for line in lines]
    confirmed=[norm(line['text']) for line in lines if line.get('status')=='confirmed']
    return {'expected_lines':len(expected),'literal_lines':sum(norm(s) in found for s in expected),
            'confirmed_literal_lines':sum(norm(s) in confirmed for s in expected),
            'missing_lines':[s for s in expected if norm(s) not in found],
            'pending_lines':[line['text'] for line in lines if line.get('status')!='confirmed'],
            'other_confirmed_observations':[line['text'] for line in lines if line.get('status')=='confirmed' and norm(line['text']) not in {norm(s) for s in expected}],
            'note':'Whitespace/case/width-normalized full-line diagnostic; extra fragments are preserved, not automatically counted as semantic errors.'}


def base_image(validation,font):
    expected=['DOCUMENT PREPROCESSING DIAGNOSTIC','Revenue 2025: 120.50 CNY','Operating profit: -18.75 CNY',
              'Growth rate: 12.5%','Sample count: 1e3','Region North: 160','Region South: 200','单位：万元','华东收入：120.50','华南利润：-18.75']
    if validation:expected=['SECOND LAYOUT CHECK','Balance 2026: -83.25 USD','Discount: 7.35%','Units sold: 2468',
                            'Rate exponent: 2e4','West total: 319.75','East total: 506.25','指标单位：万元','西北收入：319.75','华北结余：-83.25']
    im=Image.new('RGB',(1200,1500),'white');draw=ImageDraw.Draw(im);f=ImageFont.truetype(str(font),32)
    for i,text in enumerate(expected):draw.text((100 if not validation else 145,90+i*125),text,font=f,fill='black')
    return im,expected


def degrade(image, name):
    a=np.asarray(image).astype(float);h,w=a.shape[:2];rng=np.random.default_rng(719)
    if name in {'shadow','combined'}:
        x=np.linspace(.32,1,w);y=np.linspace(.85,1,h)
        a*=y[:,None,None]*x[None,:,None]
    if name in {'low_contrast','combined'}:a=190+a*(30/255)
    if name in {'noise','combined'}:a+=rng.normal(0,13 if name=='noise' else 6,a.shape[:2])[:,:,None]
    image=Image.fromarray(np.clip(a,0,255).astype('uint8'))
    if name in {'skew_positive','combined'}:image=image.rotate(-4,expand=True,fillcolor='white',resample=Image.Resampling.BICUBIC)
    elif name=='skew_negative':image=image.rotate(3.5,expand=True,fillcolor='white',resample=Image.Resampling.BICUBIC)
    elif name=='small':image=image.resize((600,750),Image.Resampling.LANCZOS)
    elif name=='blur':image=image.filter(ImageFilter.GaussianBlur(1.7))
    elif name=='sideways':image=image.transpose(Image.Transpose.ROTATE_90)
    elif name=='dark_background':image=Image.fromarray(255-np.asarray(image))
    elif name=='transparent':
        rgba=np.zeros((*a.shape[:2],4),dtype='uint8');rgba[:,:,:3]=0;rgba[:,:,3]=255-np.asarray(image)[:,:,0];image=Image.fromarray(rgba)
    return image


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--phase',choices=['development','validation'],default='development');parser.add_argument('--font',type=Path)
    args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    fonts=[args.font,Path('C:/Windows/Fonts/msyh.ttc'),Path('/System/Library/Fonts/PingFang.ttc')]
    font=next((f for f in fonts if f and f.is_file()),None)
    if font is None:raise SystemExit('Provide --font pointing to an installed Chinese font')
    ready=provider.describe()
    if not ready.get('ready'):raise SystemExit(ready.get('reason') or 'OCR pack not installed; this evaluation does not install/download it')
    source=ROOT/'01_程序代码/backend/page_preprocessing.py'
    report={'phase':args.phase,'scope':'Authored synthetic local OCR diagnostic; not independent OHR/ChartQA or end-to-end RAG accuracy.',
            'model':provider.MODEL,'engine':provider.ENGINE_VERSION,'preprocessing_version':prep.VERSION,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'cloud_calls':0,'cases':[]}
    filename=out/(args.phase+'-'+time.strftime('%Y%m%d-%H%M%S')+'.json');assert not filename.exists()
    expected_image,expected=base_image(args.phase=='validation',font)
    cases=['clear','shadow','low_contrast','noise','skew_positive','skew_negative','small','blur','combined','sideways','dark_background','transparent']
    def save():filename.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    save()
    for name in cases:
        im=degrade(expected_image,name);buf=io.BytesIO();im.save(buf,format='PNG');raw=buf.getvalue();path=out/(args.phase+'-'+name+'.png')
        if path.exists():assert path.read_bytes()==raw,'Frozen synthetic input changed'
        else:path.write_bytes(raw)
        row={'id':name,'image_file':path.name,'image_sha256':hashlib.sha256(raw).hexdigest(),'expected':expected,'rotation':90 if name=='sideways' else 0};report['cases'].append(row)
        captured=[];started=time.monotonic()
        def recognize(data,**kwargs):
            value=provider.ocr(data,**kwargs);captured.append(copy.deepcopy(value));return value
        try:
            result=prep.recognize(raw,provider._image(raw),recognize,rotation=row['rotation'])
            row.update(original=metrics(captured[0]['lines'],expected),optimized=metrics(result['lines'],expected),
                       original_lines=captured[0]['lines'],optimized_lines=result['lines'],preprocessing=result['preprocessing'],
                       seconds=round(time.monotonic()-started,3),status='completed')
        except Exception as exc:row.update(status='failed',error=type(exc).__name__+': '+str(exc),seconds=round(time.monotonic()-started,3))
        save();print(json.dumps({k:row[k] for k in ['id','status','original','optimized','seconds'] if k in row},ensure_ascii=False),flush=True)
    assert hashlib.sha256(source.read_bytes()).hexdigest()==report['source_sha256'],'Source changed during diagnostic'
    print(json.dumps({'report':filename.name,'cases':len(report['cases'])},ensure_ascii=False))


if __name__=='__main__':main()
