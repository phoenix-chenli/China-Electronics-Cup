"""Bounded, deterministic input normalization recorded as an explicit change list.

User aliases and metric terms used to take effect only on the deterministic
sales path (engine.parse). Model-driven scopes (database / knowledge / hybrid)
never saw them, so a project-defined alias had no effect on the questions that
actually call the model. This module applies the same project vocabulary before
planning and returns both the normalized text and a change list the UI can show.

Nothing here rewrites text without a recorded source, and the original question
is always retained. A term absent from the vocabulary is returned unchanged: a
miss is not evidence that the question is wrong. No model call and no network.
"""
from __future__ import annotations

import re
import unicodedata

MAX_CHANGES = 20
MAX_WINDOWS = 400
MAX_TERMS = 200
MAX_TYPOS = 3
MIN_TYPO_LENGTH = 3
CJK_RUN = re.compile(r"[一-鿿]+")
CJK_WORD = re.compile(r"^[一-鿿]{2,}$")

# Bounded traditional -> simplified table for characters this project actually
# meets in Taiwanese/Hong Kong copies of the same bulletins. Written as explicit
# one-character pairs so an entry can never be silently misaligned. It is
# deliberately not a full converter: characters outside the table are left
# untouched, and the boundary is reported with every change.
_TABLE_PAIRS = (
    "統统", "計计", "區区", "產产", "業业", "總总", "額额", "長长", "數数", "據据", "報报", "資资",
    "訊讯", "務务", "電电", "體体", "會会", "個个", "應应", "點点", "對对", "時时", "間间", "開开",
    "關关", "為为", "與与", "萬万", "億亿", "圓圆", "塊块", "質质", "準准", "標标", "類类", "學学",
    "習习", "醫医", "藥药", "農农", "漁渔", "貿贸", "銀银", "貸贷", "債债", "財财", "稅税", "補补",
    "貼贴", "費费", "價价", "車车", "輛辆", "廠厂", "員员", "勞劳", "動动", "權权", "監监", "環环",
    "濃浓", "廢废", "氣气", "溫温", "濕湿", "設设", "備备", "機机", "構构", "網网", "絡络", "線线",
    "腦脑", "軟软", "檔档", "頁页", "碼码", "語语", "詞词", "讀读", "寫写", "譯译", "圖图", "記记",
    "錄录", "庫库", "審审", "規规", "則则", "條条", "適适", "範范", "圍围", "級级", "層层", "節节",
    "確确", "認认", "檢检", "驗验", "證证", "來来", "輸输", "轉转", "換换", "結结", "單单", "種种",
    "較较", "減减", "漲涨", "穩稳", "變变", "趨趋", "勢势", "預预", "測测", "劃划", "執执", "佈布",
    "發发", "週周", "歷历", "縣县", "鄉乡", "鎮镇", "陸陆", "嶺岭", "島岛", "灣湾", "橋桥", "濟济",
    "麗丽", "陽阳", "陰阴", "華华", "龍龙", "鳳凤", "們们", "這这", "麼么", "樣样", "讓让", "嗎吗",
    "請请", "謝谢", "幫帮", "給给", "幾几", "誰谁", "裡里", "邊边", "後后", "東东", "於于", "併并",
    "並并", "從从", "眾众", "舊旧", "歸归", "復复", "複复", "係系", "繫系", "銜衔", "見见", "覺觉",
    "觀观", "聽听", "聲声", "響响", "舉举", "辦办", "廳厅", "縱纵", "橫横", "緊紧", "鬆松", "織织",
    "續续", "繼继", "紀纪", "純纯", "綠绿", "紅红", "藍蓝", "黃黄", "鋼钢", "鐵铁", "銅铜", "鉛铅",
    "鋁铝", "錯错", "誤误", "誠诚", "詢询", "訪访", "診诊", "療疗", "護护", "劑剂", "營营", "銷销",
    "廣广", "項项", "處处", "顯显", "調调", "遊游", "館馆", "雜杂", "誌志", "險险", "師师", "衛卫",
    "說说", "國国",
)


def _table():
    """Refuse a malformed entry instead of silently mapping the wrong character."""
    for item in _TABLE_PAIRS:
        if not isinstance(item, str) or len(item) != 2 or item[0] == item[1]:
            raise ValueError("繁简对照表条目无效，未做替换：" + repr(item))
    return {item[0]: item[1] for item in _TABLE_PAIRS}


