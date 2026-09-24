from __future__ import annotations
from . import engine

CASES=[
  {"id":"sql-01","question":"2024年华东净销售额最高的产品是什么？","expected":"云安全平台"},
  {"id":"sql-02","question":"2024年华北有多少客户？","expected":"1 个客户"},
  {"id":"cross-01","question":"找出2024年华东销售冠军，并结合文档说明成功经验和奖金。","expected":"云安全平台"},
  {"id":"clarify-01","question":"增长率是多少？","expected":"needs_clarification"},
  {"id":"doc-01","question":"2024年激励规则的奖金公式是什么？","expected":"max("},
]
def smoke():
    results=[]
    for c in CASES:
        try:
            r=engine.execute(c['question'])
            if c['expected']=='needs_clarification': passed=r['status']=='needs_clarification'
            else: passed=c['expected'] in (r.get('answer') or '')
            results.append({'id':c['id'],'passed':passed,'status':r['status'],'latency_ms':r.get('latency_ms',0),'run_id':r['run_id'],'evidence_count':len(r.get('evidence',[]))})
        except Exception as exc:
            results.append({'id':c['id'],'passed':False,'status':'failed','error':str(exc),'latency_ms':0,'evidence_count':0})
    passed=sum(int(x['passed']) for x in results)
    return {'suite':'smoke-v0.2','run_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'passed':passed,'total':len(results),'accuracy':passed/len(results),'results':results,'boundary':'本地确定性模式；固定示例仅用于回归，不代表全场景准确率。'}
