from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from msys_tools import dev, remote_ctl, remote_lifecycle


def rpc_result(payload: dict) -> dict:
    return {
        "welcome": {"type": "welcome"},
        "response": {"type": "return", "id": 1, "payload": payload},
    }


class RuntimeReadinessTests(unittest.TestCase):
    def test_runtime_argument_matching_is_absolute_exact_and_normalised(self) -> None:
        self.assertEqual(
            remote_lifecycle._normalise_runtime_argument("/tmp/msys-main/"),
            "/tmp/msys-main",
        )
        self.assertIsNone(
            remote_lifecycle._normalise_runtime_argument("tmp/msys-main")
        )
        self.assertIsNone(
            remote_lifecycle._normalise_runtime_argument("/tmp/../other")
        )

    def test_only_eager_background_role_provider_is_critical(self) -> None:
        components = [
            {
                "id": "org.example:preferred",
                "lifecycle": "background",
                "state": "handshaking",
                "provides": [
                    {"kind": "role", "name": "display-output", "exclusive": True}
                ],
            },
            {
                "id": "org.example:fallback",
                "lifecycle": "background",
                "state": "declared",
                "provides": [
                    {"kind": "role", "name": "display-output", "exclusive": True}
                ],
            },
            {
                "id": "org.example:worker",
                "lifecycle": "background",
                "state": "ready",
                "provides": [],
            },
        ]
        roles = [
            {
                "role": "display-output",
                "preferred": "org.example:preferred",
                "active": None,
            }
        ]

        critical, issues = remote_lifecycle._critical_component_issues(
            components, roles
        )

        self.assertEqual(
            critical, ["org.example:preferred", "org.example:worker"]
        )
        self.assertEqual(
            {issue["code"] for issue in issues},
            {"ROLE_NOT_ACTIVE", "COMPONENT_NOT_READY"},
        )

    def test_status_is_healthy_only_after_core_rpc_and_critical_readiness(self) -> None:
        components = [{
            "id": "org.example:worker",
            "lifecycle": "background",
            "state": "ready",
            "provides": [],
        }]
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(remote_lifecycle, "runtime_processes", return_value=[321]),
            mock.patch.object(remote_lifecycle, "_socket_kind", return_value="socket"),
            mock.patch.object(
                remote_lifecycle,
                "call",
                side_effect=[
                    rpc_result({"components": components}),
                    rpc_result({"roles": []}),
                ],
            ),
        ):
            result = remote_lifecycle.runtime_status(Path(temporary))

        self.assertTrue(result["healthy"])
        self.assertEqual(result["core_rpc"], "ready")
        self.assertEqual(result["critical_components"], ["org.example:worker"])

    def test_stop_signals_only_runtime_pids_and_removes_dead_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            control = runtime / "control.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(control))
            listener.close()
            with (
                mock.patch.object(
                    remote_lifecycle,
                    "runtime_processes",
                    side_effect=[[4321], []],
                ),
                mock.patch.object(remote_lifecycle.os, "kill") as kill,
            ):
                status, result = remote_lifecycle.stop_runtime(runtime, 1.0, 0.01)

        self.assertEqual(status, 0)
        kill.assert_called_once_with(4321, remote_lifecycle.signal.SIGTERM)
        self.assertTrue(result["cleaned_stale_socket"])
        self.assertFalse(control.exists())

    def test_status_command_is_structured_and_nonzero_when_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            with redirect_stdout(output):
                status = remote_lifecycle.main(
                    ["status", "--runtime-dir", temporary]
                )
        document = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertFalse(document["healthy"])
        self.assertEqual(document["schema"], remote_lifecycle.STATUS_SCHEMA)
        self.assertIn("DAEMON_NOT_RUNNING", {row["code"] for row in document["issues"]})


