"""Conservative, source-bound chart facts; never digitize graphical heights.

``prepare`` consumes independently recognized, normalized line rectangles. A
model's proposed point, orientation, colour, or confidence is not evidence.
Separated labels are supported only for an unambiguous single-series bar chart.
DOCX chart caches have their own authoritative package relationships and should
continue to use the native parser instead of passing through this OCR validator.
"""
from __future__ import annotations

import copy
import math
import re
import unicodedata

VERSION = 'chart-relations-v1'
_NUMBER = re.compile(r'[+\-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+\-]?\d+)?(?:%|‰)?|[+\-]?\.\d+(?:[eE][+\-]?\d+)?(?:%|‰)?')


def _canonical(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(text or '')).replace('−', '-').replace('–', '-'))


def _box(value):
    return (isinstance(value, (list, tuple)) and len(value) == 4
            and all(type(x) in (int, float) and math.isfinite(x) and 0 <= x <= 1 for x in value)
            and value[0] < value[2] and value[1] < value[3])


def _inside(box, region):
    if not _box(box) or not _box(region):
        return False
    area = max(0, min(box[2], region[2])-max(box[0], region[0]))*max(0, min(box[3], region[3])-max(box[1], region[1]))
    return area/((box[2]-box[0])*(box[3]-box[1])) >= .8


def _centre(box, axis):
    return (box[axis]+box[axis+2])/2


def _point_region(source, region, chart_box):
    """Allow a small tight/offset point proposal, without expanding the chart.

    This is only a location gate. Callers must first match a whole independently
    observed number (or complete inline tuple) and establish unique geometry.
    Large regions receive no new tolerance; a nearby, non-intersecting label
    never qualifies. The source rectangle remains the recorded evidence.
    """
    if _inside(source, region):
        return True
    if not _box(source) or not _box(region) or not _box(chart_box):
        return False
    width = chart_box[2]-chart_box[0]; height = chart_box[3]-chart_box[1]
    sw = source[2]-source[0]; sh = source[3]-source[1]
    rw = region[2]-region[0]; rh = region[3]-region[1]
    if rw > .18*width or rh > .09*height or rw*rh > .012*width*height:
        return False
    if sw > .24*width or sh > .09*height:
        return False
    overlap = max(0, min(source[2], region[2])-max(source[0], region[0]))*max(0, min(source[3], region[3])-max(source[1], region[1]))
    return (overlap/(sw*sh) >= .45 and overlap/(rw*rh) >= .55
            and abs(_centre(source, 0)-_centre(region, 0)) <= max(.003*width, .35*sw)
            and abs(_centre(source, 1)-_centre(region, 1)) <= max(.003*height, .35*sh))


def _anchor(line):
    return {'observation_id': line['id'], 'text': line['text'], 'box': list(line['box'])}


def _sources(observed, region):
    if not isinstance(observed, list) or len(observed) > 6000:
        raise ValueError('图表独立文字观测无效或超限')
    selected = []; identities = {}; bad = set(); total = 0
    for index, line in enumerate(observed):
        if not isinstance(line, dict) or not isinstance(line.get('text'), str) or len(line['text']) > 4000:
            raise ValueError('图表独立文字观测无效')
        total += len(line['text'])
        if total > 48000:
            raise ValueError('图表独立文字观测超过总文字限制')
        box = line.get('box', line.get('bbox')); confidence = line.get('confidence', 1)
        if not _box(box) or type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError('图表独立文字观测位置或置信度无效')
        if confidence < .8 or line.get('status') == 'pending' or not line['text'].strip() or not _inside(box, region):
            continue
        identity = line.get('id', line.get('line_id', index))
        if type(identity) not in (int, str) or isinstance(identity, str) and len(identity) > 100:
            raise ValueError('图表独立文字观测身份无效')
        key = (type(identity).__name__, identity)
        signature = (line['text'], tuple(box))
        if key in identities:
            if identities[key] != signature:
                bad.add(key)
            continue
        identities[key] = signature
        selected.append({'id': identity, 'text': line['text'], 'box': list(box)})
    return [line for line in selected if (type(line['id']).__name__, line['id']) not in bad], sorted(str(k[1]) for k in bad)


def _exact(text, lines):
    target = _canonical(text)
    return [line for line in lines if target and '\n' not in line['text'] and '\r' not in line['text']
            and _canonical(line['text']) == target]


def _number(text):
    value = _canonical(text)
    return value if _NUMBER.fullmatch(value) else None


