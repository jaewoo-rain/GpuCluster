"""
Docker SDK 얇은 래퍼.

책임:
  - fGPU 컨테이너를 spawn 할 때 필요한 모든 옵션 (--gpus all, hook .so 마운트,
    FGPU_RATIO/LD_PRELOAD env) 을 한 곳에서 조립.
  - 컨테이너 status / logs 조회, stop / remove.

세션 ID 와 컨테이너 ID 의 관계는 SessionManager 에서 관리.

Stage 10 (interactive)
  mode="jupyter" 일 때:
    - command 는 jupyter lab 으로 강제 (사용자 지정 무시).
    - 컨테이너의 8888/tcp 를 호스트의 ephemeral port (docker 가 자동 할당)
      로 publish. SessionManager 가 컨테이너 시작 후 attrs 에서 host_port
      를 읽어서 record 에 저장.
    - workspace_host_dir (호스트 디렉토리) 를 /workspace 로 bind-mount.
      컨테이너 삭제해도 노트북 파일 영속.
"""

from __future__ import annotations

import os
from typing import Optional
import docker
from docker.types import DeviceRequest


# 백엔드 프로세스의 env 에 설정돼 있으면 컨테이너로도 그대로 전달되는 변수.
# 5-A 확장 (run_correlation.sh) 에서 launch counter dump 주기를 컨테이너
# 별로 통제하기 위함. 명시적 화이트리스트라 임의 env leak 방지.
_PASSTHROUGH_ENV = ("FGPU_LAUNCH_LOG_EVERY", "FGPU_WINDOW_MS")

# Jupyter Lab 컨테이너 내부 포트. 호스트로는 ephemeral port 로 publish.
_JUPYTER_CONTAINER_PORT = 8888


def build_jupyter_command(token: str) -> list[str]:
    """jupyter lab 시작 명령. ServerApp.* 옵션은 jupyterlab 4.x 기준."""
    return [
        "jupyter", "lab",
        "--ip=0.0.0.0",
        f"--port={_JUPYTER_CONTAINER_PORT}",
        "--no-browser",
        "--allow-root",
        f"--ServerApp.token={token}",
        "--ServerApp.password=",
        "--ServerApp.root_dir=/workspace",
        "--ServerApp.allow_origin=*",
        "--ServerApp.allow_remote_access=True",
    ]


class DockerManager:
    def __init__(
        self,
        host_hook_path: str,
        container_hook_path: str,
        runtime_image: str,
    ) -> None:
        self.client = docker.from_env()
        self.host_hook_path = host_hook_path
        self.container_hook_path = container_hook_path
        self.runtime_image = runtime_image

    # ---- spawn ------------------------------------------------------- #
    def create_container(
        self,
        name: str,
        ratio: float,
        command: list[str],
        quota_bytes: Optional[int] = None,
        image: Optional[str] = None,
        gpu_index: Optional[int] = None,
        compute_ratio: Optional[float] = None,
        jupyter_mode: bool = False,
        workspace_host_dir: Optional[str] = None,
        ports: Optional[dict] = None,
    ):
        env = {
            "FGPU_RATIO": str(ratio),
            "LD_PRELOAD": self.container_hook_path,
        }
        if quota_bytes is not None:
            env["FGPU_QUOTA_BYTES"] = str(quota_bytes)
        # Stage 12: compute_ratio 가 설정되면 duty-cycle throttle 활성화.
        if compute_ratio is not None:
            env["FGPU_THROTTLE_ENABLE"] = "1"
            env["FGPU_COMPUTE_RATIO"] = str(compute_ratio)

        # 백엔드 프로세스 env 에 화이트리스트 변수가 있으면 컨테이너로 forward.
        # 운영자가 `FGPU_LAUNCH_LOG_EVERY=500 ./scripts/run_backend.sh` 로
        # 띄우면 이후 모든 세션이 그 값을 상속.
        for key in _PASSTHROUGH_ENV:
            v = os.environ.get(key)
            if v is not None and key not in env:
                env[key] = v

        # docker run --gpus all 또는 --gpus device=N 패턴.
        # gpu_index=None → count=-1 (전 GPU 노출, 기본 동작).
        # gpu_index=N    → device_ids=["N"] (멀티-GPU 호스트에서 특정 디바이스만).
        if gpu_index is None:
            device_requests = [DeviceRequest(count=-1, capabilities=[["gpu"]])]
        else:
            device_requests = [
                DeviceRequest(device_ids=[str(gpu_index)],
                              capabilities=[["gpu"]])
            ]

        # host 의 libfgpu.so 를 컨테이너 안 hook 경로에 read-only 로 bind mount.
        volumes = {
            self.host_hook_path: {
                "bind": self.container_hook_path,
                "mode": "ro",
            }
        }

        # Stage 10: jupyter 모드면 워크스페이스 호스트 디렉토리도 마운트.
        if jupyter_mode and workspace_host_dir:
            volumes[workspace_host_dir] = {
                "bind": "/workspace",
                "mode": "rw",
            }

        run_kwargs = dict(
            image=image or self.runtime_image,
            command=command,
            name=name,
            detach=True,
            remove=False,            # 종료 후에도 logs 조회 가능하도록 보존
            device_requests=device_requests,
            volumes=volumes,
            environment=env,
        )
        if ports:
            run_kwargs["ports"] = ports

        return self.client.containers.run(**run_kwargs)

    # ---- query ------------------------------------------------------- #
    def get_status(self, container_id: str) -> tuple[str, Optional[int]]:
        c = self.client.containers.get(container_id)
        c.reload()
        return c.status, c.attrs.get("State", {}).get("ExitCode")

    def get_host_port(self, container_id: str, container_port: int) -> Optional[int]:
        """컨테이너의 container_port/tcp 가 publish 된 호스트 포트를 반환.

        docker 가 ephemeral port 로 자동 할당했을 때 어디로 갔는지 알아내는
        용도. 컨테이너가 아직 살아있고 ports 가 publish 됐을 때만 의미 있음.
        """
        c = self.client.containers.get(container_id)
        c.reload()
        bindings = c.attrs.get("NetworkSettings", {}).get("Ports") or {}
        key = f"{container_port}/tcp"
        entries = bindings.get(key) or []
        if not entries:
            return None
        host_port = entries[0].get("HostPort")
        return int(host_port) if host_port else None

    def get_logs(self, container_id: str, tail: int = 200) -> str:
        c = self.client.containers.get(container_id)
        # stdout + stderr 합쳐서 가져온다 — entrypoint, [fgpu], [test] 모두 포함.
        raw = c.logs(stdout=True, stderr=True, tail=tail)
        return raw.decode("utf-8", errors="replace")

    # ---- lifecycle --------------------------------------------------- #
    def stop_container(self, container_id: str, timeout: int = 10) -> None:
        c = self.client.containers.get(container_id)
        c.stop(timeout=timeout)

    def remove_container(self, container_id: str, force: bool = True) -> None:
        c = self.client.containers.get(container_id)
        c.remove(force=force)
