"""Shared, deterministic evidence selection for library and answer retrieval.

Only authorized indexed chunks enter. Scores are retrieval heuristics, not
truth/confidence probabilities. Neighbours retain their own exact offsets.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .hybrid import terms
from ..text_normalization import expand_query, query_variants

VERSION = 'scoped-subject-evidence-v2'
_GENERIC = set('the a an and or of in on to for from by with is are as be at what how which why when where according handbook manual guide document volume vol fhwa doe epa nhi gec table figure appendix chapter section'.split())


def query_profile(question):
    expansion = expand_query(question)
    query = terms(expansion['original'])
    for variant in query_variants(expansion['original'])[1:]:
        query |= terms(variant)
    query |= terms(expansion['aliases'])
    # Bibliographic words are useful for source selection, not for ranking a
    # publisher's cover above the requested engineering evidence.
    for word in _GENERIC:
        query.pop(word, None)
    return query, expansion


def _normal(text):
    value = re.sub(r'\s+', ' ', str(text).lower().translate(str.maketrans({'–':'-','—':'-','−':'-'})))
    return re.sub(r'\bultraviolet\b', 'uv', value)


def _anchors(text):
    result = set()
    patterns = {
        'figure': r'(?:\bfig(?:ure)?\.?\s*|图\s*)(\d+(?:[.\-]\d+)*)',
        'table': r'(?:\btable\s*|表\s*)(\d+(?:[.\-]\d+)*)',
        'equation': r'(?:\beq(?:uation)?\.?\s*|式\s*)(\d+(?:[.\-]\d+)*)',
        'section': r'(?:\bsection\s*|节\s*)(\d+(?:[.\-]\d+)*)',
        'example': r'(?:\bexample\s*|示例\s*)([a-z]?\d+)',
    }
    for kind, pattern in patterns.items():
        for match in re.finditer(pattern, _normal(text), re.I):
            number = match[1].rstrip('.-')
            result.add((kind, number))
    return result


def _source_multiplier(question, source):
    name = str(source.get('name', ''))
    front = '\n'.join(p.get('text', '') for p in (source.get('pages') or [])[:4])[:16000]
    haystack = _normal(name + '\n' + front)
    titles = re.findall(r'《([^》]{2,120})》', question)
    title_hit = any(_normal(title) in haystack for title in titles)
    publication_years = set(re.findall(r'((?:19|20)\d{2})年\s*(?:《|[^，。；]{0,25}(?:指南|手册))', question))
    year_hit = bool(publication_years & set(re.findall(r'(?:19|20)\d{2}', name)))
    # Match document codes exactly, including zero padding; no external catalog
    # or benchmark ID is consulted.
    code_pattern = r'\b(gec|nhi)\s*(?:no\.?\s*)?[-_ ]*0*(\d+(?:[-_]\d+)*)'
    codes = {(m[1].lower(), m[2].lstrip('0')) for m in re.finditer(code_pattern, question, re.I)}
    source_codes = {(m[1].lower(), m[2].lstrip('0')) for m in re.finditer(code_pattern, haystack, re.I)}
    code_hit = bool(codes & source_codes)
    return ((1.7 if title_hit or code_hit else 1.0) * (1.25 if year_hit else 1.0)), {'title_match': title_hit, 'document_code_match': code_hit, 'publication_year_match': year_hit}


def rank_evidence(question, ranking, chunks, sources, *, maximum=100, allow_neighbours=True):
    """Rank payloads carrying ``source_key``; return IDs and an honest audit.

    ``chunks`` includes only currently authorized pages. Thus neighbours never
    escape an explicit page/source selection. No source text is rewritten.
    """
    query, expansion = query_profile(question)
    important = {t for t in query if len(t) > 1 and not t.isdigit()}
    anchors = _anchors(question)
    # User-written English names/technical phrases have more discriminative
    # value than newly added broad aliases. Book titles are handled per source.
    without_titles = re.sub(r'《[^》]*》', '', question)
    phrases = {_normal(p) for p in re.findall(r'[A-Za-z]{2,}(?:[\s\-–]+[A-Za-z]{2,}){1,5}', without_titles)
               if len(set(re.findall(r'[a-z]+', p.lower())) - _GENERIC) >= 2}
    original_keywords = set(re.findall(r'[a-z]{2,}', without_titles.lower())) - _GENERIC
    # Keep user-written abbreviations (e.g. two-letter indicators). Named
    # entities establish scope; repeatedly mentioning the same entity must not
    # look like new coverage of the user's requested measurements.
    symbols = {m.lower() for m in re.findall(r'(?<![A-Za-z])[A-Z][A-Z0-9]{1,7}(?![A-Za-z])', without_titles)}
    phrase_words = {t for phrase in phrases for t in terms(phrase)}
    coverage_keywords = (original_keywords | set(terms(expansion['aliases']))) - phrase_words - _GENERIC
    source_factors = {key: _source_multiplier(question, source) for key, source in sources.items()}
    confirmed_parents = {
        key: {h.get('id') for h in ((source.get('metadata') or {}).get('document_structure') or {}).get('headings', [])
              if h.get('structure_status') in {'declared', 'rule_anchored'} and h.get('inheritance_allowed') is True}
        for key, source in sources.items()}
    by_page = defaultdict(list)
    for identifier, chunk in chunks.items():
        by_page[(chunk['source_key'], chunk['page'])].append(identifier)
    pages = {(key, p['number']): p.get('text', '') for key, source in sources.items()
             for p in source.get('pages', [])}
    feature_cache = {}

    def features(identifier):
        if identifier in feature_cache:
            return feature_cache[identifier]
        chunk = chunks[identifier]
        text = chunk['text']
        word_tokens = terms(text)
        overlap = len(important & set(word_tokens))
        coverage = overlap / max(1, min(12, len(important)))
        page_text = pages.get((chunk['source_key'], chunk['page']), '')
        anchor_count = len(anchors & _anchors(page_text))
        phrase_count = sum(phrase in _normal(text + '\n' + chunk.get('retrieval_hint', '')) for phrase in phrases)
        # Only the validated hierarchy attached by the chunker can establish
        # case/section scope. A candidate heading or free-form retrieval hint
        # does not receive this stronger signal.
        heading_text = ('\n'.join(chunk.get('heading_path') or [])
                        if chunk.get('parent_id') in confirmed_parents[chunk['source_key']] else '')
        heading_phrases = sum(phrase in _normal(heading_text) for phrase in phrases)
        printed_table = bool(chunk.get('table_identity') or re.search(
            r'(?im)^\s*(?:table|表)\s*\d+(?:[.\-]\d+)*[.．:：]?\s+\S', text))
        table_subjects = len(symbols & set(word_tokens)) if printed_table else 0
        title = page_text[:600]
        contents = bool(re.search(r'(?im)^\s*(?:table\s+of\s+contents|contents|list\s+of\s+(?:figures|tables))\b', title))
        leaders = len(re.findall(r'(?:\.\s*){5,}', text))
        frontmatter = bool(re.search(r'(?i)available to (?:doe|the public)|reproduced directly from|national technical information service|disclaimer|neither the united states', text[:700]))
        labelled = any(c.isalpha() for c in text)
        quality = 1.0
        if not labelled and not chunk.get('table_identity'):
            quality = 0.0
        elif len(re.findall(r'[A-Za-z]{2,}|[\u3400-\u9fff]', text)) < 4 and len(text) < 70 and not chunk.get('table_identity'):
            quality = .22  # retain legal short labels, but seek their context
        if (contents or leaders >= 3) and not re.search(r'目录|table of contents|list of (?:figures|tables)', question, re.I):
            quality *= .02
        if frontmatter and not re.search(r'出版|发行|版权|免责声明|publisher|publication|disclaimer|copyright', question, re.I):
            quality *= .03
        value = {'quality': quality, 'overlap': overlap, 'coverage': coverage,
                 'anchors': anchor_count, 'phrases': phrase_count, 'contents': contents, 'frontmatter': frontmatter,
                 'heading_phrases': heading_phrases, 'table_subjects': table_subjects,
                 'coverage_keywords': coverage_keywords & set(word_tokens)}
        feature_cache[identifier] = value
        return value

    # Independent exact-phrase recall prevents repeated generic table rows
    # from exhausting the lexical/vector candidate budget. It uses only names
    # or technical phrases present in the user's question and visible chunk
    # text/confirmed heading hints; no external entity/page catalog is used.
    # Titles remain source hints, not a reason to add an entire document.
    phrase_added = []
    expanded_scores = dict(ranking)
    seed_score = max((float(score) for _, score in ranking), default=0.0)
    if phrases and seed_score > 0:
        phrase_candidates = [identifier for identifier in chunks if any(phrase in _normal(chunks[identifier]['text'] + '\n' + chunks[identifier].get('retrieval_hint', ''))
                                     for phrase in phrases)]
        phrase_candidates = [i for i in phrase_candidates if features(i)['quality'] >= .5]
        phrase_candidates.sort(key=lambda i: (-features(i)['phrases'], -features(i)['coverage'], i))
        for identifier in phrase_candidates[:64]:
            if expanded_scores.get(identifier, 0) < seed_score * .85:
                expanded_scores[identifier] = seed_score * .85
                phrase_added.append(identifier)
    scores, reasons = {}, {}
    numeric_skipped = 0
    for identifier, score in expanded_scores.items():
        if identifier not in chunks:
            continue
        feature = features(identifier)
        if not feature['quality']:
            numeric_skipped += 1
            continue
        source_key = chunks[identifier]['source_key']
        factor = source_factors[source_key][0]
        scores[identifier] = float(score) * feature['quality'] * factor * (1 + .8 * feature['coverage'] + .9 * min(2, feature['anchors']) + 1.2 * min(2, feature['phrases']) + 1.5 * min(1, feature['heading_phrases']))
        # A printed table containing the literal requested indicators is a
        # useful candidate for their relationships, not proof they were parsed
        # correctly. Semantic/visual table verification remains downstream.
        scores[identifier] *= 1 + .8 * min(2, feature['table_subjects'])
        reasons[identifier] = 'exact_query_phrase_recall' if identifier in phrase_added else 'ranked_candidate'

    # A relevant title/figure can end a page while its explanation continues on
    # the next. Add only neighbours with actual query overlap or matched labels;
    # adjacency alone is never enough to promote an unrelated page.
    added = []
    if allow_neighbours:
        seeds = sorted(scores, key=lambda i: (-scores[i], i))[:8]
        for seed in seeds:
            original = chunks[seed]
            seed_feature = features(seed)
            if not seed_feature['overlap'] and not seed_feature['anchors']:
                continue
            for page in (original['page'] - 1, original['page'] + 1):
                candidates = by_page.get((original['source_key'], page), [])
                eligible = [i for i in candidates if features(i)['quality'] >= .5 and
                            (len(features(i)['coverage_keywords']) >= 2 or features(i)['anchors'])]
                if not eligible:
                    continue
                best = max(eligible, key=lambda i: (features(i)['anchors'], features(i)['coverage'], len(chunks[i]['text'])))
                adjacent_score = scores[seed] * .78 * (1 + .2 * min(1, features(best)['coverage']))
                if adjacent_score > scores.get(best, 0):
                    scores[best] = adjacent_score
                    reasons[best] = 'query_supported_adjacent_page'
                    added.append({'chunk_id': best, 'seed_chunk_id': seed, 'page': page})

    # First provide distinct source/page evidence; additional blocks from the
    # same page remain eligible rather than globally deduplicating equal text.
    remaining = dict(scores)
    selected, page_counts, covered_keywords = [], defaultdict(int), set()
    def selection_score(identifier):
        page = (chunks[identifier]['source_key'], chunks[identifier]['page'])
        novel = len(features(identifier)['coverage_keywords'] - covered_keywords)
        return remaining[identifier] * (1 + .45 * min(3, novel)) / (1 + .7 * page_counts[page])
    while remaining and len(selected) < maximum:
        identifier = max(remaining, key=lambda i: (selection_score(i), i))
        adjusted = selection_score(identifier)
        remaining.pop(identifier)
        covered_keywords.update(features(identifier)['coverage_keywords'])
        selected.append((identifier, adjusted))
        page_counts[(chunks[identifier]['source_key'], chunks[identifier]['page'])] += 1
    return selected, {'selection_policy': VERSION, 'query_expansion': expansion,
        'numeric_only_candidates_skipped': numeric_skipped, 'adjacent_candidates': added,
        'exact_query_phrase_candidates': phrase_added,
        'confirmed_scope_candidates': [i for i, _ in selected if features(i)['heading_phrases']],
        'requested_indicator_table_candidates': [i for i, _ in selected if features(i)['table_subjects']],
        'source_matches': {key: detail for key, (_, detail) in source_factors.items()},
        'candidate_count_before_policy': len(ranking), 'candidate_count_after_policy': len(scores),
        'ranked_candidate_ids': [identifier for identifier, _ in ranking[:maximum]],
        'selection_reasons': {identifier: reasons[identifier] for identifier, _ in selected}}