def lexicon(project_id=None):
    """Alias and term data from the user's definitions and the project vocabulary."""
    from . import store as s
    aliases, terms = {}, []
    for item in s.definition_list():
        name = str(item.get("name") or "").strip()
        target = str(item.get("target") or "").strip()
        # A metric code is an internal identifier and is never written into the
        # question; the definition's own Chinese name is the canonical text.
        canonical = name if item.get("kind") == "metric" or item.get("field") == "metric" else target
        if not canonical:
            continue
        terms.append(canonical)
        for label in [name] + [str(x) for x in item.get("aliases") or []]:
            label = label.strip()
            if label and label != canonical:
                aliases[label] = canonical
    if project_id:
        try:
            from . import query_routing
            vocabulary = query_routing.profile(project_id).get("source_vocabulary") or {}
        except Exception:
            # Enrichment only. A missing or unreadable project binding must not
            # block a question; the built-in steps below still run.
            vocabulary = {}
        for region in vocabulary.get("regions") or []:
            terms.append(str(region.get("region_name") or ""))
        for indicator in vocabulary.get("indicators") or []:
            terms.append(str(indicator.get("metric_name") or ""))
    unique = list(dict.fromkeys(t for t in terms if CJK_WORD.match(t)))
    return {"aliases": aliases, "terms": unique[:MAX_TERMS]}


def _correct(text, terms, record):
    """Single-character typos against the project's own vocabulary only.

    Compared position by position on equal-length CJK. A similarity ratio would
    accept a shifted substring ("景山区的" against "石景山区") and rewrite the
    wrong span, so a correction needs one substitution, a term of three or more
    characters, and a unique candidate; anything ambiguous stays as written.
    """
    terms = [term for term in dict.fromkeys(terms) if len(term) >= MIN_TYPO_LENGTH]
    if not terms or len(text) > 2000:
        return text
    by_size = {}
    for term in terms:
        by_size.setdefault(len(term), []).append(term)
    known = {size: set(group) for size, group in by_size.items()}
    replacements, budget = {}, MAX_WINDOWS
    for run in CJK_RUN.findall(text):
        for size, group in by_size.items():
            if size > len(run):
                continue
            for start in range(len(run) - size + 1):
                if budget <= 0:
                    break
                budget -= 1
                window = run[start:start + size]
                if window in known[size] or window in replacements:
                    continue
                hits = [term for term in group if sum(a != b for a, b in zip(window, term)) == 1]
                if len(hits) == 1:
                    replacements[window] = hits[0]
    for window, term in list(replacements.items())[:MAX_TYPOS]:
        if window in text:
            record("typo", window, term, "词表近似匹配（等长单字替换，唯一候选）")
            text = text.replace(window, term, 1)
    return text


def normalize(question, project_id=None, data=None):
    """Return the normalized text plus every change, or the text unchanged.

    ``data`` may supply ``{"aliases": {...}, "terms": [...]}`` directly; otherwise
    the user's definitions and the project vocabulary are read locally.
    """
    text = question if isinstance(question, str) else ""
    original = text
    changes = []

    def record(kind, before, after, source):
        if before != after and len(changes) < MAX_CHANGES:
            changes.append({"kind": kind, "from": before[:80], "to": after[:80], "source": source})

    compat = unicodedata.normalize("NFKC", text)
    if compat != text:
        record("unicode", text, compat, "NFKC 兼容归一化")
        text = compat
    compact = re.sub(r"\s+", " ", text).strip()
    if compact != text:
        record("whitespace", text, compact, "空白归一化")
        text = compact
    table = _table()
    mapped = "".join(table.get(character, character) for character in text)
    if mapped != text:
        record("traditional", text, mapped, "繁简对照表（有界，非完整转换器）")
        text = mapped
    if data is None:
        data = lexicon(project_id)
    aliases = data.get("aliases") or {}
    ordered = [alias for alias in sorted(aliases, key=len, reverse=True) if alias and alias != aliases[alias]]
    if ordered:
        # One left-to-right pass. Replacing alias by alias over the whole string
        # would let a shorter alias rewrite text an earlier alias just produced
        # (智能存储→智能存储平台 then 存储→云盘 yields 智能云盘平台).
        pieces, index = [], 0
        while index < len(text):
            alias = next((item for item in ordered if text.startswith(item, index)), None)
            if alias:
                record("alias", alias, aliases[alias], "用户或项目别名")
                pieces.append(aliases[alias])
                index += len(alias)
            else:
                pieces.append(text[index])
                index += 1
        text = "".join(pieces)
    text = _correct(text, data.get("terms") or [], record)
    return {"original": original, "normalized": text, "changes": changes, "applied": bool(changes),
            "boundary": "只做词典与兼容形式的确定性归一；词表之外的写法原样返回，未命中不等于问题有误；不调用模型或外部服务。"}
