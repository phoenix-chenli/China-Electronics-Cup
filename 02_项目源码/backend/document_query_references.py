"""Exact question targets in explicitly uncertain document conclusions.

A question's identifier is not a source fact. Only its own occurrence can be
annotated; the surrounding limitation still needs independent semantic review.
"""
from __future__ import annotations

import hashlib
import re

_TARGET = re.compile(
    r'(?<![A-Za-z0-9_.])(?P<version>[Vv]\d{1,8}(?:\.\d{1,8}){1,3})(?![A-Za-z0-9_.])'
    r'|(?<![\d])(?P<date>\d{4}年\d{1,2}月\d{1,2}日|\d{4}-\d{2}-\d{2})(?![\d])')
_UNCERTAIN = re.compile(
    r'(?:无法|不能|尚不能|未能|尚未)(?:确认|确定|证实|断言|声称)'
    r'(?:用户所问的?|你所问的?|问题中提到的?|所问的?)?[\s“「\"\']*$')
_SCOPED_ABSENCE = re.compile(
    r'(?:本次|当前)(?:已读|检索到的|给定|提供的)(?:文档)?片段(?:中)?'
    r'(?:尚未|未|没有)(?:提供|给出|说明|提及|记录)(?:关于)?[\s“「\"\']*$')
_REVERSE = re.compile(r'并非|不是|不要|无需|不能说|不能认为|不得说|已经|已确认|已证实|事实上|实际上')
_REVERSE_AFTER = re.compile(r'[，,；;](?:但|然而|事实上|实际上|因此可确认|所以可确认)')
_LIMITATION_LEAD = re.compile(
    r'(?:(?:仅凭|依据|根据|基于|现有|当前|本次|目前|所给|给定|提供的|已读|检索到的|'
    r'这份|此份|这些|该|本|资料|材料|文档|片段|内容|证据|信息|中|的|仍|还|尚|暂时|因此|所以|我们|我)|\s)*')
_SEPARATORS = '。；;!?！？\n，,'

GENERATE = '''\nquery_references只表示真实用户问题中的查询对象，不是文档事实或假设授权。资料能核实的版本、日期及适用范围照常引用原文。对本次依据尚不能核实的用户查询对象，可以用“无法确认/不能声称”等明确不确定结论逐字提及该对象，不能把它说成存在、不存在、已生效或采用某规则。若说明未提供，必须限定为当前已读/给定片段，不能把未检索到等同整份原件不存在。未知结论须解释当前已引用资料的真实范围；未确认不等于证明不存在或内容不同。问题并列的对象分别回应，不能删掉某个未知对象来通过数字检查。若actual_user_question要求整理此前确认的版本或适用范围，须保留history_references中相关版本/日期查询对象的未确认边界，结合已引用原文解释，不因normalized_question省略这些对象而丢失已明确的限制；与本轮范围无关的历史对象不强行带入。'''
REVIEW = '''\ncontext_annotations.kind=user_query_reference的出现仅引用真实用户所问的对象，不是资料中的已知数值，也不是假设。对这些claim仍返回user_context_relations的四项布尔：intent_supported核对原样对象确来自当前actual_user_question或带source_answer_id的同范围真实祖先用户原话的精确span/hash；scope_supported核对结论只限当前已读依据、无全局缺失推断；rule_supported核对其quotes中已知版本/日期/适用范围及据此说明的证据边界；status_supported核对只表达未确认，没有变成存在/不存在/不同规则/已生效等确定事实。任一未知或当前已读资料实际足以确定所问内容时为false。查询对象的注记不支持同句或别处任何业务数值。并列对象必须逐个核对；只答其中一个不能把整个分项标answered，给定资料可支持范围限制但漏答时用evidence_available并引用实际片段ID。最终整理范围或版本时，同时核对actual_user_question和同范围history_references中的相关未知对象是否保留；漏掉已明确的相关未知边界时answer_complete=false，不因为normalized_question未重述或现有claims自身正确就判完整。不得为了完整性复述不相关历史或未经证据核实的旧回答。'''


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _direct(actual_user_question):
    if not isinstance(actual_user_question, str) or not actual_user_question or len(actual_user_question) > 12000:
        return None
    references = []
    seen = set()
    for match in _TARGET.finditer(actual_user_question):
        token = match[0]
        if token in seen:
            continue
        seen.add(token)
        references.append({'kind': 'version' if match.group('version') else 'date',
                           'quote': token, 'start': match.start(), 'end': match.end()})
        if len(references) == 24:
            break
    if not references:
        return None
    return {'schema_version': 'document-query-references-v1', 'origin': 'user_question',
            'source_text': actual_user_question, 'source_sha256': _hash(actual_user_question),
            'references': references, 'is_document_evidence': False}


