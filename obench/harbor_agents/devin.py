"""Harbor Devin subscription profile with strict exported-ATIF validation."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import shlex
from typing import Any

from obench.atif import SCHEMA_VERSION, assert_valid_trajectory, dump_trajectory
from obench.harbor_agents._subscription import (
    DEVIN_AUTH_ARCHIVE_ENV,
    resolve_subscription_archive,
    upload_subscription_archive,
)
from obench.harbor_oauth import (
    HarborOAuthSetupError,
    HarborOAuthUnsupportedError,
)


_REMOTE_HOME = PurePosixPath("/tmp/openbench-devin-home")
_REMOTE_ARCHIVE = PurePosixPath("/tmp/openbench-devin-auth.tar.gz")
_OUTPUT_FILENAME = "devin-export.json"
_DEVIN_MODELS = {
    "gpt-5.5-medium": None,
    "gpt-5.6-sol": "gpt-5-6-sol-medium",
}
_DEVIN_SHA256 = {
    "x86_64-unknown-linux": (
        "f0e1e9363afc6ee68c4ef87bab4aeb7ff5cc08a5fa838350ef3ceefdbb2a2be2"
    ),
    "aarch64-unknown-linux": (
        "116dc71ef085a922bc3ff0ea0377d4b26c529a431d58246e36572913e2d25624"
    ),
}


def normalize_devin_export(
    source: str | Path,
    *,
    version: str,
    model_name: str,
) -> dict[str, Any]:
    """Stamp profile identity onto Devin's export and validate every source step."""

    source_path = Path(source)
    try:
        trajectory = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarborOAuthUnsupportedError(
            "Devin did not produce a readable --export trajectory"
        ) from exc
    if not isinstance(trajectory, dict):
        raise HarborOAuthUnsupportedError(
            "Devin --export trajectory is not an object"
        )
    steps = trajectory.get("steps")
    if not isinstance(steps, list) or not steps:
        raise HarborOAuthUnsupportedError(
            "Devin --export trajectory contains no source steps"
        )

    trajectory["schema_version"] = SCHEMA_VERSION
    trajectory["agent"] = {
        "name": "devin",
        "version": version,
        "model_name": model_name,
    }
    final_metrics = trajectory.get("final_metrics")
    if final_metrics is None:
        final_metrics = {}
        trajectory["final_metrics"] = final_metrics
    if not isinstance(final_metrics, dict):
        raise HarborOAuthUnsupportedError(
            "Devin --export final_metrics is not an object"
        )
    final_metrics["total_steps"] = len(steps)
    extra = trajectory.setdefault("extra", {})
    if not isinstance(extra, dict):
        raise HarborOAuthUnsupportedError(
            "Devin --export extra metadata is not an object"
        )
    extra["source_format"] = "devin-export"
    extra["source_transcript"] = str(source_path)
    try:
        assert_valid_trajectory(trajectory)
    except ValueError as exc:
        raise HarborOAuthUnsupportedError(
            "Devin --export is not valid ATIF-v1.7"
        ) from exc
    return trajectory


def _load_harbor_base():
    try:
        from harbor.agents.installed.base import BaseInstalledAgent
        from harbor.models.trial.paths import EnvironmentPaths
    except (ImportError, ModuleNotFoundError) as exc:
        raise HarborOAuthSetupError(
            "Harbor Devin support requires Harbor 0.20.0"
        ) from exc
    return BaseInstalledAgent, EnvironmentPaths


