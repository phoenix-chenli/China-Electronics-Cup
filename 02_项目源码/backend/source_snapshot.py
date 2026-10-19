"""Source revisions without leaking local paths; reprocessing invalidates old scope."""
import hashlib
import json


def snapshot(source):
    material = {key: source.get(key) for key in ('pages', 'tables', 'data_path', 'metadata')}
    revision = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True,
                                        separators=(',', ':')).encode()).hexdigest()
    value = {'id': source['id'], 'name': source['name'], 'sha256': source['sha256'],
             'parser_version': source['parser_version'], 'revision': revision}
    if source.get('semantic_revision'):
        value['semantic_revision'] = source['semantic_revision']
    return value


def scope_matches(recorded, current):
    keys = ('id', 'sha256', 'parser_version', 'revision')
    base_matches = {tuple(item.get(k) for k in keys) for item in recorded} == {
        tuple(item.get(k) for k in keys) for item in current}
    if not base_matches:return False
    current_by_id={item['id']:item for item in current}
    # A missing historical scope is unknown, not retroactive verification.
    return all('semantic_revision' not in item or item['semantic_revision']==current_by_id[item['id']].get('semantic_revision') for item in recorded)
