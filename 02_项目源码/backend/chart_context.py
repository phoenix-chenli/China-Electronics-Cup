"""Page-anchored chart observations and conservative claim bindings.

No image interpretation, bar-height digitization, or model calls occur here.
Passing this structural gate still requires the original page and a separate
chart-relation review. Page-level hints never assert a detected chart region.
"""
from __future__ import annotations

import copy
import json
import math
import re
from decimal import Decimal, InvalidOperation

VERSION = 'chart-context-v1'
MAX_OBSERVATIONS = 240
MAX_BINDINGS = 16
_NUMBER = re.compile(r'(?<![\d.,+\-])[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?(?:%|％|‰)?(?![\d.,%％‰])')
_TITLE = re.compile(r'^\s*(?:图\s*[0-9一二三四五六七八九十]+|Fig(?:ure)?\.?\s*\d+)', re.I)
_UNITS = ('百分点', '亿元', '万元', '万人', '万辆', '万吨', '千克', '公斤', '公里', '平方米',
          '小时', '千瓦时', '度', '元', '人', '吨', '天', '个', '台', '家', '件', '%', '％', '‰')
_UNIT = re.compile('|'.join(map(re.escape, _UNITS)))

CHART_INSTRUCTIONS = '''图表上下文只是同页真实文字观测与坐标，hints/has_candidate不是已验证的图形或关系，page范围也不是自动检测出的图框。
图表事实每条提供chart_bindings。精确印刷值使用{kind:"printed_point",evidence_id,statement,category,category_observation_id,value,value_observation_id,unit,unit_observation_id,association:"same_line|aligned_vertical|aligned_horizontal|grid_point|adjacent_label",series?,series_observation_id?,title?,title_observation_id?,period?,period_observation_id?,axis_side?:"left|right"}。value须复制完整印刷数值含正负/百分号，不能估读柱高、折线位置或由颜色猜系列；不同观测只接受唯一几何对齐。类别、系列、单位和时期均须指向本条引用的真实观测。
图下带数据表可用grid_point：category为完整列标签，series为完整行系列，value为唯一行列交点；必须绑定实际title。单位不在该系列行或数值标签内时，需axis_side并绑定同一图实际左右轴单位，独立复核仍须确认系列属于该轴。饼图上下紧邻的类别和百分数可用adjacent_label：必须是双向唯一紧邻标签对，单位必须印在该value观测中；如声明title，必须是最近真实标题，不能借用另一个图或另一个类别的标签。
一条binding写一个完整事实，先写claim.text，再原样复制为statement；多个点拆开陈述。不要将轴刻度当成点值，不要把有疑义的观测当确定值。表或页中的相同数字不能证明同一图点。
定性趋势或无法读取精确值的说明用{kind:"visual_observation",evidence_id,statement,observation_type:"trend|missing_printed_values|description",observation_ids:[]}，可列真实标签ID；不得虚造value，不得在定性句中夹带无印刷依据的精确数字。没有文字观测仍可看整页原图核对定性现象，不能宣称已有完整图结构。
truncated表示部分观测未进入上下文；一个所需观测完整的点可核对，但不能据此断言全图最高、全部类别或没有其他点。任何图事实（含缺少精确标签的说明）必须有本页原图，并经单独chart_relations:[{id:claim_id,supported:boolean}]逐条复核，确认系列/类别/轴/单位/时期及用户范围。缺项、否决或未看原图不能算通过；本地绑定通过不等于语义正确。
'''


def _size(value):
    return len(json.dumps(value, ensure_ascii=False))


def _norm(value):
    return re.sub(r'\s+', '', str(value if value is not None else ''))


def _box(value):
    return (isinstance(value, (list, tuple)) and len(value) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= 1 for v in value)
            and value[0] < value[2] and value[1] < value[3])


def _inside(box, region):
    area = (box[2] - box[0]) * (box[3] - box[1])
    overlap = max(0, min(box[2], region[2]) - max(box[0], region[0])) * max(0, min(box[3], region[3]) - max(box[1], region[1]))
    return overlap / area >= .8


def _numbers(text):
    result = set()
    for match in _NUMBER.finditer(str(text)):
        try:
            result.add(Decimal(match[0].replace('−', '-').replace(',', '').rstrip('%％‰')))
        except InvalidOperation:
            pass
    return result


