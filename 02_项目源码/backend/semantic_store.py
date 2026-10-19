"""Explicit business definitions, isolated by project and immutable source scope.

Only this store creates user-confirmation provenance. Saving a model candidate
never confirms it; confirmation is a separate, optimistic-concurrency-checked
operation with the complete reviewed definition and an explicit acknowledgement.
Revocation is an event and does not rewrite older answer/source records.
"""
from __future__ import annotations

import hashlib
import json

from .schema_catalog import build_catalog
from .schema_semantics import KINDS, MAX_DEFINITIONS, build_semantic_layer
from .source_snapshot import snapshot
from .store import identity, now, unpack


class SemanticStoreError(ValueError):
    def __init__(self, message, code="invalid_definition"):
        super().__init__(message)
        self.code = code


DEFINITION_FIELDS = {"id", "kind", "label", "definition", "references", "aliases",
                     "unit", "time_grain", "timezone", "null_policy", "denominator", "grain"}
PATCH_FIELDS = {"expected_revision", "expected_source_revision", "action", "definition", "definition_id", "confirmed"}


def _parse_scope(source):
    # Root may add a separate semantic_revision to SourceSnapshot. Never feed
    # that value back into this store's immutable parsing scope.
    clean = {k: v for k, v in source.items() if k not in {'schema_semantics', 'semantic_revision'}}
    scope = snapshot(clean)
    return {k: scope[k] for k in ('id', 'name', 'sha256', 'parser_version', 'revision')}


def _storage_revision(scope):
    return hashlib.sha256(json.dumps([scope['revision'], scope['parser_version']], ensure_ascii=False).encode()).hexdigest()


def _stamp_semantic_revision(state):
    state['semantic_revision'] = hashlib.sha256(json.dumps(
        [state['project_id'], state['source_id'], state['source_sha256'], state['source_revision'],
         state['parser_version'], state['revision']], ensure_ascii=False).encode()).hexdigest()
    return state


def _validate_definition(raw, catalog):
    if not isinstance(raw, dict) or set(raw) - DEFINITION_FIELDS:
        raise SemanticStoreError("业务定义含未允许的字段，不能自行指定来源或确认状态")
    for key, limit in (("id", 100), ("label", 200), ("definition", 1200)):
        if not isinstance(raw.get(key), str) or not raw[key].strip() or len(raw[key]) > limit:
            raise SemanticStoreError(f"业务定义 {key} 缺失或超过长度限制")
    if not isinstance(raw.get("kind"), str) or raw["kind"] not in KINDS:
        raise SemanticStoreError("业务定义类别无效")
    fields = {t["name"]: {c["name"] for c in t.get("columns", [])} for t in catalog["tables"]}
    refs = raw.get("references")
    if not isinstance(refs, list) or not 1 <= len(refs) <= 16:
        raise SemanticStoreError("请提供 1 至 16 个当前资料中的表字段引用")
    clean_refs = []
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) - {"table", "column"}
                or not isinstance(ref.get("table"), str) or ref["table"] not in fields):
            raise SemanticStoreError("业务定义引用的表不属于当前资料")
        if "column" in ref and (not isinstance(ref["column"], str) or ref["column"] not in fields[ref["table"]]):
            raise SemanticStoreError("业务定义引用的字段不属于当前表")
        if ref not in clean_refs: clean_refs.append(dict(ref))
    aliases = raw.get("aliases", [])
    if (not isinstance(aliases, list) or len(aliases) > 16
            or any(not isinstance(a, str) or not a.strip() or len(a) > 100 for a in aliases)):
        raise SemanticStoreError("别名须为最多 16 项、每项不超过 100 字的非空文本")
    result = {k: raw[k].strip() for k in ("id", "kind", "label", "definition")}
    result.update(references=clean_refs, aliases=list(dict.fromkeys(a.strip() for a in aliases)))
    for key in DEFINITION_FIELDS - {"id", "kind", "label", "definition", "references", "aliases"}:
        if key in raw:
            if not isinstance(raw[key], str) or not raw[key].strip() or len(raw[key]) > 500:
                raise SemanticStoreError(f"{key} 必须是 1 至 500 字的业务说明")
            result[key] = raw[key].strip()
    return result


