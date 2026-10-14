"""

Harbor Docker environment extension for this project.

Harbor's stock Docker environment works, but image mounts (``type: image``)
need Docker Engine API >= 1.48, and some Compose versions under-negotiate the
API unless ``DOCKER_API_VERSION`` is explicit — the failure shows up as a
confusing mid-trial compose error instead of a clear message. This subclass
sets the variable (from the daemon's own reported version) only for runs that
actually contain image mounts, and fails fast with an explanation when the
daemon is too old.

本项目的 Harbor Docker 环境扩展。

Harbor 自带的 Docker 环境本身可用，但镜像挂载（``type: image``）需要
Docker Engine API >= 1.48；而且某些 Compose 版本在 ``DOCKER_API_VERSION``
不显式指定时，协商出的 API 版本会偏低——于是失败表现成一个令人困惑的
trial 中途 compose 错误，而不是一条清晰明确的报错信息。

这个子类只在确实包含镜像挂载的运行中才设置该变量（取值来自守护进程
自己报告的版本），并在守护进程版本过低时提前失败，同时给出解释说明。

"""

from itertools import zip_longest
from typing import override

from harbor.environments.docker.docker import DockerEnvironment

_MIN_IMAGE_MOUNT_API = (1, 48)
_MIN_IMAGE_MOUNT_API_TEXT = "1.48"


def _parse_api_version(value: str) -> tuple[int, ...] | None:
    parts = value.strip().split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _api_version_lt(left: tuple[int, ...], right: tuple[int, ...]) -> bool:
    for l_part, r_part in zip_longest(left, right, fillvalue=0):
        if l_part != r_part:
            return l_part < r_part
    return False


class HarborTestDockerEnvironment(DockerEnvironment):
    """Docker backend with image-mount API version negotiation.支持镜像挂载 API 版本协商的 Docker 后端。"""

    @override
    def _compose_env_vars(self, include_os_env: bool = True) -> dict[str, str]:
        env_vars = super()._compose_env_vars(include_os_env=include_os_env)
        if not any(
            isinstance(mount, dict) and mount.get("type") == "image"
            for mount in self._mounts
        ):
            return env_vars
        if env_vars.get("DOCKER_API_VERSION", "").strip():
            # Caller pinned a version; respect it (harbor_test preflight
            # already validated it against the 1.48 minimum).
            return env_vars

        import subprocess

        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.APIVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        server_api = result.stdout.strip()
        parsed = _parse_api_version(server_api)
        if parsed is None:
            raise RuntimeError(
                f"Docker reported an invalid server API version {server_api!r}; "
                f"image mounts require API >= {_MIN_IMAGE_MOUNT_API_TEXT}."
            )
        if _api_version_lt(parsed, _MIN_IMAGE_MOUNT_API):
            raise RuntimeError(
                f"Docker server API {server_api!r} is too old for image mounts; "
                f"upgrade Docker (need API >= {_MIN_IMAGE_MOUNT_API_TEXT})."
            )
        env_vars["DOCKER_API_VERSION"] = server_api
        return env_vars
