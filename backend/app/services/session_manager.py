"""
세션 라이프사이클 매니저 (Stage 8).

저장소: SQLite via SessionStore (이전엔 in-memory dict). 백엔드가 재시작
되어도 세션 record 는 살아남고, lazy reconcile 로 docker daemon 의
실제 status 를 맞춰줌.

비동기성: docker SDK 와 sqlite3 는 모두 sync 라이브러리. 이벤트 루프를
막지 않기 위해 모든 blocking 호출을 asyncio.to_thread() 로 감쌈.
이전엔 한 POST /sessions 가 docker.run() 동안 다른 요청을 막았는데,
이제 진짜 동시 처리 가능. (5-A 격리 실험의 두 컨테이너 spawn 도
실제로 병렬화됨.)

Stage 10 (interactive)
  - mode="jupyter" 이면 jupyter token 생성 + 호스트 워크스페이스 디렉토리
    생성 + jupyter lab 명령 강제 + 8888/tcp publish.
  - 컨테이너 시작 직후 attrs 를 다시 읽어서 호스트가 할당한 ephemeral
    port 를 확인하고 jupyter_url 조립.
  - delete 시 호스트 워크스페이스 디렉토리는 *보존* — 노트북은 사용자
    데이터이므로 자동 삭제하지 않음. 운영자가 필요 시 수동 정리.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import docker.errors

from app.schemas.session import Session, SessionMode
from app.services import admission
from app.services.admission import AdmissionDenied
from app.services.docker_manager import (
    DockerManager,
    build_jupyter_command,
    _JUPYTER_CONTAINER_PORT,
)
from app.services.session_store import SessionStore


# Jupyter 모드 기본 이미지. PyTorch + jupyterlab 이 깔린 stage4 이미지여야 함.
_DEFAULT_JUPYTER_IMAGE = "fgpu-runtime-pytorch:stage4"

# 사용자가 UI 에서 접속할 때 보여줄 호스트명. 로컬 기본은 localhost.
# 외부 접속이 필요하면 FGPU_PUBLIC_HOST env 로 override.
_PUBLIC_HOST = os.environ.get("FGPU_PUBLIC_HOST", "localhost")


class SessionManager:
    def __init__(
        self,
        docker_manager: DockerManager,
        runtime_image: str,
        default_command: list[str],
        store: SessionStore,
        workspace_root: str,
    ) -> None:
        self.docker = docker_manager
        self.runtime_image = runtime_image
        self.default_command = default_command
        self.store = store
        self.workspace_root = workspace_root
        # Stage 11: admission check 와 컨테이너 spawn 사이 race 방지.
        # 체크-후-삽입이 원자적이지 않으면 두 동시 POST 가 같은 capacity 로
        # 둘 다 통과해버릴 수 있음. asyncio.Lock 으로 create 전체 직렬화.
        # 여전히 docker.run 자체는 to_thread 라 이벤트 루프는 안 막힘.
        self._create_lock = asyncio.Lock()

    # ---- admission ---------------------------------------------------- #
    async def admission_snapshot(self) -> dict:
        """현재 GPU 별 ratio 사용량 + 활성 세션 수. /sessions/admission 응답."""
        sessions = await self.list_all()
        return admission.usage_snapshot(sessions)

    # ---- create ------------------------------------------------------- #
    async def create(
        self,
        ratio: float,
        mode: SessionMode = "batch",
        command: Optional[list[str]] = None,
        quota_bytes: Optional[int] = None,
        image: Optional[str] = None,
        gpu_index: Optional[int] = None,
        force: bool = False,
    ) -> Session:
        # Stage 11: admission check 와 컨테이너 spawn 을 원자적으로.
        async with self._create_lock:
            return await self._create_locked(
                ratio=ratio, mode=mode, command=command,
                quota_bytes=quota_bytes, image=image,
                gpu_index=gpu_index, force=force,
            )

    async def _create_locked(
        self,
        ratio: float,
        mode: SessionMode,
        command: Optional[list[str]],
        quota_bytes: Optional[int],
        image: Optional[str],
        gpu_index: Optional[int],
        force: bool,
    ) -> Session:
        # 1) admission check (force=False 일 때만)
        if not force:
            sessions = await self.list_all()  # docker daemon 와 reconcile
            admission.check(sessions, requested_ratio=ratio, gpu_index=gpu_index)

        sid = uuid.uuid4().hex[:12]
        name = f"fgpu-{sid}"

        # mode 별 image / command / 마운트 옵션 결정.
        if mode == "jupyter":
            img = image or _DEFAULT_JUPYTER_IMAGE
            jupyter_token = secrets.token_urlsafe(24)
            cmd = build_jupyter_command(jupyter_token)
            workspace_dir = os.path.join(self.workspace_root, sid)
            await asyncio.to_thread(
                lambda: Path(workspace_dir).mkdir(parents=True, exist_ok=True)
            )
            # 컨테이너 안에서 jupyter 가 /workspace 에 쓸 수 있어야 하는데,
            # 호스트에서 만든 디렉토리는 root:root 인 경우가 흔함. 컨테이너도
            # root 로 도므로 (--allow-root) 충돌 없음. 다른 UID 로 실행시키려면
            # chown 추가 필요 — 현재 단계에선 불필요.
            ports = {f"{_JUPYTER_CONTAINER_PORT}/tcp": None}  # ephemeral
            jupyter_mode = True
        else:
            img = image or self.runtime_image
            cmd = command or list(self.default_command)
            jupyter_token = None
            workspace_dir = None
            ports = None
            jupyter_mode = False

        # docker SDK 는 sync 라 to_thread.
        c = await asyncio.to_thread(
            self.docker.create_container,
            name=name,
            ratio=ratio,
            command=cmd,
            quota_bytes=quota_bytes,
            image=img,
            gpu_index=gpu_index,
            jupyter_mode=jupyter_mode,
            workspace_host_dir=workspace_dir,
            ports=ports,
        )

        # jupyter 모드면 docker 가 자동 할당한 host port 를 읽어옴.
        host_port: Optional[int] = None
        jupyter_url: Optional[str] = None
        if jupyter_mode:
            # 컨테이너가 막 시작돼서 ports 가 아직 attrs 에 안 올라왔을 수 있음.
            # 짧은 백오프로 재시도.
            for delay in (0.0, 0.1, 0.2, 0.4, 0.8):
                if delay:
                    await asyncio.sleep(delay)
                host_port = await asyncio.to_thread(
                    self.docker.get_host_port, c.id, _JUPYTER_CONTAINER_PORT
                )
                if host_port:
                    break
            if host_port:
                jupyter_url = (
                    f"http://{_PUBLIC_HOST}:{host_port}/lab?token={jupyter_token}"
                )

        rec = Session(
            id=sid,
            container_id=c.id,
            container_name=name,
            ratio=ratio,
            mode=mode,
            quota_bytes=quota_bytes,
            image=img,
            command=cmd,
            created_at=datetime.now(timezone.utc),
            status=c.status or "created",
            gpu_index=gpu_index,
            host_port=host_port,
            jupyter_token=jupyter_token,
            jupyter_url=jupyter_url,
            workspace_dir=workspace_dir,
        )
        await asyncio.to_thread(self.store.insert, rec)
        return rec

    # ---- read -------------------------------------------------------- #
    async def get(self, sid: str) -> Optional[Session]:
        rec = await asyncio.to_thread(self.store.get, sid)
        if rec is None:
            return None
        # docker daemon 에 status reconcile.
        try:
            status, exit_code = await asyncio.to_thread(
                self.docker.get_status, rec.container_id
            )
        except docker.errors.NotFound:
            # 컨테이너가 daemon 에서 사라짐 — record 는 보존, 상태만 갱신.
            if rec.status != "removed":
                await asyncio.to_thread(
                    self.store.update_status, sid, "removed", rec.exit_code
                )
            rec.status = "removed"
            return rec

        if rec.status != status or rec.exit_code != exit_code:
            await asyncio.to_thread(
                self.store.update_status, sid, status, exit_code
            )
        rec.status = status
        rec.exit_code = exit_code
        return rec

    async def list_all(self) -> list[Session]:
        recs = await asyncio.to_thread(self.store.list_all)
        # 각 레코드 reconcile — 동시에 진행해 list 응답 latency 감소.
        results = await asyncio.gather(
            *(self.get(r.id) for r in recs), return_exceptions=False
        )
        return [r for r in results if r is not None]

    async def get_logs(self, sid: str, tail: int = 200) -> Optional[str]:
        rec = await self.get(sid)
        if rec is None:
            return None
        try:
            return await asyncio.to_thread(
                self.docker.get_logs, rec.container_id, tail
            )
        except docker.errors.NotFound:
            return ""

    # ---- mutate ------------------------------------------------------ #
    async def stop(self, sid: str) -> Optional[Session]:
        rec = await self.get(sid)
        if rec is None:
            return None
        try:
            await asyncio.to_thread(self.docker.stop_container, rec.container_id)
        except docker.errors.NotFound:
            pass
        return await self.get(sid)

    async def delete(self, sid: str, purge_workspace: bool = False) -> bool:
        rec = await asyncio.to_thread(self.store.get, sid)
        if rec is None:
            return False
        try:
            await asyncio.to_thread(
                self.docker.remove_container, rec.container_id, True
            )
        except docker.errors.NotFound:
            pass
        await asyncio.to_thread(self.store.delete, sid)
        # 노트북 파일 보존 vs 삭제 — 기본은 보존. 명시적 purge 요청 시만 삭제.
        if purge_workspace and rec.workspace_dir:
            ws = rec.workspace_dir
            await asyncio.to_thread(
                lambda: shutil.rmtree(ws, ignore_errors=True)
            )
        return True