class SemanticStore:
    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store.transaction() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS schema_semantic_states(
                project_id TEXT NOT NULL REFERENCES projects(id),
                source_id TEXT NOT NULL REFERENCES sources(id),
                source_sha256 TEXT NOT NULL, source_revision TEXT NOT NULL,
                revision INTEGER NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY(project_id,source_id,source_sha256,source_revision))''')
            connection.execute('''CREATE TABLE IF NOT EXISTS schema_semantic_events(
                project_id TEXT NOT NULL REFERENCES projects(id),
                source_id TEXT NOT NULL REFERENCES sources(id),
                source_sha256 TEXT NOT NULL, source_revision TEXT NOT NULL,
                revision INTEGER NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY(project_id,source_id,source_sha256,source_revision,revision))''')

    @staticmethod
    def _source(connection, project_id, source_id, writable=False):
        source = unpack(connection.execute('SELECT * FROM sources WHERE id=? AND project_id=?',
                                           (source_id, project_id)).fetchone())
        if source is None:
            raise SemanticStoreError("资料不存在或不属于当前项目", "not_found")
        if writable and (source.get("archived") or source.get("status") not in {"ready", "attention"}):
            raise SemanticStoreError("当前资料不可编辑业务定义，请等待处理完成或先恢复资料", "source_unavailable")
        return source

    @staticmethod
    def _state(connection, source):
        scope = _parse_scope(source)
        keys = (source["project_id"], source["id"], scope["sha256"], _storage_revision(scope))
        row = connection.execute('''SELECT body FROM schema_semantic_states
            WHERE project_id=? AND source_id=? AND source_sha256=? AND source_revision=?''', keys).fetchone()
        if row:
            state = json.loads(row[0])
        else:
            state = {"project_id": source["project_id"], "source_id": source["id"],
                     "source_sha256": scope["sha256"], "source_revision": scope["revision"],
                     "parser_version": scope["parser_version"], "revision": 0,
                     "confirmed": [], "candidates": [], "revoked": []}
        state["stale_scope_count"] = connection.execute('''SELECT COUNT(*) FROM schema_semantic_states
            WHERE project_id=? AND source_id=? AND NOT(source_sha256=? AND source_revision=?)''', keys).fetchone()[0]
        return _stamp_semantic_revision(state)

    def get(self, project_id, source_id):
        with self.store.transaction() as connection:
            source = self._source(connection, project_id, source_id)
            return self._state(connection, source)

    def update(self, project_id, source_id, payload):
        if not isinstance(payload, dict) or set(payload) - PATCH_FIELDS:
            raise SemanticStoreError("业务定义更新字段无效")
        expected = payload.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise SemanticStoreError("请提供当前业务定义版本")
        expected_source = payload.get("expected_source_revision")
        if not isinstance(expected_source, str) or not expected_source:
            raise SemanticStoreError("请提供当前资料内容版本")
        action = payload.get("action")
        if action not in ("confirm", "save_candidate", "revoke"):
            raise SemanticStoreError("业务定义操作无效")
        if action == "confirm" and payload.get("confirmed") is not True:
            raise SemanticStoreError("确认业务定义需要明确确认完整内容，候选不会自动升级")
        if action != "confirm" and "confirmed" in payload:
            raise SemanticStoreError("保存候选或撤销不能携带确认标记")
        with self.store.transaction() as connection:
            source = self._source(connection, project_id, source_id, writable=True)
            state = self._state(connection, source)
            if state["revision"] != expected or state["source_revision"] != expected_source:
                raise SemanticStoreError("业务定义或资料版本已变化，请重新读取后再编辑", "revision_conflict")
            before = None
            if action == "revoke":
                ident = payload.get("definition_id")
                if not isinstance(ident, str) or not ident or payload.get("definition") is not None:
                    raise SemanticStoreError("撤销需指定一个现有定义标识")
                before = next((item for item in state["confirmed"] + state["candidates"] if item["id"] == ident), None)
                if before is None:
                    raise SemanticStoreError("待撤销的定义不存在", "not_found")
                updated = None
            else:
                if payload.get("definition_id") is not None:
                    raise SemanticStoreError("保存完整定义时请在 definition.id 中指定标识")
                catalog = build_catalog(source, table_names=[])
                updated = _validate_definition(payload.get("definition"), catalog)
                ident = updated["id"]
                before = next((item for item in state["confirmed"] + state["candidates"] if item["id"] == ident), None)
                if action == "save_candidate" and before and before.get("confirmation_id"):
                    raise SemanticStoreError("候选不能覆盖已确认定义，请先明确撤销或提交完整确认修改")
                if action == "confirm":
                    updated["confirmation_id"] = identity()
                # Both authoritative origin and timestamps are server-generated.
                updated["origin"] = "user_confirmed" if action == "confirm" else "model_candidate"
                updated["status"] = "confirmed" if action == "confirm" else "requires_confirmation"
                updated["updated_at"] = now()
            state["confirmed"] = [d for d in state["confirmed"] if d["id"] != ident]
            state["candidates"] = [d for d in state["candidates"] if d["id"] != ident]
            if updated:
                state["confirmed" if action == "confirm" else "candidates"].append(updated)
                if len(state["confirmed"]) + len(state["candidates"]) > MAX_DEFINITIONS:
                    raise SemanticStoreError("当前资料业务定义数量超过预算")
                normalized = build_semantic_layer(catalog, source, state)
                if normalized["truncated"]:
                    raise SemanticStoreError("业务定义总长度超过可完整核对的预算，请精简后保存")
                statuses = {entry['id']: entry['status'] for entry in normalized['definitions']}
                for entry in state['confirmed'] + state['candidates']:
                    entry['status'] = statuses.get(entry['id'], 'requires_confirmation')
                state['warnings'] = normalized['warnings']
            else:
                # Active state carries a bounded revocation index; immutable
                # event rows retain every full prior definition without a cap.
                state["revoked"] = (state["revoked"] + [{"id": ident, "revoked_at": now(),
                                                         "previous_confirmation_id": before.get("confirmation_id")}])[-128:]
                catalog = build_catalog(source, table_names=[])
                normalized = build_semantic_layer(catalog, source, state)
                statuses = {entry['id']: entry['status'] for entry in normalized['definitions']}
                for entry in state['confirmed'] + state['candidates']:
                    entry['status'] = statuses.get(entry['id'], 'requires_confirmation')
                state['warnings'] = normalized['warnings']
            state["revision"] += 1
            state["updated_at"] = now()
            _stamp_semantic_revision(state)
            event = {"action": action, "definition_id": ident, "before": before,
                     "after": updated, "revision": state["revision"], "created_at": state["updated_at"]}
            keys = (project_id, source_id, state["source_sha256"], _storage_revision(_parse_scope(source)), state["revision"])
            connection.execute('''INSERT INTO schema_semantic_events VALUES(?,?,?,?,?,?)''',
                               (*keys, json.dumps(event, ensure_ascii=False)))
            connection.execute('''INSERT INTO schema_semantic_states VALUES(?,?,?,?,?,?)
                ON CONFLICT(project_id,source_id,source_sha256,source_revision)
                DO UPDATE SET revision=excluded.revision,body=excluded.body''',
                               (*keys, json.dumps(state, ensure_ascii=False)))
            return state

    def delete(self, project_id, source_id, definition_id, *, expected_revision, expected_source_revision):
        return self.update(project_id, source_id, {"action": "revoke", "definition_id": definition_id,
            "expected_revision": expected_revision, "expected_source_revision": expected_source_revision})

    def inject(self, source):
        """Return a copy; stale or mismatched caller snapshots receive no rules."""
        with self.store.transaction() as connection:
            current = self._source(connection, source["project_id"], source["id"])
            if _parse_scope(current) != _parse_scope(source):
                raise SemanticStoreError("资料快照已变化，不能注入另一版本的业务定义", "revision_conflict")
            state = self._state(connection, current)
        return {**source, "schema_semantics": state, "semantic_revision": state['semantic_revision']}

    def events(self, project_id, source_id, after=0, limit=100):
        """Current-scope audit only; other scopes remain stored, never merged."""
        with self.store.transaction() as connection:
            source = self._source(connection, project_id, source_id)
            scope = _parse_scope(source)
            return [json.loads(row[0]) for row in connection.execute('''SELECT body FROM schema_semantic_events
                WHERE project_id=? AND source_id=? AND source_sha256=? AND source_revision=? AND revision>?
                ORDER BY revision LIMIT ?''', (project_id, source_id, scope["sha256"], _storage_revision(scope),
                                               max(0, int(after)), max(1, min(100, int(limit)))))]


def read_state(connection, source):
    """Read a current-scope state using an existing read-only SQLite connection.

    Does not initialize tables, commit, update source metadata, or incorporate
    injected semantic fields into the parsing scope. Intended for export
    freshness checks in the same transaction as the saved answer read.
    """
    return SemanticStore._state(connection, source)
