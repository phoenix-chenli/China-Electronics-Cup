"""Frozen-input validation and semantic result scoring; no model dependencies."""
from __future__ import annotations
import hashlib
import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

CASE_KEYS = {'id','question','source_ids','mode','table_name','parent_case_id','tags','split','cluster','context_patch','chain_id','turn_index'}
SOURCE_KEYS = {'id','file','sha256','origin','license','cluster','kind'}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def code_digest(root):
    parts = [(str(p.relative_to(root)),digest(p)) for p in sorted((Path(root)/'backend').rglob('*.py'))]
    return hashlib.sha256(json.dumps(parts,separators=(',',':')).encode()).hexdigest()


def record_digest(record):
    return hashlib.sha256(json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def load_suite(path, verify_files=True):
    path = Path(path).resolve()
    checksum = path.with_suffix(path.suffix+'.sha256')
    if not checksum.is_file() or checksum.read_text().strip().split()[0] != digest(path):
        raise ValueError('评测输入未冻结或摘要不匹配')
    suite = json.loads(path.read_text())
    if suite.get('schema_version') != 'zhiheng.eval-input.v1':
        raise ValueError('评测输入版本不支持')
    if set(suite)-{'schema_version','name','sources','cases','seed','description'}:
        raise ValueError('输入集合含未许可字段；参考答案必须存放在独立评分文件')
    source_map = {}; cluster_splits = {}
    for source in suite['sources']:
        if set(source)-SOURCE_KEYS or source['id'] in source_map:
            raise ValueError('资料字段不合法或ID重复')
        file = (path.parent/source['file']).resolve()
        if not file.is_relative_to(path.parent) or not file.is_file():
            raise ValueError('资料必须位于输入集合目录中')
        if verify_files and digest(file) != source['sha256']:
            raise ValueError('评测原件摘要不匹配: '+source['id'])
        source_map[source['id']] = source
    seen = set(); cases_by_id = {}
    for case in suite['cases']:
        if set(case)-CASE_KEYS or case['id'] in seen:
            raise ValueError('问题字段不合法或ID重复；不能在模型输入中放参考答案')
        if not isinstance(case['question'],str) or not case['question'].strip():
            raise ValueError('问题不能为空')
        if case.get('mode') not in {'auto','database','document','multi_source','table'}:
            raise ValueError('评测问题模式无效')
        if not isinstance(case.get('source_ids'),list) or not case['source_ids'] or len(set(case['source_ids']))!=len(case['source_ids']):
            raise ValueError('问题资料列表无效')
        if case.get('context_patch') is not None:
            from backend.conversation_state import validate_patch
            validate_patch(case['context_patch'])
        if ('chain_id' in case)!=('turn_index' in case):
            raise ValueError('会话链标识与轮次须同时提供')
        if 'chain_id' in case and (not isinstance(case['chain_id'],str) or not case['chain_id'] or type(case['turn_index']) is not int or case['turn_index']<1):
            raise ValueError('会话链标识或轮次无效')
        if case.get('parent_case_id') and case['parent_case_id'] not in seen:
            raise ValueError('追问必须引用此前已有问题')
        parent = cases_by_id.get(case.get('parent_case_id'))
        if parent and (set(parent['source_ids'])!=set(case['source_ids']) or parent['split']!=case['split']):
            raise ValueError('会话范围或划分不一致')
        if 'chain_id' in case:
            if (case['turn_index']==1 and parent) or (case['turn_index']>1 and (not parent or parent.get('chain_id')!=case['chain_id'] or parent.get('turn_index')!=case['turn_index']-1)):
                raise ValueError('计入主链的轮次必须连续；侧分支探针另计')
            if any(c.get('chain_id')==case['chain_id'] and c.get('turn_index')==case['turn_index'] for c in cases_by_id.values()):
                raise ValueError('主链轮次重复')
        for sid in case['source_ids']:
            if sid not in source_map:
                raise ValueError('问题引用未知资料')
            cluster = source_map[sid]['cluster']
            cluster_splits.setdefault(cluster,set()).add(case['split'])
        seen.add(case['id']); cases_by_id[case['id']]=case
    if any(len(splits)>1 for splits in cluster_splits.values()):
        raise ValueError('同源资料簇跨开发/保留评估划分，不能宣称来源隔离')
    return suite


def input_payload(case, source_ids, parent=None, context_revision=None):
    """Only this allowlisted payload reaches the product/model pipeline."""
    import uuid
    ids = [source_ids[sid] for sid in case['source_ids']]
    payload = dict(question=case['question'],source_id=ids[0],source_ids=ids,
                   mode=case['mode'],request_id=str(uuid.uuid4()))
    if case.get('table_name'):
        payload['table_name']=case['table_name']
    if parent:
        payload['parent_answer_id']=parent
    if case.get('context_patch'):
        from backend.conversation_state import validate_patch
        payload['context_patch']=validate_patch(case['context_patch'])
        if parent:
            if not isinstance(context_revision,str) or not context_revision:
                raise ValueError('显式口径调整必须使用真实父轮上下文版本')
            payload['expected_context_revision']=context_revision
    return payload


def equal_value(actual, expected, tolerance):
    if actual is None or expected is None:
        return actual is None and expected is None
    if isinstance(expected,bool):
        return type(actual) is bool and actual == expected
    if isinstance(expected,(int,float)):
        if isinstance(actual,bool):
            return False
        try:
            left,right=Decimal(str(actual)),Decimal(str(expected))
            return left.is_finite() and right.is_finite() and abs(left-right)<=Decimal(str(tolerance))
        except (InvalidOperation,ValueError,TypeError):
            return False
    return type(actual) is type(expected) and actual == expected


def equal_rows(actual, expected, ordered=False, tolerance=0):
    if not isinstance(actual,list) or len(actual)!=len(expected):
        return False
    def match(left,right):
        return isinstance(left,list) and len(left)==len(right) and all(equal_value(a,b,tolerance) for a,b in zip(left,right))
    if ordered:
        return all(match(a,b) for a,b in zip(actual,expected))
    remaining=list(actual)
    for row in expected:
        index=next((i for i,value in enumerate(remaining) if match(value,row)),None)
        if index is None:
            return False
        remaining.pop(index)
    return True


def assess(expected, record):
    """Exact checks cannot certify free-text correctness. Keep review pending."""
    result=record.get('result') or {}
    expected_status=expected.get('status','completed')
    statuses=expected_status if isinstance(expected_status,list) else [expected_status]
    if record.get('status') not in statuses:
        return dict(passed=False,category='availability_failure' if 'completed' in statuses else 'unexpected_status',checks=['终态不匹配'])
    checks=[]
    if 'rows' in expected:
        if not equal_rows(result.get('rows'),expected['rows'],expected.get('ordered',False),expected.get('tolerance',0)):
            return dict(passed=False,category='incorrect_result',checks=['实际结果与参考多重集/顺序不同'])
        if not (result.get('query') or {}).get('sql') and not result.get('steps'):
            return dict(passed=False,category='missing_execution',checks=['数值结果缺真实执行记录'])
        checks.append('实际结果值/重复次数/要求的顺序匹配')
    if expected.get('require_evidence', record.get('status')=='completed') and not result.get('evidence'):
        return dict(passed=False,category='missing_evidence',checks=checks+['缺来源'])
    review=expected.get('review')
    if 'rows' not in expected or expected.get('require_text_review'):
        if not isinstance(review,dict) or not review.get('reviewer') or review.get('record_sha256')!=record_digest(record):
            return dict(passed=None,category='review_pending',execution_match=True if 'rows' in expected else None,
                        checks=checks+['文本或业务拒答需绑定此条记录的逐事实评判；终态/执行匹配不等于正确'])
        if (review.get('correct') is not True or review.get('complete') is not True or review.get('grounded') is not True
                or expected.get('require_context_review') and review.get('context_correct') is not True):
            return dict(passed=False,category='text_review_failed',checks=checks+['记录绑定的正确性/完整性/依据评判未通过'])
        checks.append('记录绑定评判通过；评判者='+review['reviewer'])
    return dict(passed=True,category='correct_negative' if record.get('status')!='completed' else 'passed',
                execution_match=True if 'rows' in expected else None,checks=checks)


def summarize(entries):
    scored=[e for e in entries if e.get('assessment')]
    n=len(entries); passed=sum(e['assessment']['passed'] is True for e in scored)
    pending=sum(e['assessment']['passed'] is None for e in scored)
    chains={}
    for entry in entries:
        if entry.get('chain_id'):chains.setdefault(entry['chain_id'],[]).append(entry)
    chain_results={name:dict(turns=len(items),passed=sum(i.get('assessment',{}).get('passed') is True for i in items),
                           all_passed=bool(items) and all(i.get('assessment',{}).get('passed') is True for i in items),
                           all_executed=all(i.get('record') is not None for i in items)) for name,items in chains.items()}
    return dict(total=n,passed=passed,pending_review=pending,
                not_run=sum(e.get('record') is None for e in entries),
                end_to_end_success_rate=passed/n if n else None,
                execution_matched=sum(e['assessment'].get('execution_match') is True for e in scored),
                chains=chain_results,whole_chain_success_rate=sum(c['all_passed'] for c in chain_results.values())/len(chains) if chains else None,
                categories=dict(Counter(e.get('assessment',{}).get('category','not_scored') for e in entries)))
