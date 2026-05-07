"""
Stage 11 admission control 순수 함수 테스트.

SessionManager 의 docker SDK 호출과 분리해서 admission 의 순수 정책 로직
(sum 계산, GPU overlap, force 우회, FP 오차 허용) 만 검증.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytest

from app.schemas.session import Session
from app.services import admission
from app.services.admission import AdmissionDenied, gpu_overlaps


def _sess(
    sid: str,
    ratio: float,
    *,
    status: str = "running",
    gpu_index: Optional[int] = None,
) -> Session:
    return Session(
        id=sid,
        container_id=f"cid-{sid}",
        container_name=f"fgpu-{sid}",
        ratio=ratio,
        image="fgpu-runtime:stage2",
        command=["/opt/fgpu/test_alloc"],
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        status=status,
        gpu_index=gpu_index,
    )


# ---- gpu_overlaps ---------------------------------------------------------

@pytest.mark.parametrize("a,b,expected", [
    (None, None, True),    # 둘 다 전체 → overlap
    (None, 0,    True),    # 한쪽 전체 → overlap
    (1,    None, True),    # 한쪽 전체 → overlap
    (0,    0,    True),    # 같은 device
    (0,    1,    False),   # 다른 device → 분리
    (2,    3,    False),
])
def test_gpu_overlaps(a, b, expected) -> None:
    assert gpu_overlaps(a, b) is expected


# ---- sum_used_ratio -------------------------------------------------------

def test_sum_only_active_status() -> None:
    sessions = [
        _sess("a", 0.4, status="running"),
        _sess("b", 0.3, status="exited"),       # 종료 → 제외
        _sess("c", 0.2, status="created"),      # 곧 시작 → 포함
        _sess("d", 0.5, status="removed"),      # 제거 → 제외
    ]
    used, n = admission.sum_used_ratio(sessions, gpu_index=None)
    assert used == pytest.approx(0.4 + 0.2)
    assert n == 2


def test_sum_respects_gpu_isolation() -> None:
    sessions = [
        _sess("a", 0.4, gpu_index=0),
        _sess("b", 0.3, gpu_index=1),  # 다른 GPU → 분리
        _sess("c", 0.2, gpu_index=0),
    ]
    used0, n0 = admission.sum_used_ratio(sessions, gpu_index=0)
    used1, _ = admission.sum_used_ratio(sessions, gpu_index=1)
    assert used0 == pytest.approx(0.6)  # a + c
    assert n0 == 2
    assert used1 == pytest.approx(0.3)  # only b


def test_sum_treats_none_as_overlap_with_all() -> None:
    """gpu_index=None 세션은 모든 device 와 overlap."""
    sessions = [
        _sess("a", 0.5, gpu_index=None),
        _sess("b", 0.3, gpu_index=0),
    ]
    # 새 요청이 device 0 을 노리면 a + b 모두 카운트
    used0, _ = admission.sum_used_ratio(sessions, gpu_index=0)
    assert used0 == pytest.approx(0.8)
    # 새 요청이 None 이면 (전체) — 마찬가지
    used_all, _ = admission.sum_used_ratio(sessions, gpu_index=None)
    assert used_all == pytest.approx(0.8)


# ---- check ----------------------------------------------------------------

def test_check_passes_within_capacity() -> None:
    sessions = [_sess("a", 0.4)]
    admission.check(sessions, requested_ratio=0.5, gpu_index=None)  # 0.4+0.5=0.9 OK


def test_check_passes_at_exact_capacity() -> None:
    sessions = [_sess("a", 0.4)]
    admission.check(sessions, requested_ratio=0.6, gpu_index=None)  # 1.0 정확히


def test_check_passes_with_fp_tolerance() -> None:
    """0.3+0.3+0.4 = 1.0000000000000002 허용."""
    sessions = [_sess("a", 0.3), _sess("b", 0.3)]
    admission.check(sessions, requested_ratio=0.4, gpu_index=None)


def test_check_denies_oversubscription() -> None:
    sessions = [_sess("a", 0.7)]
    with pytest.raises(AdmissionDenied) as exc:
        admission.check(sessions, requested_ratio=0.4, gpu_index=None)
    e = exc.value
    assert e.requested == 0.4
    assert e.currently_used == pytest.approx(0.7)
    assert e.gpu_index is None
    assert e.active_sessions == 1
    assert "0.300" in str(e) or "0.30" in str(e)  # available


def test_check_ignores_terminated_sessions() -> None:
    """exited 세션은 quota 풀어준 것으로 간주."""
    sessions = [
        _sess("old", 0.9, status="exited"),
        _sess("new", 0.4, status="running"),
    ]
    # exited 0.9 무시되고 active 만 0.4. 0.5 추가 가능.
    admission.check(sessions, requested_ratio=0.5, gpu_index=None)


def test_check_isolates_per_gpu() -> None:
    """다른 GPU 의 세션은 admission 합산에 영향 안 줌."""
    sessions = [_sess("a", 0.9, gpu_index=0)]
    # GPU 1 에 새 세션 0.9 요청 — GPU 0 의 0.9 와 무관하게 OK
    admission.check(sessions, requested_ratio=0.9, gpu_index=1)


# ---- usage_snapshot -------------------------------------------------------

def test_snapshot_groups_by_gpu_with_all_label() -> None:
    sessions = [
        _sess("a", 0.4, gpu_index=None),
        _sess("b", 0.3, gpu_index=0),
        _sess("c", 0.2, gpu_index=0),
        _sess("d", 0.1, gpu_index=1),
        _sess("dead", 0.9, gpu_index=0, status="exited"),
    ]
    snap = admission.usage_snapshot(sessions)
    by_gpu = snap["by_gpu"]
    assert by_gpu["all"]["ratio_used"] == pytest.approx(0.4)
    assert by_gpu["all"]["active_sessions"] == 1
    assert by_gpu["0"]["ratio_used"] == pytest.approx(0.5)  # b + c, dead 제외
    assert by_gpu["0"]["active_sessions"] == 2
    assert by_gpu["1"]["ratio_used"] == pytest.approx(0.1)
    assert by_gpu["1"]["ratio_available"] == pytest.approx(0.9)


def test_snapshot_empty_when_no_active() -> None:
    sessions = [_sess("a", 0.5, status="exited")]
    snap = admission.usage_snapshot(sessions)
    assert snap == {"by_gpu": {}}
