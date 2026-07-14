from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from msys_tools import remote_acceptance as remote


class RemoteAcceptanceTests(unittest.TestCase):
    def components(self) -> list[dict[str, object]]:
        return [
            {
                "id": "org.msys.settings:main",
                "state": "declared",
                "lifecycle": "manual",
                "package_version": "0.2.10",
                "package_root": "/release/settings",
            },
            {
                "id": "org.msys.apps:notes",
                "state": "declared",
                "lifecycle": "manual",
                "package_version": "0.1.8",
            },
            {
                "id": "org.msys.input.touch:keyboard",
                "state": "declared",
                "lifecycle": "on-demand",
                "package_version": "0.1.8",
            },
            {
                "id": "org.msys.shell.native:desktop-shell",
                "state": "ready",
                "lifecycle": "background",
                "package_version": "0.3.4",
            },
            {
                "id": "org.msys.openstick.ch347:x11-spi-touch-output",
                "state": "ready",
                "lifecycle": "background",
                "package_version": "0.1.11",
            },
        ]

    def test_component_categories_preserve_version_state_and_path(self) -> None:
        categories, issues = remote.classify_components(self.components())
        self.assertEqual(issues, [])
        self.assertEqual(categories["settings"][0]["version"], "0.2.10")
        self.assertEqual(categories["settings"][0]["state"], "declared")
        self.assertEqual(categories["settings"][0]["path"], "/release/settings")
        self.assertEqual(categories["display"][0]["version"], "0.1.11")

    def test_unselected_background_alternative_is_reported_without_false_failure(self) -> None:
        components = self.components()
        components[-1]["state"] = "failed"
        categories, issues = remote.classify_components(components)
        self.assertEqual(issues, [])
        self.assertEqual(categories["display"][0]["state"], "failed")

    def test_display_session_requires_ready_installed_provider(self) -> None:
        session = {
            "display": ":24",
            "provider": "org.msys.openstick.ch347:x11-spi-touch-output",
            "geometry": {"width": 320, "height": 480},
        }
        self.assertEqual(remote.validate_display_session(session, self.components()), [])
        session["provider"] = "org.example:missing"
        issues = remote.validate_display_session(session, self.components())
        self.assertEqual(issues[0]["code"], "DISPLAY_PROVIDER_MISSING")

    def test_window_report_checks_exact_identity_without_input_actions(self) -> None:
        payload = {
            "windows": [
                {
                    "id": "w1",
                    "component": "org.msys.shell.native:desktop-shell",
                    "identity": "org.msys.shell.native",
                    "role": "desktop",
                    "state": "visible",
                }
            ]
        }
        with mock.patch.object(remote, "_rpc_payload", return_value=payload) as rpc:
            report, issues = remote.inspect_windows(
                Path("/tmp/msys-main"), ["role=desktop", "title=Settings"]
            )
        self.assertEqual(report["count"], 1)
        self.assertTrue(report["checks"][0]["matched"])
        self.assertFalse(report["checks"][1]["matched"])
        self.assertEqual(issues[-1]["code"], "EXPECTED_WINDOW_MISSING")
        rpc.assert_called_once_with(
            Path("/tmp/msys-main"), "role:window-manager", "list_windows"
        )

    def test_collect_combines_all_read_only_evidence(self) -> None:
        components = self.components()
        session = {
            "display": ":24",
            "provider": "org.msys.openstick.ch347:x11-spi-touch-output",
            "geometry": {"width": 320, "height": 480},
        }
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "msysd.log"
            log.write_text("normal\nwarning: recovered display\n", encoding="utf-8")
            with (
                mock.patch.object(
                    remote,
                    "runtime_status",
                    return_value={
                        "healthy": True,
                        "issues": [],
                        "processes": {"pids": [42]},
                    },
                ),
                mock.patch.object(
                    remote,
                    "_rpc_payload",
                    return_value={"components": components},
                ),
                mock.patch.object(
                    remote, "_load_display_session", return_value=(session, "window-manager")
                ),
                mock.patch.object(
                    remote,
                    "inspect_windows",
                    return_value=(
                        {
                            "available": True,
                            "count": 2,
                            "key_window_count": 2,
                            "checks": [],
                            "items": [],
                        },
                        [],
                    ),
                ),
                mock.patch.object(remote, "resources", return_value={"disk_available_kib": 1}),
                mock.patch.object(remote, "current_release", return_value="test-release"),
            ):
                report = remote.collect(
                    Path("/tmp/msys-main"), log, lines=5, strict_logs=False
                )

        self.assertTrue(report["ok"])
        self.assertEqual(report["release"], "test-release")
        self.assertEqual(report["runtime"]["pids"], [42])
        self.assertEqual(report["display"]["source"], "window-manager")
        self.assertEqual(report["recent_warnings_errors"], ["warning: recovered display"])
        self.assertEqual(report["components"]["shell"][0]["version"], "0.3.4")

    def test_recent_log_events_only_reads_latest_daemon_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "msysd.log"
            log.write_text(
                "msysd: public control socket /tmp/old/control.sock\n"
                "Traceback: historical failure\n"
                "msysd: public control socket /tmp/current/control.sock\n"
                "warning: current recovery\n",
                encoding="utf-8",
            )
            events = remote.recent_log_events(log, lines=10)

        self.assertEqual(events, ["warning: current recovery"])

    def test_recent_log_events_without_session_boundary_keeps_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "msysd.log"
            log.write_text(
                "ERROR: first\nnormal\nwarning: second\n",
                encoding="utf-8",
            )
            events = remote.recent_log_events(log, lines=10)

        self.assertEqual(events, ["ERROR: first", "warning: second"])

    def test_recent_log_events_ignores_normal_isolation_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "msysd.log"
            log.write_text(
                "msysd: public control socket /tmp/msys-main/control.sock\n"
                "msysd: isolation component=org.example:app "
                "failure=fail-closed degraded=False backend=landlock\n",
                encoding="utf-8",
            )
            events = remote.recent_log_events(log, lines=10)

        self.assertEqual(events, [])

    def test_recent_log_events_keeps_abnormal_isolation_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "msysd.log"
            log.write_text(
                "msysd: public control socket /tmp/msys-main/control.sock\n"
                "msysd: isolation component=a failure=fail-closed "
                "degraded=True backend=none\n"
                "msysd: isolation component=b failure=fail-closed "
                "degraded=False error=setup-failed\n",
                encoding="utf-8",
            )
            events = remote.recent_log_events(log, lines=10)

        self.assertEqual(
            events,
            [
                "msysd: isolation component=a failure=fail-closed "
                "degraded=True backend=none",
                "msysd: isolation component=b failure=fail-closed "
                "degraded=False error=setup-failed",
            ],
        )

    def test_strict_logs_turns_matched_lines_into_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "msysd.log"
            log.write_text("ERROR: test\n", encoding="utf-8")
            with (
                mock.patch.object(
                    remote,
                    "runtime_status",
                    return_value={
                        "healthy": True,
                        "issues": [],
                        "processes": {"pids": [1]},
                    },
                ),
                mock.patch.object(
                    remote,
                    "_rpc_payload",
                    return_value={"components": self.components()},
                ),
                mock.patch.object(
                    remote,
                    "_load_display_session",
                    return_value=(
                        {
                            "display": ":24",
                            "provider": "org.msys.openstick.ch347:x11-spi-touch-output",
                            "geometry": {"width": 320, "height": 480},
                        },
                        "window-manager",
                    ),
                ),
                mock.patch.object(
                    remote,
                    "inspect_windows",
                    return_value=(
                        {"available": True, "count": 1, "key_window_count": 1},
                        [],
                    ),
                ),
            ):
                report = remote.collect(
                    Path("/tmp/msys-main"), log, lines=10, strict_logs=True
                )
        self.assertFalse(report["ok"])
        self.assertEqual(report["issues"][-1]["code"], "RECENT_ERROR_LOGS")

    def test_strict_logs_ignores_errors_from_previous_daemon_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "msysd.log"
            log.write_text(
                "ERROR: previous daemon\n"
                "msysd: public control socket /tmp/msys-main/control.sock\n"
                "msysd: ready\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    remote,
                    "runtime_status",
                    return_value={"healthy": True, "issues": [], "processes": {"pids": [1]}},
                ),
                mock.patch.object(remote, "_rpc_payload", return_value={"components": self.components()}),
                mock.patch.object(
                    remote,
                    "_load_display_session",
                    return_value=(
                        {
                            "display": ":24",
                            "provider": "org.msys.openstick.ch347:x11-spi-touch-output",
                            "geometry": {"width": 320, "height": 480},
                        },
                        "window-manager",
                    ),
                ),
                mock.patch.object(
                    remote,
                    "inspect_windows",
                    return_value=({"available": True, "count": 1, "key_window_count": 1}, []),
                ),
            ):
                report = remote.collect(
                    Path("/tmp/msys-main"), log, lines=10, strict_logs=True
                )

        self.assertTrue(report["ok"])
        self.assertEqual(report["recent_warnings_errors"], [])


if __name__ == "__main__":
    unittest.main()
