"""Search-only variants. Never replace page text, quotations, numbers or units."""
from __future__ import annotations
import re

VERSION = 'search-variants-1'
QUERY_EXPANSION_VERSION = 'bilingual-engineering-terms-v2'
# Search vocabulary, not source corrections or answer rules. No document IDs,
# page numbers, benchmark questions, values or equations belong in this list.
# Longer phrases take precedence over overlapping short phrases.
_SEARCH_TERMS = {
    '单轴抗压强度': 'uniaxial unconfined compressive strength', '点荷载': 'point load',
    '尺寸修正': 'size correction corrected', '排水摩擦角': 'drained friction angle',
    '无黏性土': 'cohesionless soil', '密砂': 'dense sand', '松砂': 'loose sand',
    '抗滑力': 'sliding resistance', '安全系数': 'factor safety', '滑动': 'sliding',
    '桥台': 'abutment', '地基': 'foundation', '承载力': 'bearing capacity',
    '锚固': 'anchorage anchor', '抗拉': 'tensile tension', '压缩': 'compression',
    '热力学第二定律': 'second law thermodynamics', '辐射换热': 'radiation heat transfer',
    '传热': 'heat transfer', '热传导': 'conduction', '对流': 'convection',
    '灰体': 'gray grey body', '黑体': 'black body blackbody', '发射率': 'emissivity',
    '比热': 'specific heat', '热效率': 'thermal efficiency', '热交换器': 'heat exchanger',
    '蒸汽发生器': 'steam generator', '饱和压力': 'saturation saturated pressure',
    '饱和温度': 'saturation saturated temperature', '差压': 'differential pressure',
    '密度补偿': 'density compensation compensated', '液位': 'level',
    '电阻': 'resistance', '热电偶': 'thermocouple', '测温': 'temperature measurement',
    '反馈': 'feedback', '控制回路': 'control loop', '设定值': 'setpoint',
    '润滑油': 'lubricating lube oil', '冷却水': 'cooling water', '阀门': 'valve',
    '设计处理能力': 'design capacity flow', '实际流量': 'actual average flow',
    '进水': 'influent', '出水': 'effluent', '总氮': 'total nitrogen', '总磷': 'total phosphorus',
    '浓度': 'concentration', '营养盐': 'nutrient', '去除率': 'removal efficiency',
    '许可限值': 'permit limit', '研究期': 'study period', '研究时间': 'study period',
    '灯套管': 'lamp sleeve', '污垢': 'fouling', '检查频次': 'inspection frequency',
    '起始频次': 'initial frequency', '不清洗': 'no cleaning', '清洗': 'cleaning',
    '反应器': 'reactor', '紫外': 'ultraviolet UV', '透过率': 'transmittance',
    '膜过滤': 'membrane filtration', '跨膜压差': 'transmembrane pressure',
    '回收率': 'recovery', '完整性': 'integrity', '通量': 'flux',
    '逆流': 'counterflow', '错流': 'crossflow', '并流': 'parallel flow',
    '维护': 'maintenance', '操作': 'operation', '检测': 'detection',
    '温度': 'temperature', '压力': 'pressure', '流量': 'flow', '密度': 'density',
    '单位': 'unit', '公式': 'equation formula', '附录': 'appendix',
}


