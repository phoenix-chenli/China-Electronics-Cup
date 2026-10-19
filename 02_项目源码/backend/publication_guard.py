"""Shared conservative guard against deriving current reality from draft status.

This checks explicit Chinese publication/completion assertions, not every possible
paraphrase. Normal citation and semantic review remain mandatory after it passes.
"""
import re


PUBLICATION_INSTRUCTIONS = '''逐份核对资料性质，不预设所给资料是草案。仅当某份资料确有草案、征求意见稿或起草说明标记时，才能在该资料范围内描述这一性质；部分草案不能证明所有所给资料均为草案，性质未知也不能推定为正式文件。可以说“这些征求意见资料不能证明后续正式施行”，但不得扩大到未核实的其他资料。草案性质不能证明现实中已经或尚未正式发布、生效、建成或验收；若原文明确记载某历史日期的状态，必须连同该日期和对象一起引用，只说明当时，不外推至今。混入无关正式文件不构成状态证据。'''
_DRAFT = re.compile(r'征求意见(?:稿)?|起草说明|草案|草稿')
_DATE = re.compile(r'(?:19|20)\d{2}(?:年(?:\d{1,2}月(?:\d{1,2}[日号])?)?|[-/]\d{1,2}(?:[-/]\d{1,2})?)')
_CURRENT = re.compile(r'目前|当前|现在|如今|至今|迄今|现已|现仍|当下')
_CURRENT_CONTINUATION = re.compile(r'(?:目前|当前|现在|如今|至今|迄今)[^。；;！？!?\n]{0,10}(?:仍然如此|依然如此|仍是如此|保持不变|状态未变)')
_STATE = re.compile(
    r'(?P<mark>尚未|还未|仍未|仍然未|并未|未曾|没有|尚无|并无|未|现已|已经|已|正式)'
    r'\s*(?:(?:于|在)?' + _DATE.pattern + r')?\s*(?:被|予以)?\s*(?:正式|通过)?\s*'
    r'(?P<event>完成(?:建设|验收)|发布|颁布|公布|印发|生效|失效|废止|施行|实施|建成|竣工|完工|验收|投入(?:运行|运营|使用|生产)|投产|开通)'
    r'|(?P<event_after>发布|生效|建成|完工|验收)(?:了|完毕)')
_DENIAL = re.compile(r'(?:无法|不能|未能|尚不能|不足以|尚无法|不应|不可)[^，,。；;\n]{0,12}'
                     r'(?:确认|证明|认定|判断|核实|断言|确定|说明|推断)[^，,。；;\n]{0,24}$')
_AFTER_UNCERTAIN = re.compile(r'^\s*(?:与否|是否属实|这一(?:点|状态))?\s*'
                             r'(?:尚未核实|未核实|无法确认|不能确认|尚不能确认|不确定|未知)')
_EXPLANATION = '草案或起草材料不能推出当前已经或尚未发布、生效或建成；须保留资料范围，或引用原文明确记载的历史日期与状态。'
_DRAFT_STATUSES = {'public_consultation_draft', 'draft', 'draft_explanation'}
_EXPLICIT_NON_DRAFT_STATUSES = {'formal', 'final', 'official', 'published', 'effective'}
_SCOPE_ASSERTION = re.compile(
    r'(?P<scope>(?:(?:本次|当前|本题)?(?:所给|所提供|提供的|已提供|全部|所有)|'
    r'(?:本条|此处)?(?:所引|引用的|已引用))(?:的)?(?:资料|材料|文件|文档))'
    r'\s*(?:(?:均|都|全都|全部)\s*)?(?:是|为|属于|视为|标注为?|标为)\s*'
    r'(?:公开)?(?:征求意见(?:稿|材料|资料)?|起草说明|草案|草稿)')
_DRAFT_TITLE_END = re.compile(r'(?:征求意见稿|起草说明|草案|草稿)[）)]?$')
_OTHER_DOCUMENT = re.compile(r'附件|附录|引用|摘录|提及|他人|他方|另案|另文|通知|审阅|依据|根据|《|》|“|”|「|」')