class RunStopCommandTests(unittest.TestCase):
    def context(self, root: Path) -> dev.Context:
        return dev.Context(
            root,
            "root@example",
            "/opt/msys-dev",
            "/opt/msys-dev/.runtime/python/bin/python3",
            ssh_key=None,
        )

    def test_run_fails_before_spawn_when_native_policy_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = self.context(Path(temporary))
            missing = subprocess.CompletedProcess([], 78, stdout="missing native policy\n")
            stderr = io.StringIO()
            with (
                mock.patch.object(dev, "ssh_capture", return_value=missing) as capture,
                redirect_stderr(stderr),
            ):
                status = dev.command_run(
                    context,
                    "mobile-spi",
                    "/tmp/msys-main",
                    "/tmp/msysd.log",
                    context.remote_python,
                )
        self.assertEqual(status, 78)
        self.assertEqual(capture.call_count, 1)
        self.assertIn("missing native policy", stderr.getvalue())

    def test_stop_and_status_delegate_to_runtime_scoped_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = self.context(Path(temporary))
            success = subprocess.CompletedProcess([], 0, stdout='{"stopped":true}\n')
            unhealthy = subprocess.CompletedProcess([], 1, stdout='{"healthy":false}\n')
            with mock.patch.object(
                dev, "ssh_capture", side_effect=[success, unhealthy]
            ) as capture:
                self.assertEqual(dev.command_stop(context, "/tmp/one", 7), 0)
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(dev.command_status(context, "/tmp/two"), 1)
        stop_command = capture.call_args_list[0].args[1]
        status_command = capture.call_args_list[1].args[1]
        self.assertIn("remote_lifecycle 'stop'", stop_command)
        self.assertIn("--runtime-dir '/tmp/one'", stop_command)
        self.assertNotIn("pgrep", stop_command)
        self.assertIn("remote_lifecycle 'status'", status_command)

    def test_json_inventory_commands_propagate_remote_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = self.context(Path(temporary))
            with mock.patch.object(
                dev, "remote_control_command", side_effect=[17, 19]
            ):
                components = dev.command_components(
                    context, "/tmp/msys-main", as_json=True
                )
                roles = dev.command_roles(
                    context, "/tmp/msys-main", as_json=True
                )

        self.assertEqual(components, 17)
        self.assertEqual(roles, 19)


class DisplayMigrationWaitTests(unittest.TestCase):
    def _initial(self) -> dict:
        return rpc_result({
            "migration": {
                "schema": "msys.display-migration.v1",
                "id": 7,
                "phase": "planned",
            }
        })

    def _status(self, phase: str) -> dict:
        return rpc_result({
            "migration": {
                "schema": "msys.display-migration.v1",
                "id": 7,
                "phase": phase,
            }
        })

    def test_remote_ctl_waits_until_migration_succeeds(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(
                remote_ctl,
                "call",
                side_effect=[self._initial(), self._status("switching"), self._status("succeeded")],
            ),
            mock.patch.object(remote_ctl.time, "sleep"),
            redirect_stdout(output),
        ):
            status = remote_ctl.main([
                "--runtime-dir", "/tmp/msys-main",
                "--method", "select_role",
                "--payload", '{"role":"display-output","provider":"org.example:new"}',
                "--wait-display-migration",
                "--timeout", "5",
            ])
        document = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(
            document["migration_wait"]["migration"]["phase"], "succeeded"
        )

    def test_rolled_back_migration_is_a_failed_command(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(
                remote_ctl,
                "call",
                side_effect=[self._initial(), self._status("rolled-back")],
            ),
            redirect_stdout(output),
        ):
            status = remote_ctl.main([
                "--runtime-dir", "/tmp/msys-main",
                "--method", "reset_role",
                "--payload", '{"role":"display-output"}',
                "--wait-display-migration",
                "--timeout", "5",
            ])
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(output.getvalue())["migration_wait"]["ok"])

    def test_wait_rejects_a_status_for_another_migration(self) -> None:
        unrelated = rpc_result({
            "migration": {
                "schema": remote_ctl.DISPLAY_MIGRATION_SCHEMA,
                "id": 8,
                "phase": "succeeded",
            }
        })
        with mock.patch.object(remote_ctl, "call", return_value=unrelated):
            result = remote_ctl.wait_display_migration(
                "/tmp/msys-main", 7, 5
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["migration_id"], 7)
        self.assertIn("expected 7", result["error"])

    def test_wait_rejects_an_incompatible_schema_and_reports_timeout(self) -> None:
        incompatible = rpc_result({
            "migration": {
                "schema": "msys.display-migration.v0",
                "id": 7,
                "phase": "succeeded",
            }
        })
        with mock.patch.object(remote_ctl, "call", return_value=incompatible):
            rejected = remote_ctl.wait_display_migration(
                "/tmp/msys-main", 7, 5
            )
        timed_out = remote_ctl.wait_display_migration(
            "/tmp/msys-main", 7, 0
        )

        self.assertFalse(rejected["ok"])
        self.assertIn("unexpected display migration schema", rejected["error"])
        self.assertFalse(timed_out["ok"])
        self.assertIn("did not finish", timed_out["error"])

    def test_role_reply_contract_is_checked_before_status_polling(self) -> None:
        invalid_initial = rpc_result({
            "migration": {
                "schema": "msys.display-migration.v0",
                "id": 7,
                "phase": "planned",
            }
        })
        output = io.StringIO()
        with (
            mock.patch.object(
                remote_ctl, "call", return_value=invalid_initial
            ) as call,
            redirect_stdout(output),
        ):
            status = remote_ctl.main([
                "--runtime-dir", "/tmp/msys-main",
                "--method", "select_role",
                "--payload", '{"role":"display-output","provider":"org.example:new"}',
                "--wait-display-migration",
                "--timeout", "5",
            ])

        self.assertEqual(status, 1)
        self.assertEqual(call.call_count, 1)
        self.assertIn(
            "unexpected display migration schema",
            json.loads(output.getvalue())["migration_wait"]["error"],
        )


