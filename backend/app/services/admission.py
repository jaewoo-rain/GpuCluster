"""
Stage 11: Admission control.

기본 정책: 같은 GPU(또는 None) 위에서 종료되지 않은 세션의 ratio 합계가
1.0 을 넘으면 새 세션 생성 거부. force=True 로 우회 가능 (oversubscription
실험용).

설계 메모
  - SessionStore 의 raw record 만 보지 않고, SessionManager.list_all() 로
    docker 와 reconcile 된 status 를 사용. 컨테이너가 daemon 에서 사라졌거나
    이미 exited 된 세션이 quota 를 잡아두고 있는 stale 상태 방지.
  - GPU 지정 규칙
      None vs N      → overlap (None 은 "전체 GPU" 의미)
      None vs None   → overlap
      N vs N         → overlap (같은 device)
      N vs M (N≠M)   → 분리 (다른 device, 격리됨)
  - 단일 GPU 호스트 (RTX 4070) 에서는 모든 세션이 항상 overlap → 사실상
    "전체 sum(ratios) ≤ 1.0".

Backend.AI 와의 비교
  - Backend.AI 는 cluster scheduler 레이어에서 admission 검사. 우리는 단일
    호스트라 SessionManager 에서 같은 검사.
  - 우리 AdmissionDenied 는 hook 의 quota DENY 와 *다른 layer*. hook 은
    container-internal cudaMalloc 호출 시점, admission 은 컨테이너 spawn
    시점. 캡스톤 paper 에서 두 layer 의 역할 분담을 보여주는 figure 가능.
"""

from __future__ import annotations

from typing import Optional

# FP 부동소수점 합산 오차 허용 (예: 0.3 + 0.3 + 0.4 = 1.0000000000000002)
_TOL = 1e-9


class AdmissionDenied(Exception):
    """sum(ratios) > 1.0 일 때 SessionManager.create 가 raise."""

    def __init__(
        self,
        requested: float,
        currently_used: float,
        gpu_index: Optional[int],
        active_sessions: int,
    ) -> None:
        self.requested = requested
        self.currently_used = currently_used
        self.gpu_index = gpu_index
        self.active_sessions = active_sessions
        gpu_str = f"GPU {gpu_index}" if gpu_index is not None else "all GPUs"
        available = max(0.0, 1.0 - currently_used)
        super().__init__(
            f"admission denied on {gpu_str}: "
            f"requested ratio {requested:.3f}, "
            f"currently_used {currently_used:.3f} "
            f"({active_sessions} active session(s)), "
            f"available {available:.3f}. "
            "Pass force=true to bypass (oversubscription)."
        )


def gpu_overlaps(a: Optional[int], b: Optional[int]) -> bool:
    """두 GPU 지정이 같은 device 를 공유하는지.

    None = 전체 노출 (모든 device). 따라서 None 은 어떤 N 과도 overlap.
    """
    if a is None or b is None:
        return True
    return a == b


def sum_used_ratio(
    sessions, gpu_index: Optional[int]
) -> tuple[float, int]:
    """gpu_index 와 overlap 되는 활성 세션의 ratio 합계 + 카운트.

    "활성" = status in {"created", "running"}.  exited / removed 는 제외 —
    이미 quota 를 풀어준 상태로 간주.
    """
    used = 0.0
    n = 0
    for r in sessions:
        if r.status not in ("created", "running"):
            continue
        if not gpu_overlaps(r.gpu_index, gpu_index):
            continue
        used += r.ratio
        n += 1
    return used, n


def check(
    sessions,
    requested_ratio: float,
    gpu_index: Optional[int],
) -> None:
    """admission 검사. 통과하면 None, 실패하면 AdmissionDenied raise."""
    used, n = sum_used_ratio(sessions, gpu_index)
    if used + requested_ratio > 1.0 + _TOL:
        raise AdmissionDenied(
            requested=requested_ratio,
            currently_used=used,
            gpu_index=gpu_index,
            active_sessions=n,
        )


def usage_snapshot(sessions) -> dict:
    """모든 활성 세션을 GPU 별로 group, ratio 합계 + 잔여 capacity 계산.

    UI 에 "현재 사용 0.7/1.0" 같은 표시를 하기 위해 /sessions/admission 이
    리턴.  None (전체 GPU) 은 별도 키로 분리, per-device 합과 합쳐서 보여줌.
    """
    per_gpu: dict[Optional[int], float] = {}
    counts: dict[Optional[int], int] = {}
    for r in sessions:
        if r.status not in ("created", "running"):
            continue
        per_gpu[r.gpu_index] = per_gpu.get(r.gpu_index, 0.0) + r.ratio
        counts[r.gpu_index] = counts.get(r.gpu_index, 0) + 1

    # JSON 친화적으로 키 normalize
    def key(k: Optional[int]) -> str:
        return "all" if k is None else str(k)

    return {
        "by_gpu": {
            key(k): {
                "ratio_used": round(per_gpu[k], 6),
                "ratio_available": round(max(0.0, 1.0 - per_gpu[k]), 6),
                "active_sessions": counts[k],
            }
            for k in per_gpu
        },
    }