def _explicit_draft_support(source, records):
    """Positive document status only; mentioning a draft is not being one.

    This narrow check is for universal document-nature assertions. Unknown
    remains unknown, rather than becoming an inferred formal document.
    """
    status = (source.get('metadata') or {}).get('publication_status')
    if status in _EXPLICIT_NON_DRAFT_STATUSES:
        return False
    if status in _DRAFT_STATUSES:
        return True
    name = re.sub(r'\.[A-Za-z0-9]{1,8}$', '', str(source.get('name', ''))).strip()
    if _DRAFT_TITLE_END.search(name) and not _OTHER_DOCUMENT.search(name):
        return True
    for record in records:
        other_document_context = False
        for raw_line in str(record.get('quote', '')).splitlines():
            line = raw_line.strip()
            if not line or len(line) > 180 or other_document_context:
                continue
            # A self-description, not "this notice's attachment is a draft".
            if re.match(r'^(?:本文件|本资料|本材料|本文档|本稿)(?:性质)?\s*(?:为|是|属于|标注为)\s*(?:公开)?(?:征求意见稿|起草说明|草案|草稿)(?:[，,。；;]|$)', line):
                return True
            if _OTHER_DOCUMENT.search(line):
                other_document_context = True
                continue
            # A standalone title/mark is useful, but an arbitrary prose
            # reference ("其他材料中有草案") is not a title assertion.
            title_status = _DRAFT_TITLE_END.search(line)
            if title_status and not re.search(r'[。；;，,:：]|为|是|有|含|见|说|称|非|不|未', line[:title_status.start()]):
                return True
    return False


def _scope_denied_or_reported(text, match):
    start = max((text.rfind(char, 0, match.start()) for char in '。；;！？!?\n'), default=-1) + 1
    prefix = text[start:match.start()]
    # These constructions do not affirm the embedded universal proposition.
    if re.search(r'(?:并非|不是|是否|不能(?:认为|认定|确认|断言|说|将|把)?|不应(?:认为|认定|将|把)?|不可(?:将|把)?|无法(?:认定|确认))\s*$', prefix):
        return True
    if re.search(r'''(?:有人|他人|某人)(?:说|称|认为|声称)[：:“”‘’"'\s]*$''', prefix):
        return True
    return False


def _draft_scope_violation(text, records, sources_by_id):
    for match in _SCOPE_ASSERTION.finditer(text):
        if _scope_denied_or_reported(text, match):
            continue
        scope = match.group('scope')
        cited = {record.get('source_id') for record, _ in records if record.get('source_id')}
        # "所给/所有资料" denotes the selected collection, including
        # selected sources not cited by this particular claim. Only an explicit
        # citation-limited phrase narrows it to the actual cited source set.
        # Ambiguous "这些/上述" references are deliberately left to semantic
        # review instead of mechanically expanding them to all selected sources.
        source_ids = cited if re.search(r'所引|引用', scope) else set(sources_by_id) | cited
        unsupported = [sid for sid in source_ids if not _explicit_draft_support(
            sources_by_id.get(sid) or {}, [record for record, _ in records if record.get('source_id') == sid])]
        if not source_ids or unsupported:
            return ('资料整体草案性质的范围未证实：不能将部分草案概括为所有所给资料均为草案。'
                    '请逐份引用性质依据，或将说明限定到实际核实的草案子集；未知性质不等于正式文件。')
    return None


def is_draft_source(source):
    metadata = source.get('metadata') or {}
    return metadata.get('publication_status') in {'public_consultation_draft', 'draft', 'draft_explanation'} or bool(_DRAFT.search(str(source.get('name', ''))))


def _negative(match):
    return bool(re.search(r'未|没有|无', match.group('mark') or ''))


def _event(match):
    event = match.group('event') or match.group('event_after')
    return {'颁布': '发布', '公布': '发布', '印发': '发布', '施行': '生效',
            '完成建设':'建成','竣工':'建成','完成验收':'验收'}.get(event, event)


