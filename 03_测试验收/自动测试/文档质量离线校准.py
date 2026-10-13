from pathlib import Path
import os,sys,tempfile,json,hashlib,io,time,copy
root=Path(__file__).resolve().parents[2];os.environ['EVIDENCEFLOW_DATA_DIR']=tempfile.mkdtemp(prefix='evidenceflow-quality-fixtures-');sys.path[:0]=[str(root/'01_程序代码'),str(root/'03_测试验收/自动测试')]
from tests.offline_network import install
install()
from backend import documents,document_quality as dq
import pymupdf
from PIL import Image,ImageFilter
rows=[];originals=[];quality_pages=[];out=Path(tempfile.mkdtemp(prefix='evidenceflow-calibration-images-'))
for file in ['淮南市2024年国民经济和社会发展统计公报.pdf','2024年新津区国民经济和社会发展统计公报.pdf','石景山区2024年国民经济和社会发展统计公报.pdf']:
 p=root/'02_数据资料/公开统计分析/原件'/file
 with pymupdf.open(p) as doc:
  raw=doc[0].get_pixmap(matrix=pymupdf.Matrix(2,2),alpha=False).tobytes('png')
 image=Image.open(io.BytesIO(raw)).convert('RGB');originals.append({'path':str(p.relative_to(root)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
 for mode in ['clean','blur3','skew4','metadata180','pixels180']:
  variant=image.filter(ImageFilter.GaussianBlur(3)) if mode=='blur3' else image.rotate(4,resample=Image.BICUBIC,fillcolor='white') if mode=='skew4' else image.rotate(180) if mode=='pixels180' else image
  b=io.BytesIO();variant.save(b,format='PNG');raw=b.getvalue();signals=dq.page_signals(raw)
  page={'number':len(rows)+1,'width':variant.width,'height':variant.height,'text':'','blocks':[],'images':[{'box':[0,0,1,1]}],'issues':[],'quality_signals':signals,'rotation':180 if mode=='metadata180' else 0}
  result=dq.inspect(page);codes=[i['code'] for i in result['issues'] if isinstance(i,dict)]
  rows.append({'source':file,'transform':mode,'image_sha256':hashlib.sha256(raw).hexdigest(),'signals':signals,'codes':codes,
    'known_skew_degrees':4 if mode=='skew4' else 0,'rotation_metadata':page['rotation'],'orientation_pixels_supported':False})
  quality_pages.append(result)
  if mode in ['clean','blur3']:variant.thumbnail((600,850));variant.save(out/(p.stem+'_'+mode+'.png'))
  if mode=='pixels180':assert not any(i['code']=='page_upside_down' for i in result['issues'])
  if mode=='metadata180':assert 'page_upside_down' in codes
outlines=[]
for row in originals:
 p=root/row['path'];pages=[documents.parse_page(p,n) for n in range(1,len(pymupdf.open(p))+1)]
 from backend.document_native import infer_pdf_outline
 outline=infer_pdf_outline(pages)
 signing=[b['text'] for pg in pages for b in pg['blocks'] if b.get('structure_role')=='attribution_candidate']
 assert not any(any(x in item['title'] for x in ['淮南市统计局国家统计局淮南调查队']) for item in outline)
 outlines.append({'source':row['path'],'count':len(outline),'attribution_candidates':signing,'titles':[i['title'] for i in outline],
  'source_text_preserved':True,'human_outline_labels':'pending','boundary':'真实原件的署名排除检查；不是完整目录人工F1评测。'})
# Labels are assigned by known transformations before measurements, not by this classifier.
blur=[x for x in rows if x['transform']=='blur3'];clean=[x for x in rows if x['transform']=='clean'];skew=[x for x in rows if x['transform']=='skew4']
report={'observed_at':'2026-10-12','source_snapshot':originals,'cases':rows,'outlines':outlines,
 'metrics':{'blur_absolute_alerts':sum('low_image_sharpness' in r['codes'] for r in blur),'blur_cases':len(blur),
  'clean_absolute_false_alerts':sum('low_image_sharpness' in r['codes'] for r in clean),'clean_cases':len(clean),
  'skew_alerts':sum('page_skewed' in r['codes'] for r in skew),'skew_cases':len(skew)},
 'boundaries':['受控退化样本不是独立真人标注集','像素180度颠倒未自动识别；仅有旋转元数据时告警','复杂度分数不表示问答准确率','表格、公式与历史定位另由结构和引用回归核验'],
 'external_model_calls':0}
import argparse
parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
path=Path(args.output).resolve();assert not path.exists();path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report['metrics'],ensure_ascii=False));print('Outlines',[(r['count'],len(r['attribution_candidates'])) for r in outlines])
