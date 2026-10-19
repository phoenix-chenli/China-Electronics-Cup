"""Versioned SQLite generations, resumable staging and atomic active publication."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class IndexStore:
    def __init__(self, directory):
        self.path = Path(directory) / 'retrieval.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS generations (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_id TEXT NOT NULL,
                    version_json TEXT NOT NULL, state TEXT NOT NULL, mode TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL, chunk_count INTEGER NOT NULL DEFAULT 0,
                    scan_truncated INTEGER NOT NULL DEFAULT 0, diagnostics TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS active (
                    project_id TEXT NOT NULL, source_id TEXT NOT NULL, generation TEXT NOT NULL,
                    PRIMARY KEY(project_id,source_id));
                CREATE TABLE IF NOT EXISTS chunks (
                    generation TEXT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL, chunk_id TEXT NOT NULL, payload TEXT NOT NULL,
                    length INTEGER NOT NULL, vector BLOB, PRIMARY KEY(generation,chunk_id));
                CREATE INDEX IF NOT EXISTS chunks_order ON chunks(generation,ordinal);
                CREATE TABLE IF NOT EXISTS postings (
                    generation TEXT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
                    term TEXT NOT NULL, chunk_id TEXT NOT NULL, tf INTEGER NOT NULL,
                    PRIMARY KEY(generation,term,chunk_id));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def invalidate(self, project_id, source_id):
        with self.connect() as db:
            db.execute('DELETE FROM active WHERE project_id=? AND source_id=?', (project_id, source_id))
            db.execute("UPDATE generations SET state='invalidated', updated=? WHERE project_id=? AND source_id=?",
                       (time.time(), project_id, source_id))

    def cleanup(self, *, older_than_seconds=86400):
        """Remove only inactive generations; ready active and fresh resumable builds stay."""
        with self.connect() as db:
            cursor = db.execute('DELETE FROM generations WHERE updated<? AND id NOT IN (SELECT generation FROM active)',
                                (time.time() - max(0, older_than_seconds),))
            return cursor.rowcount

    def active(self, project_id, source_id, generation):
        with self.connect() as db:
            return db.execute('''SELECT 1 FROM active a JOIN generations g ON g.id=a.generation
                WHERE a.project_id=? AND a.source_id=? AND a.generation=? AND g.state='ready' ''',
                (project_id, source_id, generation)).fetchone() is not None

    def status(self, project_id, source_id):
        with self.connect() as db:
            row = db.execute('''SELECT g.* FROM active a JOIN generations g ON g.id=a.generation
                WHERE a.project_id=? AND a.source_id=?''', (project_id, source_id)).fetchone()
            if row is None:
                return {'state': 'not_ready'}
            return {key: row[key] for key in ('id', 'state', 'mode', 'chunk_count', 'scan_truncated', 'updated')}
