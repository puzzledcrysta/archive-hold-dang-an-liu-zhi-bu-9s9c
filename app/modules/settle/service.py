import sqlite3
from datetime import datetime, timezone

from app.errors import DataError
from app.modules.runs.repository import lookup
from app.store import connection
from . import repository

ACTION_LABELS = {
    'CLAIMED': '首次核销',
    'RETRY_NOOP': '重试确认（未重复核销）',
    'CONTENDED': '争用未核销',
    'RETURN_DENIED': '无权退回',
    'RETURNED': '核销退回',
    'RECLAIMED': '退回后重新认领',
}
RETURN_REASONS = ('依据不足', '归属待核', '对方复核', '其他')
STATUS_LABELS = {'HELD': '已核销', 'RETURNED': '已退回'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def register(run_id, result, created):
    """Index every external credential a persisted batch reconciles against (idempotent)."""
    return repository.register_credentials(run_id, result['book']['rows'], created)


def credential_status(run_id):
    with connection() as db:
        lookup(db, run_id)
    credentials = repository.list_credentials(run_id)
    for item in credentials:
        item['status_label'] = STATUS_LABELS.get(item['status'], '待认领')
    return {'run_id': run_id, 'credentials': credentials,
            'events': repository.list_events(run_id=run_id)}


def claim(run_id, credential_key, request_id, note):
    """First valid claimant writes off the credential; retries never write off twice."""
    note = (note or '').strip()
    request_id = (request_id or '').strip() or None
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        repository.lookup_credential(db, run_id, credential_key)
        held = repository.active_claim(db, credential_key)
        stamp = _now()
        if held is not None:
            if held['run_id'] == run_id:
                # Idempotent retry of the same batch: keep the single write-off, log the retry.
                event_id = repository.insert_event(
                    db, credential_key, run_id, 'RETRY_NOOP', 'CLAIM_RETRY',
                    f'重试确认，凭据仍由批次 {run_id[:9]} 核销，未重复写入', stamp)
                db.commit()
                return {'credential_key': credential_key, 'status': 'HELD',
                        'holder_run': held['run_id'], 'written_off': False,
                        'reason_code': 'CLAIM_RETRY', 'event_id': event_id}
            # Another batch already wrote off: contention is recorded but not written off.
            event_id = repository.insert_event(
                db, credential_key, run_id, 'CONTENDED', 'CLAIM_CONTENDED',
                f'凭据已由先到批次 {held["run_id"][:9]} 核销，本批等待双方复核', stamp)
            db.commit()
            raise Contention(credential_key, held['run_id'], event_id)
        # No active claim: either a first write-off or a reclaim after a return.
        previous = db.execute(
            'SELECT id FROM claims WHERE credential_key=? ORDER BY id DESC LIMIT 1',
            (credential_key,)).fetchone()
        action, reason = ('RECLAIMED', 'RECLAIM_AFTER_RETURN') if previous else ('CLAIMED', 'FIRST_CLAIM')
        try:
            claim_id = repository.insert_claim(db, credential_key, run_id, request_id, note, stamp)
        except sqlite3.IntegrityError as error:
            # Lost a same-instant race; the unique partial index still allows one write-off.
            holder = repository.active_claim(db, credential_key)
            event_id = repository.insert_event(
                db, credential_key, run_id, 'CONTENDED', 'CLAIM_CONTENDED',
                f'并发争用，凭据由批次 {holder["run_id"][:9]} 核销', _now())
            db.commit()
            raise Contention(credential_key, holder['run_id'], event_id) from error
        detail = '退回后重新认领并核销' if previous else '先到批次核销，当天可结账'
        event_id = repository.insert_event(db, credential_key, run_id, action, reason, detail, stamp)
        db.commit()
        return {'credential_key': credential_key, 'status': 'HELD', 'holder_run': run_id,
                'written_off': True, 'claim_id': claim_id, 'reason_code': reason,
                'action': action, 'event_id': event_id}


def send_back(run_id, credential_key, reason, note):
    note = (note or '').strip()
    if reason not in RETURN_REASONS:
        raise DataError('RETURN_REASON', '退回依据无效')
    if reason == '其他' and not note:
        raise DataError('RETURN_NOTE', '选择其他退回依据时须填写说明')
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        repository.lookup_credential(db, run_id, credential_key)
        held = repository.active_claim(db, credential_key)
        stamp = _now()
        if held is None:
            repository.insert_event(
                db, credential_key, run_id, 'CONTENDED', 'NOT_HELD',
                '凭据不在已核销状态，退回未生效', stamp)
            db.commit()
            raise DataError('NOT_HELD', '凭据尚未核销，无法退回', 409)
        if held['run_id'] != run_id:
            repository.insert_event(
                db, credential_key, run_id, 'RETURN_DENIED', 'RETURN_DENIED',
                f'凭据由批次 {held["run_id"][:9]} 持有，本批无权退回', stamp)
            db.commit()
            raise DataError('RETURN_DENIED', '只有核销凭据的批次可以退回', 409)
        repository.release_claim(db, held['id'], reason, note, stamp)
        repository.insert_event(
            db, credential_key, run_id, 'RETURNED', 'RETURNED',
            reason + (f'：{note}' if note else ''), stamp)
        db.commit()
        return {'credential_key': credential_key, 'status': 'RETURNED',
                'holder_run': None, 'reason_code': 'RETURNED',
                'return_reason': reason}


def audit(run_id):
    events = repository.list_events(run_id=run_id)
    for item in events:
        item['action_label'] = ACTION_LABELS[item['action']]
    return {'events': events}


class Contention(DataError):
    def __init__(self, credential_key, holder_run, event_id):
        super().__init__('CLAIM_CONTENDED',
                         f'凭据 {credential_key} 已由先到批次 {holder_run[:9]} 核销，争用已登记，等待双方复核',
                         409)
        self.holder_run = holder_run
        self.event_id = event_id
