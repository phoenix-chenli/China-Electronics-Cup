"""Source-scoped business definitions and bounded query-element contracts.

The caller supplies confirmed entries from its user-confirmation store. A model
cannot promote itself by putting ``origin=user_confirmed`` in a candidate. DDL
facts come exclusively from the inspected catalog, never from either overlay.
Definitions are descriptions, not executable SQL or instructions.
"""
from __future__ import annotations

import json

MAX_DEFINITIONS = 128
MAX_SEMANTIC_CHARS = 18000
MAX_ELEMENT_CHARS = 14000
KINDS = {"field", "metric", "alias", "relationship", "time", "unit"}


def _text(value, limit=500):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _bounded(value, depth=0):
    """Retain only JSON data, with a visible cap rather than arbitrary objects."""
    if depth > 4:
        return {"truncated": True}
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if float('-inf') < value < float('inf') else None
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, list):
        result = [_bounded(v, depth + 1) for v in value[:24]]
        if len(value) > 24:
            result.append({"truncated": True})
        return result
    if isinstance(value, dict):
        # Query elements describe semantics; they never carry authentication,
        # arbitrary nested model traces, or purported review outcomes.
        allowed = {"table", "column", "name", "value", "operator", "op", "direction",
                   "start", "end", "inclusive", "timezone", "grain", "unit", "kind",
                   "definition_id", "label", "numerator", "denominator", "baseline",
                   "period", "tie_policy", "expression", "references", "source_id"}
        return {k: _bounded(v, depth + 1) for k, v in value.items() if k in allowed}
    return None


def schema_fields(catalog):
    return {t["name"]: {c["name"] for c in t.get("columns", [])}
            for t in catalog.get("tables", [])}


def build_semantic_layer(catalog, source, overlay=None):
    """Normalize an optional trusted-store envelope, bound to this source SHA.

    Envelope: ``{source_sha256, confirmed: [...], candidates: [...]}``.
    Confirmed items additionally require ``confirmation_id``. Both collections
    must reference actual schema fields. Source identity mismatch rejects the
    entire envelope; names or aliases never transfer across imported sources.
    """
    fields = schema_fields(catalog)
    sha = _text(source.get("sha256"), 128)
    layer = {"version": "schema-semantics-1", "source_sha256": sha,
             "definitions": [], "warnings": [], "truncated": False,
             "ddl_basis": "current_readonly_sqlite_schema",
             "candidate_definitions_are_facts": False}
    envelope = source.get("schema_semantics") if overlay is None else overlay
    if not envelope:
        return layer
    if not isinstance(envelope, dict) or not sha or envelope.get("source_sha256") != sha:
        layer["warnings"].append("业务定义的资料版本不一致，未采用")
        return layer
    seen = set()
    for collection, origin in (("confirmed", "user_confirmed"), ("candidates", "model_candidate")):
        entries = envelope.get(collection, [])
        if not isinstance(entries, list):
            layer["warnings"].append(f"{collection} 不是定义列表，未采用")
            continue
        for raw in entries[:MAX_DEFINITIONS]:
            if len(layer["definitions"]) >= MAX_DEFINITIONS:
                layer["truncated"] = True
                break
            if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str) or raw.get("kind") not in KINDS:
                layer["warnings"].append("忽略格式不合法的业务定义")
                continue
            ident = _text(raw.get("id"), 100)
            confirmation = _text(raw.get("confirmation_id"), 100)
            refs = raw.get("references")
            if not isinstance(refs, list) or not refs or len(refs) > 16:
                layer["warnings"].append("业务定义缺少有界的实际表字段引用，未采用")
                continue
            normalized = []
            for ref in refs:
                if not isinstance(ref, dict) or not isinstance(ref.get("table"), str) or ref.get("table") not in fields:
                    break
                table, column = ref["table"], ref.get("column")
                if column is not None and (not isinstance(column, str) or column not in fields[table]):
                    break
                normalized.append({"table": table, **({"column": column} if column is not None else {})})
            if (not ident or ident in seen or len(normalized) != len(refs)
                    or (origin == "user_confirmed" and not confirmation)):
                layer["warnings"].append("业务定义标识、确认记录或表字段引用无效，未采用")
                continue
            entry = {"id": ident, "kind": raw["kind"], "origin": origin,
                     "source_sha256": sha, "references": normalized,
                     "label": _text(raw.get("label"), 200),
                     "definition": _text(raw.get("definition"), 1200),
                     "aliases": list(dict.fromkeys(_text(v, 100) for v in raw.get("aliases", [])[:16]
                                                   if isinstance(v, str) and v.strip()))[:16]
                     if isinstance(raw.get("aliases", []), list) else [],
                     "status": "confirmed" if origin == "user_confirmed" else "requires_confirmation"}
            for key in ("unit", "time_grain", "timezone", "null_policy", "denominator", "grain"):
                if _text(raw.get(key)):
                    entry[key] = _text(raw[key])
            if confirmation and origin == "user_confirmed":
                entry["confirmation_id"] = confirmation
            if len(json.dumps(layer, ensure_ascii=False)) + len(json.dumps(entry, ensure_ascii=False)) > MAX_SEMANTIC_CHARS - 1000:
                layer["truncated"] = True
                break
            layer["definitions"].append(entry)
            seen.add(ident)
        if len(entries) > MAX_DEFINITIONS:
            layer["truncated"] = True
    # Two saved confirmations can still disagree. Do not silently prefer last.
    by_ref = {}
    for entry in layer["definitions"]:
        if entry["origin"] != "user_confirmed":
            continue
        for ref in entry["references"]:
            for key in ("unit", "time_grain", "timezone", "null_policy", "denominator", "grain"):
                if key in entry:
                    by_ref.setdefault((ref["table"], ref.get("column"), key), []).append(entry)
    for (_, _, attr), entries in by_ref.items():
        if len({e[attr] for e in entries}) > 1:
            for entry in entries:
                entry["status"] = "conflicting_confirmations"
            layer["warnings"].append(f"已确认定义的 {attr} 存在冲突，须重新确认")
    layer["warnings"] = list(dict.fromkeys(layer["warnings"]))[:20]
    if layer["truncated"]:
        layer["warnings"].append("业务定义达到本轮预算，未展示的定义不作为当前口径依据")
    return layer


