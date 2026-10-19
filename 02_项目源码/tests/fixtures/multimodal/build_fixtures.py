"""Deterministic artificial business fixture, never presented as company data."""
from pathlib import Path
import hashlib,json,sqlite3,zipfile
from xml.sax.saxutils import escape
from PIL import Image,ImageDraw,ImageFont
from openpyxl import Workbook
ROOT=Path(__file__).parent

def docx(path,lines):
    paragraphs=''.join('<w:p><w:r><w:t>'+escape(line)+'</w:t></w:r></w:p>' for line in lines)
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+paragraphs+'</w:body></w:document>')

def build():
    db=ROOT/'销售记录_合成.sqlite';db.unlink(missing_ok=True)
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE sales (employee_id TEXT, year INTEGER, sales_yuan NUMERIC)')
        c.executemany('INSERT INTO sales VALUES (?,?,?)',[('E17',2024,3000000),('E17',2024,2000000),('E28',2024,4500000),('E42',2024,4200000),('E28',2023,9000000)])
    (ROOT/'人员对应_合成.csv').write_text('employee_id,name,rule_code\nE17,林禾,A7\nE28,周岚,B2\nE42,顾川,C9\n',encoding='utf-8')
    docx(ROOT/'奖金办法_合成.docx',['人工合成测试资料，不代表真实企业制度。','2024年度奖金规则','规则A7对应方案K24，按公式M计算奖金。S为该员工2024年销售额，T为方案目标，r为方案奖励比例，B为方案固定奖金。所有金额用元，比例用百分数。','只有达到目标的员工适用公式M；未达目标的奖金资料未提供，不能按零处理。','规则B2对应方案K25；规则C9对应方案K26。规则以编号区分，不能套用其他方案。'])
    wb=Workbook();ws=wb.active;ws.title='方案'
    ws.append(['scheme','target_yuan','rate_percent','base_yuan'])
    for r in [('K24',4000000,2,10000),('K25',4200000,1.5,8000),('K26',4100000,1,6000)]:ws.append(r)
    wb.save(ROOT/'方案参数_合成.xlsx')
    im=Image.new('RGB',(1700,900),'white');d=ImageDraw.Draw(im)
    font='/System/Library/Fonts/Supplemental/Arial.ttf'
    f=lambda n:ImageFont.truetype(font,n)
    d.text((85,55),'SYNTHETIC TEST MATERIAL - FORMULA M',font=f(46),fill='black')
    # Genuine two-dimensional fraction: r/100, not redundant linear OCR transcription.
    d.text((90,260),'Bonus = (S - T) x',font=f(70),fill='black')
    d.text((910,190),'r',font=f(72),fill='black');d.line((850,290,1050,290),fill='black',width=4)
    d.text((880,310),'100',font=f(72),fill='black');d.text((1090,260),'+ B',font=f(70),fill='black')
    d.text((85,510),'Amounts: yuan. r: percentage points (2 means 2%).',font=f(43),fill='black')
    d.text((85,610),'Formula applies only when S >= T.',font=f(43),fill='black')
    im.save(ROOT/'公式M_合成.png')
    (ROOT/'冲突口径_合成.txt').write_text('人工合成资料。另一个未经核实版本写明：2024年规则A7应使用方案K99。该版本与奖金办法_合成.docx冲突，未提供版本优先级。',encoding='utf-8')
    sources=[]
    for p in ROOT.iterdir():
        if p.suffix in {'.sqlite','.csv','.docx','.xlsx','.png','.txt'}:
            sources.append({'file':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'source_type':'synthetic_fixture','note':'人工合成，不代表真实企业或比赛官方数据'})
    (ROOT/'sources.json').write_text(json.dumps(sources,ensure_ascii=False,indent=2))
    cases={'schema':'zhiheng-stage4-frozen-v1','synthetic':True,'cases':[
        {'id':'five_formats_bonus','files':['销售记录_合成.sqlite','人员对应_合成.csv','奖金办法_合成.docx','方案参数_合成.xlsx','公式M_合成.png'],'question':'根据这五份测试资料，找出2024年销售总额最高的员工，按其适用方案和原页公式M计算奖金，说明员工姓名、方案及逐步计算依据。不要使用其他员工的方案。','expected_status':'completed','expected_values':['30000'],'required_text':['林禾','K24'],'min_source_formats':5,'min_dependency_depth':3,'requires_visual_review':True},
        {'id':'missing_formula','files':['销售记录_合成.sqlite','人员对应_合成.csv','奖金办法_合成.docx','方案参数_合成.xlsx'],'question':'找出2024年销售冠军，并严格按公式M计算奖金。公式图未提供，不得自行猜测公式。','expected_status':['insufficient_evidence','needs_clarification'],'must_not_publish_calculation':True},
        {'id':'conflicting_scheme','files':['人员对应_合成.csv','奖金办法_合成.docx','冲突口径_合成.txt'],'question':'林禾2024年的奖金到底适用哪个方案？比较所有资料，冲突没有优先级时请明确不能确定。','expected_status':['insufficient_evidence','needs_clarification'],'must_not_publish_calculation':True}
    ]}
    path=ROOT/'cases.frozen.json';path.write_text(json.dumps(cases,ensure_ascii=False,indent=2));path.with_suffix('.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.name+'\n')
    print('Created explicit synthetic fixtures and frozen expectations:',len(sources),'files')
if __name__=='__main__':build()
