from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from msys_tools import dev
from msys_tools.remote_visual_smoke import run_visual_smoke


COMPONENT = "org.msys.apps:calculator"


def returned(payload: dict) -> dict:
    return {"welcome": {}, "response": {"type": "return", "id": 1, "payload": payload}}


class VisualSmokeFlowTests(unittest.TestCase):
    def test_clean_flow_uses_only_typed_core_and_window_manager_calls(self) -> None:
        replies = iter([
            returned({
                "components": [{
                    "id": COMPONENT,
                    "launchable": True,
                    "lifecycle": "manual",
                    "state": "declared",
                }]
            }),
            returned({"windows": []}),
            returned({"windows": []}),
            returned({"ok": True}),
            returned({"component": COMPONENT, "state": "ready", "activation": {"ok": True}}),
            returned({"windows": [{"component": COMPONENT, "id": "window-1"}]}),
            returned({"ok": True, "closed_component": COMPONENT}),
            returned({"windows": []}),
            returned({"ok": True}),
        ])
        calls: list[tuple[str, str, dict]] = []

        def rpc(_runtime: str, target: str, method: str, payload: dict, **_kwargs: object) -> dict:
            calls.append((target, method, payload))
            return next(replies)

        status, document = run_visual_smoke(
            "/tmp/msys-main", COMPONENT, rpc_call=rpc
        )

        self.assertEqual(status, 0)
        self.assertTrue(document["ok"])
        self.assertTrue(document["restored"])
        self.assertEqual(
            [method for _target, method, _payload in calls],
            [
                "list_components",
                "foreground_stack",
                "recents",
                "home",
                "start",
                "recents",
                "back",
                "recents",
                "home",
            ],
        )
        self.assertEqual({target for target, _method, _payload in calls}, {
            "msys.core", "role:window-manager"
        })

    def test_failed_back_stops_test_app_and_restores_home(self) -> None:
        replies = iter([
            returned({
                "components": [{
                    "id": COMPONENT,
                    "launchable": True,
                    "lifecycle": "manual",
                    "state": "stopped",
                }]
            }),
            returned({"windows": []}),
            returned({"windows": []}),
            returned({"ok": True}),
            returned({"component": COMPONENT, "state": "ready", "activation": {"ok": True}}),
            returned({"windows": [{"component": COMPONENT}]}),
            {"welcome": {}, "response": {
                "type": "error", "id": 1, "code": "WM_FAILED", "message": "test"
            }},
            returned({"component": COMPONENT, "state": "stopped"}),
            returned({"ok": True}),
        ])
        calls: list[tuple[str, str, dict]] = []

        def rpc(_runtime: str, target: str, method: str, payload: dict, **_kwargs: object) -> dict:
            calls.append((target, method, payload))
            return next(replies)

        status, document = run_visual_smoke(
            "/tmp/msys-main", COMPONENT, rpc_call=rpc
        )

        self.assertEqual(status, 1)
        self.assertFalse(document["ok"])
        self.assertTrue(document["restored"])
        self.assertIn(("msys.core", "stop", {"component": COMPONENT}), calls)
        self.assertEqual(calls[-1][:2], ("role:window-manager", "home"))

    def test_dirty_session_aborts_before_any_mutation(self) -> None:
        replies = iter([
            returned({
                "components": [{
                    "id": COMPONENT,
                    "launchable": True,
                    "lifecycle": "manual",
                    "state": "declared",
                }]
            }),
            returned({"windows": [{"component": "org.example:already-open"}]}),
            returned({"windows": [{"component": "org.example:already-open"}]}),
        ])
        calls: list[tuple[str, str]] = []

        def rpc(_runtime: str, target: str, method: str, _payload: dict, **_kwargs: object) -> dict:
            calls.append((target, method))
            return next(replies)

        status, document = run_visual_smoke(
            "/tmp/msys-main", COMPONENT, rpc_call=rpc
        )

        self.assertEqual(status, 1)
        self.assertTrue(document["restored"])
        self.assertNotIn("components", str(document["steps"][0]["response"]))
        self.assertEqual(
            [method for _target, method in calls],
            ["list_components", "foreground_stack", "recents"],
        )


class VisualSmokeHostCommandTests(unittest.TestCase):
    def test_host_wrapper_invokes_only_remote_typed_helper(self) -> None:
        context = dev.Context(
            Path("/workspace"),
            "root@device",
            "/opt/msys-dev",
            "/opt/msys-dev/.runtime/python/bin/python3",
        )
        completed = subprocess.CompletedProcess([], 0, stdout='{"ok":true}\n')
        with mock.patch.object(dev, "ssh_capture", return_value=completed) as capture:
            status = dev.command_visual_smoke(
                context,
                "/tmp/msys-main",
                COMPONENT,
                timeout=12,
            )

        self.assertEqual(status, 0)
        command = capture.call_args.args[1]
        self.assertIn("msys_tools.remote_visual_smoke", command)
        self.assertIn("'/tmp/msys-main'", command)
        self.assertIn(f"'{COMPONENT}'", command)
        for forbidden in ("xdotool", "--debug-click", "--debug-swipe"):
            self.assertNotIn(forbidden, command)


if __name__ == "__main__":
    unittest.main()