def _printed(point):
    """Match a whole printed number, including its sign/decimal/percent suffix.

    An expected ``12`` never matches ``120``, ``-12``, ``12.5`` or ``12%``.
    Removing surrounding category text first is unsafe for numeric categories.
    """
    value = _number(point.get('value'))
    if value is None:
        return False
    printed = unicodedata.normalize('NFKC', point.get('printed_text') or '').replace('−', '-').replace('–', '-')
    tokens = re.findall(r'(?<![\w.,+\-])[+\-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+\-]?\d+)?(?:%|‰)?(?![\w.,])|(?<![\w.,+\-])[+\-]?\.\d+(?:[eE][+\-]?\d+)?(?:%|‰)?(?![\w.,])', printed)
    # In Chinese strings, category text abuts the number. Exact composite pair
    # matching below is still safe; only accept this fallback if no truncation
    # can arise at either edge of the expected numeric representation.
    if not tokens:
        tokens = re.findall(r'(?<![\d.,+\-])[+\-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+\-]?\d+)?(?:%|‰)?(?![\d.,%‰])', printed)
    return value in [_canonical(token) for token in tokens]


def _pairs(point, series, lines, multiple, chart_box):
    label = _canonical(point.get('label')); value = _number(point.get('value'))
    name = _canonical(series.get('name'))
    if not label or not value:
        return []
    if multiple:
        # Series legend text elsewhere in the chart cannot identify a colour.
        options = {name+label+value, name+value+label, label+name+value,
                   label+value+name, value+name+label, value+label+name} if name else set()
    else:
        options = {label+value, value+label}
        if name:
            options |= {name+label+value, label+name+value}
    return [line for line in lines if _point_region(line['box'], point.get('box'), chart_box)
            and '\n' not in line['text'] and '\r' not in line['text']
            and _printed(dict(point, printed_text=line['text']))
            and _canonical(line['text']).strip(':：,，;；=') in options]


def _aligned(label, value, orientation, extent):
    cat = label['box']; val = value['box']
    if orientation == 'vertical':
        tolerance = min(.055*extent[0], max(.012*extent[0], .45*min(cat[2]-cat[0], val[2]-val[0])))
        return abs(_centre(cat, 0)-_centre(val, 0)) <= tolerance and val[3] < cat[1]-.003*extent[1]
    tolerance = min(.045*extent[1], max(.01*extent[1], .45*min(cat[3]-cat[1], val[3]-val[1])))
    return abs(_centre(cat, 1)-_centre(val, 1)) <= tolerance and val[0] > cat[2]+.003*extent[0]


def _separated(chart, points, lines):
    """Return unique aligned observations; ambiguous alternatives stay pending."""
    out = {}; bounds = chart['box']; extent = (bounds[2]-bounds[0], bounds[3]-bounds[1])
    if len(points) < 2:
        return out
    for orientation in ('vertical', 'horizontal'):
        candidates = {}
        for pi, point in enumerate(points):
            labels = _exact(point.get('label'), lines)
            values = [line for line in _exact(point.get('value'), lines) if _point_region(line['box'], point.get('box'), bounds)]
            pairs = [(label, value) for label in labels for value in values if label['id'] != value['id'] and _aligned(label, value, orientation, extent)]
            if len(pairs) == 1:
                candidates[pi] = pairs[0]
        if len(candidates) < 2:
            continue
        labels = [pair[0] for pair in candidates.values()]
        axis = 1 if orientation == 'vertical' else 0
        band = max(.02*extent[axis], 1.2*max(line['box'][axis+2]-line['box'][axis] for line in labels))
        if max(_centre(line['box'], axis) for line in labels)-min(_centre(line['box'], axis) for line in labels) > band:
            continue
        # Category/value rectangles must be unique. Reusing one observation to
        # give several differently named points the same number is not proof.
        counts = {}
        for label, value in candidates.values():
            for line in (label, value):
                counts[line['id']] = counts.get(line['id'], 0)+1
        for pi, (label, value) in candidates.items():
            if counts[label['id']] == counts[value['id']] == 1:
                out.setdefault(pi, []).append((orientation, label, value))
    return {pi: matches[0] for pi, matches in out.items() if len(matches) == 1}


def _axis_source(text, unit, lines):
    """An axis name may be printed with its unit in the same line."""
    target = _canonical(text); symbol = _canonical(unit)
    if not target:
        return []
    decorated = {target, target+'('+symbol+')', target+'['+symbol+']', target+'/'+symbol,
                 target+':'+symbol, target+'：'+symbol} if symbol else {target}
    return [line for line in lines if '\n' not in line['text'] and '\r' not in line['text']
            and _canonical(line['text']) in decorated]


