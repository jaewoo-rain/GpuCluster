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

보상정리 (고아 컨테이너 누수 방지)
  - create_container 성공 후 store.insert 전 구간을 try/except BaseException 으로 감쌈.
  - 예외(CancelledError 포함) 발생 시 _rollback_spawn 이 컨테이너와 워크스페이스를
    best-effort 로 제거한 뒤 원래 예외를 그대로 재전파.
  - reconcile_orphans: docker daemon 의 fgpu- 컨테이너 중 DB record 가 없는 것을 회수.
    앱 startup 시 1회 호출해 과거 orphan 청소.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Set

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

# Step B: orphan 스윕 grace period (초). 이 시간 이내에 시작된 컨테이너는
# 아직 insert 진행 중일 수 있으므로 보수적으로 제외.
_ORPHAN_GRACE_SECONDS = 30


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
        # Step B: spawn 이 진행 중인(아직 insert 전) 컨테이너 id 집합.
        # reconcile_orphans 가 spawn-중 컨테이너를 orphan 으로 오인하지 않도록.
        self._inflight_cids: Set[str] = set()

    # ---- 보상정리 헬퍼 ----------------------------------------------- #
    async def _rollback_spawn(
        self,
        container_id: str,
        ws_dir: Optional[str],
    ) -> None:
        """컨테이너 spawn 후 insert 실패 시 best-effort 로 자원 회수.

        원래 예외를 덮지 않기 위해 내부 예외는 전부 swallow(경고 로그만).
        CancelledError 컨텍스트에서도 정리가 완주하도록 asyncio.shield 사용.
        """
        # 1) 컨테이너 제거 — 취소 컨텍스트에서도 완주하도록 shield.
        try:
            await asyncio.shield(
                asyncio.to_thread(self.docker.remove_container, container_id, True)
            )
        except docker.errors.NotFound:
            # 이미 사라진 컨테이너 — 문제 없음.
            pass
        except BaseException as e:
            # 보상정리 실패를 원래 예외 위로 올리지 않는다.
            print(
                f"[fgpu] rollback: remove_container 실패(무시): {e}",
                file=sys.stderr,
            )

        # 2) jupyter workspace 디렉토리 제거 (우리가 새로 만든 빈 dir 일 때만).
        if ws_dir:
            try:
                await asyncio.to_thread(
                    lambda: shutil.rmtree(ws_dir, ignore_errors=True)
                )
            except BaseException as e:
                print(
                    f"[fgpu] rollback: workspace rmtree 실패(무시): {e}",
                    file=sys.stderr,
                )

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
        compute_ratio: Optional[float] = None,
        force: bool = False,
        env: Optional[dict] = None,
    ) -> Session:
        # Stage 11: admission check 와 컨테이너 spawn 을 원자적으로.
        async with self._create_lock:
            return await self._create_locked(
                ratio=ratio, mode=mode, command=command,
                quota_bytes=quota_bytes, image=image,
                gpu_index=gpu_index, compute_ratio=compute_ratio,
                force=force, env=env,
            )

    async def _create_locked(
        self,
        ratio: float,
        mode: SessionMode,
        command: Optional[list[str]],
        quota_bytes: Optional[int],
        image: Optional[str],
        gpu_index: Optional[int],
        compute_ratio: Optional[float],
        force: bool,
        env: Optional[dict] = None,
    ) -> Session:
        # 1) admission check (force=False 일 때만)
        if not force:
            sessions = await self.list_all()  # docker daemon 와 reconcile
            admission.check(sessions, requested_ratio=ratio, gpu_index=gpu_index)

        sid = uuid.uuid4().hex[:12]
        name = f"fgpu-{sid}"

        # mode 별 image / command / 마운트 옵션 결정.
        # jupyter workspace 디렉토리: 우리가 새로 만들었는지 여부를 추적해
        # 보상정리 시 "기존 노트북 보호" vs "새로 만든 빈 dir 삭제" 를 결정.
        created_ws: bool = False
        if mode == "jupyter":
            img = image or _DEFAULT_JUPYTER_IMAGE
            jupyter_token = secrets.token_urlsafe(24)
            cmd = build_jupyter_command(jupyter_token)
            workspace_dir = os.path.join(self.workspace_root, sid)
            # exist_ok=False 시도 → FileExistsError 이면 이미 있던 dir.
            # 이미 있던 dir 은 롤백 시 절대 삭제하지 않음.
            try:
                await asyncio.to_thread(
                    lambda: Path(workspace_dir).mkdir(parents=True, exist_ok=False)
                )
                created_ws = True
            except FileExistsError:
                created_ws = False
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
        # create_container 자체가 실패하면 컨테이너가 없으므로 누수 없음 —
        # try 범위는 c 가 성공 반환된 직후부터 시작.
        c = await asyncio.to_thread(
            self.docker.create_container,
            name=name,
            ratio=ratio,
            command=cmd,
            quota_bytes=quota_bytes,
            image=img,
            gpu_index=gpu_index,
            compute_ratio=compute_ratio,
            jupyter_mode=jupyter_mode,
            workspace_host_dir=workspace_dir,
            ports=ports,
            env_extra=env,
        )

        # Step B: 이 컨테이너 id 를 "진행 중" 집합에 등록.
        # reconcile_orphans 가 insert 완료 전에 orphan 으로 오인하지 않도록.
        self._inflight_cids.add(c.id)
        try:
            # ── c 반환 직후 ~ insert 성공까지 누수 위험 구간 ──────────────
            # 이 구간에서 예외(CancelledError 포함)가 나면 이미 떠 있는
            # 컨테이너를 best-effort 로 제거하고 원래 예외를 재전파한다.
            try:
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
                    compute_ratio=compute_ratio,
                    host_port=host_port,
                    jupyter_token=jupyter_token,
                    jupyter_url=jupyter_url,
                    workspace_dir=workspace_dir,
                )
                await asyncio.to_thread(self.store.insert, rec)
                # insert 성공 → 정상 경로. 롤백 없이 반환.
                return rec

            except BaseException:
                # insert 실패 또는 구간 내 임의 예외(CancelledError 포함).
                # 컨테이너와 (새로 만든 경우) workspace 를 best-effort 정리.
                await self._rollback_spawn(
                    c.id,
                    workspace_dir if created_ws else None,
                )
                raise  # 원래 예외 그대로 재전파 — 취소 의미 보존.
        finally:
            # 정상/예외 어느 경로에서도 진행 중 집합에서 제거.
            self._inflight_cids.discard(c.id)

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

    # ---- orphan 스윕 -------------------------------------------------- #
    async def reconcile_orphans(
        self,
        grace_seconds: int = _ORPHAN_GRACE_SECONDS,
    ) -> int:
        """docker daemon 의 fgpu- 컨테이너 중 DB record 가 없는 orphan 을 회수.

        앱 startup 시 1회 호출해 과거 orphan(이전 버전 버그, SIGKILL 잔해 등)을 청소한다.

        반환값: 회수한 컨테이너 수.

        안전 가드:
          - _inflight_cids: 지금 이 프로세스에서 spawn 진행 중인 컨테이너는 제외.
          - grace_seconds: started_at 이 현재보다 이 시간 이내인 컨테이너는 보수적으로 제외.
            (started_at 파싱 실패 시에도 제외 — 건드리지 않음.)
          - 한 컨테이너 회수 실패가 전체를 막지 않도록 각 제거는 개별 try/except.

        계약 (docker_manager.list_fgpu_containers 에 대한 가정):
          list[{"id": str, "name": str, "status": str, "started_at": str | None}]
          started_at 은 ISO 8601 형식(docker inspect 의 State.StartedAt 값) 또는 None.
        """
        try:
            containers = await asyncio.to_thread(
                self.docker.list_fgpu_containers
            )
        except AttributeError:
            # docker_manager 가 아직 list_fgpu_containers 를 구현하지 않은 경우.
            # 조용히 skip — 부팅을 막지 않는다.
            print(
                "[fgpu] reconcile_orphans: list_fgpu_containers 미구현, 스킵.",
                file=sys.stderr,
            )
            return 0
        except BaseException as e:
            print(
                f"[fgpu] reconcile_orphans: daemon enumerate 실패(무시): {e}",
                file=sys.stderr,
            )
            return 0

        now = datetime.now(timezone.utc)
        grace = timedelta(seconds=grace_seconds)
        removed_count = 0

        for item in containers:
            cid: str = item["id"]
            cname: str = item.get("name", "")

            # 진행 중 컨테이너는 건드리지 않음 (insert 완료 전 race 방지).
            if cid in self._inflight_cids:
                continue

            # fgpu-<sid> 에서 sid 추출.
            # 컨테이너 이름은 /fgpu-... 형태일 수도 있으므로 lstrip('/').
            cname_stripped = cname.lstrip("/")
            if not cname_stripped.startswith("fgpu-"):
                continue
            sid = cname_stripped[len("fgpu-"):]

            # DB record 가 있으면 orphan 이 아님.
            try:
                rec = await asyncio.to_thread(self.store.get, sid)
            except BaseException as e:
                print(
                    f"[fgpu] reconcile_orphans: store.get({sid}) 실패(건너뜀): {e}",
                    file=sys.stderr,
                )
                continue
            if rec is not None:
                continue

            # grace period 체크 — 막 뜬 컨테이너는 제외.
            started_at_raw: Optional[str] = item.get("started_at")
            if started_at_raw:
                try:
                    # Python 3.11+ 는 fromisoformat 이 Z 를 처리하지만
                    # 3.9/3.10 은 못 하므로 replace("Z", "+00:00") 로 통일.
                    started_at = datetime.fromisoformat(
                        started_at_raw.replace("Z", "+00:00")
                    )
                    if started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=timezone.utc)
                    if (now - started_at) < grace:
                        # 너무 최근 — 보수적으로 건드리지 않음.
                        continue
                except (ValueError, TypeError):
                    # 파싱 실패 → 보수적으로 제외.
                    continue
            else:
                # started_at 을 알 수 없음 → 보수적으로 제외.
                continue

            # orphan 확정 — 제거 시도.
            try:
                await asyncio.to_thread(
                    self.docker.remove_container, cid, True
                )
                removed_count += 1
                print(
                    f"[fgpu] reconcile_orphans: orphan 회수 완료 cid={cid} name={cname}",
                    file=sys.stderr,
                )
            except docker.errors.NotFound:
                # 이미 사라진 컨테이너 — 개수에는 포함(누수 해소).
                removed_count += 1
            except BaseException as e:
                print(
                    f"[fgpu] reconcile_orphans: 제거 실패(건너뜀) cid={cid}: {e}",
                    file=sys.stderr,
                )

        if removed_count:
            print(
                f"[fgpu] reconcile_orphans: {removed_count}개 orphan 회수 완료.",
                file=sys.stderr,
            )
        return removed_count
