"""Harbor Pi OAuth profile with isolated auth and ATIF conversion."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import shlex
import tempfile

from obench.atif import assert_valid_trajectory, dump_trajectory
from obench.harbor_agents._oauth import (
    capture_auth_json,
    refresh_staged_auth,
    resolve_auth_paths,
    upload_auth_json,
)
from obench.harbor_oauth import (
    HarborOAuthCaptureError,
    HarborOAuthSetupError,
    HarborOAuthUnsupportedError,
)
from obench.harbor_metering import HarborMeteringSession, UsageCounters
from obench.tools.atif_convert import convert_pi


PI_AUTH_JSON_PATH = "OPENBENCH_PI_AUTH_JSON_PATH"
PI_AUTH_RETURN_PATH = "OPENBENCH_PI_AUTH_RETURN_PATH"
PI_BASE_URL = "OPENBENCH_PI_BASE_URL"

_REMOTE_HOME = PurePosixPath("/tmp/openbench-pi-home")
_REMOTE_AGENT_DIR = _REMOTE_HOME / ".pi" / "agent"
_REMOTE_AUTH = _REMOTE_AGENT_DIR / "auth.json"
_REMOTE_MODELS = _REMOTE_AGENT_DIR / "models.json"


def _load_harbor_pi():
    try:
        from harbor.agents.installed.pi import Pi
    except (ImportError, ModuleNotFoundError) as exc:
        raise HarborOAuthSetupError(
            "Harbor Pi OAuth support requires Harbor 0.20.0"
        ) from exc
    return Pi


def _build_agent_class(harbor_pi):
    required = (
        "_build_register_skills_command",
        "_get_env",
        "build_cli_flags",
        "exec_as_agent",
        "exec_as_root",
        "render_instruction",
    )
    missing = [name for name in required if not hasattr(harbor_pi, name)]
    if missing:
        raise HarborOAuthUnsupportedError(
            "installed Harbor Pi lacks required profile members: "
            + ", ".join(missing)
        )

    class OpenBenchPiOAuth(harbor_pi):
        SUPPORTS_ATIF = True

        def __init__(self, *args, **kwargs):
            self._oauth_run_active = False
            self._openbench_instruction = None
            super().__init__(*args, **kwargs)

        async def run(self, instruction, environment, context):
            if self._oauth_run_active:
                raise HarborOAuthSetupError(
                    "one OpenBenchPiOAuth instance cannot run concurrently"
                )
            input_path, return_path = resolve_auth_paths(
                self,
                input_env=PI_AUTH_JSON_PATH,
                return_env=PI_AUTH_RETURN_PATH,
            )
            if not self.model_name or "/" not in self.model_name:
                raise HarborOAuthUnsupportedError(
                    "Pi OAuth model must be openai-codex/<model>"
                )
            provider, model_id = self.model_name.split("/", 1)
            if provider != "openai-codex" or not model_id:
                raise HarborOAuthUnsupportedError(
                    "OpenBenchPiOAuth supports only the openai-codex provider"
                )

            logs_dir = Path(self.logs_dir)
            metering = HarborMeteringSession(
                logs_dir / "harbor-metering",
                logs_dir.parent.name or "harbor-trial",
                harness="pi",
                base_route="backend-api",
            )
            missing = object()
            prior_base_url = self._extra_env.get(PI_BASE_URL, missing)
            self._extra_env[PI_BASE_URL] = metering.runtime_base_url
            instruction = self.render_instruction(instruction)
            self._openbench_instruction = instruction
            remote_home = _REMOTE_HOME.as_posix()
            remote_agent_dir = _REMOTE_AGENT_DIR.as_posix()
            remote_auth = _REMOTE_AUTH.as_posix()
            staged = False
            run_error = None
            capture_error = None
            self._oauth_run_active = True
            try:
                await self.exec_as_agent(
                    environment,
                    command=(
                        f"rm -rf {shlex.quote(remote_home)} && "
                        f"mkdir -p {shlex.quote(remote_agent_dir)}"
                    ),
                )
                await upload_auth_json(
                    self,
                    environment,
                    input_path=input_path,
                    remote_path=remote_auth,
                )
                staged = True

                proxy_base_url = self._get_env(PI_BASE_URL)
                if proxy_base_url:
                    models = json.dumps(
                        {
                            "providers": {
                                "openai-codex": {"baseUrl": proxy_base_url}
                            }
                        },
                        sort_keys=True,
                    )
                    await self.exec_as_agent(
                        environment,
                        command=(
                            f"printf %s {shlex.quote(models)} > "
                            f"{shlex.quote(_REMOTE_MODELS.as_posix())}"
                        ),
                    )

                skills_command = self._build_register_skills_command()
                if skills_command:
                    await self.exec_as_agent(
                        environment,
                        command=skills_command,
                        env={
                            "HOME": remote_home,
                            "PI_CODING_AGENT_DIR": remote_agent_dir,
                        },
                    )

                cli_flags = self.build_cli_flags()
                flags = f"{cli_flags} " if cli_flags else ""
                resume = "--continue " if self._resume else ""
                await self.exec_as_agent(
                    environment,
                    command=(
                        'OPENBENCH_REAL_HOME="$HOME"; '
                        'if [ -s "$OPENBENCH_REAL_HOME/.nvm/nvm.sh" ]; then '
                        '. "$OPENBENCH_REAL_HOME/.nvm/nvm.sh"; fi; '
                        f"export HOME={shlex.quote(remote_home)} "
                        f"PI_CODING_AGENT_DIR={shlex.quote(remote_agent_dir)}; "
                        "unset OPENAI_API_KEY PI_PACKAGE_DIR "
                        "PI_CODING_AGENT_SESSION_DIR; "
                        "pi --print --mode json "
                        "--session-dir /logs/agent/pi/sessions "
                        f"{resume}"
                        "--no-approve --no-extensions "
                        "--provider openai-codex "
                        f"--model {shlex.quote(model_id)} "
                        f"{flags}"
                        f"{shlex.quote(instruction)} "
                        "2>&1 </dev/null | "
                        "grep -v '\"type\":\"message_update\"' | "
                        "stdbuf -oL tee /logs/agent/pi.txt"
                    ),
                )
            except BaseException as exc:
                run_error = exc
            finally:
                if staged:
                    try:
                        await capture_auth_json(
                            environment,
                            remote_path=remote_auth,
                            return_path=return_path,
                            harness="Pi",
                        )
                        refresh_staged_auth(input_path, return_path)
                    except HarborOAuthCaptureError as exc:
                        capture_error = exc
                try:
                    await self.exec_as_agent(
                        environment,
                        command=f"rm -rf {shlex.quote(remote_home)}",
                    )
                except BaseException:
                    pass
                self._oauth_run_active = False
                try:
                    self.populate_context_post_run(context)
                    trajectory_path = logs_dir / "trajectory.json"
                    trajectory = (
                        json.loads(trajectory_path.read_text(encoding="utf-8"))
                        if trajectory_path.is_file()
                        else None
                    )
                    metering.seal(
                        UsageCounters.from_atif_trajectory(trajectory),
                        proxy_required=True,
                    )
                finally:
                    metering.close()
                    if prior_base_url is missing:
                        self._extra_env.pop(PI_BASE_URL, None)
                    else:
                        self._extra_env[PI_BASE_URL] = prior_base_url

            if capture_error is not None:
                raise capture_error from run_error
            if run_error is not None:
                raise run_error

        def populate_context_post_run(self, context):
            """Write Pi ATIF and populate Harbor aggregate usage."""

            output_path = self.logs_dir / self._OUTPUT_FILENAME
            if not output_path.is_file():
                return
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    prefix=".pi-atif-",
                    suffix=".txt",
                    dir=self.logs_dir,
                    delete=False,
                ) as handle:
                    converted_path = Path(handle.name)
                    handle.write("# OpenBench Harbor Pi transcript\n")
                    handle.write(
                        f"# harness=pi model={self.model_name} "
                        "task=harbor trial=1\n"
                    )
                    handle.write("# LOCAL-ONLY\n")
                    handle.write(output_path.read_text(encoding="utf-8"))
                trajectory = convert_pi(converted_path)
            except Exception:
                self.logger.exception("Failed to convert Pi output to ATIF")
                return
            finally:
                if "converted_path" in locals():
                    converted_path.unlink(missing_ok=True)

            trajectory["agent"]["version"] = self.version() or "unknown"
            trajectory["agent"]["model_name"] = self.model_name
            trajectory["extra"]["source_transcript"] = str(output_path)
            assert_valid_trajectory(trajectory)
            dump_trajectory(trajectory, self.logs_dir / "trajectory.json")

            metrics = trajectory.get("final_metrics") or {}
            context.n_input_tokens = metrics.get("total_prompt_tokens") or 0
            context.n_output_tokens = metrics.get("total_completion_tokens") or 0
            context.n_cache_tokens = metrics.get("total_cached_tokens") or 0
            context.cost_usd = metrics.get("total_cost_usd")

    OpenBenchPiOAuth.__name__ = "OpenBenchPiOAuth"
    OpenBenchPiOAuth.__qualname__ = "OpenBenchPiOAuth"
    OpenBenchPiOAuth.__module__ = __name__
    return OpenBenchPiOAuth


def load_agent_class():
    return _build_agent_class(_load_harbor_pi())


def __getattr__(name):
    if name != "OpenBenchPiOAuth":
        raise AttributeError(name)
    agent_class = load_agent_class()
    globals()[name] = agent_class
    return agent_class