def _axis_position(line, chart, key, orientation, category, value):
    cb = chart['box']; box = line['box']; width = cb[2]-cb[0]; height = cb[3]-cb[1]
    if key == 'y_axis':
        if _centre(box, 0) > cb[0]+.22*width:
            return False
        if category and box[2] >= category['box'][0]-.005*width:
            return False
        return cb[1]+.15*height <= _centre(box, 1) <= cb[1]+.85*height
    if _centre(box, 1) < cb[1]+.78*height:
        return False
    if category and orientation == 'vertical' and box[1] <= category['box'][3]:
        return False
    if category and orientation == 'horizontal' and box[1] <= max(category['box'][3], value['box'][3]):
        return False
    return True


def _metadata(chart, series, lines, orientation, association, category=None, point_value=None):
    result = {}; missing = []
    fields = [('title', chart.get('title')), ('series', series.get('name')),
              ('x_axis', chart.get('x_label')), ('y_axis', chart.get('y_label'))]
    for key, value in fields:
        matches = _axis_source(value, chart.get('unit'), lines) if key.endswith('_axis') else _exact(value, lines)
        if key.endswith('_axis'):
            matches = [line for line in matches if _axis_position(line, chart, key, orientation, category, point_value)]
        if key == 'title' and not value:
            continue
        if len(matches) != 1:
            # An inline source can prove its series name if it actually prints
            # the complete (series, category, value) tuple.
            if key == 'series' and association and len(association) == 1 and _canonical(series.get('name')) in _canonical(association[0]['text']):
                matches = association
            else:
                missing.append(key); continue
        result[key] = _anchor(matches[0])
    unit = _canonical(chart.get('unit'))
    if not unit:
        missing.append('unit'); return result, missing
    numeric_axis = 'y_axis' if orientation == 'vertical' else 'x_axis' if orientation == 'horizontal' else None
    # Same-line inline pairs have no independent bar orientation. Their unit
    # must be printed with a unique axis name, never merely elsewhere in title.
    axes = [numeric_axis] if numeric_axis else ['x_axis', 'y_axis']
    attributed = []
    for key in axes:
        axis = result.get(key)
        if not axis:
            continue
        text = _canonical(axis['text'])
        axis_name = _canonical(chart.get('x_label' if key == 'x_axis' else 'y_label'))
        embedded = any(axis_name.endswith(suffix) and len(axis_name) > len(suffix)
                       for suffix in ('('+unit+')', '['+unit+']'))
        if (text == axis_name and embedded) or (text != axis_name and text in {axis_name+'('+unit+')', axis_name+'['+unit+']', axis_name+'/'+unit, axis_name+':'+unit, axis_name+'：'+unit}):
            attributed.append((axis, key)); continue
        if not orientation:
            continue
        # A separate unit is acceptable only beside its numeric-axis title at
        # the appropriate outer edge. A nearby plot title/legend is excluded.
        cb = chart['box']; width = cb[2]-cb[0]; height = cb[3]-cb[1]; ab = axis['box']
        for line in _exact(chart.get('unit'), lines):
            if line['id'] == axis['observation_id']:
                continue
            ub = line['box']
            if orientation == 'vertical':
                near = abs(_centre(ub, 0)-_centre(ab, 0)) <= .06*width and abs(_centre(ub, 1)-_centre(ab, 1)) <= .14*height
                edge = max(_centre(ub, 0), _centre(ab, 0)) <= cb[0]+.22*width
            else:
                near = abs(_centre(ub, 1)-_centre(ab, 1)) <= .05*height and abs(_centre(ub, 0)-_centre(ab, 0)) <= .14*width
                edge = min(_centre(ub, 1), _centre(ab, 1)) >= cb[1]+.78*height
            if near and edge:
                attributed.append((_anchor(line), key))
    if len(attributed) == 1:
        result['unit'] = dict(attributed[0][0], axis=attributed[0][1])
    else:
        missing.append('unit_axis_attribution')
    return result, missing


