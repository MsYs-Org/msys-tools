from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from msys_tools import dev


WORKSPACE = Path(__file__).resolve().parents[2]


class FastDebugCommandTests(unittest.TestCase):
    def context(self) -> dev.Context:
        return dev.Context(
            root=Path("/workspace"),
            target="root@device",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
            ssh_key=None,
            ssh_control_path=Path("/tmp/msys-control-%C"),
            ssh_control_persist="2h",
        )

    def test_ssh_warm_checks_then_backgrounds_one_control_master(self) -> None:
        check = subprocess.CompletedProcess(["ssh"], 1)
        started = subprocess.CompletedProcess(["ssh"], 0)
        with mock.patch.object(dev, "run_local", side_effect=[check, started]) as run:
            result = dev.command_ssh_warm(self.context())

        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 2)
        checked = run.call_args_list[0].args[0]
        started_args = run.call_args_list[1].args[0]
        self.assertIn("-O", checked)
        self.assertIn("check", checked)
        self.assertIn("-M", started_args)
        self.assertIn("-N", started_args)
        self.assertIn("-f", started_args)
        self.assertEqual(started_args[-1], "root@device")

    def test_ssh_warm_does_not_replace_an_existing_master(self) -> None:
        check = subprocess.CompletedProcess(["ssh"], 0)
        with mock.patch.object(dev, "run_local", return_value=check) as run:
            result = dev.command_ssh_warm(self.context())

        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 1)

    def test_debug_collects_snapshot_and_logs_in_one_ssh_call(self) -> None:
        completed = subprocess.CompletedProcess(["ssh"], 0)
        with mock.patch.object(dev, "ssh", return_value=completed) as ssh:
            result = dev.command_debug(
                self.context(), "/tmp/msys-main", "/tmp/msysd.log", lines=73
            )

        self.assertEqual(result, 0)
        ssh.assert_called_once()
        command = ssh.call_args.args[1]
        self.assertIn("msys_tools.remote_lifecycle", command)
        self.assertIn("'status' '--runtime-dir' '/tmp/msys-main'", command)
        self.assertIn("tail -n 73 '/tmp/msysd.log'", command)
        self.assertIn("status=$?", command)
        self.assertNotIn("tail -n 0 -f", command)
        self.assertFalse(ssh.call_args.kwargs["check"])

    def test_debug_follow_reuses_the_same_ssh_transport_for_tail(self) -> None:
        completed = subprocess.CompletedProcess(["ssh"], 0)
        with mock.patch.object(dev, "ssh", return_value=completed) as ssh:
            result = dev.command_debug(
                self.context(), "/tmp/msys-main", "/tmp/msysd.log", follow=True
            )

        self.assertEqual(result, 0)
        ssh.assert_called_once()
        self.assertIn("tail -n 0 -f '/tmp/msysd.log'", ssh.call_args.args[1])

    def test_debug_cli_uses_persisted_runtime_and_log_paths(self) -> None:
        with (
            mock.patch.dict(os.environ, {"MSYS_DEV_TARGET": "root@device"}),
            mock.patch.object(dev, "CONFIG_PATH", Path("/missing/msys-dev.json")),
            mock.patch.object(dev, "command_debug", return_value=0) as debug,
        ):
            result = dev.main(["debug", "--lines", "91", "--follow"])

        self.assertEqual(result, 0)
        self.assertEqual(debug.call_args.args[1:3], ("/run/msys/main", "/tmp/msysd.log"))
        self.assertEqual(debug.call_args.kwargs, {"lines": 91, "follow": True})


