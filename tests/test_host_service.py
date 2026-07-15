from __future__ import annotations

import io
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from msys_tools import dev
from msys_tools.host_service import (
    HOOK_BEGIN,
    HOOK_END,
    MANAGED_MARKER,
    HostServiceError,
    HostServiceSpec,
    atomic_install_command,
    detection_command,
    disable_command,
    enabled_test_command,
    enable_command,
    hook_marker_presence_test,
    hook_edit_command,
    integration_binding_test,
    integration_path,
    parse_detection,
    parse_state,
    render_launcher,
    render_openrc,
    render_state,
    render_sysv,
    select_backend,
    validate_state_binding,
)


class HostServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = HostServiceSpec(
            root="/opt/msys-dev",
            python="/opt/msys-dev/.runtime/python/bin/python3",
            runtime_dir="/tmp/msys-main",
        )

    def assert_shell_syntax(self, source: str) -> None:
        result = subprocess.run(
            ["sh", "-n"], input=source, text=True, capture_output=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generated_scripts_are_small_non_pid1_and_have_no_banned_stack(self) -> None:
        scripts = [
            render_launcher(self.spec),
            render_sysv(self.spec),
            render_openrc(self.spec),
            detection_command(),
            enable_command("sysv"),
            disable_command("sysv"),
            enable_command("openrc"),
            disable_command("openrc"),
            enabled_test_command("sysv"),
            enabled_test_command("openrc"),
        ]
        for source in scripts:
            self.assert_shell_syntax(source)
        combined = "\n".join(scripts).lower()
        for forbidden in ("systemctl", "dbus", "apt ", "apt-get", "pip install"):
            self.assertNotIn(forbidden, combined)
        launcher = scripts[0]
        self.assertIn("-m msys_core.msysd --foreground", launcher)
        self.assertIn(") >> \"$MSYS_LOG_FILE\" 2>&1 < /dev/null &", launcher)
        self.assertIn("MSYS_PYTHON_DEFAULT=/opt/msys-dev/.runtime/python/bin/python3", launcher)
        self.assertIn("find_external_msysd", launcher)
        self.assertIn("refusing to start a duplicate msysd", launcher)
        self.assertIn("$MSYS_ROOT/msys-hal", launcher)
        self.assertIn('MSYS_PLATFORM_PYTHONPATH="$MSYS_ROOT/msys-sdk"', launcher)
        self.assertIn('MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"', launcher)
        self.assertIn(
            'MALLOC_TRIM_THRESHOLD_="${MALLOC_TRIM_THRESHOLD_:-262144}"',
            launcher,
        )
        self.assertIn("--manifest \"$shell_manifest\"", launcher)
        self.assertIn("--manifest \"$native_shell_manifest\"", launcher)
        self.assertIn("--manifest \"$hal_manifest\"", launcher)
        canonical = "$MSYS_ROOT/msys-x11-session/manifest.json"
        ch347 = "$MSYS_ROOT/msys-openstick-ch347/manifest.json"
        install = "$MSYS_ROOT/msys-install/manifest.json"
        input_method = "$MSYS_ROOT/msys-input-touch/manifest.json"
        native_shell = "$MSYS_ROOT/msys-shell-native/manifest.json"
        self.assertIn(native_shell, launcher)
        self.assertIn(canonical, launcher)
        self.assertIn(ch347, launcher)
        self.assertIn(install, launcher)
        self.assertIn(input_method, launcher)
        self.assertLess(launcher.index(canonical), launcher.index(ch347))
        self.assertLess(launcher.index(ch347), launcher.index(install))
        self.assertIn('--manifest "$x11_session_manifest"', launcher)
        self.assertIn('--manifest "$ch347_manifest"', launcher)
        self.assertIn('--manifest "$install_manifest"', launcher)
        self.assertIn('--manifest "$input_manifest"', launcher)
        for application in (
            "msys-notes",
            "msys-calculator",
            "msys-device-info",
            "msys-apps",
        ):
            self.assertNotIn(f"$MSYS_ROOT/{application}/manifest.json", launcher)
        self.assertNotIn("exec python3", launcher)
        self.assertNotIn("\0", launcher)

    def test_launcher_exports_no_bytecode_guard_before_release_resolution(self) -> None:
        spec = HostServiceSpec(
            root="/opt/msys/current",
            python="/opt/msys/current/.runtime/python/bin/python3",
            release_root="/opt/msys",
        )
        launcher = render_launcher(spec)

        self.assertEqual(launcher.count("PYTHONDONTWRITEBYTECODE=1"), 1)
        self.assertLess(
            launcher.index("PYTHONDONTWRITEBYTECODE=1"),
            launcher.index("resolve_start_root()"),
        )
        self.assertIn("set -- -B -m msys_core.msysd --foreground", launcher)
        self.assertIn("exec setsid \"$MSYS_PYTHON\" \"$@\"", launcher)

    def test_hook_install_is_idempotent_and_uninstall_preserves_user_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            hook = Path(temporary) / "rc.local"
            original = "#!/bin/sh\necho user-start\nexit 0\n"
            hook.write_text(original, encoding="utf-8")
            hook.chmod(0o700)
            install = hook_edit_command(str(hook), self.spec.launcher, install=True)
            self.assert_shell_syntax(install)
            subprocess.run(["sh", "-c", install], check=True)
            subprocess.run(["sh", "-c", install], check=True)
            installed = hook.read_text(encoding="utf-8")
            self.assertTrue(installed.startswith("#!/bin/sh\n"))
            self.assertEqual(installed.count(HOOK_BEGIN), 1)
            self.assertEqual(installed.count(HOOK_END), 1)
            self.assertIn("echo user-start\nexit 0\n", installed)
            self.assertTrue(stat.S_IMODE(hook.stat().st_mode) & stat.S_IXUSR)

            uninstall = hook_edit_command(str(hook), self.spec.launcher, install=False)
            self.assert_shell_syntax(uninstall)
            subprocess.run(["sh", "-c", uninstall], check=True)
            self.assertEqual(hook.read_text(encoding="utf-8"), original)

    def test_atomic_install_refuses_unmanaged_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            incoming = root / "incoming"
            destination = root / "service"
            incoming.write_text(f"#!/bin/sh\n{MANAGED_MARKER}\necho new\n", encoding="utf-8")
            destination.write_text("#!/bin/sh\necho user\n", encoding="utf-8")
            command = atomic_install_command(str(incoming), str(destination), "755")
            self.assert_shell_syntax(command)
            refused = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
            self.assertEqual(refused.returncode, 3)
            self.assertIn("echo user", destination.read_text(encoding="utf-8"))

            destination.write_text(
                f"#!/bin/sh\n{MANAGED_MARKER}\necho old\n", encoding="utf-8"
            )
            subprocess.run(["sh", "-c", command], check=True)
            self.assertIn("echo new", destination.read_text(encoding="utf-8"))
            self.assertTrue(os.access(destination, os.X_OK))

    def test_detection_selection_and_persisted_state(self) -> None:
        detected = parse_detection("ssh warning\nopenrc\nsysv\nunknown\n")
        self.assertEqual(detected, ["openrc", "sysv"])
        self.assertEqual(select_backend("auto", detected), "openrc")
        self.assertEqual(select_backend("rc-local", []), "rc-local")
        with self.assertRaises(HostServiceError):
            select_backend("auto", [])
        state = render_state(self.spec, "hook", "/etc/board-startup.sh")
        parsed = parse_state(state)
        self.assertEqual(parsed["backend"], "hook")
        self.assertEqual(parsed["integration"], "/etc/board-startup.sh")
        self.assertEqual(integration_path("rc-local"), "/etc/rc.local")
        with self.assertRaises(HostServiceError):
            integration_path("hook")

    def test_integration_binding_is_exact_for_each_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            foreign = "/opt/msys/service/msys-service"
            for backend, source in (
                ("sysv", render_sysv(self.spec)),
                ("openrc", render_openrc(self.spec)),
            ):
                integration = root / backend
                integration.write_text(source, encoding="utf-8")
                own_test = integration_binding_test(
                    backend, str(integration), self.spec.launcher
                )
                foreign_test = integration_binding_test(
                    backend, str(integration), foreign
                )
                self.assert_shell_syntax(own_test)
                self.assertEqual(subprocess.run(["sh", "-c", own_test]).returncode, 0)
                self.assertNotEqual(
                    subprocess.run(["sh", "-c", foreign_test]).returncode, 0
                )

            hook = root / "rc.local"
            hook.write_text("#!/bin/sh\necho user\n", encoding="utf-8")
            subprocess.run(
                [
                    "sh",
                    "-c",
                    hook_edit_command(str(hook), self.spec.launcher, install=True),
                ],
                check=True,
            )
            own_hook_test = integration_binding_test(
                "rc-local", str(hook), self.spec.launcher
            )
            foreign_hook_test = integration_binding_test(
                "rc-local", str(hook), foreign
            )
            self.assertEqual(
                subprocess.run(["sh", "-c", own_hook_test]).returncode, 0
            )
            self.assertNotEqual(
                subprocess.run(["sh", "-c", foreign_hook_test]).returncode, 0
            )

            malformed = root / "malformed-hook"
            malformed.write_text(f"#!/bin/sh\n{HOOK_BEGIN}\n", encoding="utf-8")
            self.assertEqual(
                subprocess.run(
                    ["sh", "-c", hook_marker_presence_test(str(malformed))]
                ).returncode,
                0,
            )

    def test_persisted_state_must_belong_to_requested_layout(self) -> None:
        development = parse_state(
            render_state(self.spec, "sysv", "/etc/init.d/msys")
        )
        validate_state_binding(self.spec, development)
        formal = HostServiceSpec(
            root="/opt/msys/current",
            python="/opt/msys/current/.runtime/python/bin/python3",
            release_root="/opt/msys",
        )
        with self.assertRaisesRegex(HostServiceError, "another layout"):
            validate_state_binding(formal, development)

    def test_install_dry_run_performs_no_remote_write(self) -> None:
        context = dev.Context(
            root=Path("/workspace"),
            target="root@example",
            remote=self.spec.root,
            remote_python=self.spec.python,
        )
        output = io.StringIO()
        with (
            mock.patch.object(dev, "read_host_service_state", return_value=None),
            mock.patch.object(dev, "detect_host_service_backends", return_value=["sysv"]),
            mock.patch.object(dev, "remote_ownership", return_value="absent"),
            mock.patch.object(
                dev, "remote_integration_ownership", return_value="absent"
            ),
            mock.patch.object(dev, "ssh") as ssh,
            mock.patch.object(dev, "upload_host_service_files") as upload,
            redirect_stdout(output),
        ):
            result = dev.command_host_service_install(
                context,
                self.spec,
                "auto",
                None,
                dry_run=True,
                start_now=False,
            )
        self.assertEqual(result, 0)
        self.assertIn('"dry_run": true', output.getvalue())
        self.assertIn('"installation_state": "absent"', output.getvalue())
        ssh.assert_not_called()
        upload.assert_not_called()

    def test_formal_status_reports_foreign_development_integration(self) -> None:
        context = dev.Context(
            root=Path("/workspace"),
            target="root@example",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
        )
        formal = HostServiceSpec(
            root="/opt/msys/current",
            python="/opt/msys/current/.runtime/python/bin/python3",
            release_root="/opt/msys",
        )
        output = io.StringIO()
        with (
            mock.patch.object(dev, "read_host_service_state", return_value=None),
            mock.patch.object(dev, "detect_host_service_backends", return_value=["sysv"]),
            mock.patch.object(dev, "remote_ownership", return_value="absent"),
            mock.patch.object(
                dev,
                "remote_integration_ownership",
                return_value="foreign-managed",
            ),
            mock.patch.object(dev, "remote_boolean", return_value=True),
            redirect_stdout(output),
        ):
            result = dev.command_host_service_status(
                context, formal, "auto", None
            )
        self.assertEqual(result, 4)
        document = output.getvalue()
        self.assertIn('"installation_state": "absent"', document)
        self.assertIn('"startup_integration": "foreign-managed"', document)
        self.assertIn("startup-integration-bound-to-another-launcher", document)

    def test_formal_uninstall_refuses_foreign_integration_without_writes(self) -> None:
        context = dev.Context(
            root=Path("/workspace"),
            target="root@example",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
        )
        formal = HostServiceSpec(
            root="/opt/msys/current",
            python="/opt/msys/current/.runtime/python/bin/python3",
            release_root="/opt/msys",
        )
        with (
            mock.patch.object(dev, "read_host_service_state", return_value=None),
            mock.patch.object(dev, "detect_host_service_backends", return_value=["sysv"]),
            mock.patch.object(dev, "remote_ownership", return_value="absent"),
            mock.patch.object(
                dev,
                "remote_integration_ownership",
                return_value="foreign-managed",
            ),
            mock.patch.object(dev, "ssh") as ssh,
            self.assertRaisesRegex(HostServiceError, "another host-service launcher"),
        ):
            dev.command_host_service_uninstall(
                context,
                formal,
                "auto",
                None,
                dry_run=False,
            )
        ssh.assert_not_called()

    def test_formal_install_refuses_foreign_integration_without_upload(self) -> None:
        context = dev.Context(
            root=Path("/workspace"),
            target="root@example",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
        )
        formal = HostServiceSpec(
            root="/opt/msys/current",
            python="/opt/msys/current/.runtime/python/bin/python3",
            release_root="/opt/msys",
        )
        with (
            mock.patch.object(
                dev, "read_host_service_state", side_effect=[None, None]
            ),
            mock.patch.object(dev, "detect_host_service_backends", return_value=["sysv"]),
            mock.patch.object(dev, "remote_ownership", return_value="absent"),
            mock.patch.object(
                dev,
                "remote_integration_ownership",
                return_value="foreign-managed",
            ),
            mock.patch.object(dev, "ssh") as ssh,
            mock.patch.object(dev, "upload_host_service_files") as upload,
            self.assertRaisesRegex(HostServiceError, "uninstall that layout first"),
        ):
            dev.command_host_service_install(
                context,
                formal,
                "auto",
                None,
                dry_run=True,
                start_now=False,
            )
        ssh.assert_not_called()
        upload.assert_not_called()

    def test_formal_service_lives_outside_current_and_pins_one_verified_root(self) -> None:
        spec = HostServiceSpec(
            root="/opt/msys/current",
            python="/opt/msys/current/.runtime/python/bin/python3",
            runtime_dir="/tmp/msys-main",
            release_root="/opt/msys",
        )
        self.assertEqual(spec.service_dir, "/opt/msys/service")
        self.assertEqual(spec.launcher, "/opt/msys/service/msys-service")
        launcher = render_launcher(spec)
        self.assert_shell_syntax(launcher)
        self.assertIn("test ! -L \"$MSYS_ROOT\"", launcher)
        self.assertIn('resolved=$(CDPATH= cd "$MSYS_ROOT"', launcher)
        self.assertIn('MSYS_PYTHON="$MSYS_ROOT/.runtime/python/bin/python3"', launcher)
        self.assertIn('MSYS_PLATFORM_PYTHONPATH="$MSYS_ROOT/msys-sdk"', launcher)
        state = parse_state(render_state(spec, "sysv", "/etc/init.d/msys"))
        self.assertEqual(state["layout"], "release")
        self.assertEqual(state["release_root"], "/opt/msys")

        with self.assertRaises(HostServiceError):
            HostServiceSpec(
                root="/opt/msys/releases/direct",
                python="/opt/msys/releases/direct/.runtime/python/bin/python3",
                release_root="/opt/msys",
            )

    def test_formal_install_refuses_to_orphan_a_development_service_state(self) -> None:
        context = dev.Context(
            root=Path("/workspace"),
            target="root@example",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
        )
        formal = HostServiceSpec(
            root="/opt/msys/current",
            python="/opt/msys/current/.runtime/python/bin/python3",
            release_root="/opt/msys",
        )
        development_state = {
            "backend": "sysv",
            "integration": "/etc/init.d/msys",
            "launcher": "/opt/msys-dev/.service/msys-service",
            "root": "/opt/msys-dev",
        }
        with (
            mock.patch.object(
                dev,
                "read_host_service_state",
                side_effect=[None, development_state],
            ),
            mock.patch.object(dev, "detect_host_service_backends", return_value=["sysv"]),
            mock.patch.object(dev, "ssh") as ssh,
            self.assertRaisesRegex(HostServiceError, "development-tree host service"),
        ):
            dev.command_host_service_install(
                context,
                formal,
                "auto",
                None,
                dry_run=False,
                start_now=False,
            )
        ssh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
