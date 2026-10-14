"""
Harbor agents for this test project.

``MountedOpenCode`` is Harbor's stock OpenCode agent with two changes:

1. The CLI is not installed into the container at trial setup. It comes from the
   prebuilt harness image mounted read-only at ``/usr/local``, so no
   ``npm i -g`` runs per trial and the CLI version is fixed by the image tag
   (2.0.28 here).
2. The command line targets the OpenCode v2 CLI. Harbor's stock agent targets
   the legacy ``opencode-ai`` 1.x package, where global flags precede the
   subcommand and ``--dangerously-skip-permissions`` exists; the v2 CLI takes
   those flags after ``opencode run`` and replaced the latter with ``--auto``.

``MountedPi`` is Harbor's stock pi agent with one change: the CLI is not
installed per trial either — it comes from the ``harness/pi`` image mounted
read-only at ``/usr/local`` (version fixed by the image tag, 1.2.0 here).
Harbor's own pi run and trajectory logic (session-dir JSONL) is untouched.

本测试项目的 Harbor agent。

``MountedOpenCode`` 在 Harbor 原版 OpenCode agent 的基础上做了两处改动：

1. CLI 不在 trial 启动时安装进容器。它来自预构建的 harness 镜像，
   以只读方式挂载到 ``/usr/local``，因此每个 trial 不再执行
   ``npm i -g``，且 CLI 版本由镜像 tag 固定（此处为 2.0.28）。
2. 命令行面向 OpenCode v2 CLI。Harbor 原版 agent 面向旧的
   ``opencode-ai`` 1.x 包——全局 flag 放在子命令之前，且有
   ``--dangerously-skip-permissions``；v2 CLI 把这些 flag 放在
   ``opencode run`` 之后，并用 ``--auto`` 取代了后者。

``MountedPi`` 在 Harbor 原版 pi agent 的基础上只有一处改动：CLI 同样
不在每个 trial 里安装，而是来自 ``harness/pi`` 镜像（只读挂载到
``/usr/local``，版本由镜像 tag 固定，此处为 1.2.0）。pi 的运行与轨迹
逻辑（基于 ``--session-dir`` 的 JSONL）保持原版不动。

"""

from pathlib import Path
from typing import override

import shlex
import yaml

from harbor.agents.installed.base import (
    NonZeroAgentExitCodeError,
    with_prompt_template,
)
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.agents.installed.opencode import OpenCode
from harbor.agents.installed.pi import Pi
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


def _manifest_version(harness: str) -> str:
    """Read the pinned version from harness/<harness>/manifest.yaml."""
    manifest = (
        Path(__file__).resolve().parent / "harness" / harness / "manifest.yaml"
    )
    return str(yaml.safe_load(manifest.read_text())["version"])


# Version the harness image must provide; read from the single source so a
# version bump never leaves this file behind.
HARNESS_EXPECTED_VERSION = _manifest_version("opencode")


class MountedOpenCode(OpenCode):
    """OpenCode agent that expects the CLI to be mounted in by the harness image.
    期望 CLI 由 harness 镜像挂载进来的 OpenCode agent。"""

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        result = await self.exec_as_agent(
            environment,
            command="command -v opencode && opencode --version",
        )
        output = (result.stdout or "") + (result.stderr or "")
        if HARNESS_EXPECTED_VERSION not in output:
            raise RuntimeError(
                "opencode CLI missing or wrong version; expected "
                f"{HARNESS_EXPECTED_VERSION} from the harness image mount, "
                f"got: {output.strip()!r}"
            )

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        self._instruction = instruction
        escaped_instruction = shlex.quote(instruction)

        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Model name must be in the format provider/model_name")

        env = dict(self.model_connection.env)
        env["OPENCODE_FAKE_VCS"] = "git"
        env["XDG_DATA_HOME"] = (
            self.environment_logs_dir / "opencode" / "xdg-data"
        ).as_posix()
        env["XDG_STATE_HOME"] = (
            self.environment_logs_dir / "opencode" / "xdg-state"
        ).as_posix()

        skills_command = self._build_register_skills_command()
        if skills_command:
            await self.exec_as_agent(environment, command=skills_command, env=env)

        mcp_command = self._build_register_config_command()
        if mcp_command:
            await self.exec_as_agent(environment, command=mcp_command, env=env)

        cli_flags = self.build_cli_flags()
        cli_flags_arg = (cli_flags + " ") if cli_flags else ""
        resume_flag = "--continue " if self._resume else ""

        await self.exec_as_agent(
            environment,
            command=(
                f"opencode run --model={self.model_name} --format=json "
                f"{resume_flag}{cli_flags_arg}--thinking --auto "
                f"-- {escaped_instruction} "
                f"2>&1 </dev/null | stdbuf -oL tee "
                f"{shlex.quote((self.environment_logs_dir / self._OUTPUT_FILENAME).as_posix())}"
            ),
            env=env,
        )

        if messages := self._error_messages():
            raise NonZeroAgentExitCodeError(
                "OpenCode emitted error event(s): " + "; ".join(messages[:3])
            )


class MountedPi(Pi):
    """pi agent that expects the CLI to be mounted in by the harness image.
    期望 CLI 由 harness 镜像挂载进来的 pi agent。"""

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        expected = _manifest_version("pi")
        result = await self.exec_as_agent(
            environment,
            command="command -v pi && pi --version",
        )
        output = (result.stdout or "") + (result.stderr or "")
        if expected not in output:
            raise RuntimeError(
                "pi CLI missing or wrong version; expected "
                f"{expected} from the harness image mount, "
                f"got: {output.strip()!r}"
            )


class MountedClaudeCode(ClaudeCode):
    """Claude Code agent that expects the CLI to be mounted in by the harness
    image. 期望 CLI 由 harness 镜像挂载进来的 Claude Code agent。"""

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        expected = _manifest_version("claude-code")
        result = await self.exec_as_agent(
            environment,
            command="command -v claude && claude --version",
        )
        output = (result.stdout or "") + (result.stderr or "")
        if expected not in output:
            raise RuntimeError(
                "claude CLI missing or wrong version; expected "
                f"{expected} from the harness image mount, "
                f"got: {output.strip()!r}"
            )