def _number_tokens(text):
    # Preserve signs and ratio glyphs: the printed 12 cannot authorize 12%.
    return {match[0].replace('−', '-').replace('％', '%').replace(',', '')
            for match in _NUMBER.finditer(str(text))}


def context_for_page(source, page_number, *, bbox=None, max_chars=6000):
    """Keep whole, source-observed blocks. Never fabricate a chart rectangle."""
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 128:
        raise ValueError('图表上下文预算至少为128字符')
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError('图表页号必须是正整数')
    if bbox is not None and not _box(bbox):
        raise ValueError('图表观测范围必须是原页归一化矩形')
    page = next((p for p in source.get('pages', []) if p.get('number') == page_number), {})
    scope = list(bbox) if bbox is not None else [0., 0., 1., 1.]
    result = {'version': VERSION, 'source_id': source.get('id'), 'sha256': source.get('sha256'),
              'parser_version': source.get('parser_version'), 'page': page_number,
              'scope_bbox': scope, 'scope_basis': 'requested_region' if bbox is not None else 'page',
              'observations': [], 'hints': {'title_ids': [], 'unit_ids': [], 'period_ids': []},
              'has_candidate': False, 'selection_state': 'no_observations', 'truncated': False,
              'omitted_observations': 0, 'requires_visual_review': True}
    blocks = page.get('blocks') if isinstance(page.get('blocks'), list) else []
    ocr_blocks = page.get('chart_observations') if isinstance(page.get('chart_observations'), list) else []
    candidates = []
    # OCR is independent source text, never injected into page.text/its offsets.
    inputs = [('B', index, block) for index, block in enumerate(blocks[:MAX_OBSERVATIONS])]
    inputs += [('O', index, block) for index, block in enumerate(ocr_blocks[:MAX_OBSERVATIONS])]
    for prefix, index, block in inputs:
        if not isinstance(block, dict) or block.get('derived_view') or block.get('chart_point_binding'):
            continue
        if block.get('source_method') in {'visual', 'model', 'chart_view'}:
            continue
        text, box = block.get('text'), block.get('bbox')
        if not isinstance(text, str) or not text.strip() or not _box(box) or not _inside(box, scope):
            continue
        if len(text) > 2000:
            result['omitted_observations'] += 1
            continue
        confidence = block.get('confidence')
        pending = (block.get('status') == 'pending' or bool(block.get('source_pending_reason'))
                   or bool(block.get('requires_review')) or bool(block.get('ocr_disagreement'))
                   or prefix == 'O' and confidence is None
                   or confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                       or not math.isfinite(confidence) or confidence < .8 or confidence > 1))
        item = {'id': f'P{page_number}{prefix}{index}', 'text': text, 'bbox': [round(value, 6) for value in box],
                'origin': 'chart_ocr' if prefix == 'O' else 'page_block',
                'status': 'pending' if pending else 'observed'}
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and math.isfinite(confidence):
            item['confidence'] = confidence
        start, end = block.get('start'), block.get('end')
        if (prefix == 'B' and isinstance(start, int) and not isinstance(start, bool) and isinstance(end, int) and not isinstance(end, bool)
                and 0 <= start < end and page.get('text', '')[start:end] == text):
            item.update(start=start, end=end)
        candidates.append(item)
    result['omitted_observations'] += max(0, len(blocks) - MAX_OBSERVATIONS) + max(0, len(ocr_blocks) - MAX_OBSERVATIONS)
    # Prefer actual captions then dedicated chart OCR, before unrelated body text.
    candidates.sort(key=lambda row: (not bool(_TITLE.search(row['text'])), row['origin'] != 'chart_ocr'))
    deduplicated = []
    for item in candidates:
        duplicate = next((row for row in deduplicated if row['origin'] != item['origin']
                          and _norm(row['text']) == _norm(item['text'])
                          and max(abs(a - b) for a, b in zip(row['bbox'], item['bbox'])) <= .015
                          and _inside(row['bbox'], item['bbox']) and _inside(item['bbox'], row['bbox'])), None)
        if duplicate is None:
            deduplicated.append(item)
        elif duplicate['status'] == 'pending' and item['status'] == 'observed':
            deduplicated[deduplicated.index(duplicate)] = item
    candidates = deduplicated
    result['omitted_observations'] += max(0, len(candidates) - MAX_OBSERVATIONS)
    candidates = candidates[:MAX_OBSERVATIONS]
    result['has_candidate'] = any(_TITLE.search(item['text']) and item['status'] == 'observed' for item in candidates)
    if _size(result) > max_chars:
        return {'page': page_number, 'observations': [], 'has_candidate': result['has_candidate'],
                'truncated': True, 'selection_state': 'budget_exceeded'}
    for item in candidates:
        proposed = copy.deepcopy(result)
        proposed['observations'].append(item)
        if _TITLE.search(item['text']):
            proposed['hints']['title_ids'].append(item['id'])
        if _UNIT.search(item['text']):
            proposed['hints']['unit_ids'].append(item['id'])
        if re.search(r'(?<!\d)(?:19|20)\d{2}\s*年', item['text']):
            proposed['hints']['period_ids'].append(item['id'])
        proposed['selection_state'] = 'observed'
        if _size(proposed) + 32 <= max_chars:
            result = proposed
        else:
            result['omitted_observations'] += 1
    result['truncated'] = result['omitted_observations'] > 0
    return result