def prepare(chart, observed):
    """Recompute visual point relationships in place, discarding old verdicts.

    Source observations must belong to the same page/media coordinate frame.
    Caller assigns globally unique chart/point IDs after joining page results.
    Model-declared secondary axes, multiple axes, and non-bar separated labels
    are retained as candidates rather than precise numeric facts.
    """
    if not isinstance(chart, dict) or not _box(chart.get('box')):
        raise ValueError('图表区域无效')
    if not isinstance(chart.get('series'), list) or len(chart['series']) > 30:
        raise ValueError('图表系列无效或超限')
    lines, duplicate_ids = _sources(observed, chart['box'])
    series_list = chart['series']; count = 0; issues = []; bound = 0
    multiple = len(series_list) != 1
    axes = {series.get('axis', 'unknown') for series in series_list}
    axis_safe = axes == {'primary'} and not chart.get('dual_axis') and len(chart.get('axes') or []) <= 2
    for si, series in enumerate(series_list):
        points = series.get('points', [])
        if not isinstance(points, list) or len(points) > 200:
            raise ValueError('图表数据点无效或超限')
        count += len(points)
        candidates = [point for point in points if point.get('value_source') == 'printed' and _printed(point)]
        separated = _separated(chart, points, lines) if not multiple and axis_safe and chart.get('kind') == 'bar' else {}
        for pi, point in enumerate(points):
            point.pop('source_binding', None)
            point.update(status='pending', association_status='pending', verification='unverified_chart_association', evidence_lines=[])
            reason = None; matches = []; orientation = None; label = value = None
            if point not in candidates:
                reason = 'estimated_or_unobserved_chart_value'
            elif not axis_safe:
                reason = 'unknown_or_multiple_chart_axes'
            else:
                matches = _pairs(point, series, lines, multiple, chart['box'])
                if len(matches) == 1:
                    label = value = matches[0]
                elif not matches and pi in separated:
                    orientation, label, value = separated[pi]
                else:
                    reason = 'ambiguous_or_unobserved_chart_pair'
            metadata, missing = _metadata(chart, series, lines, orientation, matches, label, value)
            if not reason and missing:
                reason = 'unverified_chart_metadata'
            if reason:
                issues.append({'series_index': si, 'point_index': pi, 'reason': reason, 'missing_metadata': missing})
                continue
            method = 'same_local_observed_label_pair' if matches else 'unique_observed_bar_alignment'
            binding = {'version': VERSION, 'chart_id': chart.get('id'), 'point_id': point.get('id'),
                       'series_index': si, 'series_name': series.get('name'), 'label': point.get('label'),
                       'value': point.get('value'), 'unit_text': chart.get('unit'), 'axis': 'primary',
                       'title_text': chart.get('title'), 'x_label_text': chart.get('x_label'),
                       'y_label_text': chart.get('y_label'), 'chart_box': list(chart['box']),
                       'point_box': list(point['box']), 'chart_kind': chart.get('kind'),
                       'orientation': orientation or 'inline', 'association': method,
                       'category': _anchor(label), 'printed_value': _anchor(value), **metadata}
            ids = list(dict.fromkeys(source['observation_id'] for source in binding.values()
                                     if isinstance(source, dict) and 'observation_id' in source))
            point.update(status='confirmed', association_status='confirmed', verification=method,
                         evidence_lines=ids, source_binding=binding)
            bound += 1
        series['name_status'] = 'confirmed' if points and any(p.get('source_binding', {}).get('series') for p in points) else 'pending'
        series['status'] = 'confirmed' if points and all(p.get('status') == 'confirmed' for p in points) else 'pending'
    # Reusing category or value observations in the same series invalidates all
    # participants, including a duplicate model point that appeared later.
    for si, series in enumerate(series_list):
        uses = {}
        for pi, point in enumerate(series.get('points', [])):
            binding = point.get('source_binding')
            if binding:
                for key in ('category', 'printed_value'):
                    uses.setdefault((key, binding[key]['observation_id']), []).append(pi)
        duplicate = {pi for indices in uses.values() if len(indices) > 1 for pi in indices}
        for pi in sorted(duplicate):
            point = series['points'][pi]; point.pop('source_binding', None)
            point.update(status='pending', association_status='pending', verification='reused_chart_observation', evidence_lines=[])
            issues.append({'series_index': si, 'point_index': pi, 'reason': 'reused_chart_observation'}); bound -= 1
        if duplicate:
            series['status'] = 'pending'
    chart['status'] = 'confirmed' if count and bound == count else 'pending'
    chart['relation_status'] = 'consistent' if count and bound == count else 'partial' if bound else 'pending'
    chart['relation_audit'] = {'version': VERSION, 'confirmed_points': bound, 'candidate_points': count,
                               'issues': issues, 'conflicting_observation_ids': duplicate_ids,
                               'scope': 'printed text and unique observable relationships; no graphical estimates or colour inference'}
    chart['metadata_status'] = {key: 'confirmed' if bound and all(key2 in p.get('source_binding', {}) for series in series_list for p in series.get('points', []) if p.get('status') == 'confirmed') else 'pending'
                                for key, key2 in [('title', 'title'), ('x_label', 'x_axis'), ('y_label', 'y_axis'), ('unit', 'unit')]}
    return chart


