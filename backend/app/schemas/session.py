"""
세션 관련 Pydantic 모델.

설계 메모: Session 클래스는 *내부 record* 와 *API 응답* 을 겸한다.
Stage 3 MVP 에서 별도 DTO 를 둘 만큼 변환 로직이 필요하지 않으므로
모델 하나로 통합. Stage 8 (Redis 저장) 에서 분리할 가능성 있음.

Stage 10 (interactive): mode 필드 추가.
  - "batch"   — 기존 동작. command 가 끝나면 컨테이너 종료.
  - "jupyter" — Jupyter Lab 서버 띄움. 컨테이너는 사용자가 stop/delete
                할 때까지 유지. host_port + jupyter_token 으로 브라우저
                접속 URL 구성.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field


SessionMode = Literal["batch", "jupyter"]


class SessionCreate(BaseModel):
    """POST /sessions body."""
    ratio: float = Field(..., gt=0.0, le=1.0,
                         description="GPU 메모리 비율 (0.0 < ratio <= 1.0)")
    mode: SessionMode = Field(
        default="batch",
        description="batch=명령 1회 실행 후 종료, jupyter=Jupyter Lab 서버.",
    )
    command: Optional[list[str]] = Field(
        default=None,
        description="컨테이너 내부에서 실행할 명령. None 이면 이미지 default CMD. "
                    "mode=jupyter 일 때는 무시되고 jupyter lab 명령으로 강제.",
    )
    image: Optional[str] = Field(
        default=None,
        description="사용할 docker image. None 이면 settings.runtime_image. "
                    "mode=jupyter 일 때는 PyTorch 이미지가 필요 (jupyterlab 설치됨).",
    )
    quota_bytes: Optional[int] = Field(
        default=None, gt=0,
        description="절대 quota (bytes). 설정 시 ratio 보다 우선.",
    )
    gpu_index: Optional[int] = Field(
        default=None, ge=0,
        description="멀티-GPU 호스트에서 특정 device 만 노출. None 이면 모든 GPU.",
    )
    force: bool = Field(
        default=False,
        description="Stage 11 admission control 우회. True 이면 sum(ratios) > 1.0 "
                    "이어도 세션 생성 허용 (oversubscription 데모용).",
    )


class Session(BaseModel):
    """세션 record + API 응답 공용."""
    id: str
    container_id: str
    container_name: str
    status: str = "created"        # docker container status
    mode: SessionMode = "batch"
    ratio: float
    quota_bytes: Optional[int] = None
    image: str
    command: list[str]
    created_at: datetime
    exit_code: Optional[int] = None
    gpu_index: Optional[int] = None  # Stage 9 minimal — 사용된 GPU device id
    # Stage 10 — jupyter 모드일 때만 채워짐.
    host_port: Optional[int] = None
    jupyter_token: Optional[str] = None
    jupyter_url: Optional[str] = None
    workspace_dir: Optional[str] = None


class SessionLogs(BaseModel):
    id: str
    logs: str
