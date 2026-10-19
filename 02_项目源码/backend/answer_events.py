"""Durable observable execution events, without prompts, secrets or thoughts."""
from __future__ import annotations
import json
from datetime import datetime


def initialize(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS answer_events(
        answer_id TEXT NOT NULL REFERENCES answers(id), sequence INTEGER NOT NULL,
        project_id TEXT NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY(answer_id, sequence))''')
    connection.execute('CREATE INDEX IF NOT EXISTS answer_events_project ON answer_events(project_id,answer_id,sequence)')


def append(connection, item, previous=None, *, kind='transition'):
    row = connection.execute('SELECT sequence,body FROM answer_events WHERE answer_id=? ORDER BY sequence DESC LIMIT 1', (item['id'],)).fetchone()
    seq = row[0] + 1 if row else 1
    prior = json.loads(row[1]) if row else {}
    elapsed = max(int(item.get('elapsed_ms') or 0), int(prior.get('elapsed_ms') or 0))
    interval = max(0,round((datetime.fromisoformat(item['updated_at'])-datetime.fromisoformat(prior['at'])).total_seconds()*1000)) if prior.get('at') else 0
    event = dict(id=f"{item['id']}:{seq}", sequence=seq, kind=kind,
                 answer_id=item['id'], status=item['status'], stage=item['stage'],
                 at=item['updated_at'], elapsed_ms=elapsed,
                 since_previous_event_ms=interval)
    result = item.get('result')
    if result and item['status'] not in ('queued', 'running'):
        event['output_summary'] = dict(claims=len(result.get('claims') or []),
                                       evidence=len(result.get('evidence') or []),
                                       calculations=len(result.get('calculations') or []),
                                       verification=(result.get('verification') or {}).get('status'))
    connection.execute('INSERT INTO answer_events(answer_id,sequence,project_id,body) VALUES(?,?,?,?)',
                       (item['id'], seq, item['project_id'], json.dumps(event, ensure_ascii=False)))
    return event