ELEMENT_KEYS = {
    "entities": ("entities", "entity", "object"),
    "metrics": ("metrics", "metric"),
    "time": ("time", "time_range"),
    "grouping": ("grouping", "dimensions", "group_by"),
    "filters": ("filters",), "sort": ("sort", "order_by"),
    "top_k": ("top_k", "topk", "limit"),
    "comparison_baseline": ("comparison_baseline", "comparison_base", "baseline"),
    "scope": ("scope",), "grain": ("grain",), "denominator": ("denominator",),
    "null_policy": ("null_policy",), "timezone": ("timezone",),
    "tie_policy": ("tie_policy",), "percentage_kind": ("percentage_kind",),
    "unit": ("unit",), "topic": ("topic",),
}


def element_contract(intent, catalog, source):
    """Create visible candidate elements; missing values stay unknown.

    This validates schema references, not the truth of the model's interpretation.
    The independent verifier must compare the elements to the question/history.
    """
    intent = intent if isinstance(intent, dict) else {}
    fields = schema_fields(catalog)
    elements = {}
    issues = []
    definitions = {d['id']: d for d in catalog.get('semantic_layer', {}).get('definitions', [])}
    for name, aliases in ELEMENT_KEYS.items():
        value = next((intent[k] for k in aliases if k in intent), None)
        elements[name] = _bounded(value)
    def check(value, path):
        if isinstance(value, list):
            for i, v in enumerate(value): check(v, f"{path}[{i}]")
        elif isinstance(value, dict):
            if 'definition_id' in value:
                ident = value['definition_id']
                definition = definitions.get(ident) if isinstance(ident, str) else None
                if not definition:
                    issues.append({'code':'unknown_semantic_definition','element':path})
                elif definition.get('origin') != 'user_confirmed' or definition.get('status') != 'confirmed':
                    issues.append({'code':'unconfirmed_semantic_definition','element':path})
            if "table" in value:
                table = value["table"]
                if not isinstance(table, str) or table not in fields:
                    issues.append({"code": "unknown_table_reference", "element": path})
                elif "column" in value and (not isinstance(value["column"], str) or value["column"] not in fields[table]):
                    issues.append({"code": "unknown_column_reference", "element": path})
            for k, v in value.items(): check(v, f"{path}.{k}")
    for name, value in elements.items(): check(value, name)
    top = elements["top_k"]
    if top is not None and (isinstance(top, bool) or not isinstance(top, int) or top < 1 or top > 10000):
        issues.append({"code": "invalid_top_k", "element": "top_k"})
    contract = {"version": "query-elements-1", "status": "candidate_requires_independent_review",
                "source_scope": {"source_id": _text(source.get("id"), 128),
                                 "sha256": _text(source.get("sha256"), 128)},
                "elements": elements, "issues": issues[:32],
                "unknown_elements": [k for k, v in elements.items() if v in (None, "", [], {})]}
    if len(json.dumps(contract, ensure_ascii=False)) > MAX_ELEMENT_CHARS:
        contract["elements"] = {k: None for k in elements}
        contract["truncated"] = True
        contract["issues"] = [{"code": "element_budget_exceeded", "element": "all"}]
    return contract