def _annotate_one(text, context):
    if not isinstance(text, str) or not isinstance(context, dict):
        return text, []
    raw = context.get('source_text')
    fresh = _direct(raw)
    if not fresh or context.get('source_sha256') != fresh['source_sha256']:
        return text, []
    annotations = []
    chars = list(text)
    for target in context.get('references', []):
        if target not in fresh['references']:
            continue
        for occurrence in _TARGET.finditer(text):
            if occurrence[0] != target['quote']:
                continue
            start, end = occurrence.span()
            clause_start = max((text.rfind(mark, 0, start) for mark in _SEPARATORS), default=-1)+1
            prefix = text[clause_start:start]
            uncertain = _UNCERTAIN.search(prefix)
            scoped = _SCOPED_ABSENCE.search(prefix)
            if not (uncertain or scoped) or _REVERSE.search(prefix):
                continue
            marker = uncertain or scoped
            # A report of someone else's assertion is not our own uncertainty
            # statement (for example, "the manual says cannot confirm ...").
            if not _LIMITATION_LEAD.fullmatch(prefix[:marker.start()]):
                continue
            sentence_end = min((p for mark in '。！？!?\n' if (p := text.find(mark, end)) >= 0), default=len(text))
            if _REVERSE_AFTER.search(text[end:sentence_end]):
                continue
            annotation = {'kind': 'user_query_reference', 'origin': 'user_question',
                          'target_kind': target['kind'], 'quote': target['quote'],
                          'source_answer_id': context.get('source_answer_id','current'),
                          'source_text': raw, 'source_sha256': fresh['source_sha256'],
                          'source_start': target['start'], 'source_end': target['end'],
                          'start': start, 'end': end, 'is_document_evidence': False,
                          'is_hypothetical': False, 'uncertainty_required': True,
                          'semantic_review_required': True}
            if annotation not in annotations:
                annotations.append(annotation)
                chars[start:end] = ' '*(end-start)
    return ''.join(chars), annotations



def build(actual_user_question, history=(), *, source_revisions=None):
    """Current references or explicitly continued real same-scope user targets."""
    if not isinstance(actual_user_question,str) or len(actual_user_question)>12000:return None
    current=_direct(actual_user_question)
    inherited=[]
    if re.search(r'我们|以上|此前|刚才|继续|总结|整理|最终|这些|沿用',actual_user_question):
        from .document_user_context import validated_history
        for item in validated_history(history,source_revisions):
            previous=_direct(item['original_question'])
            if previous:
                inherited.append({**previous,'source_answer_id':item['answer_id']})
    if not current and not inherited:return None
    context=current or {'schema_version':'document-query-references-v1','origin':'user_question',
        'source_text':actual_user_question,'source_sha256':_hash(actual_user_question),
        'references':[],'is_document_evidence':False}
    context={**context,'source_answer_id':'current'}
    if inherited:context['history_references']=inherited
    return context


def annotate(text, context):
    if not isinstance(context,dict):return text,[]
    current_text=context.get('source_text')
    if not isinstance(current_text,str) or _hash(current_text)!=context.get('source_sha256'):return text,[]
    history=context.get('history_references',[])
    if not isinstance(history,list):return text,[]
    # Current raw words take precedence over recent ancestors. Only the exact
    # output occurrence is annotated, never a shared value whitelist.
    all_annotations=[];seen=set();chars=list(text)
    for item in [context]+list(reversed(history[-30:])):
        _,annotations=_annotate_one(text,item)
        for annotation in annotations:
            key=(annotation['start'],annotation['end'])
            if key in seen:continue
            seen.add(key);all_annotations.append(annotation)
            chars[key[0]:key[1]]=' '*(key[1]-key[0])
    return ''.join(chars),all_annotations
