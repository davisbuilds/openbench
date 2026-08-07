import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest

from obench.native_macos import (
    AppEvidence,
    AppRequirement,
    FocusEvent,
    LeaseOwner,
    LeaseUnavailableError,
    MacOSAppInspector,
    MacOSFocusMonitor,
    MacOSSessionReader,
    NSWorkspaceActivationEventSource,
    NativeMacOSHelperResolver,
    PhaseName,
    PhaseSpec,
    PhaseStatus,
    PreflightEvidenceError,
    PreflightSpec,
    SubprocessPhaseRunner,
    WholeRunLease,
    evaluate_preflight,
    parse_health_report_json,
    run_preflight,
)


def health_report(**overrides):
    value = {
        "reportVersion": 1,
        "version": "0.3.0",
        "executablePath": "/opt/bin/computer-use-mcp",
        "bundleIdentifier": "com.example.computer-use",
        "permissions": {
            "accessibility": {"granted": True, "status": "granted"},
            "screenRecording": {"granted": True, "status": "granted"},
        },
        "captureService": {"status": "responsive"},
    }
    value.update(overrides)
    return value


class WholeRunLeaseTests(unittest.TestCase):
    def test_exclusive_lease_reports_current_owner_and_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "native.lock"
            owner = LeaseOwner(
                run_id="run-123",
                pid=123,
                hostname="host",
                started_at="2026-08-06T00:00:00+00:00",
                argv=("obench", "native"),
            )
            first = WholeRunLease(path, owner=owner)
            second = WholeRunLease(path)

            with first:
                self.assertTrue(first.held)
                self.assertEqual(WholeRunLease.read_owner(path)["run_id"], "run-123")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(LeaseUnavailableError) as caught:
                    second.acquire()
                self.assertEqual(caught.exception.owner["pid"], 123)

            with second:
                self.assertTrue(second.held)

    def test_os_releases_lease_when_owner_process_exits(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "native.lock"
            script = (
                "from obench.native_macos import WholeRunLease;"
                "import sys;"
                "lease=WholeRunLease(sys.argv[1]);"
                "lease.acquire()"
            )
            completed = subprocess.run(
                [sys.executable, "-c", script, str(path)],
                cwd=Path(__file__).parents[2],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            with WholeRunLease(path):
                pass


class HealthReportTests(unittest.TestCase):
    def test_parses_source_proven_v1_schema(self):
        report = parse_health_report_json(json.dumps(health_report()))
        self.assertEqual(report.version, "0.3.0")
        self.assertTrue(report.accessibility.granted)
        self.assertEqual(report.capture_status, "responsive")

    def test_rejects_unknown_schema_or_safety_status(self):
        with self.assertRaises(PreflightEvidenceError):
            parse_health_report_json(health_report(reportVersion=2))
        with self.assertRaises(PreflightEvidenceError):
            parse_health_report_json(health_report(reportVersion=True))
        bad = health_report(captureService={"status": "probably_healthy"})
        with self.assertRaises(PreflightEvidenceError):
            parse_health_report_json(bad)

    def test_rejects_inconsistent_permission_evidence(self):
        permissions = health_report()["permissions"]
        permissions["accessibility"] = {"granted": True, "status": "not_granted"}
        with self.assertRaises(PreflightEvidenceError):
            parse_health_report_json(health_report(permissions=permissions))

    def test_rejects_missing_required_fields(self):
        value = health_report()
        del value["permissions"]["screenRecording"]
        with self.assertRaises(PreflightEvidenceError):
            parse_health_report_json(value)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.health = parse_health_report_json(health_report())
        self.spec = PreflightSpec(
            required_apps=(AppRequirement("com.example.Target", "2.4.1"),),
            computer_use_version="0.3.0",
            computer_use_bundle_identifier="com.example.computer-use",
        )
        self.apps = (
            AppEvidence("com.example.Target", "2.4.1", True, "/Applications/Target.app"),
        )

    def test_complete_exact_evidence_passes(self):
        result = evaluate_preflight(
            self.spec,
            platform_name="Darwin",
            health=self.health,
            apps=self.apps,
            screen_unlocked=True,
        )
        self.assertTrue(result.passed)
        result.require_passed()

    def test_unknown_unlock_and_skipped_capture_fail_closed(self):
        degraded = parse_health_report_json(
            health_report(captureService={"status": "skipped"})
        )
        result = evaluate_preflight(
            self.spec,
            platform_name="Darwin",
            health=degraded,
            apps=self.apps,
            screen_unlocked=None,
        )
        self.assertFalse(result.passed)
        self.assertEqual(
            {failure.name for failure in result.failures},
            {"capture_health", "screen_unlocked"},
        )

    def test_wrong_or_ambiguous_app_identity_version_fails(self):
        apps = (
            AppEvidence("com.example.Target", "2.4.0", True),
            AppEvidence("com.example.Target", "2.4.1", True),
            AppEvidence("com.example.Target", "2.4.1", True),
        )
        result = evaluate_preflight(
            self.spec,
            platform_name="Darwin",
            health=self.health,
            apps=apps,
            screen_unlocked=True,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.failures[0].name, "app:com.example.Target")

    def test_runner_uses_non_prompting_bounded_health_command(self):
        calls = []

        def command_runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, json.dumps(health_report()), "")

        class Inspector:
            def inspect(inner_self, requirements):
                self.assertEqual(tuple(requirements), self.spec.required_apps)
                return self.apps

        class Session:
            def screen_unlocked(inner_self):
                return True

        result = run_preflight(
            self.spec,
            command_runner=command_runner,
            app_inspector=Inspector(),
            session_reader=Session(),
            platform_reader=lambda: "Darwin",
            timeout_s=7,
        )
        self.assertTrue(result.passed)
        argv, kwargs = calls[0]
        self.assertEqual(
            argv,
            ["computer-use-mcp", "health_report", "--json", "--probe-capture"],
        )
        self.assertNotIn("--prompt", argv)
        self.assertEqual(kwargs["timeout"], 7)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)

    def test_runner_raises_on_command_failure(self):
        def command_runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 4, "", "unavailable")

        class Inspector:
            def inspect(inner_self, requirements):
                return self.apps

        class Session:
            def screen_unlocked(inner_self):
                return True

        with self.assertRaisesRegex(PreflightEvidenceError, "exited 4"):
            run_preflight(
                self.spec,
                command_runner=command_runner,
                app_inspector=Inspector(),
                session_reader=Session(),
                platform_reader=lambda: "Darwin",
            )

    def test_runner_explicitly_resolves_helper_for_native_evidence(self):
        calls = []

        class Resolver:
            resolved = False

            def resolve(inner_self):
                inner_self.resolved = True
                return Path("/resolved/native-helper")

        resolver = Resolver()

        def command_runner(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "computer-use-mcp":
                payload = health_report()
            elif argv[1] == "apps":
                payload = {
                    "protocolVersion": 1,
                    "kind": "apps",
                    "apps": [
                        {
                            "bundleIdentifier": "com.example.Target",
                            "version": "2.4.1",
                            "running": True,
                            "path": "/Applications/Target.app",
                        }
                    ],
                }
            elif argv[1] == "session":
                payload = {
                    "protocolVersion": 1,
                    "kind": "session",
                    "status": "known",
                    "screenUnlocked": True,
                }
            else:
                self.fail(f"unexpected command: {argv}")
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        result = run_preflight(
            self.spec,
            command_runner=command_runner,
            helper_resolver=resolver,
            platform_reader=lambda: "Darwin",
        )
        self.assertTrue(result.passed)
        self.assertTrue(resolver.resolved)
        self.assertEqual(
            calls,
            [
                [
                    "computer-use-mcp",
                    "health_report",
                    "--json",
                    "--probe-capture",
                ],
                [
                    "/resolved/native-helper",
                    "apps",
                    "com.example.Target",
                ],
                ["/resolved/native-helper", "session"],
            ],
        )

    def test_non_darwin_fails_before_running_commands(self):
        def command_runner(argv, **kwargs):
            self.fail("preflight must not run macOS commands off Darwin")

        with self.assertRaisesRegex(PreflightEvidenceError, "requires Darwin"):
            run_preflight(
                self.spec,
                command_runner=command_runner,
                platform_reader=lambda: "Linux",
            )


class NativeHelperProtocolTests(unittest.TestCase):
    def test_checked_in_swift_source_is_in_package_data(self):
        root = Path(__file__).parents[2]
        config = tomllib.loads(
            (root / "pyproject.toml").read_text(encoding="utf-8")
        )
        package_data = config["tool"]["setuptools"]["package-data"]["obench"]
        self.assertIn("*.swift", package_data)
        self.assertTrue((root / "obench/native_macos_helper.swift").is_file())

    def test_resolver_compiles_caches_and_probes_versioned_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "helper.swift"
            source.write_text("// fixture", encoding="utf-8")
            calls = []

            def command_runner(argv, **kwargs):
                calls.append(argv)
                if argv[0] == "/usr/bin/swiftc":
                    output = Path(argv[argv.index("-o") + 1])
                    output.write_text("#!/bin/sh\n", encoding="utf-8")
                    output.chmod(0o755)
                    return subprocess.CompletedProcess(argv, 0, "", "")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps({"protocolVersion": 1, "kind": "protocol"}),
                    "",
                )

            resolver = NativeMacOSHelperResolver(
                source_path=source,
                cache_dir=root / "cache",
                command_runner=command_runner,
                which=lambda name: "/usr/bin/swiftc",
                platform_reader=lambda: "Darwin",
                machine_reader=lambda: "arm64",
            )
            first = resolver.resolve()
            resolver.which = lambda name: None
            second = resolver.resolve()
            self.assertEqual(first, second)
            self.assertTrue(os.access(first, os.X_OK))
            compile_calls = [
                argv for argv in calls if argv[0] == "/usr/bin/swiftc"
            ]
            self.assertEqual(len(compile_calls), 1)
            self.assertEqual(
                [argv[1] for argv in calls if argv[0] == str(first)],
                ["protocol", "protocol"],
            )

    def test_resolver_fails_clearly_without_swift_toolchain(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "helper.swift"
            source.write_text("// fixture", encoding="utf-8")
            resolver = NativeMacOSHelperResolver(
                source_path=source,
                cache_dir=Path(temp) / "cache",
                which=lambda name: None,
                platform_reader=lambda: "Darwin",
            )
            with self.assertRaisesRegex(PreflightEvidenceError, "requires swiftc"):
                resolver.resolve()

    def test_resolver_rejects_unknown_prebuilt_protocol(self):
        with tempfile.TemporaryDirectory() as temp:
            helper = Path(temp) / "helper"
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            helper.chmod(0o755)

            def command_runner(argv, **kwargs):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps({"protocolVersion": 2, "kind": "protocol"}),
                    "",
                )

            resolver = NativeMacOSHelperResolver(
                prebuilt_path=helper,
                command_runner=command_runner,
                platform_reader=lambda: "Darwin",
            )
            with self.assertRaisesRegex(
                PreflightEvidenceError, "unsupported native helper protocol"
            ):
                resolver.resolve()

    def test_app_and_session_adapters_fail_closed_on_unknown_fields(self):
        def command_runner(argv, **kwargs):
            if argv[1] == "apps":
                payload = {
                    "protocolVersion": 1,
                    "kind": "apps",
                    "apps": [
                        {
                            "bundleIdentifier": "com.example.Target",
                            "version": "1.0",
                            "running": True,
                            "path": None,
                        }
                    ],
                }
            else:
                payload = {
                    "protocolVersion": 1,
                    "kind": "session",
                    "status": "maybe",
                    "screenUnlocked": True,
                }
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        apps = MacOSAppInspector(
            "/helper", command_runner=command_runner
        ).inspect([AppRequirement("com.example.Target", "1.0")])
        self.assertEqual(apps[0].bundle_identifier, "com.example.Target")
        with self.assertRaisesRegex(
            PreflightEvidenceError, "session status is unknown"
        ):
            MacOSSessionReader(
                "/helper", command_runner=command_runner
            ).screen_unlocked()

    def test_focus_source_consumes_helper_jsonl_and_stops_process(self):
        with tempfile.TemporaryDirectory() as temp:
            helper = Path(temp) / "focus-helper"
            helper.write_text(
                "#!/usr/bin/env python3\n"
                "import json, time\n"
                "print(json.dumps({"
                "'protocolVersion': 1, 'kind': 'focus', "
                "'bundleIdentifier': 'com.example.Target', "
                "'applicationName': 'Target', 'pid': 123}), flush=True)\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            helper.chmod(0o755)

            class Resolver:
                def resolve(inner_self):
                    return helper

            events = []
            source = NSWorkspaceActivationEventSource(
                helper_resolver=Resolver(),
                startup_timeout_s=2,
            )
            source.start(events.append)
            source.stop()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].bundle_identifier, "com.example.Target")
            self.assertIsNone(source.error)