def chart_relation_review_passed(review, required_ids):
    ids = set(required_ids)
    if not ids:
        return True
    rows = review.get('chart_relations') if isinstance(review, dict) else None
    return (isinstance(rows, list) and len(rows) == len(ids)
            and all(isinstance(row, dict) and isinstance(row.get('id'), str)
                    and row.get('supported') is True for row in rows)
            and {row.get('id') for row in rows} == ids)


def _span(text, fragment):
    positions = [i for i, char in enumerate(text) if not char.isspace()]
    needle = _norm(fragment)
    offset = _norm(text).find(needle)
    if not needle or offset < 0:
        return None
    return positions[offset], positions[offset + len(needle) - 1] + 1


def _observations(context):
    rows = context.get('observations')
    if not isinstance(rows, list) or len(rows) > MAX_OBSERVATIONS:
        return {}
    output, duplicates = {}, set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            continue
        if row['id'] in output:
            duplicates.add(row['id'])
        elif isinstance(row.get('text'), str) and len(row['text']) <= 2000 and _box(row.get('bbox')):
            output[row['id']] = row
    return {key: value for key, value in output.items() if key not in duplicates}


def _contexts(records):
    output = []
    for record in records:
        context = record.get('chart_context') if isinstance(record, dict) else None
        if (not isinstance(context, dict) or not isinstance(context.get('page'), int)
                or isinstance(context.get('page'), bool) or context['page'] < 1):
            continue
        if any(context.get(key) is not None and record.get(key) is not None and context[key] != record[key]
               for key in ('source_id', 'sha256', 'parser_version', 'page')):
            continue
        output.append((record, context, _observations(context)))
    return output


def _aligned(category, value, association):
    a, b = category['bbox'], value['bbox']
    if association == 'aligned_vertical':
        tolerance = min(.04, max(.008, .55 * min(a[2] - a[0], b[2] - b[0])))
        return abs((a[0] + a[2] - b[0] - b[2]) / 2) <= tolerance and b[3] < a[1] - .003
    if association == 'aligned_horizontal':
        tolerance = min(.035, max(.008, .6 * min(a[3] - a[1], b[3] - b[1])))
        return abs((a[1] + a[3] - b[1] - b[3]) / 2) <= tolerance and b[0] > a[2] + .003
    return False


def _axis_aligned(first, second, axis):
    a, b = first['bbox'], second['bbox']
    size_a, size_b = a[axis + 2] - a[axis], b[axis + 2] - b[axis]
    tolerance = min(.025, max(.004, .6 * min(size_a, size_b)))
    return abs((a[axis] + a[axis + 2] - b[axis] - b[axis + 2]) / 2) <= tolerance


def _adjacent(category, value):
    a, b = category['bbox'], value['bbox']
    gap = max(a[1] - b[3], b[1] - a[3])
    return _axis_aligned(category, value, 0) and -.003 <= gap <= .025


