"""
/sessions REST 엔드포인트.

  POST   /sessions               세션 생성 (컨테이너 spawn)
  GET    /sessions               전체 세션 목록
  GET    /sessions/admission     Stage 11: 현재 GPU 별 ratio 사용 현황
  GET    /sessions/{id}          세션 상세 (status, exit_code 자동 reconcile)
  GET    /sessions/{id}/logs     컨테이너 stdout+stderr 일부
  POST   /sessions/{id}/stop     컨테이너 정지 (record 는 보존)
  DELETE /sessions/{id}          컨테이너 강제 삭제 + record 제거

Stage 9 minimal: 모든 라우트가 _require_auth dependency 거침.
FGPU_API_TOKEN env 가 비어있으면 통과, 설정돼 있으면 'Authorization: Bearer'
헤더 토큰 일치 요구. /healthz 와 / (UI) 는 별도 라우터라 영향 없음.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.schemas.session import Session, SessionCreate, SessionLogs
from app.services.admission import AdmissionDenied
from app.services.session_manager import SessionManager


def _require_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    expected = getattr(request.app.state, "api_token", "") or ""
    if not expected:
        return  # 인증 비활성 — 토큰 미설정 시 통과
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    given = authorization[len("Bearer "):]
    # 상수 시간 비교 — timing attack 방어.
    if not hmac.compare_digest(given, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(
    prefix="/sessions",
    tags=["sessions"],
    dependencies=[Depends(_require_auth)],
)


def _get_manager(request: Request) -> SessionManager:
    return request.app.state.session_manager


@router.post("", response_model=Session, status_code=201)
async def create_session(
    body: SessionCreate,
    mgr: SessionManager = Depends(_get_manager),
) -> Session:
    try:
        return await mgr.create(
            ratio=body.ratio,
            mode=body.mode,
            command=body.command,
            quota_bytes=body.quota_bytes,
            image=body.image,
            gpu_index=body.gpu_index,
            compute_ratio=body.compute_ratio,
            force=body.force,
        )
    except AdmissionDenied as e:
        # Stage 11: sum(ratios) > 1.0 거부. 클라이언트가 force=true 로 재시도 가능.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "admission_denied",
                "message": str(e),
                "requested_ratio": e.requested,
                "currently_used": e.currently_used,
                "available": max(0.0, 1.0 - e.currently_used),
                "gpu_index": e.gpu_index,
                "active_sessions": e.active_sessions,
            },
        )
    except Exception as e:
        # docker daemon 미동작, 이미지 없음, 이름 충돌 등 모두 여기로.
        raise HTTPException(status_code=500, detail=f"create failed: {e}")


@router.get("/admission")
async def get_admission(mgr: SessionManager = Depends(_get_manager)) -> dict:
    """Stage 11: 현재 GPU 별 ratio 사용 현황. UI / 운영자가 capacity 확인용."""
    return await mgr.admission_snapshot()


@router.get("", response_model=list[Session])
async def list_sessions(mgr: SessionManager = Depends(_get_manager)) -> list[Session]:
    return await mgr.list_all()


@router.get("/{sid}", response_model=Session)
async def get_session(sid: str, mgr: SessionManager = Depends(_get_manager)) -> Session:
    rec = await mgr.get(sid)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    return rec


@router.get("/{sid}/logs", response_model=SessionLogs)
async def get_session_logs(
    sid: str,
    tail: int = 200,
    mgr: SessionManager = Depends(_get_manager),
) -> SessionLogs:
    logs = await mgr.get_logs(sid, tail=tail)
    if logs is None:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionLogs(id=sid, logs=logs)


@router.post("/{sid}/stop", response_model=Session)
async def stop_session(sid: str, mgr: SessionManager = Depends(_get_manager)) -> Session:
    rec = await mgr.stop(sid)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    return rec


@router.delete("/{sid}")
async def delete_session(
    sid: str,
    purge_workspace: bool = False,
    mgr: SessionManager = Depends(_get_manager),
) -> dict:
    """컨테이너 삭제 + record 삭제.

    purge_workspace=true 면 jupyter 모드의 호스트 워크스페이스 디렉토리도
    삭제 (노트북 파일 영구 손실). 기본은 보존.
    """
    ok = await mgr.delete(sid, purge_workspace=purge_workspace)
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return {"deleted": sid, "purged_workspace": purge_workspace}