class ManualActivationSource:
    def __init__(self, initial_bundle_identifier=None):
        self.callback = None
        self.stopped = False
        self.initial_bundle_identifier = initial_bundle_identifier

    def start(self, callback):
        self.callback = callback
        if self.initial_bundle_identifier is not None:
            self.emit(self.initial_bundle_identifier)

    def stop(self):
        self.stopped = True
        self.callback = None

    def emit(self, bundle_identifier, name="App", pid=100):
        self.callback(FocusEvent(bundle_identifier, name, pid, 12.5))


class FocusMonitorTests(unittest.TestCase):
    def test_stop_fails_closed_when_event_source_died(self):
        class FailedSource(ManualActivationSource):
            error = RuntimeError("helper exited")

        monitor = MacOSFocusMonitor(
            ["com.example.Target"],
            event_source=FailedSource(),
        )
        monitor.start()
        with self.assertRaisesRegex(
            Exception, "native focus monitor failed: helper exited"
        ):
            monitor.stop()

    def test_source_can_report_disallowed_baseline_focus_at_start(self):
        source = ManualActivationSource("com.example.AlreadyFrontmost")
        monitor = MacOSFocusMonitor(
            ["com.example.Target"],
            event_source=source,
        )
        monitor.start()
        monitor.stop()
        self.assertEqual(len(monitor.violations), 1)
        self.assertEqual(
            monitor.violations[0].event.bundle_identifier,
            "com.example.AlreadyFrontmost",
        )

    def test_deterministic_source_records_disallowed_and_unknown_focus(self):
        source = ManualActivationSource()
        observed = []
        monitor = MacOSFocusMonitor(
            ["com.example.Target"],
            event_source=source,
            on_violation=observed.append,
        )
        with monitor:
            source.emit("com.example.Target")
            source.emit("com.example.Other")
            source.emit(None)

        self.assertTrue(source.stopped)
        self.assertEqual(len(monitor.violations), 2)
        self.assertEqual(observed, list(monitor.violations))
        self.assertIn("not allowed", monitor.violations[0].reason)
        self.assertIn("no bundle identifier", monitor.violations[1].reason)

    def test_empty_allowlist_is_rejected(self):
        with self.assertRaises(ValueError):
            MacOSFocusMonitor([], event_source=ManualActivationSource())


