from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from msys_tools import dev
from msys_tools.dev import build_parser


class CallCommandTests(unittest.TestCase):
    def test_ipc_target_does_not_replace_ssh_target(self) -> None:
        args = build_parser().parse_args([
            "call",
            "interface:org.msys.demo.echo.v1",
            "ping",
            "--target",
            "root@192.0.2.1",
            "--idempotent",
        ])

        self.assertEqual(args.call_target, "interface:org.msys.demo.echo.v1")
        self.assertEqual(args.target, "root@192.0.2.1")
        self.assertTrue(args.idempotent)

    def test_repeatable_fields_build_a_typed_payload_without_json_object_quotes(self) -> None:
        with (
            mock.patch.dict(os.environ, {"MSYS_DEV_TARGET": "root@device"}),
            mock.patch.object(dev, "CONFIG_PATH", Path("/missing/msys-dev.json")),
            mock.patch.object(dev, "command_call", return_value=0) as command,
        ):
            status = dev.main(
                [
                    "call",
                    "role:hal",
                    "set",
                    "--field",
                    "id=network:wlan0",
                    "--field",
                    "enabled=true",
                    "--field",
                    "priority=10",
                    "--field",
                    "changes.powered=true",
                    "--field",
                    "changes.discoverable=false",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(
            command.call_args.args[4],
            {
                "id": "network:wlan0",
                "enabled": True,
                "priority": 10,
                "changes": {"powered": True, "discoverable": False},
            },
        )

    def test_payload_remains_available_and_must_be_an_object(self) -> None:
        self.assertEqual(
            dev.parse_call_payload('{"id":"network:wlan0"}', []),
            {"id": "network:wlan0"},
        )
        with self.assertRaisesRegex(ValueError, "must decode to a JSON object"):
            dev.parse_call_payload("[]", [])

    def test_invalid_or_duplicate_field_fails_before_transport(self) -> None:
        for fields, message in (
            (["missing-separator"], "KEY=VALUE"),
            (["id=first", "id=second"], "repeated"),
            ([" =value"], "invalid key"),
            (["changes..powered=true"], "invalid key path"),
            (["a.b.c.d.e=true"], "1 to 4 segments"),
            (["changes=true", "changes.powered=true"], "path conflict"),
            (["changes.powered=true", "changes=false"], "path conflict"),
        ):
            with self.subTest(fields=fields):
                stderr = io.StringIO()
                argv = ["call", "role:hal", "get"]
                for field in fields:
                    argv.extend(["--field", field])
                with (
                    mock.patch.dict(
                        os.environ, {"MSYS_DEV_TARGET": "root@device"}
                    ),
                    mock.patch.object(
                        dev, "CONFIG_PATH", Path("/missing/msys-dev.json")
                    ),
                    mock.patch.object(dev, "command_call") as command,
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    dev.main(argv)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(message, stderr.getvalue())
                command.assert_not_called()

    def test_payload_and_field_are_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "call",
                    "role:hal",
                    "get",
                    "--payload",
                    "{}",
                    "--field",
                    "id=network:wlan0",
                ]
            )


if __name__ == "__main__":
    unittest.main()
