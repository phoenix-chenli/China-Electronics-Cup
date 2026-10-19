"""Transparent chart gateway receipts; no envelope rules or retry behavior."""
from __future__ import annotations

import copy

from .document_receipts import MAX_RECEIPTS, received_snapshot


class ChartResponses:
    def __init__(self, gateway, audit, check_cancelled, diagnostics):
        self.gateway = gateway
        self.audit = audit
        self.check_cancelled = check_cancelled
        self.diagnostics = diagnostics
        self.records = []
        self.sequence = 0
        self.omitted = 0

    def __getattr__(self, name):
        # Reader request accounting inspects config/attempts/usage/models on
        # the actual gateway. Keep those objects and their identity intact.
        return getattr(self.gateway, name)

    def snapshot(self):
        return copy.deepcopy({
            'chart_responses': self.records,
            'chart_response_omissions': self.omitted,
            'chart_response_progress': received_snapshot(self.diagnostics()),
            'publication_status': 'diagnostic_only',
        })

    def emit(self):
        if self.audit is not None:
            self.audit(self.snapshot())

    def __call__(self, messages, purpose):
        self.check_cancelled()
        if self.records:
            # Include checks computed since the previous return before a new
            # POST could fail. This does not grant those candidates support.
            self.emit()
            self.check_cancelled()
        raw = self.gateway(messages, purpose)  # Unknown POST outcome propagates.
        self.sequence += 1
        self.records.append({
            'sequence': self.sequence,
            'purpose': purpose,
            'received': True,
            'publication_status': 'not_adopted',
            'response': received_snapshot(raw),
        })
        if len(self.records) > MAX_RECEIPTS:
            self.records.pop(1)  # Retain first and newest; never hide omissions.
            self.omitted += 1
        self.emit()  # Save a late reply before checking cancellation.
        self.check_cancelled()
        return raw
