"""Deterministic, source-local schema retrieval and declared-FK bridge discovery.

Lexical retrieval is deliberately labelled lexical. No pseudo-embedding or
matching field name is presented as a proven semantic relationship.
"""
from __future__ import annotations

import re
from collections import deque


def _terms(text):
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text))
    tokens = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text.casefold())
    result = set(tokens)
    for token in tokens:
        if re.fullmatch(r"[\u3400-\u9fff]+", token) and len(token) > 2:
            result.update(token[i:i + 2] for i in range(len(token) - 1))
    return result


def _match(question, label):
    if not isinstance(label, str) or not label.strip():
        return 0
    terms = _terms(label)
    if len(label) >= 2 and label.casefold() in question.casefold():
        # An ASCII token must not accidentally match a longer word.
        if not label.isascii() or re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", question, re.I):
            return 4
    return min(3, len(terms & _terms(question)))


def retrieve_schema(question, catalog, limit=16):
    """Rank all inspected tables with auditable table/column/value matches."""
    limit = max(1, min(32, int(limit)))
    question = str(question)[:8000]
    by_table = {}
    for table in catalog.get("tables", [])[:256]:
        matches, columns = [], []
        score = _match(question, table["name"]) * 4
        if score:
            matches.append({"kind": "table_name", "text": table["name"], "origin": "ddl"})
        for col in table.get("columns", [])[:256]:
            col_score = _match(question, col["name"])
            if col_score:
                columns.append({"column": col["name"], "score": col_score, "origin": "ddl"})
                score += col_score * 2
        for profile in table.get("details", {}).get("column_profiles", [])[:256]:
            for value in profile.get("distinct_values", [])[:12]:
                if isinstance(value, str) and len(value) >= 2 and _match(question, value) >= 4:
                    matches.append({"kind": "sample_value", "column": profile["name"], "text": value,
                                    "origin": "bounded_sample", "scope": "sample_only"})
                    score += 2
                    break
        by_table[table["name"]] = {"table": table["name"], "score": score,
                                   "columns": columns[:24], "matches": matches[:24]}
    for definition in catalog.get("semantic_layer", {}).get("definitions", [])[:128]:
        if definition.get("origin") != "user_confirmed" or definition.get("status") != "confirmed":
            continue
        labels = [definition.get("label", ""), *definition.get("aliases", [])]
        matched = [label for label in labels if _match(question, label)]
        if not matched:
            continue
        for ref in definition.get("references", []):
            entry = by_table.get(ref.get("table"))
            if entry is not None:
                entry["score"] += max(_match(question, s) for s in matched) * 3
                entry["matches"].append({"kind": "confirmed_definition", "definition_id": definition["id"],
                                         "text": matched[0], "origin": "user_confirmed", **ref})
                if ref.get("column"):
                    entry["columns"].append({"column": ref["column"], "origin": "user_confirmed",
                                             "definition_id": definition["id"], "score": 4})
    ranked = sorted(by_table.values(), key=lambda entry: (-entry["score"], entry["table"]))
    for entry in ranked:
        entry["matches"] = entry["matches"][:24]
        entry["columns"] = sorted(entry["columns"], key=lambda c: -c.get("score", 0))[:24]
    return {"method": "lexical_schema_confirmed_aliases_bounded_values",
            "embedding_used": False, "ranked_tables": ranked[:limit],
            "inspected_table_count": len(by_table), "returned_table_count": min(limit, len(by_table)),
            "truncated": len(by_table) > limit or bool(catalog.get("truncated")),
            "no_match": not ranked or ranked[0]["score"] == 0,
            "negative_value_matches_do_not_prove_absence": True}


def _graph(catalog):
    names = {t["name"] for t in catalog.get("tables", [])}
    graph = {n: [] for n in names}
    for rel in catalog.get("relationships", []):
        a, b = rel.get("table"), rel.get("referenced_table")
        if a not in names or b not in names or not rel.get("target_available"):
            continue
        if (not rel.get("columns") or len(rel["columns"]) != len(rel.get("referenced_columns", []))
                or any(not isinstance(c, str) for c in rel["referenced_columns"])):
            continue
        edge = {"table": a, "columns": rel["columns"], "referenced_table": b,
                "referenced_columns": rel["referenced_columns"], "origin": "ddl",
                "foreign_key_id": rel.get("id"), "data_integrity_verified": False}
        graph[a].append((b, edge)); graph[b].append((a, edge))
    return graph


def _paths(graph, start, end, max_hops=8):
    queue = deque([(start, [start], [])])
    shortest = None
    found = []
    expanded = 0
    while queue and expanded < 4096:
        node, nodes, edges = queue.popleft(); expanded += 1
        if shortest is not None and len(edges) > shortest:
            break
        if node == end:
            found.append({"tables": nodes, "relationships": edges})
            shortest = len(edges)
            if len(found) >= 2:
                break
            continue
        if len(edges) >= max_hops:
            continue
        for other, edge in graph.get(node, []):
            if other not in nodes:
                queue.append((other, nodes + [other], edges + [edge]))
    return found, bool(queue and expanded >= 4096)


def expand_fk_bridges(catalog, selected, max_tables=16):
    """Connect selected endpoints only through unambiguous declared paths.

    Alternative equal-length FK paths are returned for explicit planning; none
    is silently treated as the intended business relation. Unconnected tables
    may still be legitimate UNION/independent subquery sources.
    """
    graph = _graph(catalog)
    if (not isinstance(selected, list) or not selected or len(selected) > max_tables
            or any(not isinstance(n, str) or n not in graph for n in selected)):
        raise ValueError("关联端点必须属于当前已读取的资料结构且不超过预算")
    original = list(dict.fromkeys(selected))
    result = list(original)
    paths, ambiguous, disconnected = [], [], []
    truncated = False
    # A spanning forest avoids forcing every pair's shortest path into the
    # query. Existing connected components are merged only when a path is unique.
    components = [{n} for n in original]
    while len(components) > 1:
        choices = []
        for i, left in enumerate(components):
            for j in range(i + 1, len(components)):
                right = components[j]
                best = []
                for a in sorted(left):
                    for b in sorted(right):
                        candidates, capped = _paths(graph, a, b)
                        truncated |= capped
                        if candidates:
                            best.append((len(candidates[0]["relationships"]), a, b, candidates))
                if best:
                    choices.append((min(best, key=lambda c: (c[0], c[1], c[2])), i, j))
        if not choices:
            break
        # Prefer a unique route. Ambiguous alternatives are recorded afterward.
        choices.sort(key=lambda c: (len(c[0][3]) != 1, c[0][0], c[0][1], c[0][2]))
        (distance, a, b, alternatives), i, j = choices[0]
        if len(alternatives) > 1:
            ambiguous.append({"from": a, "to": b, "alternatives": alternatives})
            break
        path = alternatives[0]
        additions = [n for n in path["tables"] if n not in result]
        if len(result) + len(additions) > max_tables:
            truncated = True
            break
        result.extend(additions); paths.append(path)
        components[i] |= components[j] | set(additions)
        components.pop(j)
    if len(components) > 1:
        disconnected = [sorted(c) for c in components]
    return {"selected_tables": result, "requested_tables": original,
            "bridge_tables": [n for n in result if n not in original],
            "paths": paths, "ambiguous_paths": ambiguous,
            "unconnected_components": disconnected, "truncated": truncated,
            "relationship_basis": "declared_foreign_keys_only",
            "data_integrity_verified": False}