def expand_query(text, max_chars=10000):
    """Return transparent query-only aliases without changing the original.

    English terms already supplied by the user remain intact. The bounded
    dictionary is language assistance, never a substitute for evidence.
    """
    original = str(text)[:max_chars]
    normalized = original.translate(_TRAD)
    covered, matches, words = set(), [], []
    for phrase, english in sorted(_SEARCH_TERMS.items(), key=lambda item: -len(item[0])):
        for found in re.finditer(re.escape(phrase), normalized):
            positions = set(range(found.start(), found.end()))
            if positions & covered:
                continue
            covered |= positions
            matches.append({'term': phrase, 'aliases': english})
            words.extend(english.split())
            break
    # Recover coordinated ellipsis only when BOTH complete terms exist in the
    # finite dictionary: e.g. 进、出水 supplies 进水 as well as the literal 出水.
    # No document, page, answer value or inferred scientific definition enters.
    for found in re.finditer(r'([\u3400-\u9fff]{1,8})[、/和与]([\u3400-\u9fff]{2,8})', normalized):
        left, right_text = found[1], found[2]
        for right in _SEARCH_TERMS:
            if not right_text.startswith(right):
                continue
            for split in range(1, len(right)):
                for size in range(1, min(4, len(left)) + 1):
                    reconstructed = left[-size:] + right[split:]
                    if reconstructed not in _SEARCH_TERMS or reconstructed == right:
                        continue
                    english = _SEARCH_TERMS[reconstructed]
                    if not any(m['term'] == reconstructed for m in matches):
                        matches.append({'term': reconstructed, 'aliases': english,
                                        'basis': 'dictionary_confirmed_coordinated_ellipsis', 'literal': found[0]})
                        words.extend(english.split())
    aliases = ' '.join(dict.fromkeys(words))[:1200]
    return {'original': original, 'normalized': normalized, 'aliases': aliases,
            'expanded': original + ('\n' + aliases if aliases else ''),
            'matches': matches, 'version': QUERY_EXPANSION_VERSION,
            'scope': 'query_only_no_source_or_answer_rewrite'}
# Deliberately finite, auditable orthographic mapping; not a complete converter.
# Correct mapping explicitly, avoiding accidental positional strings.
_TRAD = str.maketrans({'體':'体','國':'国','學':'学','術':'术','圖':'图','書':'书','資':'资','產':'产','業':'业','務':'务','單':'单','數':'数','據':'据','總':'总','額':'额','報':'报','計':'计','劃':'划','時':'时','間':'间','區':'区','長':'长','發':'发','現':'现','實':'实','際':'际','標':'标','準':'准','關':'关','聯':'联','參':'参','變':'变','應':'应','經':'经','濟':'济','與':'与','為':'为','萬':'万','費':'费','臺':'台','灣':'湾','環':'环','質':'质','統':'统','測':'测','試':'试','處':'处','錄':'录','頁':'页','後':'后'})
_TRAD.update(str.maketrans({'內':'内','億':'亿'}))


def query_variants(text, max_chars=4000):
    text = str(text)[:max_chars]
    # NFKC of number/symbols may change business values; normalize CJK text only.
    simplified = text.translate(_TRAD)
    return list(dict.fromkeys([text, simplified]))


def candidates(pages, vocabulary=(), limit=200):
    result = []
    words = sorted({w for w in vocabulary if isinstance(w, str) and re.fullmatch(r'[\u4e00-\u9fff]{2,8}', w)})[:2000]
    for page in pages:
        text = page.get('text', '')
        for match in re.finditer(r'[\u4e00-\u9fff]{2,80}', text):
            original = match[0]
            normalized = original.translate(_TRAD)
            if normalized != original:
                result.append({'kind':'traditional_variant','original':original,'candidate':normalized,
                    'source_position':{'page':page['number'],'start':match.start(),'end':match.end()},
                    'usage':'retrieval_only','verified':False,'applied_to_source':False})
            # A spelling alternative requires an explicit finite vocabulary and
            # low OCR confidence at this exact source span; never "fix" numbers.
            low = any(isinstance(b.get('confidence'), (float,int)) and b['confidence'] < .75 and
                      b.get('start', -1) <= match.start() and b.get('end', -1) >= match.end()
                      for b in page.get('blocks', []))
            if low and original not in words:
                for word in words:
                    if len(word) == len(original) and sum(a != b for a,b in zip(word,original)) == 1:
                        result.append({'kind':'low_confidence_spelling_candidate','original':original,'candidate':word,
                            'source_position':{'page':page['number'],'start':match.start(),'end':match.end()},
                            'usage':'review_prompt_only','verified':False,'applied_to_source':False})
            if len(result) >= limit:
                return {'schema_version':VERSION,'items':result[:limit],'truncated':True,'source_modified':False}
    return {'schema_version':VERSION,'items':result,'truncated':False,'source_modified':False,
            'conversion_scope':'finite_orthographic_map_not_full_dictionary'}