def _clauses(text):
    """Carry the sentence's date while keeping negation local to each clause."""
    for sentence in re.split(r'[。；;！？!?\n]', text):
        cursor = 0
        for separator in re.finditer(r'[，,：:]|但是|然而|不过|因此|所以', sentence):
            yield sentence, sentence[cursor:separator.start()], cursor
            cursor = separator.end()
        yield sentence, sentence[cursor:], cursor


def _state_date(sentence, clause_start, match):
    # A later, unrelated year's presence in the same quote cannot date an
    # earlier state assertion. A date may be in the preceding introductory
    # clause or inside "已于2024年建成" itself.
    dates = _DATE.findall(sentence[:clause_start + match.end()])
    return dates[-1] if dates else None


def _subject(prefix):
    prefix = _DATE.sub('', prefix)
    prefix = re.sub(r'\s|截至|截止|当时|于|在|年末|年底|年初|月底|月末', '', prefix)
    prefix = re.sub(r'^(?:原文|文件|资料|材料|草案|起草说明)(?:明确)?(?:记载|载明|显示|说明|指出|写明|记录)(?:的)?', '', prefix)
    return re.sub(r'^(?:该|此|本)', '', prefix).strip('“”‘’「」()（）')


def _local_denial(prefix, denial):
    """Do not carry a negation through its negation, quotation or contrast.

    This only narrows the existing evidential-denial exemptions. It does not
    establish a document's state, or decide whether the quoted evidence is true.
    """
    before, body = prefix[:denial.start()], prefix[denial.start():]
    if re.search(r'(?:不是|并非|并不是|不能说|不能认为|不能断言|不是说|并非说)'
                 r'[^，,。；;\n]{0,24}$', before):
        return False
    if re.search(r'(?:不存在|没有)(?:任何)?\s*$', before):
        return False
    if re.match(r'(?:尚不能|不能|无法|未能|尚无法|不足以|不应|不可)\s*(?:不|没有|否认)', body):
        return False
    if re.match(r'(?:不能说|不能认为|不能断言|不能认定)[^，,。；;\n]{0,24}'
                r'(?:不能|无法|不可|不足以)', body):
        return False
    if re.search(r'但(?!书)|然而|不过|却|事实上|实际上|[”’」』]', body):
        return False
    # An ASCII-quoted denial must not qualify an assertion after its quote.
    if any(before.count(quote) % 2 and quote in body for quote in ('"', "'")):
        return False
    return True