def phase(name, code, timeout=2.0):
    return PhaseSpec(
        name=name,
        argv=(sys.executable, "-c", code),
        timeout_s=timeout,
    )


class SubprocessPhaseRunnerTests(unittest.TestCase):
    def test_successful_phases_return_explicit_records(self):
        runner = SubprocessPhaseRunner()
        result = runner.run(
            setup=phase(PhaseName.SETUP, "print('setup')"),
            agent=phase(PhaseName.AGENT, "print('agent')"),
            verifier=phase(PhaseName.VERIFIER, "print('verify')"),
            reset=phase(PhaseName.RESET, "print('reset')"),
        )
        self.assertTrue(result.passed)
        self.assertEqual(
            [item.name for item in result.outcomes],
            list(PhaseName),
        )
        self.assertEqual(
            [item.status for item in result.outcomes],
            [PhaseStatus.PASSED] * 4,
        )
        self.assertEqual(result.outcome(PhaseName.AGENT).stdout, "agent\n")

    def test_failure_skips_dependent_phases_but_always_resets(self):
        runner = SubprocessPhaseRunner()
        result = runner.run(
            setup=phase(PhaseName.SETUP, "raise SystemExit(9)"),
            agent=phase(PhaseName.AGENT, "raise AssertionError('must not run')"),
            verifier=phase(PhaseName.VERIFIER, "raise AssertionError('must not run')"),
            reset=phase(PhaseName.RESET, "print('cleaned')"),
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.outcome(PhaseName.SETUP).status, PhaseStatus.FAILED)
        self.assertEqual(result.outcome(PhaseName.SETUP).exit_code, 9)
        self.assertEqual(result.outcome(PhaseName.AGENT).status, PhaseStatus.SKIPPED)
        self.assertEqual(result.outcome(PhaseName.VERIFIER).status, PhaseStatus.SKIPPED)
        self.assertEqual(result.outcome(PhaseName.RESET).status, PhaseStatus.PASSED)

    @unittest.skipUnless(hasattr(os, "killpg"), "requires Unix process groups")
    def test_timeout_terminates_entire_process_group(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "child-survived"
            child_code = (
                "import pathlib,signal,time,sys;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                "time.sleep(0.8);"
                "pathlib.Path(sys.argv[1]).write_text('alive')"
            )
            parent_code = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child_code!r},sys.argv[1]]);"
                "time.sleep(30)"
            )
            runner = SubprocessPhaseRunner(terminate_grace_s=0.1)
            outcome = runner.run_phase(
                PhaseSpec(
                    PhaseName.AGENT,
                    (sys.executable, "-c", parent_code, str(marker)),
                    timeout_s=0.2,
                )
            )
            self.assertEqual(outcome.status, PhaseStatus.TIMED_OUT)
            self.assertEqual(outcome.termination, "SIGTERM+SIGKILL")
            time.sleep(1.0)
            self.assertFalse(marker.exists(), "descendant escaped process-group cleanup")

    @unittest.skipUnless(hasattr(os, "killpg"), "requires Unix process groups")
    def test_cleanup_reaches_child_after_successful_leader_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "child-survived"
            ready = Path(temp) / "child-ready"
            child_code = (
                "import pathlib,signal,time,sys;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                "pathlib.Path(sys.argv[2]).write_text('ready');"
                "time.sleep(0.8);"
                "pathlib.Path(sys.argv[1]).write_text('alive')"
            )
            parent_code = (
                "import pathlib,subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child_code!r},sys.argv[1],sys.argv[2]]);"
                "deadline=time.monotonic()+2;"
                "ready=pathlib.Path(sys.argv[2]);"
                "\nwhile not ready.exists() and time.monotonic() < deadline: time.sleep(0.01)"
            )
            runner = SubprocessPhaseRunner(terminate_grace_s=0.1)
            outcome = runner.run_phase(
                PhaseSpec(
                    PhaseName.AGENT,
                    (sys.executable, "-c", parent_code, str(marker), str(ready)),
                    timeout_s=2,
                )
            )
            self.assertEqual(outcome.status, PhaseStatus.PASSED)
            self.assertEqual(outcome.termination, "SIGTERM+SIGKILL")
            time.sleep(1.0)
            self.assertFalse(marker.exists(), "orphan descendant escaped cleanup")

    def test_interruption_still_runs_reset_and_preserves_partial_outcomes(self):
        class InterruptingRunner(SubprocessPhaseRunner):
            def run_phase(inner_self, spec):
                if spec.name == PhaseName.AGENT:
                    raise KeyboardInterrupt("cancelled")
                return super().run_phase(spec)

        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "reset-ran"
            runner = InterruptingRunner()
            with self.assertRaises(KeyboardInterrupt) as caught:
                runner.run(
                    setup=phase(PhaseName.SETUP, "pass"),
                    agent=phase(PhaseName.AGENT, "pass"),
                    verifier=phase(PhaseName.VERIFIER, "pass"),
                    reset=phase(
                        PhaseName.RESET,
                        f"from pathlib import Path; Path({str(marker)!r}).write_text('yes')",
                    ),
                )
            self.assertTrue(marker.is_file())
            partial = caught.exception.native_phase_run
            self.assertEqual(
                [item.name for item in partial.outcomes],
                [PhaseName.SETUP, PhaseName.RESET],
            )
            self.assertTrue(partial.outcome(PhaseName.RESET).passed)

    def test_spawn_error_is_recorded_not_raised(self):
        runner = SubprocessPhaseRunner()
        outcome = runner.run_phase(
            PhaseSpec(PhaseName.SETUP, ("/definitely/missing/binary",), 1)
        )
        self.assertEqual(outcome.status, PhaseStatus.SPAWN_ERROR)
        self.assertIsNone(outcome.exit_code)
        self.assertIn("FileNotFoundError", outcome.error)

    def test_output_is_bounded(self):
        runner = SubprocessPhaseRunner(output_limit_bytes=1024)
        outcome = runner.run_phase(
            phase(PhaseName.VERIFIER, "print('x' * (1024 * 1024))")
        )
        self.assertTrue(outcome.stdout.startswith("[output truncated]\n"))
        self.assertLessEqual(
            len(outcome.stdout.encode("utf-8")),
            len("[output truncated]\n".encode("utf-8")) + 1024,
        )


if __name__ == "__main__":
    unittest.main()