def _build_agent_class(base, environment_paths):
    class OpenBenchDevinSubscription(base):
        SUPPORTS_ATIF = True
        SUPPORTS_RESUME = False

        @staticmethod
        def name():
            return "devin"

        def get_version_command(self):
            return "devin --version"

        async def install(self, environment):
            version = shlex.quote(self._version or "")
            arm_sha = _DEVIN_SHA256["aarch64-unknown-linux"]
            x64_sha = _DEVIN_SHA256["x86_64-unknown-linux"]
            await self.ensure_system_dependencies(
                environment, ("curl", "tar", "ca_certificates")
            )
            await self.exec_as_root(
                environment,
                command=(
                    'case "$(uname -m)" in '
                    f"x86_64|amd64) arch=x86_64-unknown-linux; sha={x64_sha} ;; "
                    f"arm64|aarch64) arch=aarch64-unknown-linux; sha={arm_sha} ;; "
                    '*) echo "unsupported devin architecture" >&2; exit 1 ;; '
                    "esac; "
                    f"curl -fsSL https://static.devin.ai/cli/{version}/"
                    f"devin-{version}-$arch.tar.gz -o /tmp/devin.tar.gz && "
                    'printf "%s  %s\\n" "$sha" /tmp/devin.tar.gz | '
                    "sha256sum -c - && "
                    "rm -rf /installed-agent/devin && "
                    "mkdir -p /installed-agent/devin && "
                    "tar -xzf /tmp/devin.tar.gz "
                    "-C /installed-agent/devin && "
                    "find /installed-agent/devin -type f -name devin "
                    "-exec ln -sf {} /usr/local/bin/devin \\; && "
                    "rm -f /tmp/devin.tar.gz && "
                    "devin --version"
                ),
            )

        async def run(self, instruction, environment, context):
            del context
            archive = resolve_subscription_archive(
                self, DEVIN_AUTH_ARCHIVE_ENV
            )
            if self.model_name not in _DEVIN_MODELS:
                raise HarborOAuthUnsupportedError(
                    f"unsupported Devin model: {self.model_name!r}"
                )
            model_uid = _DEVIN_MODELS[self.model_name]
            model_flag = (
                f"--model {shlex.quote(model_uid)} " if model_uid else ""
            )
            remote_home = _REMOTE_HOME.as_posix()
            remote_archive = _REMOTE_ARCHIVE.as_posix()
            await self.exec_as_agent(
                environment,
                command=(
                    f"rm -rf {shlex.quote(remote_home)} "
                    f"{shlex.quote(remote_archive)} && "
                    f"mkdir -p {shlex.quote(remote_home)}"
                ),
            )
            await upload_subscription_archive(
                self,
                environment,
                archive=archive,
                remote_archive=remote_archive,
            )
            try:
                await self.exec_as_agent(
                    environment,
                    command=(
                        f"tar -xzf {shlex.quote(remote_archive)} "
                        f"-C {shlex.quote(remote_home)} && "
                        f"find {shlex.quote(remote_home)} -type d "
                        "-exec chmod 700 {} + && "
                        f"find {shlex.quote(remote_home)} -type f "
                        "-exec chmod 600 {} + && "
                        f"export HOME={shlex.quote(remote_home)}; "
                        "unset DEVIN_PERMISSION_MODE DEVIN_MODEL; "
                        "devin -p --permission-mode dangerous "
                        f"{model_flag}"
                        f"--export /logs/agent/{_OUTPUT_FILENAME} -- "
                        f"{shlex.quote(self.render_instruction(instruction))} "
                        "2>&1 </dev/null | "
                        "stdbuf -oL tee /logs/agent/devin.txt"
                    ),
                    cwd=environment_paths.app_dir.as_posix(),
                )
            finally:
                try:
                    await self.exec_as_agent(
                        environment,
                        command=(
                            f"rm -rf {shlex.quote(remote_home)} "
                            f"{shlex.quote(remote_archive)}"
                        ),
                    )
                except BaseException:
                    pass

        def populate_context_post_run(self, context):
            del context
            source = self.logs_dir / _OUTPUT_FILENAME
            trajectory = normalize_devin_export(
                source,
                version=self.version() or "unknown",
                model_name=self.model_name or "unknown",
            )
            dump_trajectory(trajectory, self.logs_dir / "trajectory.json")

    OpenBenchDevinSubscription.__name__ = "OpenBenchDevinSubscription"
    OpenBenchDevinSubscription.__qualname__ = "OpenBenchDevinSubscription"
    OpenBenchDevinSubscription.__module__ = __name__
    return OpenBenchDevinSubscription


def load_agent_class():
    base, environment_paths = _load_harbor_base()
    return _build_agent_class(base, environment_paths)


def __getattr__(name):
    if name != "OpenBenchDevinSubscription":
        raise AttributeError(name)
    agent_class = load_agent_class()
    globals()[name] = agent_class
    return agent_class
