from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from msys_tools import dev, remote_shield


def returned(payload: dict) -> dict:
    return {
        "welcome": {"type": "welcome", "component": "public"},
        "response": {"type": "return", "id": 1, "payload": payload},
    }


def role_catalog(
    *,
    active: str | None,
    preferred: str | None = "org.vendor.shield:main",
    active_state: str = "ready",
) -> dict:
    candidates = []
    for component in dict.fromkeys(
        item for item in (preferred, active) if item is not None
    ):
        candidates.append({
            "component": component,
            "state": active_state if component == active else "declared",
            "priority": 50,
        })
    return returned({
        "roles": [{
            "role": "screen-shield",
            "active": active,
            "preferred": preferred,
            "candidates": candidates,
        }]
    })


def shield_status(visible: bool, *, changed: bool) -> dict:
    return returned({
        "schema": remote_shield.STATUS_SCHEMA,
        "visible": visible,
        "revision": 4,
        "touch_dismiss_enabled": True,
        "last_reason": "rpc-show" if visible else "rpc-hide",
        "changed": changed,
    })


class RemoteShieldTransactionTests(unittest.TestCase):
    def test_show_starts_selected_provider_then_calls_typed_role(self) -> None:
        provider = "org.vendor.shield:main"
        with mock.patch.object(
            remote_shield,
            "call",
            side_effect=[
                role_catalog(active=None, preferred=provider),
                returned({"component": provider, "state": "ready"}),
                shield_status(True, changed=True),
            ],
        ) as call:
            result = remote_shield.control_shield(
                "/tmp/msys-main", "show", timeout=5
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], provider)
        self.assertTrue(result["status"]["visible"])
        self.assertEqual(
            [(row.args[1], row.args[2], row.args[3]) for row in call.call_args_list],
            [
                ("msys.core", "list_roles", {}),
                ("msys.core", "start", {"component": provider}),
                ("role:screen-shield", "show", {}),
            ],
        )
        self.assertNotIn("org.msys.shell.pyside", str(call.call_args_list))
        self.assertTrue(all(row.kwargs["idempotent"] for row in call.call_args_list))

    def test_show_uses_active_provider_ahead_of_preference(self) -> None:
        active = "org.vendor.running:shield"
        preferred = "org.vendor.preferred:shield"
        with mock.patch.object(
            remote_shield,
            "call",
            side_effect=[
                role_catalog(active=active, preferred=preferred),
                returned({"component": active, "state": "ready"}),
                shield_status(True, changed=False),
            ],
        ) as call:
            result = remote_shield.control_shield(
                "/tmp/msys-main", "show", timeout=5
            )

        self.assertEqual(result["provider"], active)
        self.assertEqual(
            call.call_args_list[1].args[3], {"component": active}
        )

    def test_hide_running_provider_only_calls_typed_hide(self) -> None:
        provider = "org.vendor.shield:main"
        with mock.patch.object(
            remote_shield,
            "call",
            side_effect=[
                role_catalog(active=provider, preferred=provider),
                shield_status(False, changed=True),
            ],
        ) as call:
            result = remote_shield.control_shield(
                "/tmp/msys-main", "hide", timeout=5
            )

        self.assertFalse(result["already_hidden"])
        self.assertEqual(
            [(row.args[1], row.args[2]) for row in call.call_args_list],
            [("msys.core", "list_roles"), ("role:screen-shield", "hide")],
        )
        self.assertNotIn("start", [row.args[2] for row in call.call_args_list])
        self.assertNotIn("stop", [row.args[2] for row in call.call_args_list])

    def test_hide_without_running_provider_is_an_explicit_noop(self) -> None:
        provider = "org.vendor.shield:main"
        with mock.patch.object(
            remote_shield,
            "call",
            return_value=role_catalog(active=None, preferred=provider),
        ) as call:
            result = remote_shield.control_shield(
                "/tmp/msys-main", "hide", timeout=5
            )

        self.assertEqual(call.call_count, 1)
        self.assertTrue(result["already_hidden"])
        self.assertFalse(result["provider_running"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["reason"], "provider-not-running")
        self.assertEqual(result["provider"], provider)

    def test_every_terminal_payload_is_verified(self) -> None:
        provider = "org.vendor.shield:main"
        cases = [
            (
                [
                    role_catalog(active=None, preferred=provider),
                    returned({"component": provider, "state": "starting"}),
                ],
                "PROVIDER_NOT_READY",
            ),
            (
                [
                    role_catalog(active=None, preferred=provider),
                    returned({"component": provider, "state": "ready"}),
                    shield_status(False, changed=True),
                ],
                "SHIELD_STATE_MISMATCH",
            ),
            (
                [{"welcome": {"type": "not-welcome"}, "response": {}}],
                "MALFORMED_WELCOME",
            ),
        ]
        for replies, code in cases:
            with self.subTest(code=code), mock.patch.object(
                remote_shield, "call", side_effect=replies
            ):
                with self.assertRaises(remote_shield.ShieldControlError) as raised:
                    remote_shield.control_shield(
                        "/tmp/msys-main", "show", timeout=5
                    )
            self.assertEqual(raised.exception.code, code)

    def test_remote_cli_returns_structured_nonzero_rpc_error(self) -> None:
        failure = {
            "welcome": {"type": "welcome"},
            "response": {
                "type": "error",
                "id": 1,
                "code": "NO_PROVIDER",
                "message": "screen-shield",
            },
        }
        output = io.StringIO()
        with (
            mock.patch.object(remote_shield, "call", return_value=failure),
            redirect_stdout(output),
        ):
            status = remote_shield.main([
                "show", "--runtime-dir", "/tmp/msys-main", "--timeout", "5"
            ])

        result = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NO_PROVIDER")


class ShieldWorkstationCommandTests(unittest.TestCase):
    def context(self) -> dev.Context:
        return dev.Context(
            Path("/workspace"),
            "root@device",
            "/opt/msys-dev",
            "/opt/msys-dev/.runtime/python/bin/python3",
            ssh_key=None,
        )

    @staticmethod
    def terminal(action: str) -> dict:
        visible = action == "show"
        return {
            "schema": dev.SHIELD_CONTROL_SCHEMA,
            "action": action,
            "ok": True,
            "role": "screen-shield",
            "provider": "org.vendor.shield:main",
            "provider_running": True,
            "provider_ready": True,
            "changed": True,
            "already_hidden": False,
            "status": {
                "schema": dev.SHIELD_STATUS_SCHEMA,
                "visible": visible,
            },
        }

    def test_workstation_command_invokes_helper_and_validates_result(self) -> None:
        completed = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(self.terminal("show"))
        )
        output = io.StringIO()
        with (
            mock.patch.object(dev, "ssh_capture", return_value=completed) as capture,
            redirect_stdout(output),
        ):
            status = dev.command_shield(
                self.context(), "/tmp/msys-main", "show", timeout=7
            )

        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output.getvalue())["status"]["visible"])
        command = capture.call_args.args[1]
        self.assertIn("-m msys_tools.remote_shield", command)
        self.assertIn("'show' --runtime-dir '/tmp/msys-main' --timeout 7", command)

    def test_workstation_command_rejects_false_or_mismatched_terminal(self) -> None:
        bad = self.terminal("show")
        bad["status"]["visible"] = False
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(bad))
        stderr = io.StringIO()
        with (
            mock.patch.object(dev, "ssh_capture", return_value=completed),
            redirect_stderr(stderr),
        ):
            status = dev.command_shield(
                self.context(), "/tmp/msys-main", "show", timeout=7
            )

        self.assertEqual(status, 1)
        self.assertIn("invalid terminal result", stderr.getvalue())

    def test_cli_routes_action_runtime_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing_config = Path(temporary) / "missing.json"
            with (
                mock.patch.object(dev, "CONFIG_PATH", missing_config),
                mock.patch.dict("os.environ", {"MSYS_DEV_TARGET": "root@device"}),
                mock.patch.object(dev, "command_shield", return_value=0) as command,
            ):
                status = dev.main([
                    "shield",
                    "hide",
                    "--runtime-dir",
                    "/tmp/custom-msys",
                    "--timeout",
                    "9",
                ])

        self.assertEqual(status, 0)
        self.assertEqual(
            command.call_args.args[1:3], ("/tmp/custom-msys", "hide")
        )
        self.assertEqual(command.call_args.kwargs, {"timeout": 9.0})


if __name__ == "__main__":
    unittest.main()