def _title_scope(title, category, value, observations):
    """A caption is an anchor, not an invented chart rectangle."""
    if not title or not _TITLE.search(title['text']):
        return False
    point_top = min(category['bbox'][1], value['bbox'][1])
    captions = [row for row in observations.values() if row.get('status') == 'observed'
                and _TITLE.search(row['text']) and row['bbox'][3] < point_top]
    if not captions:
        return False
    nearest = max(row['bbox'][3] for row in captions)
    nearest_rows = [row for row in captions if nearest - row['bbox'][3] <= .02]
    return len(nearest_rows) == 1 and nearest_rows[0]['id'] == title['id']


def _grid_intersection(category, series, value, observations):
    a, s, v = category['bbox'], series['bbox'], value['bbox']
    if not (a[3] < v[1] and s[2] < v[0] and _axis_aligned(category, value, 0)
            and _axis_aligned(series, value, 1)):
        return False
    matches = [row for row in observations.values() if row.get('status') == 'observed'
               and _NUMBER.fullmatch(row['text'].strip()) and _axis_aligned(category, row, 0)
               and _axis_aligned(series, row, 1)]
    return len(matches) == 1 and matches[0]['id'] == value['id']


def _grid_axis_unit(binding, category, title, unit, observations):
    side = binding.get('axis_side')
    if side not in {'left', 'right'} or not title:
        return False
    # Actual column headers determine the grid width; no bar extents are inferred.
    headers = [row for row in observations.values() if row.get('status') == 'observed'
               and _axis_aligned(category, row, 1) and row['bbox'][1] < category['bbox'][3]
               and _numbers(row['text'])
               and _NUMBER.sub('', _norm(row['text'])) == _NUMBER.sub('', _norm(category['text']))]
    if len(headers) < 2:
        return False
    left = min((row['bbox'][0] + row['bbox'][2]) / 2 for row in headers)
    right = max((row['bbox'][0] + row['bbox'][2]) / 2 for row in headers)
    box = unit['bbox']; x = (box[0] + box[2]) / 2
    if not title['bbox'][3] <= box[1] < box[3] < category['bbox'][1]:
        return False
    if not (x < left - .005 if side == 'left' else x > right + .005):
        return False
    # Only standalone axis units are accepted here; nearby prose is not an axis.
    cleaned = _norm(unit['text']).removeprefix('单位').strip(':：（）()')
    if cleaned != _norm(binding['unit']):
        return False
    peers = [row for row in observations.values() if row.get('status') == 'observed'
             and _axis_aligned(unit, row, 1) and _UNIT.fullmatch(row['text'].strip())
             and (((row['bbox'][0] + row['bbox'][2]) / 2 < left - .005) if side == 'left'
                  else ((row['bbox'][0] + row['bbox'][2]) / 2 > right + .005))]
    return len(peers) == 1 and peers[0]['id'] == unit['id']


def _anchors(record, context, row):
    return {'source_id': record.get('source_id') or context.get('source_id'),
            'sha256': record.get('sha256') or context.get('sha256'),
            'parser_version': record.get('parser_version') or context.get('parser_version'),
            'page': context['page'], 'observation_id': row['id'], 'text': row['text'],
            'bbox': copy.deepcopy(row['bbox']), **{k: row[k] for k in ('start', 'end') if k in row}}


def _metadata_annotations(text, record, context, selected):
    result = []
    for row in selected:
        patterns = []
        title = _TITLE.search(row['text'])
        if title:
            patterns.append(('chart_title', re.escape(title[0].strip())))
        patterns.extend(('chart_period', re.escape(match[0]))
                        for match in re.finditer(r'(?<!\d)(?:19|20)\d{2}\s*年(?:度)?(?!\s*\d)', row['text']))
        for kind, pattern in patterns:
            for match in re.finditer(pattern + r'(?!\d)', text):
                if _UNIT.match(text[match.end():].lstrip()):
                    continue
                item = {'kind': kind, 'text': match[0], 'start': match.start(), 'end': match.end(),
                        'evidence_id': record.get('id'), 'source_anchor': _anchors(record, context, row)}
                if not any(a['start'] == item['start'] and a['end'] == item['end'] for a in result):
                    result.append(item)
    return result