def point_views(chart):
    """Yield complete citable point atoms; pending siblings do not erase facts.

    A truncated slice of ``text`` is not a complete chart relation. Consumers
    should keep these atoms intact and propagate ``source_binding`` only for a
    complete atom. Identity is regenerated here after page/media ID assignment.
    """
    if not chart.get('id'):
        return
    for si, series in enumerate(chart.get('series', [])):
        for point in series.get('points', []):
            binding = point.get('source_binding')
            source = point.get('value_source')
            if point.get('status') != 'confirmed' or not point.get('id') or not str(point.get('label') or '').strip() or _number(point.get('value')) is None:
                continue
            context = {'label': point.get('label'), 'value': point.get('value'), 'series_name': series.get('name'),
                       'unit_text': chart.get('unit'), 'title_text': chart.get('title'),
                       'x_label_text': chart.get('x_label'), 'y_label_text': chart.get('y_label')}
            if source == 'native_cache':
                part = point.get('source_part') or chart.get('source_part')
                if not part or chart.get('method') != 'docx_chart_cache':
                    continue
                binding = {'version': VERSION, **context, 'value_source': source, 'association': 'native_chart_cache_index',
                           'source_part': part, 'chart_source_part': chart.get('source_part'),
                           'axis_ids': copy.deepcopy(series.get('axis_ids', [])), 'axes': copy.deepcopy(chart.get('axes', []))}
            elif source == 'human_review':
                axisless = chart.get('kind') in {'pie', 'donut', 'pieChart', 'doughnutChart'}
                axis_text = [str(chart.get(key) or '').strip() for key in ('x_label', 'y_label')]
                if (point.get('human_reviewed') is not True or point.get('verification') != 'human_reviewed'
                        or not point.get('reviewed_by') or not point.get('reviewed_at')
                        or not all(str(chart.get(key) or '').strip() for key in ('title', 'unit'))
                        or (any(axis_text) if axisless else not all(axis_text))
                        or not str(series.get('name') or '').strip()):
                    continue
                if binding and any(binding.get(key) != value for key, value in context.items()):
                    continue
                binding = {'version': VERSION, **context, 'value_source': source, 'association': 'human_reviewed',
                           'reviewed_by': point['reviewed_by'], 'reviewed_at': point['reviewed_at'],
                           'verification': 'human_reviewed', 'media_id': chart.get('media_id'),
                           'box': copy.deepcopy(point.get('box') or chart.get('box'))}
                # Freeze shared metadata on the reviewed point. Later changes
                # cannot silently relabel a previously approved measurement.
                point['source_binding'] = copy.deepcopy(binding)
            elif source != 'printed' or not binding or binding.get('version') != VERSION:
                continue
            elif (binding.get('axis') != series.get('axis') or binding.get('chart_box') != chart.get('box')
                  or binding.get('point_box') != point.get('box') or binding.get('chart_kind') != chart.get('kind')
                  or chart.get('dual_axis')):
                continue
            if (binding.get('label') != point.get('label') or binding.get('value') != point.get('value')
                    or binding.get('series_name') != series.get('name') or binding.get('unit_text') != chart.get('unit')
                    or any(binding.get(key) != value for key, value in context.items())):
                continue
            binding = copy.deepcopy(binding); binding.update(chart_id=chart['id'], point_id=point['id'], series_index=si)
            title = chart.get('title') if binding.get('title') or source in {'native_cache', 'human_review'} else '图表结构视图'
            title = title or '图表结构视图'; unit = chart.get('unit') or '单位未标明'
            source_text = {'printed': '明确印刷值', 'native_cache': 'DOCX 原生保存数据', 'human_review': '人工复核值'}[source]
            text = f"图表：{title}\n系列：{series.get('name', '')}；分类：{point.get('label', '')}；数值：{point['value']}；单位：{unit}；数值来源：{source_text}"
            yield {'text': text, 'chart_id': chart['id'], 'point_id': point['id'], 'series_index': si,
                   'box': point.get('box') or chart.get('box'), 'source_binding': binding, 'chart_relation_version': VERSION}
