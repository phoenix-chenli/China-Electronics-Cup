"""Retrieval context: original task scope accompanies each tool search, never gold.

The original task and actual tool query remain distinct in the audit.
"""
from __future__ import annotations

VERSION = 'original-task-and-action-query-v1'


def contextual_search_query(question: str, action_query: str, completion_focus: str | None = None) -> dict:
    """Keep action first, then original scope; all inputs remain auditable.

    This does not filter sources/pages or infer entity names. A narrower action
    keeps its literal terms; the original task can still establish case scope.
    No source/answer text is accepted as an additional argument.
    """
    if not isinstance(question, str) or not isinstance(action_query, str):
        raise TypeError('query inputs must be strings')
    if completion_focus is not None and not isinstance(completion_focus, str):
        raise TypeError('completion focus must be a string or None')
    parts = []
    for value in (action_query, question, completion_focus):
        if value and value not in parts:
            parts.append(value)
    return {'version': VERSION, 'original_question': question,
            'action_query': action_query, 'completion_focus': completion_focus,
            'effective_query': '\n'.join(parts), 'scope': 'retrieval_only',
            'source_page_filter_changed': False}