def _masked(text, annotations):
    chars = list(text)
    for item in annotations:
        chars[item['start']:item['end']] = ' ' * (item['end'] - item['start'])
    return ''.join(chars)


def _mapped_annotations(text, statement, span, annotations):
    """Map whitespace-normalized statement positions back to the actual claim."""
    actual = [i for i in range(*span) if not text[i].isspace()]
    output = []
    for item in annotations:
        first = len(_norm(statement[:item['start']]))
        last = len(_norm(statement[:item['end']]))
        if first < last <= len(actual):
            start, end = actual[first], actual[last - 1] + 1
            output.append({**item, 'start': start, 'end': end, 'text': text[start:end]})
    return output


def check_chart_bindings(text, bindings, evidence_records, *, required=False):
    """Validate source identities and literal point proposals, not chart meaning."""
    result = {'required': bool(required or bindings), 'valid': True, 'bindings': [], 'errors': [],
              'numeric_text': text, 'context_annotations': []}
    def reject(message):
        result['valid'] = False
        if message not in result['errors']:
            result['errors'].append(message)
    if not result['required']:
        return result
    if not isinstance(text, str) or not text.strip() or len(text) > 1800:
        reject('图表事实文本为空或超限'); return result
    if not isinstance(bindings, list) or not 1 <= len(bindings) <= MAX_BINDINGS:
        reject('图表事实需要有限且显式的chart_bindings'); return result
    contexts = _contexts(evidence_records)
    spans = []
    for binding in bindings:
        if not isinstance(binding, dict):
            reject('图表绑定格式无效'); continue
        statement = binding.get('statement')
        span = _span(text, statement) if isinstance(statement, str) and len(statement) <= 1800 else None
        if not span:
            reject('图表statement必须是本条答案中可定位的完整精确子句'); continue
        kind = binding.get('kind')
        observation_ids = binding.get('observation_ids', []) if kind == 'visual_observation' else [
            binding.get(key + '_observation_id') for key in ('category', 'value', 'unit', 'series', 'title', 'period')
            if key in binding or key in ('category', 'value', 'unit')]
        if not isinstance(observation_ids, list) or len(observation_ids) > 16 or any(not isinstance(i, str) for i in observation_ids):
            reject('图表观测ID缺失或超限'); continue
        matches = [(record, context, observations) for record, context, observations in contexts
                   if (binding.get('evidence_id') is None or record.get('id') == binding['evidence_id'])
                   and all(identifier in observations for identifier in observation_ids)]
        identities = {(r.get('source_id') or c.get('source_id'), r.get('sha256') or c.get('sha256'),
                       r.get('parser_version') or c.get('parser_version'), c['page']) for r, c, _ in matches}
        if len(identities) != 1:
            reject('图表绑定未唯一对应本条引用的真实来源、页及观测'); continue
        record, context, observed = matches[0]
        selected = [observed[i] for i in dict.fromkeys(observation_ids)]
        annotations = _metadata_annotations(statement, record, context, selected)
        numeric_statement = _masked(statement, annotations)
        normalized = {key: copy.deepcopy(binding[key]) for key in ('kind', 'statement', 'observation_type', 'association', 'axis_side',
                       'category', 'value', 'unit', 'series', 'title', 'period') if key in binding}
        normalized.update({key + '_observation_id': binding[key + '_observation_id']
                           for key in ('category', 'value', 'unit', 'series', 'title', 'period')
                           if key + '_observation_id' in binding})
        normalized.update(evidence_id=record.get('id'), source_id=record.get('source_id') or context.get('source_id'),
                          sha256=record.get('sha256') or context.get('sha256'),
                          parser_version=record.get('parser_version') or context.get('parser_version'), page=context['page'],
                          source_observations=[_anchors(record, context, row) for row in selected],
                          requires_visual_review=True, context_truncated=bool(context.get('truncated')))
        if kind == 'visual_observation':
            if binding.get('observation_type') not in {'trend', 'missing_printed_values', 'description'}:
                reject('定性图表说明类型无效'); continue
            if any(key in binding for key in ('value', 'value_observation_id', 'unit', 'category')) or _numbers(numeric_statement):
                reject('定性图表说明不能夹带未经印刷标签支持的精确数值'); continue
            normalized['observation_ids'] = list(dict.fromkeys(observation_ids))
        elif kind == 'printed_point':
            if context.get('version') != VERSION or any(row.get('status') != 'observed' for row in selected):
                reject('精确图值需要可用原始观测，低置信、冲突或新增待复核文字不可放行'); continue
            field_rows = {key: observed.get(binding.get(key + '_observation_id'))
                          for key in ('category', 'value', 'unit', 'series', 'title', 'period') if key in binding}
            if any(not isinstance(binding.get(key), str) or not binding[key].strip() or not field_rows.get(key)
                   or not _span(field_rows[key]['text'], binding[key]) for key in ('category', 'value', 'unit')):
                reject('类别、印刷值及单位必须逐项绑定真实原文'); continue
            if any(key in binding and (not isinstance(binding[key], str) or not field_rows.get(key)
                   or not _span(field_rows[key]['text'], binding[key])) for key in ('series', 'title', 'period')):
                reject('图例、标题或时期不能由未绑定文字补写'); continue
            value_row, category_row, unit_row = field_rows['value'], field_rows['category'], field_rows['unit']
            value = _norm(binding['value']).replace('−', '-')
            printed = [_norm(match[0]).replace('−', '-') for match in _NUMBER.finditer(value_row['text'])]
            if value not in printed or not _NUMBER.fullmatch(binding['value']):
                reject('图值必须匹配完整印刷数值，不能串接12/120、正负或百分号'); continue
            if binding['unit'].replace('％', '%') not in {m[0].replace('％', '%') for m in _UNIT.finditer(unit_row['text'])}:
                reject('图值单位必须精确对应原文量纲，不能将万元当元'); continue
            association = binding.get('association')
            anchored_axis = False
            if association == 'same_line':
                if category_row['id'] != value_row['id']:
                    reject('同一来源行关系必须由同一真实观测支持'); continue
                fragments = [binding['category'], binding['value']]
                if field_rows.get('series', {}).get('id') == value_row['id']:
                    fragments.append(binding['series'])
                if unit_row['id'] == value_row['id']:
                    fragments.append(binding['unit'])
                remaining = _norm(value_row['text'])
                for fragment in fragments:
                    if _norm(fragment) not in remaining:
                        # A percent glyph belongs to the whole printed value.
                        if fragment in ('%', '％', '‰') and value.endswith(fragment):
                            continue
                        remaining = '?'; break
                    remaining = remaining.replace(_norm(fragment), '', 1)
                if remaining.strip(':：,，;；=()（）[]【】。'):
                    reject('同一来源行含未区分的其他类别或数值，关系不唯一'); continue
            elif association in {'aligned_vertical', 'aligned_horizontal'}:
                if (category_row['id'] == value_row['id'] or _norm(category_row['text']) != _norm(binding['category'])
                        or _norm(value_row['text']).replace('−', '-') != value or not _aligned(category_row, value_row, association)):
                    reject('分类与印刷值没有唯一可定位的几何对应'); continue
                alternatives = [row for row in observed.values() if row.get('status') == 'observed'
                    and row['id'] != category_row['id'] and _NUMBER.fullmatch(row['text'].strip())
                    and _aligned(category_row, row, association)]
                if len(alternatives) != 1 or alternatives[0]['id'] != value_row['id']:
                    reject('存在多个对齐印刷值，不能由位置或颜色猜系列'); continue
                if 'series' in binding and field_rows['series']['id'] != value_row['id']:
                    reject('分离图例不能证明该点所属系列，请保留定性或另找明确完整标签'); continue
            elif association == 'grid_point':
                series_row, title_row = field_rows.get('series'), field_rows.get('title')
                if (not series_row or not title_row or _norm(category_row['text']) != _norm(binding['category'])
                        or _norm(value_row['text']).replace('−', '-') != value
                        or not _title_scope(title_row, category_row, value_row, observed)
                        or not _grid_intersection(category_row, series_row, value_row, observed)):
                    reject('图下数据表需要真实标题、完整列标签和系列行的唯一交点'); continue
                series_remainder = _norm(series_row['text']).replace(_norm(binding['series']), '', 1)
                if unit_row['id'] == series_row['id']:
                    series_remainder = series_remainder.replace(_norm(binding['unit']), '', 1)
                    anchored_axis = True
                if series_remainder.strip(':：,，;；()（）[]【】。'):
                    reject('数据表系列行标签不完整，不能省略会改变所属系列的原文'); continue
                if unit_row['id'] not in {series_row['id'], value_row['id']}:
                    anchored_axis = _grid_axis_unit(binding, category_row, title_row, unit_row, observed)
                    if not anchored_axis:
                        reject('双轴图单位需要本图真实且唯一的左右轴锚点'); continue
            elif association == 'adjacent_label':
                if (category_row['id'] == value_row['id'] or unit_row['id'] != value_row['id']
                        or binding['unit'] not in {'%', '％', '‰'} or 'series' in binding
                        or _norm(category_row['text']) != _norm(binding['category'])
                        or _norm(value_row['text']).replace('−', '-') != value
                        or not _adjacent(category_row, value_row)):
                    reject('相邻饼图标签需要完整类别和自带百分号的紧邻印刷值'); continue
                values = [row for row in observed.values() if row.get('status') == 'observed'
                          and _NUMBER.fullmatch(row['text'].strip()) and _adjacent(category_row, row)]
                categories = [row for row in observed.values() if row.get('status') == 'observed'
                              and not _NUMBER.fullmatch(row['text'].strip()) and _adjacent(row, value_row)]
                if (len(values) != 1 or values[0]['id'] != value_row['id']
                        or len(categories) != 1 or categories[0]['id'] != category_row['id']
                        or 'title' in binding and not _title_scope(field_rows['title'], category_row, value_row, observed)):
                    reject('标签配对不是双向唯一，或标题属于同页另一图'); continue
            else:
                reject('图点关系必须声明真实同一行、唯一对齐、数据表交点或相邻标签'); continue
            if unit_row['id'] != value_row['id'] and not anchored_axis:
                ub = unit_row['bbox']
                if re.search(re.escape(binding['unit']) + r'\s*(?:[/／]|每)', unit_row['text']):
                    reject('独立单位含比率或复合量纲，不能截成单一单位'); continue
                if not ('单位' in unit_row['text'] or ub[2] <= .25 or ub[1] >= .75):
                    reject('独立单位文字没有明确单位声明或轴边缘位置'); continue
                all_units = {m[0].replace('％', '%') for row in observed.values() if row.get('status') == 'observed'
                             and ('单位' in row['text'] or row['bbox'][2] <= .25 or row['bbox'][1] >= .75)
                             for m in _UNIT.finditer(row['text'])}
                if len(all_units) > 1:
                    reject('页面存在不同轴或单位声明，不能自动归属精确图值'); continue
            for key in ('category', 'value', 'unit', 'series', 'period'):
                if key in binding and not _span(statement, binding[key]):
                    reject('图表事实子句未写出所绑定的完整类别、数值、单位或时期'); break
            else:
                allowed = _number_tokens(binding['value']) | _number_tokens(binding['category']) | _number_tokens(binding.get('series', ''))
                if binding['unit'] in {'%', '％', '‰'} and not value.endswith(('%', '％', '‰')):
                    allowed |= _number_tokens(binding['value'] + binding['unit'])
                extra = _number_tokens(numeric_statement) - allowed
                if extra:
                    reject('图表子句含未由该点及类别支持的精确数值'); continue
                normalized['field_observations'] = {key: _anchors(record, context, row) for key, row in field_rows.items()}
                result['bindings'].append(normalized)
                spans.append(span)
                result['context_annotations'].extend(_mapped_annotations(text, statement, span, annotations))
            continue
        else:
            reject('图表绑定类型无效'); continue
        result['bindings'].append(normalized)
        spans.append(span)
        result['context_annotations'].extend(_mapped_annotations(text, statement, span, annotations))
    if not result['bindings']:
        reject('未形成可核对的图表事实绑定')
    result['numeric_text'] = _masked(text, result['context_annotations'])
    uncovered = list(result['numeric_text'])
    for start, end in spans:
        uncovered[start:end] = ' ' * (end - start)
    if ''.join(uncovered).strip(' \t\r\n。；;，,、'):
        reject('本条还有未由图表绑定子句覆盖的事实，请拆分并逐条绑定')
    return result
