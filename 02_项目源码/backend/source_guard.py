"""Read-only source-version checks without repeated decoding of large JSON.

A dedicated read-only SQLite connection observes PRAGMA data_version. Every
external commit invalidates the fast path, including direct SQL updates that do
not touch updated_at. On such a commit the exact stored source fields are read
and hashed again. No trigger, mutable cache column or writer connection is used.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import weakref


FIELDS = ('id', 'project_id', 'name', 'sha256', 'parser_version', 'archived', 'status',
          'original_path', 'data_path', 'pages', 'tables', 'metadata')
JSON_FIELDS = frozenset(('pages', 'tables', 'metadata'))


def _digest(row):
    result = hashlib.sha256()
    for field in FIELDS:
        value = row[field]
        data = b'N' if value is None else b'V' + str(value).encode('utf-8')
        result.update(len(data).to_bytes(8, 'big'))
        result.update(data)
    return result.digest()


class StoreSourceGuard:
    def __init__(self, store, source):
        self.path = Path(store.path).resolve()
        self.identifier = source['id']
        self._lock = threading.RLock()
        self._valid = True
        self.full_reads = 0
        stat = self.path.stat()
        self._database_identity = (stat.st_dev, stat.st_ino)
        self._db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True,
                                   timeout=5, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._finalizer = weakref.finalize(self, self._db.close)
        row, version = self._read_stable()
        if (row is None or row['archived'] or row['status'] not in ('ready', 'attention')
                or any((json.loads(row[k]) if k in JSON_FIELDS else row[k]) != source.get(k)
                       for k in FIELDS)):
            self.close()
            raise ValueError('资料在建立版本检查时已改变')
        self._expected = _digest(row)
        self._version = version

    def _version_now(self):
        return self._db.execute('PRAGMA data_version').fetchone()[0]

    def _read_stable(self):
        # Never hold a read transaction across requests. If a concurrent writer
        # commits during the read, retry once and otherwise fail closed.
        for _ in range(2):
            before = self._version_now()
            row = self._db.execute('SELECT ' + ','.join(FIELDS) + ' FROM sources WHERE id=?',
                                   (self.identifier,)).fetchone()
            self.full_reads += 1
            after = self._version_now()
            if before == after:
                return row, after
        raise ValueError('资料正在变化，请重新准备检索')

    def __call__(self):
        with self._lock:
            if not self._valid:
                return False
            try:
                stat = self.path.stat()
                if (stat.st_dev, stat.st_ino) != self._database_identity:
                    self._valid = False
                elif self._version_now() != self._version:
                    row, version = self._read_stable()
                    self._valid = row is not None and _digest(row) == self._expected
                    self._version = version
            except (OSError, sqlite3.Error, ValueError):
                self._valid = False
            return self._valid

    def close(self):
        with self._lock:
            self._valid = False
            self._finalizer()
