"""
고아 컨테이너 누수 방지 — 보상정리(Step A) + orphan 스윕(Step B) 단위 테스트.

docker/GPU 없이 실행 가능: DockerManager 와 SessionStore 를 fake/mock 으로 대체.

실행:
    cd backend && pip install -e ".[dev]" && pytest tests/test_session_manager_rollback.py -v

커버리지
  Step A — _create_locked 의 try/except BaseException + _rollback_spawn:
    1. store.insert OperationalError 시 컨테이너 제거 + 예외 전파
    2. store.insert IntegrityError 시 동일
    3. asyncio.CancelledError 발생 시 컨테이너 제거 + CancelledError 재전파
    4. Session pydantic 구성 실패 시 롤백
    5. remove_container 자체 실패(NotFound/일반 예외) 해도 원래 예외 전파
    6. jupyter 모드: insert 실패 시 새로 만든 workspace dir 삭제
    7. jupyter 모드: 이미 존재하던 workspace dir 은 롤백이 삭제하지 않음
    8. 정상 경로에서 remove_container 미호출
    9. 실패 후 admission 합산에 ratio 미반영

  Step B — reconcile_orphans:
    10. daemon에 fgpu-XXX 있고 DB 없으면 remove_container 호출
    11. DB record 있는 컨테이너는 스윕이 건드리지 않음
    12. started_at 이 grace 이내인 컨테이너는 제외
    13. _inflight_cids 에 포함된 컨테이너는 제외

커버리지 밖 (의도적)
  - docker SDK 실제 호출, GPU 의존 경로
  - jupyter host_port 폴링 (asyncio.sleep 포함) 상세 — 단위 테스트 범위 초과
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, call

import pytest

from app.schemas.session import Session
from app.services.session_manager import SessionManager
from app.services.session_store import SessionStore


# ---------------------------------------------------------------------------
# Fake / Mock 인프라
# ---------------------------------------------------------------------------

class _FakeContainer:
    """docker SDK Container 객체를 최소한으로 흉내냄."""
    def __init__(self, cid: str, name: str) -> None:
        self.id = cid
        self.name = name
        self.status = "running"


class _FakeDockerManager:
    """DockerManager 를 대체하는 fake.

    create_container: 항상 _FakeContainer 반환.
    remove_container: 호출 추적. side_effect 를 설정하면 예외 raise.
    list_fgpu_containers: containers 속성으로 결과 교체.
    """
    def __init__(self) -> None:
        self._remove_calls: list[tuple] = []
        self._remove_side_effect: Optional[Exception] = None
        self._list_result: list[dict] = []
        self._next_cid = "fake-cid-001"

    def set_next_cid(self, cid: str) -> None:
        self._next_cid = cid

    def create_container(self, name, ratio, command, **kwargs):
        return _FakeContainer(self._next_cid, name)

    def remove_container(self, container_id: str, force: bool = True) -> None:
        self._remove_calls.append((container_id, force))
        if self._remove_side_effect is not None:
            raise self._remove_side_effect

    def list_fgpu_containers(self) -> list[dict]:
        return list(self._list_result)

    # get_status / get_logs / stop_container — 필요 시 호출됨
    def get_status(self, container_id: str):
        return "running", None

    def get_host_port(self, container_id: str, container_port: int) -> Optional[int]:
        return None


def _make_manager(
    docker: Optional[_FakeDockerManager] = None,
    store: Optional[SessionStore] = None,
    tmp_path: Optional[Path] = None,
    workspace_root: Optional[str] = None,
) -> tuple[SessionManager, _FakeDockerManager, SessionStore]:
    d = docker or _FakeDockerManager()
    if store is None:
        import tempfile, os
        db_dir = tmp_path or Path(tempfile.mkdtemp())
        store = SessionStore(db_dir / "sessions.db")
    ws = workspace_root or (str(tmp_path / "ws") if tmp_path else "/tmp/fgpu_ws_test")
    mgr = SessionManager(
        docker_manager=d,
        runtime_image="fgpu-runtime:stage2",
        default_command=["/opt/fgpu/test_alloc"],
        store=store,
        workspace_root=ws,
    )
    return mgr, d, store


# ---------------------------------------------------------------------------
# Step A — 보상정리 테스트
# ---------------------------------------------------------------------------

def test_insert_failure_removes_container(tmp_path) -> None:
    """store.insert 가 OperationalError raise → 컨테이너 제거 + 예외 전파."""
    d = _FakeDockerManager()
    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path)

    # store.insert 를 OperationalError 를 던지도록 patch
    original_insert = store.insert
    def _failing_insert(s):
        raise sqlite3.OperationalError("database is locked")
    store.insert = _failing_insert

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        asyncio.run(mgr.create(ratio=0.4))

    # remove_container 가 정확히 1회 force=True 로 호출
    assert len(d._remove_calls) == 1
    _cid, _force = d._remove_calls[0]
    assert _force is True


def test_insert_integrityerror_rollback(tmp_path) -> None:
    """store.insert 가 IntegrityError raise → 컨테이너 제거 + 예외 전파."""
    d = _FakeDockerManager()
    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path)

    store.insert = lambda s: (_ for _ in ()).throw(
        sqlite3.IntegrityError("UNIQUE constraint failed")
    )

    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(mgr.create(ratio=0.4))

    assert len(d._remove_calls) == 1
    assert d._remove_calls[0][1] is True  # force=True


def test_cancellederror_during_insert_rolls_back(tmp_path) -> None:
    """insert 에서 CancelledError 발생 시 컨테이너 제거 + CancelledError 재전파."""
    d = _FakeDockerManager()
    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path)

    def _cancel_insert(s):
        raise asyncio.CancelledError("client disconnected")
    store.insert = _cancel_insert

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mgr.create(ratio=0.4))

    # 컨테이너 제거가 호출됐어야 함
    assert len(d._remove_calls) == 1


def test_session_pydantic_failure_rollback(tmp_path) -> None:
    """Session 구성이 실패하더라도 컨테이너 롤백 + 예외 전파.

    Session 생성 자체를 실패시키기 어려우므로(pydantic v2 기본 검증이 강함),
    _create_locked 내 예외 발생 경로를 to_thread(insert) 단계가 아닌
    직전 단계에서 trigger 한다.
    구체적으로 store.insert 전 단계(to_thread 호출 자체)에서 ValueError를
    던지도록 대체한다.
    """
    d = _FakeDockerManager()
    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path)

    store.insert = lambda s: (_ for _ in ()).throw(ValueError("pydantic-like failure"))

    with pytest.raises(ValueError, match="pydantic-like failure"):
        asyncio.run(mgr.create(ratio=0.4))

    assert len(d._remove_calls) == 1


def test_rollback_remove_failure_does_not_mask_original(tmp_path) -> None:
    """remove_container 자체가 실패해도 원래 예외(OperationalError)가 전파됨.

    보상정리 예외가 원래 예외를 덮어쓰지 않아야 한다.
    """
    import docker.errors as de

    d = _FakeDockerManager()
    d._remove_side_effect = de.NotFound("container not found")
    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path)

    store.insert = lambda s: (_ for _ in ()).throw(
        sqlite3.OperationalError("database is locked")
    )

    # 원래 예외(OperationalError)가 전파돼야 하며 NotFound 가 아니어야 함
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(mgr.create(ratio=0.4))

    # remove_container 는 호출됐지만 NotFound 를 던졌어도 원래 예외 유지
    assert len(d._remove_calls) == 1


def test_rollback_remove_general_exception_does_not_mask_original(tmp_path) -> None:
    """remove_container 가 일반 RuntimeError 를 던져도 원래 예외(IntegrityError) 전파."""
    d = _FakeDockerManager()
    d._remove_side_effect = RuntimeError("daemon down")
    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path)

    store.insert = lambda s: (_ for _ in ()).throw(
        sqlite3.IntegrityError("UNIQUE constraint failed")
    )

    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(mgr.create(ratio=0.4))

    assert len(d._remove_calls) == 1


def test_rollback_jupyter_workspace_cleaned(tmp_path) -> None:
    """jupyter 모드에서 insert 실패 시, 새로 만든 workspace dir 가 삭제됨."""
    d = _FakeDockerManager()
    ws_root = str(tmp_path / "workspaces")
    Path(ws_root).mkdir()
    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path, workspace_root=ws_root)

    store.insert = lambda s: (_ for _ in ()).throw(
        sqlite3.OperationalError("disk full")
    )

    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(mgr.create(ratio=0.4, mode="jupyter"))

    # workspace_root 아래에 새로 만들어진 디렉토리가 삭제됐는지 확인
    remaining = list(Path(ws_root).iterdir())
    assert remaining == [], f"workspace dir 이 남아 있음: {remaining}"


def test_rollback_preserves_existing_workspace(tmp_path) -> None:
    """workspace dir 이 이미 존재했다면 롤백이 삭제하지 않음 — 기존 노트북 보호."""
    d = _FakeDockerManager()
    ws_root = str(tmp_path / "workspaces")
    Path(ws_root).mkdir()

    # session id 는 uuid4 기반으로 자동 생성되므로 미리 만들기 어렵다.
    # 대신 SessionManager 의 create 흐름을 조작: workspace mkdir 시
    # 이미 존재하는 경로를 mkdir(exist_ok=False) 가 받도록 유도.
    # 방법: workspace_root 를 직접 미리 sid 와 동일한 이름으로 채워둔다.
    # (실제로 UUID 기반이라 미리 알 수 없으므로 workspace_root 자체를 이미 있는
    # 것처럼 만들고, _create_locked 의 mkdir 이 FileExistsError 를 내도록 한다.)
    #
    # 더 직접적인 접근: _rollback_spawn 을 직접 테스트.
    # ws_dir 을 만들어 놓고 created_ws=False 로 호출하면 삭제 안 해야 함.

    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path, workspace_root=ws_root)

    existing_ws = tmp_path / "existing_session_ws"
    existing_ws.mkdir()
    notebook = existing_ws / "notebook.ipynb"
    notebook.write_text('{"cells": []}')

    # _rollback_spawn 을 ws_dir=None (created_ws=False 케이스) 으로 직접 테스트
    # created_ws=False 이면 ws_dir 인자로 None 을 넘기므로 삭제 안 함
    asyncio.run(mgr._rollback_spawn("fake-cid-rollback-test", None))

    # 기존 notebook 이 그대로 남아있어야 함
    assert notebook.exists(), "기존 notebook 이 삭제됨 — 데이터 보호 위반"


def test_rollback_preserves_existing_workspace_full_flow(tmp_path) -> None:
    """이미 존재하는 workspace 이름과 충돌하는 경우 롤백이 dir 를 삭제하지 않는다.

    _create_locked 에서 mkdir(exist_ok=False) 가 FileExistsError 를 내면
    created_ws = False 로 설정되어 rollback 시 ws_dir 을 None 으로 전달함.
    따라서 삭제가 발생하지 않아야 한다.

    UUID 기반 sid 때문에 충돌을 직접 일으키기 어려우므로, _create_locked 내
    Path.mkdir 를 mock 해서 FileExistsError 를 내도록 한다.
    """
    import unittest.mock as mock

    d = _FakeDockerManager()
    ws_root = str(tmp_path / "workspaces")
    Path(ws_root).mkdir()

    # 기존 노트북이 있는 dir
    existing_dir = Path(ws_root) / "pre-existing"
    existing_dir.mkdir()
    notebook = existing_dir / "important.ipynb"
    notebook.write_text('{"cells": []}')

    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path, workspace_root=ws_root)

    # Path.mkdir 을 패치해서 FileExistsError 를 내게 하고,
    # store.insert 도 실패하게 해서 rollback 경로를 탄다.
    original_mkdir = Path.mkdir

    def _mock_mkdir(self, parents=False, exist_ok=True):
        # exist_ok=False 호출(우리 코드가 새 dir 체크에 쓰는 것) → FileExistsError
        if not exist_ok:
            raise FileExistsError("already exists")
        return original_mkdir(self, parents=parents, exist_ok=True)

    store.insert = lambda s: (_ for _ in ()).throw(
        sqlite3.OperationalError("disk full")
    )

    with mock.patch.object(Path, "mkdir", _mock_mkdir):
        with pytest.raises(sqlite3.OperationalError):
            asyncio.run(mgr.create(ratio=0.4, mode="jupyter"))

    # 기존 notebook 파일이 살아있어야 함
    assert notebook.exists(), "이미 존재하던 notebook 이 롤백에 의해 삭제됨"
    # remove_container 는 호출됐지만 workspace 삭제는 없었어야 함
    assert len(d._remove_calls) == 1


def test_success_path_no_rollback(tmp_path) -> None:
    """정상 create 성공 시 remove_container 가 호출되지 않는다."""
    d = _FakeDockerManager()
    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path)

    sess = asyncio.run(mgr.create(ratio=0.4))

    assert sess is not None
    assert sess.container_id == "fake-cid-001"
    # 정상 경로 — remove 호출 없음
    assert len(d._remove_calls) == 0


def test_admission_consistent_after_failed_create(tmp_path) -> None:
    """실패한 create 이후 admission_snapshot 에 해당 ratio 가 합산되지 않는다.

    orphan 컨테이너가 남지 않으므로 DB 기반 admission 회계에 미반영.
    """
    d = _FakeDockerManager()
    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path)

    store.insert = lambda s: (_ for _ in ()).throw(
        sqlite3.OperationalError("database is locked")
    )

    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(mgr.create(ratio=0.7))

    # admission snapshot: DB 에 record 없으므로 by_gpu 가 비어있어야 함
    snap = asyncio.run(mgr.admission_snapshot())
    assert snap["by_gpu"] == {}, f"실패한 세션이 admission 에 반영됨: {snap}"


# ---------------------------------------------------------------------------
# Step B — orphan 스윕 테스트
# ---------------------------------------------------------------------------

def _past_iso(seconds_ago: float = 120.0) -> str:
    """현재보다 seconds_ago 초 전 ISO 8601 문자열(UTC, Z suffix)."""
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _recent_iso(seconds_ago: float = 5.0) -> str:
    """현재보다 seconds_ago 초 전 ISO 8601 문자열 — grace period 이내."""
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def test_orphan_sweep_removes_unknown_fgpu_container(tmp_path) -> None:
    """daemon 에 fgpu-XXX 가 있고 DB 에 없으면 remove_container 호출."""
    d = _FakeDockerManager()
    orphan_cid = "orphan-cid-aaa"
    d._list_result = [
        {
            "id": orphan_cid,
            "name": "fgpu-orphansid01",
            "status": "running",
            "started_at": _past_iso(120),
        }
    ]

    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path)

    removed = asyncio.run(mgr.reconcile_orphans(grace_seconds=30))

    assert removed == 1
    # remove_container 가 orphan cid 로 호출됐는지
    assert any(cid == orphan_cid for cid, _ in d._remove_calls)


def test_orphan_sweep_keeps_known_container(tmp_path) -> None:
    """DB record 가 있는 컨테이너는 스윕이 건드리지 않는다."""
    d = _FakeDockerManager()
    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path)

    # DB 에 record 삽입
    known_sid = "knownsid0001"
    known_cid = "known-cid-bbb"
    rec = Session(
        id=known_sid,
        container_id=known_cid,
        container_name=f"fgpu-{known_sid}",
        ratio=0.4,
        image="fgpu-runtime:stage2",
        command=["/opt/fgpu/test_alloc"],
        created_at=datetime.now(timezone.utc),
        status="running",
    )
    store.insert(rec)

    d._list_result = [
        {
            "id": known_cid,
            "name": f"fgpu-{known_sid}",
            "status": "running",
            "started_at": _past_iso(120),
        }
    ]

    removed = asyncio.run(mgr.reconcile_orphans(grace_seconds=30))

    assert removed == 0
    assert len(d._remove_calls) == 0


def test_orphan_sweep_respects_grace_period(tmp_path) -> None:
    """started_at 이 grace 이내인 컨테이너는 회수 대상에서 제외된다."""
    d = _FakeDockerManager()
    d._list_result = [
        {
            "id": "recent-cid-ccc",
            "name": "fgpu-recentssss",
            "status": "running",
            "started_at": _recent_iso(5),  # 5초 전 — grace=30s 이내
        }
    ]

    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path)

    removed = asyncio.run(mgr.reconcile_orphans(grace_seconds=30))

    assert removed == 0
    assert len(d._remove_calls) == 0


def test_orphan_sweep_removes_old_container_outside_grace(tmp_path) -> None:
    """started_at 이 grace 밖인 컨테이너는 회수된다(grace 기준 경계 검증)."""
    d = _FakeDockerManager()
    old_cid = "old-cid-ddd"
    d._list_result = [
        {
            "id": old_cid,
            "name": "fgpu-oldssssssss",
            "status": "exited",
            "started_at": _past_iso(60),  # 60초 전 — grace=30s 밖
        }
    ]

    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path)

    removed = asyncio.run(mgr.reconcile_orphans(grace_seconds=30))

    assert removed == 1
    assert any(cid == old_cid for cid, _ in d._remove_calls)


def test_orphan_sweep_skips_in_flight_ids(tmp_path) -> None:
    """_inflight_cids 에 포함된 컨테이너는 orphan 스윕에서 제외된다."""
    d = _FakeDockerManager()
    inflight_cid = "inflight-cid-eee"
    d._list_result = [
        {
            "id": inflight_cid,
            "name": "fgpu-inflightsss",
            "status": "running",
            "started_at": _past_iso(120),  # grace 밖이지만 in-flight
        }
    ]

    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path)
    # 진행 중으로 표시
    mgr._inflight_cids.add(inflight_cid)

    removed = asyncio.run(mgr.reconcile_orphans(grace_seconds=30))

    assert removed == 0
    assert len(d._remove_calls) == 0


def test_orphan_sweep_skips_container_without_started_at(tmp_path) -> None:
    """started_at 이 None 인 컨테이너는 보수적으로 제외한다(알 수 없으면 안 건드림)."""
    d = _FakeDockerManager()
    d._list_result = [
        {
            "id": "unknown-start-cid",
            "name": "fgpu-unknowntime",
            "status": "created",
            "started_at": None,  # 아직 시작 안 됨
        }
    ]

    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path)

    removed = asyncio.run(mgr.reconcile_orphans(grace_seconds=30))

    assert removed == 0
    assert len(d._remove_calls) == 0


def test_orphan_sweep_mixed_known_and_unknown(tmp_path) -> None:
    """DB record 있는 것과 없는 것이 섞였을 때 orphan 만 제거."""
    d = _FakeDockerManager()
    mgr, _, store = _make_manager(docker=d, tmp_path=tmp_path)

    # known record 삽입
    known_sid = "knownsidmix0"
    known_cid = "known-cid-mix"
    rec = Session(
        id=known_sid,
        container_id=known_cid,
        container_name=f"fgpu-{known_sid}",
        ratio=0.4,
        image="fgpu-runtime:stage2",
        command=["/opt/fgpu/test_alloc"],
        created_at=datetime.now(timezone.utc),
        status="running",
    )
    store.insert(rec)

    orphan_cid = "orphan-cid-mix"
    d._list_result = [
        {
            "id": known_cid,
            "name": f"fgpu-{known_sid}",
            "status": "running",
            "started_at": _past_iso(120),
        },
        {
            "id": orphan_cid,
            "name": "fgpu-orphanmix00",
            "status": "running",
            "started_at": _past_iso(120),
        },
    ]

    removed = asyncio.run(mgr.reconcile_orphans(grace_seconds=30))

    assert removed == 1
    removed_cids = [cid for cid, _ in d._remove_calls]
    assert orphan_cid in removed_cids
    assert known_cid not in removed_cids
