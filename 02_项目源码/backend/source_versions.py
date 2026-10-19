"""Read-only comparison of saved answer scope with current stored source scope.

Snapshots are version identifiers, not retained historical derived content. This
endpoint neither reopens old indexes nor rewrites answers/semantic definitions.
"""
from .semantic_store import SemanticStoreError
from .source_snapshot import snapshot


SNAPSHOT_FIELDS = ('id', 'name', 'sha256', 'parser_version', 'revision', 'semantic_revision')
VERSION_FIELDS = ('sha256', 'parser_version', 'revision', 'semantic_revision')
LABELS = {'sha256': '原件摘要', 'parser_version': '解析版本', 'revision': '解析内容版本',
          'semantic_revision': '业务定义版本'}
SOURCE_STATUSES = {'queued', 'processing', 'ready', 'attention', 'failed', 'cancelled'}


def _public_snapshot(value):
    # Deliberately no wholesale serialization of a source or stored snapshot.
    return {key: value[key] for key in SNAPSHOT_FIELDS
            if isinstance(value.get(key), str) and value[key]}


def _known_version(value, key):
    if not isinstance(value, str) or not value:
        return False
    if key != 'parser_version':
        return len(value) == 64 and all(c in '0123456789abcdef' for c in value)
    return True


def compare_answer_sources(answer, project_id, source_store, semantics):
    if not answer or answer.get('project_id') != project_id:
        raise LookupError('回答不存在或不属于当前项目')
    old_sources = answer.get('sources')
    if not isinstance(old_sources, list) or not old_sources:
        # Legacy records have no content/semantic revision: do not manufacture
        # either from the present source and then declare the old answer current.
        old_sources = [dict(id=answer.get('source_id'), name=answer.get('source_name'),
                            sha256=answer.get('source_sha256'),
                            parser_version=answer.get('parser_version'))]
    rows = []
    for raw in old_sources:
        raw = raw if isinstance(raw, dict) else {}
        old = _public_snapshot(raw)
        sid = old.get('id')
        source = source_store.get('sources', sid) if sid else None
        # A malformed saved answer must not disclose another project's source.
        if source and source.get('project_id') != project_id:
            source = None
        availability = ('archived' if source.get('archived') else
                        'available' if source.get('status') in {'ready', 'attention'} else
                        'unavailable') if source else 'unavailable'
        current, changed, unknown, reasons = None, [], [], []
        for key in VERSION_FIELDS:
            if not _known_version(old.get(key), key):
                unknown.append(key)
                reasons.append(dict(code='historical_version_unknown', field=key,
                                    text=f'历史记录未保存有效的{LABELS[key]}，不能确认这一项一致。'))
        if not source:
            reasons.append(dict(code='current_source_unavailable', text='当前项目中已找不到这份资料，可能已删除；不能读取当前版本。'))
        else:
            try:
                # Same injection and snapshot rules as answer execution; no
                # synthetic semantic revision based on raw source metadata.
                current = _public_snapshot(snapshot(semantics.inject(source)))
            except SemanticStoreError as exc:
                if exc.code not in {'revision_conflict', 'not_found'}:
                    raise
                reasons.append(dict(code='current_source_changed_during_read',
                                    text='资料在比较过程中变化，请刷新后重新比较。'))
            if availability == 'archived':
                changed.append('availability')
                reasons.append(dict(code='source_archived', field='availability',
                                    text='资料已归档，当前不能继续用于提问；原回答保留。'))
            elif availability == 'unavailable':
                reasons.append(dict(code='source_not_ready', field='availability',
                                    text='资料当前尚不可用于提问，请先查看解析状态或资料诊断。'))
        if current:
            for key in VERSION_FIELDS:
                if key not in unknown and old[key] != current.get(key):
                    changed.append(key)
                    reasons.append(dict(code='version_changed', field=key,
                                        text=f'{LABELS[key]}已变化，旧回答不能直接视为当前版本的结论。'))
        status = 'changed' if changed else 'unknown' if unknown or current is None or availability != 'available' else 'unchanged'
        if status == 'unchanged':
            reasons.append(dict(code='stored_versions_match', text='记录中的原件、解析内容及业务定义版本一致；这不是重新执行答案核验。'))
        rows.append(dict(source_id=sid, name=old.get('name') or (current or {}).get('name') or '历史资料',
                         availability=availability,
                         current_status=source.get('status') if source and source.get('status') in SOURCE_STATUSES else None,
                         old_snapshot=old, current_snapshot=current, comparison_status=status,
                         changed_fields=changed, unknown_fields=unknown, reasons=reasons,
                         old_snapshot_queryable=False))
    overall = ('changed' if any(row['comparison_status'] == 'changed' for row in rows) else
               'unknown' if any(row['comparison_status'] == 'unknown' for row in rows) else 'unchanged')
    return dict(project_id=project_id, answer_id=answer['id'], comparison_status=overall,
                sources=rows, old_snapshot_queryable=False,
                comparison_basis='stored_source_and_semantic_snapshots', original_bytes_rehashed=False,
                note='这里只比较保存的版本标识，未重新读取原件摘要或核验答案。历史派生内容未保留为可查询快照；更新文件请重新导入并保留两份原件。')
