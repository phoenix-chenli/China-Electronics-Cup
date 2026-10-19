"""Durable metadata, with transactional deduplication and isolated projects."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def identity() -> str:
    return uuid.uuid4().hex


def unpack(row):
    if row is None:
        return None
    data = dict(row)
    for name in ("warnings", "pages", "tables", "metadata", "source_ids"):
        if name in data:
            data[name] = json.loads(data[name])
    if "archived" in data:
        data["archived"] = bool(data["archived"])
    return data


SOURCE_FIELDS = ("id", "project_id", "name", "kind", "format", "bytes", "sha256", "status", "stage", "progress", "summary", "error", "warnings", "page_count", "table_count", "row_count", "created_at", "updated_at", "parser_version", "archived", "review_status", "review_note", "reviewed_at", "reviewed_parser_version")


def public_source(source):
    from .parsers import PARSER_VERSION
    from .source_snapshot import snapshot
    result = {field: source[field] for field in SOURCE_FIELDS}
    result['content_revision'] = snapshot(source)['revision']
    result['reprocess_available'] = (not source['archived'] and source['status'] in {'ready', 'attention'} and source['parser_version'] != PARSER_VERSION)
    result["usable"] = not source["archived"] and source["status"] in {"ready", "attention"} and (any(p["text"].strip() for p in source.get("pages", [])) or bool(source.get("tables", [])))
    for field in ("origin", "source_group", "derived_from"):
        value = source.get("metadata", {}).get(field)
        result[field] = value if isinstance(value, str) and value.strip() else None
    return result


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "workspace.sqlite"
        self.lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self.lock:
            connection = sqlite3.connect(self.path, timeout=15)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in ("originals", "incoming", "derived", "previews"):
            (self.root / folder).mkdir(exist_ok=True)
        with sqlite3.connect(self.path) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
                CREATE TABLE IF NOT EXISTS projects(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    name TEXT NOT NULL, kind TEXT NOT NULL, format TEXT NOT NULL,
                    bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL,
                    stage TEXT NOT NULL, progress INTEGER NOT NULL, summary TEXT NOT NULL,
                    error TEXT, warnings TEXT NOT NULL DEFAULT '[]', page_count INTEGER NOT NULL DEFAULT 0,
                    table_count INTEGER NOT NULL DEFAULT 0, row_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, parser_version TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0, original_path TEXT NOT NULL,
                    data_path TEXT, pages TEXT NOT NULL DEFAULT '[]', tables TEXT NOT NULL DEFAULT '[]',
                    metadata TEXT NOT NULL DEFAULT '{}');
                CREATE UNIQUE INDEX IF NOT EXISTS unique_active_content
                    ON sources(project_id, sha256) WHERE archived=0;
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    source_id TEXT NOT NULL REFERENCES sources(id), source_name TEXT NOT NULL,
                    status TEXT NOT NULL, stage TEXT NOT NULL, progress INTEGER NOT NULL,
                    message TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_project ON jobs(project_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS drafts(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    question TEXT NOT NULL, source_ids TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS sources_project ON sources(project_id, archived, created_at DESC);
                CREATE INDEX IF NOT EXISTS sources_content ON sources(sha256, format, archived, status);
            """)
            # Additive and repeatable: existing originals, source IDs and drafts survive upgrades.
            additions = {
                "sources": {
                    "review_status": "TEXT NOT NULL DEFAULT 'pending'",
                    "review_note": "TEXT NOT NULL DEFAULT ''",
                    "reviewed_at": "TEXT",
                    "reviewed_parser_version": "TEXT",
                },
                "drafts": {
                    "archived": "INTEGER NOT NULL DEFAULT 0",
                    "updated_at": "TEXT",
                },
            }
            for table, columns in additions.items():
                existing = {row[1] for row in c.execute(f"PRAGMA table_info({table})")}
                for name, declaration in columns.items():
                    if name not in existing:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            c.execute("UPDATE drafts SET updated_at=created_at WHERE updated_at IS NULL")
            c.execute("CREATE INDEX IF NOT EXISTS drafts_project ON drafts(project_id,archived,created_at DESC)")

    def get(self, table, item_id):
        if table not in {"projects", "sources", "jobs", "drafts"}:
            raise ValueError("Unknown metadata table")
        with self.transaction() as c:
            return unpack(c.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone())

    def projects(self):
        with self.transaction() as c:
            return [unpack(r) for r in c.execute("SELECT * FROM projects ORDER BY updated_at DESC, id")]

    def create_project(self, name, description):
        item = dict(id=identity(), name=name, description=description, created_at=now(), updated_at=now())
        with self.transaction() as c:
            c.execute("INSERT INTO projects VALUES(:id,:name,:description,:created_at,:updated_at)", item)
        return item

    def update_project(self, project_id, name, description):
        with self.transaction() as c:
            c.execute("UPDATE projects SET name=?,description=COALESCE(?,description),updated_at=? WHERE id=?", (name, description, now(), project_id))
        return self.get("projects", project_id)

    def sources(self, project_id, archived=False):
        with self.transaction() as c:
            return [unpack(r) for r in c.execute("SELECT * FROM sources WHERE project_id=? AND archived=? ORDER BY created_at DESC,id", (project_id, int(archived)))]

    def jobs(self, project_id):
        with self.transaction() as c:
            return [unpack(r) for r in c.execute("SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC,id", (project_id,))]

    def drafts(self, project_id, archived=False):
        with self.transaction() as c:
            return [unpack(r) for r in c.execute("SELECT * FROM drafts WHERE project_id=? AND archived=? ORDER BY created_at DESC,id", (project_id, int(archived)))]

    def draft(self, project_id, draft_id):
        with self.transaction() as c:
            return unpack(c.execute("SELECT * FROM drafts WHERE id=? AND project_id=?", (draft_id, project_id)).fetchone())

    def add_source(self, source, original, metadata):
        """Caller stages a complete original; only the winning transaction moves it."""
        with self.transaction() as c:
            duplicate = unpack(c.execute("SELECT * FROM sources WHERE project_id=? AND sha256=? AND archived=0", (source["project_id"], source["sha256"])).fetchone())
            if duplicate:
                return None, None, duplicate
            # Reuse immutable parsed content across projects; ownership, review
            # and business definitions remain attached to a new source ID.
            from .parsers import PARSER_VERSION
            candidates = c.execute("SELECT * FROM sources WHERE sha256=? AND format=? AND archived=0 AND status IN ('ready','attention') AND parser_version=? ORDER BY updated_at DESC,id", (source['sha256'], source['format'], PARSER_VERSION)).fetchall()
            for candidate in candidates:
                try:
                    reused = self._reuse_source(c, unpack(candidate), source['project_id'], name=source['name'], metadata=metadata)
                except ValueError:
                    continue  # A missing cache must not block a fresh upload.
                return reused, None, None
            target = self.root / "originals" / (source["id"] + "." + source["format"])
            original.replace(target)
            target.chmod(0o444)
            record = {**source, "original_path": str(target), "warnings": "[]", "metadata": json.dumps(metadata, ensure_ascii=False)}
            fields = list(record)
            c.execute(f"INSERT INTO sources({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", [record[k] for k in fields])
            job = self._new_job(c, source)
            c.execute("UPDATE projects SET updated_at=? WHERE id=?", (now(), source["project_id"]))
        return self.get("sources", source["id"]), job, None

    def _reuse_source(self, connection, source, project_id, name=None, metadata=None):
        if source['archived'] or not public_source(source)['usable']:
            raise ValueError('所选资料尚未完成解析或已归档，请刷新后重选')
        original = Path(source['original_path'])
        try:
            with original.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        except OSError as exc:
            raise ValueError('已有资料的原件缺失，请重新上传') from exc
        if digest != source['sha256']:
            raise ValueError('已有资料原件已改变，请重新上传')
        if source['tables'] and (not source['data_path'] or not Path(source['data_path']).is_file()):
            raise ValueError('已有资料的解析表格缺失，请重新解析后添加')
        record = {**source, 'id': identity(), 'project_id': project_id,
                  'name': name or source['name'], 'created_at': now(), 'updated_at': now(),
                  'archived': False, 'review_status': 'pending', 'review_note': '',
                  'reviewed_at': None, 'reviewed_parser_version': None,
                  'metadata': {**source['metadata'], **(metadata or {}),
                               'import_method': 'reuse', 'reused_from': source['id']}}
        # Original and derived files are versioned and never overwritten by
        # retry. Sharing these paths avoids another file copy or parsing job.
        fields = list(record)
        values = [json.dumps(record[k], ensure_ascii=False) if k in {'pages','tables','warnings','metadata'} else record[k] for k in fields]
        connection.execute(f"INSERT INTO sources({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", values)
        connection.execute('UPDATE projects SET updated_at=? WHERE id=?', (record['updated_at'], project_id))
        return record

    def reuse_sources(self, project_id, source_ids):
        sources, duplicates = [], []
        with self.transaction() as c:
            for source_id in dict.fromkeys(source_ids):
                source = unpack(c.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone())
                if not source or source['archived'] or not public_source(source)['usable']:
                    raise ValueError('所选资料不存在、已归档或尚未完成解析，请刷新后重选')
                duplicate = unpack(c.execute('SELECT * FROM sources WHERE project_id=? AND sha256=? AND archived=0', (project_id, source['sha256'])).fetchone())
                if duplicate:
                    duplicates.append(public_source(duplicate))
                else:
                    sources.append(public_source(self._reuse_source(c, source, project_id)))
        return dict(sources=sources, jobs=[], duplicates=duplicates)

    def _new_job(self, connection, source):
        job = dict(id=identity(), project_id=source["project_id"], source_id=source["id"], source_name=source["name"], status="queued", stage="等待处理", progress=0, message="文件已保存，等待解析", created_at=now(), updated_at=now())
        connection.execute("INSERT INTO jobs VALUES(:id,:project_id,:source_id,:source_name,:status,:stage,:progress,:message,:created_at,:updated_at)", job)
        return job

    def retry(self, source_id):
        from .parsers import PARSER_VERSION
        with self.transaction() as c:
            source = unpack(c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())
            if not source or source["archived"]:
                raise ValueError("请先恢复已归档资料")
            if source["status"] in {"queued", "processing"}:
                raise ValueError("该资料仍在处理中")
            if source["status"] == "ready" and source["parser_version"] == PARSER_VERSION:
                raise ValueError("资料已处理完成，无需重试")
            c.execute("UPDATE sources SET status='queued',stage='等待重新处理',progress=0,error=NULL,review_status='pending',reviewed_at=NULL,reviewed_parser_version=NULL,updated_at=? WHERE id=?", (now(), source_id))
            job = self._new_job(c, source)
        return self.get("sources", source_id), job

    def recover(self):
        with self.transaction() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM jobs WHERE status IN ('queued','processing')")]
            c.execute("UPDATE jobs SET status='queued',stage='等待恢复处理',progress=0,message='上次处理中断，已加入恢复队列',updated_at=? WHERE status IN ('queued','processing')", (now(),))
            c.execute("UPDATE sources SET status='queued',stage='等待恢复处理',progress=0,updated_at=? WHERE status IN ('queued','processing')", (now(),))
        return ids

    def claim(self, job_id):
        with self.transaction() as c:
            result = c.execute("UPDATE jobs SET status='processing',stage='检查文件',progress=5,updated_at=? WHERE id=? AND status='queued'", (now(), job_id))
            if not result.rowcount:
                return None
            job = unpack(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            c.execute("UPDATE sources SET status='processing',stage='检查文件',progress=5,updated_at=? WHERE id=?", (now(), job["source_id"]))
        return job

    def progress(self, job_id, stage, progress, message):
        with self.transaction() as c:
            job = c.execute("SELECT source_id FROM jobs WHERE id=? AND status='processing'", (job_id,)).fetchone()
            if not job:
                return
            c.execute("UPDATE jobs SET stage=?,progress=?,message=?,updated_at=? WHERE id=?", (stage, progress, message, now(), job_id))
            c.execute("UPDATE sources SET stage=?,progress=?,updated_at=? WHERE id=?", (stage, progress, now(), job[0]))

    def finish(self, job_id, result=None, error=None):
        with self.transaction() as c:
            job = c.execute("SELECT source_id FROM jobs WHERE id=? AND status='processing'", (job_id,)).fetchone()
            if not job:return
            source_id = job[0]
            if error:
                c.execute("UPDATE sources SET status='failed',stage='处理失败',error=?,summary='未完成解析，可查看原件后重试',updated_at=? WHERE id=?", (error, now(), source_id))
                c.execute("UPDATE jobs SET status='failed',stage='处理失败',message=?,updated_at=? WHERE id=?", (error, now(), job_id))
                return
            status = "attention" if result["warnings"] else "ready"
            stage = "需要关注" if status == "attention" else "处理完成"
            existing = json.loads(c.execute("SELECT metadata FROM sources WHERE id=?", (source_id,)).fetchone()[0])
            existing.update(result["metadata"])
            c.execute("""UPDATE sources SET status=?,stage=?,progress=100,summary=?,error=NULL,warnings=?,
                page_count=?,table_count=?,row_count=?,pages=?,tables=?,metadata=?,data_path=?,parser_version=COALESCE(?,parser_version),updated_at=? WHERE id=?""",
                (status, stage, result["summary"], json.dumps(result["warnings"], ensure_ascii=False), result["page_count"], len(result["tables"]), sum(t["row_count"] for t in result["tables"]), json.dumps(result["pages"], ensure_ascii=False), json.dumps(result["tables"], ensure_ascii=False), json.dumps(existing, ensure_ascii=False), result.get("data_path"), result.get("parser_version"), now(), source_id))
            c.execute("UPDATE jobs SET status=?,stage=?,progress=100,message=?,updated_at=? WHERE id=?", ("attention" if status == "attention" else "completed", stage, result["summary"], now(), job_id))

    def archive(self, source_id, archived):
        with self.transaction() as c:
            c.execute("UPDATE sources SET archived=?,updated_at=? WHERE id=?", (int(archived), now(), source_id))

    def review(self, source_id, status, note):
        with self.transaction() as c:
            source = unpack(c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())
            if not source or source["archived"]:
                raise ValueError("请先恢复已归档资料，再保存核对记录")
            if status == "reviewed" and source["status"] not in {"ready", "attention"}:
                raise ValueError("资料尚未完成解析，不能标记为已核对")
            stamp = now()
            reviewed_at = stamp if status == "reviewed" else None
            parser_version = source["parser_version"] if status == "reviewed" else None
            c.execute("UPDATE sources SET review_status=?,review_note=?,reviewed_at=?,reviewed_parser_version=?,updated_at=? WHERE id=?", (status, note, reviewed_at, parser_version, stamp, source_id))
        return self.get("sources", source_id)

    def _validate_draft_sources(self, connection, project_id, source_ids):
        for source_id in source_ids:
            source = unpack(connection.execute("SELECT * FROM sources WHERE id=? AND project_id=? AND archived=0", (source_id, project_id)).fetchone())
            if not source:
                raise ValueError("所选资料不存在、已归档或不属于当前项目")
            readable = any(p["text"].strip() for p in source["pages"]) or bool(source["tables"])
            if source["status"] not in {"ready", "attention"} or not readable:
                raise ValueError(f"「{source['name']}」尚无可用文字或数据表，请等待解析或选择其他资料")

    def save_draft(self, project_id, question, source_ids):
        with self.transaction() as c:
            self._validate_draft_sources(c, project_id, source_ids)
            stamp = now()
            draft = dict(id=identity(), project_id=project_id, question=question, source_ids=source_ids, created_at=stamp, updated_at=stamp, archived=False)
            c.execute("INSERT INTO drafts(id,project_id,question,source_ids,created_at,updated_at,archived) VALUES(?,?,?,?,?,?,?)", (draft["id"], project_id, question, json.dumps(source_ids), stamp, stamp, 0))
        return draft

    def update_draft(self, project_id, draft_id, question, source_ids):
        with self.transaction() as c:
            draft = unpack(c.execute("SELECT * FROM drafts WHERE id=? AND project_id=?", (draft_id, project_id)).fetchone())
            if not draft or draft["archived"]:
                raise ValueError("请先恢复已归档问题")
            self._validate_draft_sources(c, project_id, source_ids)
            c.execute("UPDATE drafts SET question=?,source_ids=?,updated_at=? WHERE id=? AND project_id=?", (question, json.dumps(source_ids), now(), draft_id, project_id))
        return self.draft(project_id, draft_id)

    def archive_draft(self, project_id, draft_id, archived):
        with self.transaction() as c:
            c.execute("UPDATE drafts SET archived=?,updated_at=? WHERE id=? AND project_id=?", (int(archived), now(), draft_id, project_id))
