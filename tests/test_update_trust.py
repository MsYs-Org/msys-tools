from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from msys_tools import dev
from msys_tools.package_flow import (
    generate_update_signing_key,
    inspect_update_public_key,
    sign_update_index_file,
)


WORKSPACE = Path(__file__).resolve().parents[2]


class UpdatePublisherToolTests(unittest.TestCase):
    @staticmethod
    def context() -> dev.Context:
        return dev.Context(
            WORKSPACE,
            "root@example",
            "/opt/msys-dev",
            "/opt/msys-dev/.runtime/python/bin/python3",
            ssh_key=None,
        )

    @staticmethod
    def write_index(path: Path) -> None:
        path.write_text(
            json.dumps({
                "schema": "msys.update-index.v1",
                "packages": [{
                    "id": "org.example.publisher",
                    "version": "1.0.0",
                    "artifact": "org.example.publisher-1.0.0.maf",
                    "sha256": "12" * 32,
                }],
            }),
            encoding="utf-8",
        )

    def test_generate_and_sign_index_keep_private_material_local(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = root / "publisher.private.json"
            public_key = root / "publisher.public.json"
            generated = generate_update_signing_key(
                WORKSPACE, private_key, public_key
            )
            private_document = json.loads(private_key.read_text(encoding="utf-8"))
            public_document = json.loads(public_key.read_text(encoding="utf-8"))
            self.assertIn("private_seed", private_document)
            self.assertNotIn("private_seed", public_document)
            self.assertEqual(generated["key_id"], public_document["key_id"])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(private_key.stat().st_mode), 0o600)

            index = root / "index.json"
            signed = root / "signed-index.json"
            self.write_index(index)
            result = sign_update_index_file(
                WORKSPACE,
                index,
                private_key,
                sequence=42,
                expires="2099-01-01T00:00:00Z",
                output=signed,
            )
            document = json.loads(signed.read_text(encoding="utf-8"))
            self.assertEqual(result["sequence"], 42)
            self.assertEqual(document["signature"]["algorithm"], "Ed25519")
            self.assertEqual(document["signature"]["key_id"], generated["key_id"])
            self.assertNotIn(private_document["private_seed"], signed.read_text())

            inspected = inspect_update_public_key(WORKSPACE, public_key)
            self.assertEqual(inspected["key_id"], generated["key_id"])
            self.assertEqual(inspected["size"], 32)

    def test_cli_generate_and_sign_are_local_only_without_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = root / "publisher.private.json"
            public_key = root / "publisher.public.json"
            index = root / "index.json"
            self.write_index(index)
            with (
                mock.patch.object(dev, "CONFIG_PATH", root / "missing-config.json"),
                redirect_stdout(io.StringIO()),
            ):
                generated = dev.main([
                    "update-trust",
                    "generate",
                    "--private",
                    str(private_key),
                    "--public",
                    str(public_key),
                    "--root",
                    str(WORKSPACE),
                ])
                signed = dev.main([
                    "update-trust",
                    "sign-index",
                    str(index),
                    "--private",
                    str(private_key),
                    "--sequence",
                    "7",
                    "--expires",
                    "2099-01-01T00:00:00Z",
                    "--root",
                    str(WORKSPACE),
                ])

        self.assertEqual(generated, 0)
        self.assertEqual(signed, 0)

    def test_install_public_uploads_only_strict_public_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = root / "publisher.private.json"
            public_key = root / "publisher.public.json"
            generate_update_signing_key(WORKSPACE, private_key, public_key)
            completed = subprocess.CompletedProcess([], 0, stdout="")
            with (
                mock.patch.object(dev, "ssh", return_value=completed) as ssh,
                mock.patch.object(dev, "run_local") as upload,
                redirect_stdout(io.StringIO()),
            ):
                status = dev.command_install_update_public_key(
                    self.context(),
                    public_key,
                    state_dir="/srv/msys-state",
                )

            self.assertEqual(status, 0)
            self.assertEqual(ssh.call_count, 2)
            upload_argv = upload.call_args.args[0]
            self.assertIn(str(public_key), upload_argv)
            self.assertNotIn(str(private_key), upload_argv)
            remote_commands = "\n".join(call.args[1] for call in ssh.call_args_list)
            self.assertIn("msys_install.cli install-public-key", remote_commands)
            self.assertIn("/msys-install:/opt/msys-dev/msys-sdk", remote_commands)
            self.assertIn("trap cleanup EXIT", remote_commands)
            self.assertIn("/srv/msys-state", remote_commands)
            self.assertNotIn(str(private_key), remote_commands)
            private_seed = json.loads(private_key.read_text())["private_seed"]
            self.assertNotIn(private_seed, remote_commands)

            stderr = io.StringIO()
            with (
                mock.patch.object(dev, "ssh") as rejected_ssh,
                mock.patch.object(dev, "run_local") as rejected_upload,
                redirect_stderr(stderr),
            ):
                rejected = dev.command_install_update_public_key(
                    self.context(), private_key
                )
            self.assertEqual(rejected, 2)
            rejected_ssh.assert_not_called()
            rejected_upload.assert_not_called()
            self.assertIn("public key", stderr.getvalue())

    def test_unsigned_override_is_forwarded_only_when_explicit(self) -> None:
        context = self.context()
        with mock.patch.object(
            dev, "_typed_agent_request", return_value=0
        ) as request:
            dev.command_check_update(
                context,
                "/tmp/msys-main",
                "/tmp/index.json",
            )
            dev.command_check_update(
                context,
                "/tmp/msys-main",
                "/tmp/index.json",
                allow_unsigned=True,
            )
            dev.command_apply_update(
                context,
                "/tmp/msys-main",
                "/tmp/index.json",
                None,
                False,
                True,
            )

        self.assertNotIn("allow_unsigned", request.call_args_list[0].kwargs["payload"])
        self.assertIs(
            request.call_args_list[1].kwargs["payload"]["allow_unsigned"], True
        )
        self.assertIs(
            request.call_args_list[2].kwargs["payload"]["allow_unsigned"], True
        )


if __name__ == "__main__":
    unittest.main()
