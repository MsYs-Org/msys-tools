from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