class WindowsShortcutFilesTests(unittest.TestCase):
    def test_root_entry_point_delegates_to_the_versioned_tools_wrapper(self) -> None:
        source = (WORKSPACE / "msys.ps1").read_text(encoding="utf-8")
        self.assertIn("msys-tools\\msys.ps1", source)
        self.assertIn("& $entryPoint @args", source)
        root_cmd = (WORKSPACE / "msys.cmd").read_text(encoding="utf-8")
        self.assertIn("msys-tools\\msys.cmd", root_cmd)
        self.assertIn("call", root_cmd.lower())

    def test_cmd_launchers_bypass_policy_preserve_arguments_and_exit_code(self) -> None:
        tools_cmd = (WORKSPACE / "msys-tools" / "msys.cmd").read_text(
            encoding="utf-8"
        )
        self.assertIn("powershell.exe", tools_cmd.lower())
        self.assertIn("-NoProfile", tools_cmd)
        self.assertIn("-ExecutionPolicy Bypass", tools_cmd)
        self.assertIn('"%~dp0msys.ps1" %*', tools_cmd)
        self.assertIn("MSYS_EXIT_CODE=%ERRORLEVEL%", tools_cmd)
        self.assertIn("exit /b %MSYS_EXIT_CODE%", tools_cmd)

    def test_tools_wrapper_uses_a_loopback_broker_or_an_interactive_shell(self) -> None:
        source = (WORKSPACE / "msys-tools" / "msys.ps1").read_text(encoding="utf-8")
        self.assertIn("MSYS_DEV_ROOT=$workspaceWsl", source)
        self.assertIn('"--cd", $workingDirectoryWsl', source)
        self.assertIn("$currentWindows.StartsWith($workspacePrefix", source)
        self.assertIn('"ssh-warm"', source)
        self.assertIn('"msys_tools.dev"', source)
        self.assertIn('"msys_tools.dev_broker"', source)
        self.assertIn('"127.0.0.1"', source)
        self.assertIn('"--token-state", $stateWslPath', source)
        self.assertNotIn('"--token", $token', source)
        self.assertIn('"broker"', source)
        self.assertIn("msys-dev-shell.rc", source)
        self.assertIn("ConvertTo-MsysArgument", source)

    def test_child_action_option_does_not_rebind_the_wrapper_command(self) -> None:
        source = (WORKSPACE / "msys-tools" / "msys.ps1").read_text(encoding="utf-8")
        self.assertIn('[string]$Command = "help"', source)
        self.assertIn("switch ($Command.ToLowerInvariant())", source)
        self.assertNotIn('[string]$Action = "help"', source)

    def test_auto_reuses_but_never_starts_a_broker(self) -> None:
        source = (WORKSPACE / "msys-tools" / "msys.ps1").read_text(encoding="utf-8")
        normal_path = source.split(
            "# Key setup and initial password authentication", maxsplit=1
        )[1].split("$wslArgs = @()", maxsplit=1)[0]

        self.assertIn(
            'if ($null -eq $state -and $brokerMode -eq "on") {', normal_path
        )
        self.assertEqual(normal_path.count("Start-MsysBroker"), 1)
        self.assertIn(
            "if ($null -ne $state) {\n            $brokerResult = "
            "Invoke-MsysBrokerCommand",
            normal_path,
        )

    def test_help_recommends_the_persistent_shell_as_the_fastest_loop(self) -> None:
        source = (WORKSPACE / "msys-tools" / "msys.ps1").read_text(encoding="utf-8")
        readme = (WORKSPACE / "msys-tools" / "README.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("fastest loop: cd a repo, then mq/mqs/mqshot", source)
        self.assertIn("Auto mode never starts a broker", readme)
        self.assertIn("msys debug", readme)

    def test_persistent_shell_defines_the_short_msys_commands(self) -> None:
        source = (
            WORKSPACE / "msys-tools" / "scripts" / "msys-dev-shell.rc"
        ).read_text(encoding="utf-8")
        self.assertIn("msys()", source)
        self.assertIn("mq()", source)
        self.assertIn("mqs()", source)
        self.assertIn("mqshot()", source)
        self.assertIn("mf()", source)
        self.assertIn('alias m=msys', source)
        self.assertIn("msys_tools.dev", source)

    def test_fast_shortcut_starts_broker_and_infers_current_repository(self) -> None:
        source = (WORKSPACE / "msys-tools" / "msys.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('{ $_ -in @("fast", "q") }', source)
        self.assertIn('$fastBrokerDefault = $true', source)
        self.assertIn('if ($fastBrokerDefault) { "on" } else { "auto" }', source)
        self.assertIn('$candidateRepo', source)

    def test_accept_shortcut_reuses_the_persistent_broker_without_repo_inference(self) -> None:
        source = (WORKSPACE / "msys-tools" / "msys.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('"accept" {', source)
        self.assertIn('$cliArgs = @("accept") + $translatedArgs', source)
        self.assertIn('$fastBrokerDefault = $true', source)

    def test_ui_accept_shortcut_uses_the_broker_and_one_canonical_command(self) -> None:
        source = (WORKSPACE / "msys-tools" / "msys.ps1").read_text(
            encoding="utf-8"
        )
        branch = source.split(
            '{ $_ -in @("ui-accept", "p0-ui") } {', maxsplit=1
        )[1].split("    }", maxsplit=1)[0]

        self.assertIn('$fastBrokerDefault = $true', branch)
        self.assertIn('$cliArgs = @("ui-accept") + $translatedArgs', branch)

    def test_debug_shortcuts_start_the_broker_and_use_one_canonical_command(self) -> None:
        source = (WORKSPACE / "msys-tools" / "msys.ps1").read_text(
            encoding="utf-8"
        )
        branch = source.split(
            '{ $_ -in @("debug", "inspect") } {', maxsplit=1
        )[1].split("}", maxsplit=1)[0]
        self.assertIn('$fastBrokerDefault = $true', branch)
        self.assertIn('$cliArgs = @("debug") + $translatedArgs', branch)

    def test_quick_shortcuts_start_the_broker_and_use_one_canonical_command(self) -> None:
        source = (WORKSPACE / "msys-tools" / "msys.ps1").read_text(
            encoding="utf-8"
        )
        branch = source.split(
            '{ $_ -in @("quick", "deploy") } {', maxsplit=1
        )[1].split("}", maxsplit=1)[0]
        self.assertIn('$fastBrokerDefault = $true', branch)
        self.assertIn('$cliArgs = @("quick") + $translatedArgs', branch)

    def test_call_shortcut_forwards_quote_free_payload_fields_as_plain_argv(self) -> None:
        source = (WORKSPACE / "msys-tools" / "msys.ps1").read_text(
            encoding="utf-8"
        )
        call_branch = source.split('    "call" {', maxsplit=1)[1].split(
            "    }", maxsplit=1
        )[0]

        self.assertIn('$cliArgs = @("call") + $translatedArgs', call_branch)
        self.assertIn("--field KEY=VALUE", source)
        self.assertNotIn("ConvertTo-Json", call_branch)


if __name__ == "__main__":
    unittest.main()
