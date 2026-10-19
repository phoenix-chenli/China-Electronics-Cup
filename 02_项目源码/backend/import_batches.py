"""Atomic, source/version-bound page checkpoints. No model requests are replayed."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

MAX_PAGE_BYTES = 24_000_000


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


class PageCheckpoints:
    def __init__(self, root, original, parser_version, policy=None):
        self.source_sha256 = digest(original)
        self.identity = {'source_sha256': self.source_sha256, 'parser_version': parser_version,
                         'policy': policy or {}, 'schema_version': 1}
        key = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()).hexdigest()
        self.root = Path(root) / key
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def load(self, number, *, incomplete=False):
        suffix = '.pending.json' if incomplete else '.json'
        path = self.root / f'{int(number):06}{suffix}'
        try:
            if path.stat().st_size > MAX_PAGE_BYTES:
                return None
            value = json.loads(path.read_text(encoding='utf-8'))
            body = value['body']
            checksum = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if value.get('sha256') != checksum or body.get('identity') != self.identity:
                return None
            if body['page']['number'] != number or body.get('incomplete', False) != incomplete:
                return None
            if not incomplete and body['page'].get('extraction') not in ('native', 'ocr') and not body['page'].get('quality', {}).get('blank'):
                return None
            return body
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, page, regions, multi_column=False, *, incomplete=False):
        if not incomplete and page.get('extraction') not in ('native', 'ocr') and not page.get('quality', {}).get('blank'):
            return False
        body = {'identity': self.identity, 'page': page, 'regions': regions, 'multi_column': bool(multi_column), 'incomplete': incomplete}
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True)
        payload = json.dumps({'body': body, 'sha256': hashlib.sha256(raw.encode()).hexdigest()}, ensure_ascii=False)
        if len(payload.encode()) > MAX_PAGE_BYTES:
            return False
        suffix = '.pending.json' if incomplete else '.json'
        path = self.root / f"{int(page['number']):06}{suffix}"
        temporary = path.with_suffix(f'.{os.getpid()}.tmp')
        try:
            with temporary.open('w', encoding='utf-8') as f:
                f.write(payload); f.flush(); os.fsync(f.fileno())
            os.chmod(temporary, 0o600)
            temporary.replace(path)
            if not incomplete:
                (self.root / f"{int(page['number']):06}.pending.json").unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
        return True