class TypedAgentRequestTests(unittest.TestCase):
    def context(self, root: Path) -> dev.Context:
        return dev.Context(
            root,
            "root@example",
            "/opt/msys-dev",
            "/opt/msys-dev/.runtime/python/bin/python3",
            ssh_key=None,
        )

    def test_ok_false_typed_result_is_nonzero(self) -> None:
        response = {
            "type": "return",
            "id": 1,
            "payload": {
                "schema": dev.INSTALL_AGENT_RESULT_SCHEMA,
                "operation": "apply_updates",
                "ok": False,
                "result": {"errors": ["health failed"]},
            },
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(response))
        with tempfile.TemporaryDirectory() as temporary:
            context = self.context(Path(temporary))
            with (
                mock.patch.object(dev, "remote_control_command", return_value=completed),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                status = dev._typed_agent_request(
                    context,
                    "/tmp/msys-main",
                    target="role:update-agent",
                    method="apply_updates",
                    payload={"source": "https://example.invalid/index.json"},
                    operation="apply_updates",
                )
        self.assertEqual(status, 1)

    def test_registry_check_and_apply_route_to_their_typed_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = self.context(Path(temporary))
            with (
                mock.patch.object(
                    dev, "_typed_agent_request", return_value=0
                ) as request,
                mock.patch.object(dev, "command_broadcast") as broadcast,
            ):
                self.assertEqual(
                    dev.command_registry(context, "/tmp/msys-main"), 0
                )
                self.assertEqual(
                    dev.command_check_update(
                        context,
                        "/tmp/msys-main",
                        "https://example.invalid/index.json",
                        "org.example.app",
                    ),
                    0,
                )
                self.assertEqual(
                    dev.command_apply_update(
                        context,
                        "/tmp/msys-main",
                        "https://example.invalid/index.json",
                        "org.example.app",
                        False,
                    ),
                    0,
                )

        self.assertEqual(
            [row.kwargs["target"] for row in request.call_args_list],
            ["role:install-agent", "role:update-agent", "role:update-agent"],
        )
        self.assertEqual(
            [row.kwargs["method"] for row in request.call_args_list],
            ["registry", "check_updates", "apply_updates"],
        )
        self.assertEqual(
            [row.kwargs["operation"] for row in request.call_args_list],
            ["registry", "check_updates", "apply_updates"],
        )
        broadcast.assert_not_called()

    def test_typed_success_survives_ssh_diagnostics_and_nested_json(self) -> None:
        response = {
            "type": "return",
            "id": 1,
            "payload": {
                "schema": dev.INSTALL_AGENT_RESULT_SCHEMA,
                "operation": "rollback",
                "ok": True,
                "result": {"nested": {"value": 1}},
            },
        }
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout="SSH diagnostic without JSON\n" + json.dumps(response, indent=2),
        )
        with tempfile.TemporaryDirectory() as temporary:
            context = self.context(Path(temporary))
            with (
                mock.patch.object(dev, "remote_control_command", return_value=completed),
                redirect_stdout(io.StringIO()),
            ):
                status = dev._typed_agent_request(
                    context,
                    "/tmp/msys-main",
                    target="role:install-agent",
                    method="rollback",
                    payload={"package": "org.example.app"},
                    operation="rollback",
                )
        self.assertEqual(status, 0)

    def test_nonzero_remote_command_cannot_be_mistaken_for_typed_success(self) -> None:
        response = {
            "type": "return",
            "id": 1,
            "payload": {
                "schema": dev.INSTALL_AGENT_RESULT_SCHEMA,
                "operation": "registry",
                "ok": True,
            },
        }
        completed = subprocess.CompletedProcess(
            [], 255, stdout=json.dumps(response)
        )
        with tempfile.TemporaryDirectory() as temporary:
            context = self.context(Path(temporary))
            with (
                mock.patch.object(
                    dev, "remote_control_command", return_value=completed
                ),
                redirect_stderr(io.StringIO()),
            ):
                status = dev._typed_agent_request(
                    context,
                    "/tmp/msys-main",
                    target="role:install-agent",
                    method="registry",
                    payload={},
                    operation="registry",
                )

        self.assertEqual(status, 255)

    def test_install_archive_stages_hash_and_uses_install_agent_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "package.tar.gz"
            archive.write_bytes(b"verified archive")
            context = self.context(root)
            details = {
                "package": "org.example.app",
                "version": "1.2.3",
                "content_sha256": "c" * 64,
            }
            with (
                mock.patch.object(dev, "validate_package", return_value=details),
                mock.patch.object(dev, "ssh") as ssh,
                mock.patch.object(dev, "run_local") as upload,
                mock.patch.object(dev, "_typed_agent_request", return_value=0) as request,
                redirect_stdout(io.StringIO()),
            ):
                status = dev.command_install_archive(
                    context,
                    "/tmp/msys-main",
                    archive,
                    state_dir="/srv/msys-state",
                )
        self.assertEqual(status, 0)
        self.assertEqual(ssh.call_count, 2)
        self.assertEqual(upload.call_count, 1)
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["target"], "role:install-agent")
        self.assertEqual(kwargs["method"], "install_archive")
        self.assertTrue(kwargs["payload"]["remote"])
        self.assertTrue(kwargs["payload"]["require_content_hashes"])
        self.assertTrue(kwargs["payload"]["path"].startswith("/srv/msys-state/updates/staged-rpc/"))
        self.assertEqual(len(kwargs["payload"]["sha256"]), 64)
        self.assertIn(
            "chmod 0700 '/srv/msys-state/updates/staged-rpc'",
            ssh.call_args_list[1].args[1],
        )

    def test_delivery_reuses_build_identity_and_archive_hash_without_revalidating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "package.maf"
            archive.write_bytes(b"already verified by build_package")
            built = {
                "artifact": str(archive),
                "package": "org.example.app",
                "version": "1.2.3",
                "sha256": "a" * 64,
                "content_sha256": "c" * 64,
            }
            with (
                mock.patch.object(dev, "validate_package") as validate,
                mock.patch.object(dev, "_file_sha256") as hash_file,
                mock.patch.object(dev, "ssh"),
                mock.patch.object(dev, "run_local"),
                mock.patch.object(dev, "_typed_agent_request", return_value=0) as request,
                redirect_stdout(io.StringIO()),
            ):
                status = dev.command_install_archive(
                    self.context(root),
                    "/tmp/msys-main",
                    archive,
                    built=built,
                )

        self.assertEqual(status, 0)
        validate.assert_not_called()
        hash_file.assert_not_called()
        self.assertEqual(request.call_args.kwargs["payload"]["sha256"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
