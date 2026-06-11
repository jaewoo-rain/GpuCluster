"""
FastAPI app factory.

실행:
  cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

설정:
  scripts/run_backend.sh 가 venv + env 셋업까지 알아서 해 줌.
"""

from __future__ import annotations

import os
import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.sessions import router as sessions_router
from app.core.config import get_settings
from app.services.docker_manager import DockerManager
from app.services.session_manager import SessionManager
from app.services.session_store import SessionStore


# Stage 5-B: 단일 HTML UI. StaticFiles 마운트 안 하고 한 줄 라우트로 충분.
STATIC_INDEX = Path(__file__).parent / "static" / "index.html"


logger = logging.getLogger("fgpu.backend")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="fGPU Backend",
        version="0.1.0",
        description="Spawns user containers with libfgpu.so injected via LD_PRELOAD.",
    )

    # 시작 시점에 host hook 경로 존재 여부 확인 (실패해도 부팅은 함 — 경고만).
    if not os.path.isfile(settings.host_hook_path):
        logger.warning(
            "host hook .so not found at %s — sessions will fail to start until "
            "scripts/build_hook.sh produces it.",
            settings.host_hook_path,
        )

    docker_mgr = DockerManager(
        host_hook_path=settings.host_hook_path,
        container_hook_path=settings.container_hook_path,
        runtime_image=settings.runtime_image,
    )
    session_store = SessionStore(settings.db_path)
    # Stage 10: jupyter 세션의 워크스페이스 루트 디렉토리. 부재 시 생성.
    Path(settings.workspace_root).mkdir(parents=True, exist_ok=True)
    session_mgr = SessionManager(
        docker_manager=docker_mgr,
        runtime_image=settings.runtime_image,
        default_command=settings.default_command,
        store=session_store,
        workspace_root=settings.workspace_root,
    )
    app.state.docker_manager = docker_mgr
    app.state.session_store = session_store
    app.state.session_manager = session_mgr
    # Stage 9 minimal: bearer token 인증. 빈 문자열이면 인증 비활성.
    app.state.api_token = settings.api_token

    if settings.api_token:
        logger.info("FGPU_API_TOKEN set → /sessions routes require Bearer auth")
    else:
        logger.info("FGPU_API_TOKEN not set → /sessions routes are unauthenticated")

    @app.on_event("startup")
    async def _startup_reconcile_orphans() -> None:
        """앱 부팅 시 orphan 컨테이너 1회 스윕.

        과거 버전이 남긴 고아 컨테이너(DB record 없이 실행 중인 fgpu- 컨테이너)를
        회수해 GPU/메모리 누수를 초기화한다.
        실패해도 앱 부팅을 막지 않는다.
        """
        try:
            removed = await session_mgr.reconcile_orphans()
            if removed:
                logger.info(
                    "startup reconcile_orphans: %d orphan container(s) removed.", removed
                )
            else:
                logger.debug("startup reconcile_orphans: no orphans found.")
        except BaseException as e:
            logger.warning(
                "startup reconcile_orphans failed (ignored): %s", e, exc_info=True
            )

    @app.get("/healthz")
    def healthz() -> dict:
        return {
            "ok": True,
            "runtime_image": settings.runtime_image,
            "host_hook_path": settings.host_hook_path,
            "host_hook_exists": os.path.isfile(settings.host_hook_path),
            "db_path": settings.db_path,
            "db_exists": os.path.isfile(settings.db_path),
            "auth_enabled": bool(settings.api_token),
        }

    @app.get("/", include_in_schema=False)
    def ui_index() -> FileResponse:
        return FileResponse(STATIC_INDEX)

    app.include_router(sessions_router)
    return app


app = create_app()
