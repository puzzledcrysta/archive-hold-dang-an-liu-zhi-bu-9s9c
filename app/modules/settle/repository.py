from app.errors import DataError
from app.store import connection


def register_credentials(run_id, book_rows, created):
    """Register the external credentials a batch reconciles against (idempotent)."""
    rows = [(run_id, row['key'], row['book_line'], row['reference'], created)
            for row in book_rows if row['book_line'] is not None]
    if not rows:
        return 0
    with connection() as db:
        db.executemany(
            'INSERT OR IGNORE INTO credentials(run_id,credential_key,book_line,amount,registered)'
            ' VALUES (?,?,?,?,?)', rows)
        return db.total_changes


def lookup_credential(db, run_id, credential_key):
    row = db.execute(
        'SELECT * FROM credentials WHERE run_id=? AND credential_key=?',
        (run_id, credential_key)).fetchone()
    if row is None:
        raise DataError('CREDENTIAL_UNKNOWN', '该批次没有这张外部凭据', 404)
    return row


def active_claim(db, credential_key):
    return db.execute(
        "SELECT * FROM claims WHERE credential_key=? AND status='HELD' ORDER BY id DESC LIMIT 1",
        (credential_key,)).fetchone()


def insert_claim(db, credential_key, run_id, request_id, note, claimed_at):
    cursor = db.execute(
        "INSERT INTO claims(credential_key,run_id,status,request_id,note,claimed_at)"
        " VALUES (?,?, 'HELD', ?, ?, ?)",
        (credential_key, run_id, request_id, note, claimed_at))
    return cursor.lastrowid


def insert_event(db, credential_key, run_id, action, reason_code, detail, created):
    cursor = db.execute(
        'INSERT INTO claim_events(credential_key,run_id,action,reason_code,detail,created)'
        ' VALUES (?,?,?,?,?,?)',
        (credential_key, run_id, action, reason_code, detail, created))
    return cursor.lastrowid


def release_claim(db, claim_id, reason, note, returned_at):
    db.execute(
        "UPDATE claims SET status='RETURNED', returned_at=?, return_reason=?, note=? WHERE id=?",
        (returned_at, reason, note, claim_id))


def list_credentials(run_id):
    with connection() as db:
        rows = db.execute(
            'WITH latest AS ('
            ' SELECT credential_key, run_id AS holder_run, status, claimed_at, returned_at,'
            '        return_reason, note, request_id,'
            '        ROW_NUMBER() OVER (PARTITION BY credential_key ORDER BY id DESC) rn'
            ' FROM claims)'
            ' SELECT c.credential_key, c.book_line, c.amount AS reference,'
            '        l.status, l.holder_run, l.claimed_at, l.returned_at,'
            '        l.return_reason, l.note, l.request_id'
            ' FROM credentials c LEFT JOIN latest l'
            ' ON c.credential_key=l.credential_key AND l.rn=1'
            ' WHERE c.run_id=? ORDER BY c.book_line', (run_id,)).fetchall()
        return [dict(row) for row in rows]


def list_events(run_id=None, credential_key=None):
    # A batch sees the full trail of every external credential it reconciles,
    # including contention and return events written by other batches.
    sql = ('SELECT e.id, e.credential_key, e.run_id, e.action, e.reason_code,'
           ' e.detail, e.created FROM claim_events e')
    params = []
    clauses = []
    if run_id is not None:
        sql += ' JOIN credentials c ON c.credential_key=e.credential_key AND c.run_id=?'
        params.append(run_id)
    if credential_key is not None:
        clauses.append('e.credential_key=?')
        params.append(credential_key)
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += ' ORDER BY e.id'
    with connection() as db:
        return [dict(row) for row in db.execute(sql, params).fetchall()]
