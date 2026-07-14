from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from msys_tools.remote_x11_debug import (
    X11DebugError,
    main,
    native_argument_candidates,
    native_arguments,
    resolve_display,
)


class DisplayResolutionTests(unittest.TestCase):
    def test_ready_runtime_session_supplies_display_without_board_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            (runtime / "display-session.json").write_text(
                json.dumps(
                    {
                        "schema": "msys.display-session.v1",
                        "state": "ready",
                        "display": ":91.0",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(resolve_display(runtime), ":91.0")

    def test_explicit_display_is_a_strict_recovery_override(self) -> None:
        self.assertEqual(resolve_display(Path("/missing"), ":24"), ":24")
        for value in ("", "24", "localhost:0", ":-1", ":0 extra"):
            with self.subTest(value=value), self.assertRaises(X11DebugError):
                resolve_display(Path("/missing"), value)

    def test_missing_malformed_not_ready_and_symlink_states_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            state = runtime / "display-session.json"
            with self.assertRaises(X11DebugError):
                resolve_display(runtime)
            state.write_text('{"schema":"wrong"}', encoding="utf-8")
            with self.assertRaises(X11DebugError):
                resolve_display(runtime)
            state.unlink()
            target = runtime / "target.json"
            target.write_text(
                '{"schema":"msys.display-session.v1","state":"ready","display":":1"}',
                encoding="utf-8",
            )
            state.symlink_to(target)
            with self.assertRaises(X11DebugError):
                resolve_display(runtime)


class NativeArgumentsTests(unittest.TestCase):
    def test_tap_and_swipe_match_native_xtest_cli(self) -> None:
        tap = argparse.Namespace(
            gesture="tap",
            identity="org.msys.shell.navigation",
            title=None,
            x=50,
            y=20,
        )
        swipe = argparse.Namespace(
            gesture="swipe",
            identity="org.msys.shell.navigation-pill",
            title="MSYS Navigation",
            x1=160,
            y1=34,
            x2=160,
            y2=5,
            duration_ms=220,
        )
        self.assertEqual(
            native_arguments(tap),
            ["--debug-click-identity", "org.msys.shell.navigation", "50", "20"],
        )
        self.assertEqual(
            native_arguments(swipe),
            [
                "--debug-swipe-window",
                "org.msys.shell.navigation-pill",
                "MSYS Navigation",
                "160",
                "34",
                "160",
                "5",
                "220",
            ],
        )

    def test_swipe_supports_identity_title_and_exact_window_selectors(self) -> None:
        common = {
            "gesture": "swipe",
            "x1": 10,
            "y1": 20,
            "x2": 30,
            "y2": 40,
            "duration_ms": 200,
        }
        identity = argparse.Namespace(
            **common,
            identity="org.example.app",
            title=None,
            window=None,
        )
        title = argparse.Namespace(
            **common,
            identity=None,
            title="Legacy App",
            window=None,
        )
        window = argparse.Namespace(
            **common,
            identity=None,
            title=None,
            window=["org.example.app", "Example App"],
        )

        self.assertEqual(
            native_arguments(identity)[:2],
            ["--debug-swipe-identity", "org.example.app"],
        )
        self.assertEqual(
            native_arguments(title)[:2],
            ["--debug-swipe-title", "Legacy App"],
        )
        self.assertEqual(
            native_arguments(window)[:3],
            ["--debug-swipe-window", "org.example.app", "Example App"],
        )

    def test_swipe_rejects_ambiguous_or_missing_selector(self) -> None:
        common = {
            "gesture": "swipe",
            "x1": 10,
            "y1": 20,
            "x2": 30,
            "y2": 40,
            "duration_ms": 200,
        }
        with self.assertRaisesRegex(X11DebugError, "requires"):
            native_arguments(
                argparse.Namespace(
                    **common, identity=None, title=None, window=None
                )
            )
        with self.assertRaisesRegex(X11DebugError, "cannot be combined"):
            native_arguments(
                argparse.Namespace(
                    **common,
                    identity="org.example.app",
                    title=None,
                    window=["org.example.app", "Example App"],
                )
            )

    def test_runner_propagates_native_status_and_injects_resolved_display(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "msys-x11-policy"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(binary.stat().st_mode | 0o100)
            completed = subprocess.CompletedProcess([], 23)
            with mock.patch(
                "msys_tools.remote_x11_debug.subprocess.run",
                return_value=completed,
            ) as run:
                result = main(
                    [
                        "--runtime-dir",
                        str(root),
                        "--binary",
                        str(binary),
                        "--display",
                        ":77",
                        "swipe",
                        "10",
                        "20",
                        "30",
                        "40",
                        "--duration-ms",
                        "200",
                        "--identity",
                        "org.msys.shell.navigation-pill",
                    ]
                )

        self.assertEqual(result, 23)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], str(binary))
        self.assertEqual(run.call_args.kwargs["env"]["DISPLAY"], ":77")
        self.assertEqual(run.call_args.kwargs["env"].get("PATH"), os.environ.get("PATH"))

    def test_role_resolver_uses_the_visible_native_navigation_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "msys-x11-policy"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(binary.stat().st_mode | 0o100)
            windows = subprocess.CompletedProcess(
                [],
                0,
                json.dumps(
                    {
                        "schema": "msys.window-list.v1",
                        "windows": [
                            {
                                "identity": "org.example.foreground",
                                "role": "application",
                                "state": "visible",
                            },
                            {
                                "identity": "org.msys.shell.native.navigation-pill",
                                "role": "navigation-bar",
                                "state": "visible",
                            },
                            {
                                "identity": "org.msys.shell.navigation",
                                "role": "navigation-bar",
                                "state": "hidden",
                            },
                        ],
                    }
                ),
                "",
            )
            gesture = subprocess.CompletedProcess([], 0)
            with mock.patch(
                "msys_tools.remote_x11_debug.subprocess.run",
                side_effect=[windows, gesture],
            ) as run:
                result = main(
                    [
                        "--runtime-dir",
                        str(root),
                        "--binary",
                        str(binary),
                        "--display",
                        ":24",
                        "tap",
                        "267",
                        "459",
                        "--role",
                        "navigation-bar",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], [str(binary), "--list-windows"])
        self.assertEqual(
            run.call_args_list[1].args[0],
            [
                str(binary),
                "--debug-click-identity",
                "org.msys.shell.native.navigation-pill",
                "267",
                "459",
            ],
        )

    def test_role_resolver_supports_pyside_and_swipe(self) -> None:
        args = argparse.Namespace(
            gesture="swipe",
            identity=None,
            title=None,
            window=None,
            role="navigation-bar",
            x1=160,
            y1=34,
            x2=160,
            y2=5,
            duration_ms=220,
        )
        windows = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "schema": "msys.window-list.v1",
                    "windows": [
                        {
                            "identity": "org.msys.shell.navigation",
                            "role": "navigation-bar",
                            "state": "visible",
                        }
                    ],
                }
            ),
            "",
        )
        with mock.patch(
            "msys_tools.remote_x11_debug.subprocess.run", return_value=windows
        ):
            commands = native_argument_candidates(args, Path("/policy"), {"DISPLAY": ":24"})

        self.assertEqual(
            commands,
            (
                [
                    "--debug-swipe-identity",
                    "org.msys.shell.navigation",
                    "160",
                    "34",
                    "160",
                    "5",
                    "220",
                ],
            ),
        )

    def test_old_policy_fallback_is_bounded_to_known_navigation_identities(self) -> None:
        args = argparse.Namespace(
            gesture="tap",
            identity=None,
            title=None,
            window=None,
            role="navigation-bar",
            x=50,
            y=20,
        )
        unsupported = subprocess.CompletedProcess([], 64, "", "usage")
        with mock.patch(
            "msys_tools.remote_x11_debug.subprocess.run", return_value=unsupported
        ):
            commands = native_argument_candidates(args, Path("/policy"), {"DISPLAY": ":24"})

        self.assertEqual(len(commands), 3)
        self.assertEqual(
            [command[1] for command in commands],
            [
                "org.msys.shell.native.navigation-pill",
                "org.msys.shell.navigation",
                "org.msys.shell.navigation-pill",
            ],
        )

    def test_authoritative_window_list_does_not_click_through_when_role_is_hidden(self) -> None:
        args = argparse.Namespace(
            gesture="tap",
            identity=None,
            title=None,
            window=None,
            role="navigation-bar",
            x=50,
            y=20,
        )
        windows = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "schema": "msys.window-list.v1",
                    "windows": [
                        {
                            "identity": "org.msys.shell.navigation",
                            "role": "navigation-bar",
                            "state": "hidden",
                        },
                        {
                            "identity": "org.example.app",
                            "role": "application",
                            "state": "visible",
                        },
                    ],
                }
            ),
            "",
        )
        with mock.patch(
            "msys_tools.remote_x11_debug.subprocess.run", return_value=windows
        ), self.assertRaisesRegex(X11DebugError, "no visible X11 window"):
            native_argument_candidates(args, Path("/policy"), {"DISPLAY": ":24"})


if __name__ == "__main__":
    unittest.main()
