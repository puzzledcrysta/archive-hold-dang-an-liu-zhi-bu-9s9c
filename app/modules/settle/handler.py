from fastapi import APIRouter
from app.inputs import Claim, SettleReturn
from . import service

router = APIRouter()

@router.get('/api/settle/{run_id}')
def status(run_id: str):
    return service.credential_status(run_id)

@router.post('/api/settle/{run_id}/claim')
def claim(run_id: str, payload: Claim):
    return service.claim(run_id, payload.credential_key, payload.request_id, payload.note)

@router.post('/api/settle/{run_id}/return')
def send_back(run_id: str, payload: SettleReturn):
    return service.send_back(run_id, payload.credential_key, payload.reason, payload.note)

@router.get('/api/settle/{run_id}/events')
def events(run_id: str):
    return service.audit(run_id)