def _uncertain(clause, match):
    prefix, suffix = clause[:match.start()], clause[match.end():]
    denial = _DENIAL.search(prefix)
    if (denial and _local_denial(prefix, denial)) or _AFTER_UNCERTAIN.match(suffix):
        return True
    # A local denial of evidential support does not assert the embedded state:
    # "不能作为方案已施行的证据". Require both sides of this construction;
    # it cannot authorize a later assertion elsewhere in the same sentence.
    denial = re.search(r'(?:不能|不可|不应|无法|不足以)作为[^，,。；;\n]{0,24}$', prefix)
    # A state can modify the noun "文件": "正式施行文件的证据". Only
    # this bounded noun tail (optionally closing its quoted proposition) is
    # accepted; arbitrary prose after the state is not evidence of negation.
    evidence_suffix = re.match(r'^(?:文件)?[”」』]?的(?:充分|可靠|有效|直接)?(?:证据|依据)', suffix)
    if denial and evidence_suffix:
        return _local_denial(prefix, denial)
    # A bounded "does not constitute evidence of P" denies the evidential
    # relation, not P. Both ends must belong to this same unquoted clause;
    # a further negation, proposition or contrast cannot inherit the exemption.
    denial = re.search(r'不构成(?:对)?(?P<subject>[^，,。；;\n“”‘’「」『』《》"\']{1,24})$', prefix)
    evidence_suffix = re.fullmatch(r'的(?:充分|可靠|有效|直接)?(?:证明|依据|证据)\s*', suffix)
    if denial and evidence_suffix:
        subject = denial.group('subject')
        before = prefix[:denial.start()]
        if (not re.search(r'不|并非|否认|且|而|但|却|然而|不过|事实上|实际上', subject)
                and not _STATE.search(subject)
                and not re.search(r'(?:否认|不承认)[^，,。；;\n]{0,24}$', before)
                and not any(before.count(q) % 2 for q in ('"', "'"))
                and not any(before.count(left) != before.count(right)
                            for left, right in (('“', '”'), ('‘', '’'), ('「', '」'), ('『', '』')))
                and _local_denial(prefix, denial)):
            return True
    # Negated existence of evidence: the state is embedded in a proposition,
    # rather than asserted. Bind both ends and reject double negation/contrast
    # using the same local-scope rule as other evidence-denial constructions.
    denial = re.search(r'(?:不存在|没有)(?:任何|充分的|足够的)?'
                      r'(?:可|能够|足以)?(?:证明|确认|认定)'
                      r'[^，,。；;\n]{0,24}$', prefix)
    evidence_suffix = re.match(r'^[”」』]?的(?:关联|充分|可靠|有效|直接)?(?:证据|依据)[”」』]?\s*$', suffix)
    if denial and evidence_suffix:
        return _local_denial(prefix, denial)
    # "与方案的正式施行无关联证据" is a nominal event in a denied
    # evidential relation. Do not exempt "已/未施行": those can independently
    # assert a state even when its relationship to another event is denied.
    nominal = re.search(r'与[^，,。；;\n]{1,24}的$', prefix)
    if (match.group('mark') == '正式' and nominal
            and re.match(r'^无(?:直接)?关联(?:证据|依据)(?:[”」』]?\s*)$', suffix)):
        return _local_denial(prefix, nominal)
    return False


def _direct_state_match(clause, match, quote, date=None):
    subject = _subject(clause[:match.start()])
    # An isolated verb, or a different project's state, is not corroboration.
    if not subject:
        return False
    for quote_sentence, quote_clause, quote_start in _clauses(quote):
        for candidate in _STATE.finditer(quote_clause):
            if (not _uncertain(quote_clause, candidate) and _negative(candidate) == _negative(match)
                    and _event(candidate) == _event(match)
                    and _subject(quote_clause[:candidate.start()]) == subject
                    and (date is None or _state_date(quote_sentence, quote_start, candidate) == date)):
                return True
    return False


def publication_violation(text, cited_evidence, sources_by_id):
    """Return a bounded diagnostic, or None; never mutate sources or evidence.

    ``cited_evidence`` must contain only the claim's genuine cited records. A
    non-draft source only helps when its quote names the same subject and state.
    Historical draft assertions additionally need a shared explicit date, and
    cannot contain present-time extrapolation in that sentence.
    """
    records = [(record, sources_by_id.get(record.get('source_id'), {}))
               for record in cited_evidence if isinstance(record, dict)]
    scope_error = _draft_scope_violation(text, records, sources_by_id)
    if scope_error:
        return scope_error
    draft = lambda pair: is_draft_source(pair[1]) or bool(_DRAFT.search(str(pair[0].get('quote', ''))))
    if not any(draft(pair) for pair in records):
        return None
    if _CURRENT_CONTINUATION.search(text) and _STATE.search(text):
        return _EXPLANATION
    for sentence, clause, clause_start in _clauses(text):
        for match in _STATE.finditer(clause):
            if _uncertain(clause, match):
                continue
            if any(not draft(pair) and _direct_state_match(clause, match, str(pair[0].get('quote', ''))) for pair in records):
                continue
            date = _state_date(sentence, clause_start, match)
            historical = date and not _CURRENT.search(sentence) and any(
                _direct_state_match(clause, match, str(record.get('quote', '')), date=date)
                for record, _ in records)
            if not historical:
                return _EXPLANATION
    return None
