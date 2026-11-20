import csv
import threading
from io import StringIO
from concurrent.futures import ThreadPoolExecutor

import pytest


def make_run(client, source_text=None, book_text=None):
    payload = {}
    if source_text is not None:
        payload['data'] = source_text
    if book_text is not None:
        payload['book'] = book_text
    response = client.post('/api/runs', json=payload)
    assert response.status_code == 200, response.text
    return response.json()['manifest']['run_id']


def book_with(key, amount='366'):
    return f'档案件,外部记录值(天)\n{key},{amount}\n'


@pytest.fixture
def two_batches(client, sample_csv):
    # Two disposal batches reconcile the same external credential number.
    source_a = sample_csv({}).decode()
    reader = csv.reader(StringIO(source_a))
    header, row = next(reader), next(reader)
    shared = row[0]
    # Batch B carries a fuller disposal basis but arrives later.
    row_b = row.copy()
    row_b[header.index('原始备注')] = '晚到批次：补齐留置通知与处置依据'
    stream = StringIO(newline='')
    csv.writer(stream).writerows((header, row_b))
    run_a = make_run(client, source_text=source_a, book_text=book_with(shared))
    run_b = make_run(client, source_text=stream.getvalue(), book_text=book_with(shared))
    return run_a, run_b, shared


def claim(client, run_id, key, request_id=''):
    return client.post(f'/api/settle/{run_id}/claim',
                       json={'credential_key': key, 'request_id': request_id})


def test_credentials_registered_from_book_only(client):
    run_id = make_run(client)
    report = client.get(f'/api/settle/{run_id}').json()
    keys = {item['credential_key'] for item in report['credentials']}
    # 00103 has no external row (MISSING_BOOK), so it is not a claimable credential.
    assert '00101' in keys and '00103' not in keys
    assert all(item['status_label'] == '待认领' for item in report['credentials'])


def test_first_claim_wins_and_second_is_contended(client, two_batches):
    run_a, run_b, key = two_batches
    first = claim(client, run_a, key)
    assert first.status_code == 200
    assert first.json()['written_off'] is True and first.json()['reason_code'] == 'FIRST_CLAIM'

    second = claim(client, run_b, key)
    # The later batch never writes off the same fee twice.
    assert (second.status_code, second.json()['code']) == (409, 'CLAIM_CONTENDED')

    status = client.get(f'/api/settle/{run_a}').json()
    held = next(item for item in status['credentials'] if item['credential_key'] == key)
    assert held['status'] == 'HELD' and held['holder_run'] == run_a

    events = client.get(f'/api/settle/{run_b}/events').json()['events']
    contended = [item for item in events if item['action'] == 'CONTENDED']
    assert len(contended) == 1 and contended[0]['reason_code'] == 'CLAIM_CONTENDED'
    assert run_a[:9] in contended[0]['detail']


def test_retry_is_idempotent_and_never_double_writes(client, two_batches):
    run_a, _, key = two_batches
    assert claim(client, run_a, key, request_id='req-1').json()['written_off'] is True
    for request_id in ('req-1', 'req-2', 'req-1'):
        retry = claim(client, run_a, key, request_id=request_id)
        assert retry.status_code == 200
        body = retry.json()
        assert body['written_off'] is False and body['reason_code'] == 'CLAIM_RETRY'
    events = client.get(f'/api/settle/{run_a}/events').json()['events']
    assert [item['action'] for item in events].count('RETRY_NOOP') == 3
    assert [item['action'] for item in events].count('CLAIMED') == 1


def test_return_allows_reclaim_and_keeps_full_trail(client, two_batches):
    run_a, run_b, key = two_batches
    claim(client, run_a, key)
    denied = client.post(f'/api/settle/{run_b}/return',
                         json={'credential_key': key, 'reason': '对方复核'})
    assert (denied.status_code, denied.json()['code']) == (409, 'RETURN_DENIED')

    other_without_note = client.post(f'/api/settle/{run_a}/return',
                                     json={'credential_key': key, 'reason': '其他'})
    assert (other_without_note.status_code, other_without_note.json()['code']) == (422, 'RETURN_NOTE')

    returned = client.post(f'/api/settle/{run_a}/return',
                           json={'credential_key': key, 'reason': '依据不足', 'note': '留置链缺页'})
    assert returned.status_code == 200 and returned.json()['status'] == 'RETURNED'

    # The later, better-evidenced batch can now claim the freed credential.
    reclaim = claim(client, run_b, key)
    assert reclaim.status_code == 200
    assert reclaim.json()['written_off'] is True and reclaim.json()['action'] == 'RECLAIMED'

    status = client.get(f'/api/settle/{run_b}').json()
    held = next(item for item in status['credentials'] if item['credential_key'] == key)
    assert held['status'] == 'HELD' and held['holder_run'] == run_b
    # Each batch sees the full trail of the shared credential it reconciles.
    for run_id in (run_a, run_b):
        trail = {item['action'] for item in client.get(f'/api/settle/{run_id}/events').json()['events']}
        assert {'CLAIMED', 'RETURN_DENIED', 'RETURNED', 'RECLAIMED'} <= trail


def test_unknown_credential_and_bad_return_reason(client, two_batches):
    run_a, _, key = two_batches
    missing = claim(client, run_a, 'NOT-A-KEY')
    assert (missing.status_code, missing.json()['code']) == (404, 'CREDENTIAL_UNKNOWN')
    bad_reason = client.post(f'/api/settle/{run_a}/return',
                             json={'credential_key': key, 'reason': '随便退'})
    assert bad_reason.status_code == 422


def test_return_without_claim_is_rejected(client, two_batches):
    run_a, _, key = two_batches
    response = client.post(f'/api/settle/{run_a}/return',
                           json={'credential_key': key, 'reason': '归属待核'})
    assert (response.status_code, response.json()['code']) == (409, 'NOT_HELD')


def test_concurrent_claims_only_one_write_off(client, two_batches):
    run_a, run_b, key = two_batches
    barrier = threading.Barrier(2)

    def contend(run_id):
        barrier.wait()
        response = claim(client, run_id, key)
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(contend, (run_a, run_b)))
    statuses = sorted(code for code, _ in outcomes)
    assert statuses == [200, 409]
    held = [item for item in client.get(f'/api/settle/{run_a}').json()['credentials']
            if item['credential_key'] == key]
    assert len(held) == 1 and held[0]['status'] == 'HELD'


def test_replay_reregisters_same_credentials(client):
    run_id = make_run(client)
    before = client.get(f'/api/settle/{run_id}').json()['credentials']
    replayed = client.post(f'/api/runs/{run_id}/replay').json()
    assert replayed['manifest']['run_id'] == run_id
    after = client.get(f'/api/settle/{run_id}').json()['credentials']
    assert [(item['credential_key'], item['book_line']) for item in after] == \
           [(item['credential_key'], item['book_line']) for item in before]


def test_export_carries_settlement_trail(client, two_batches):
    run_a, run_b, key = two_batches
    claim(client, run_a, key)
    claim(client, run_b, key)
    proof = client.get(f'/api/export/{run_a}').json()
    assert proof['settlement']['run_id'] == run_a
    actions = {item['action'] for item in proof['settlement']['events']}
    assert {'CLAIMED', 'CONTENDED'} <= actions
    # Export stays reproducible.
    assert client.get(f'/api/export/{run_a}').content == client.get(f'/api/export/{run_a}').content
