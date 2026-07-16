from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from msys_tools import dev
from msys_tools.package_flow import PackageFlowError


class PersistentConfigTests(unittest.TestCase):
    def test_config_persists_workspace_ssh_and_repository_values_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            root = Path(temporary) / "workspace"
            root.mkdir()
            with mock.patch.object(dev, "CONFIG_PATH", config):
                result = dev.main(
                    [
                        "config",
                        "set",
                        "--root",
                        str(root),
                        "--target",
                        "root@example",
                        "--remote",
                        "/opt/msys-dev",
                        "--runtime-dir",
                        "/tmp/msys-main",
                        "--ssh-key",
                        "~/.ssh/msys-test",
                        "--ssh-control-path",
                        "~/.ssh/msys-%C",
                        "--ssh-control-persist",
                        "15m",
                        "--repo",
                        "msys-core",
                        "--repo",
                        " msys-x11-session ",
                        "--repo",
                        "msys-x11-session",
                        "--repo",
                        " msys-apps ",
                        "--repo",
                        "msys-apps",
                    ]
                )
                data = dev.load_config()

            self.assertEqual(result, 0)
            self.assertEqual(data["target"], "root@example")
            self.assertEqual(data["runtime_dir"], "/tmp/msys-main")
            self.assertEqual(data["ssh_control_persist"], "15m")
            self.assertEqual(
                data["repos"], ["msys-core", "msys-x11-session", "msys-apps"]
            )
            self.assertFalse(list(config.parent.glob(".config.json.*.tmp")))

    def test_invalid_repository_does_not_replace_persisted_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text('{"repos": ["msys-core"]}\n', encoding="utf-8")
            stderr = io.StringIO()
            with (
                mock.patch.object(dev, "CONFIG_PATH", config),
                redirect_stderr(stderr),
            ):
                result = dev.main(["config", "set", "--repo", "../outside"])
                data = dev.load_config()

        self.assertEqual(result, 2)
        self.assertEqual(data["repos"], ["msys-core"])
        self.assertIn("invalid repository", stderr.getvalue())

    def test_context_specific_ssh_options_are_used_by_all_transports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "key"
            key.write_text("private", encoding="utf-8")
            context = dev.Context(
                Path(temporary),
                "root@example",
                "/opt/msys-dev",
                "/opt/msys-dev/.runtime/python/bin/python3",
                ssh_key=key,
                ssh_control_path=Path(temporary) / "control-%C",
                ssh_control_persist="30m",
            )
            ssh_args = dev.ssh_base_args(context)
            scp_args = dev.scp_base_args(context)

        for args in (ssh_args, scp_args):
            self.assertIn("ControlPersist=30m", args)
            self.assertIn(f"ControlPath={context.ssh_control_path}", args)
            self.assertIn(str(key), args)


class DoctorTests(unittest.TestCase):
    @staticmethod
    def complete_rows() -> dict[str, tuple[str, str]]:
        return {
            "sh": ("ok", "/bin/sh"),
            "tar": ("ok", "/bin/tar"),
            "cp": ("ok", "/bin/cp"),
            "mv": ("ok", "/bin/mv"),
            "uname": ("ok", "/bin/uname"),
            "bash": ("ok", "/bin/bash"),
            "xdpyinfo": ("ok", "/usr/bin/xdpyinfo"),
            "x-server-xorg": ("ok", "/usr/bin/Xorg"),
            "x-server-xvfb": ("missing", ""),
            "x-server": (
                "ok",
                "selected=Xorg Xorg=/usr/bin/Xorg Xvfb=missing",
            ),
            "rsync": ("missing", ""),
            "system-python3": ("missing", ""),
            "system-python": ("missing", ""),
            "native-build-make": ("ok", "/usr/bin/make"),
            "native-build-cc": ("ok", "/usr/bin/cc"),
            "native-build-cxx": ("ok", "/usr/bin/c++"),
            "isolated-python": ("ok", "Python 3.10.20"),
            "isolated-python-tkinter": ("ok", "Tk 8.6"),
            "x11-policy": (
                "ok",
                "/opt/msys-dev/msys-x11-session/bin/msys-x11-policy",
            ),
            "native-shell": (
                "ok",
                "/opt/msys-dev/msys-shell-native/bin/msys-shell-native",
            ),
            "native-hal": (
                "ok",
                "/opt/msys-dev/msys-hal/files/bin/msys-hal-native",
            ),
            "native-core-lite": (
                "ok",
                "/opt/msys-dev/msys-core/native/build/msysd-native-lite",
            ),
            "ch347-provider-script": (
                "ok",
                "/opt/msys-dev/msys-x11-session/scripts/msys_ch347_x11_provider.sh",
            ),
            "ch347-start-script": (
                "ok",
                "/root/x11display/scripts/start_ch347_dirty_usb_x11.sh",
            ),
            "ch347-stop-script": (
                "ok",
                "/root/x11display/scripts/stop_ch347_dirty_usb_x11.sh",
            ),
            "ch347-library": (
                "ok",
                "/root/x11display/ch347/libch347spi.so",
            ),
            "ch347-runtime-binaries": (
                "ok",
                "5 verified under /root/x11display/bin",
            ),
            "architecture": ("ok", "aarch64"),
            "kernel": ("ok", "Linux 5.15"),
            "remote-root": ("ok", "writable"),
        }

    def run_doctor(
        self,
        root: Path,
        rows: dict[str, tuple[str, str]],
        *,
        profile: str = "desktop-spi",
    ) -> tuple[int, str, str, mock.Mock]:
        for name in dev.DEFAULT_REPOS:
            (root / name).mkdir(exist_ok=True)
        context = dev.Context(
            root,
            "root@example",
            "/opt/msys-dev",
            "/opt/msys-dev/.runtime/python/bin/python3",
            ssh_key=None,
        )
        output = "\n".join(
            f"{dev.DOCTOR_PROBE_PREFIX}|{name}|{status}|{detail}"
            for name, (status, detail) in rows.items()
        )
        completed = subprocess.CompletedProcess([], 0, stdout=output)
        manifest_report = {"valid": True, "count": 3, "manifests": []}
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                dev.shutil,
                "which",
                side_effect=lambda name: None if name == "rsync" else f"/bin/{name}",
            ),
            mock.patch.object(
                dev, "discover_manifests", return_value=manifest_report
            ),
            mock.patch.object(
                dev, "ssh_capture", return_value=completed
            ) as capture,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = dev.command_doctor(context, profile)
        return result, stdout.getvalue(), stderr.getvalue(), capture

    def test_doctor_uses_one_profile_aware_probe_without_system_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, stdout, _stderr, capture = self.run_doctor(
                Path(temporary), self.complete_rows()
            )

        self.assertEqual(result, 0)
        capture.assert_called_once()
        self.assertIn("profile: desktop-spi (display=spi)", stdout)
        self.assertIn("one multiplexed SSH session", stdout)
        self.assertIn("optional-missing] target:system-python3", stdout)
        self.assertIn("selected=Xorg", stdout)
        remote_script = capture.call_args.args[1]
        self.assertIn("import tkinter", remote_script)
        syntax = subprocess.run(
            ["sh", "-n"], input=remote_script, text=True, capture_output=True
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        for forbidden in ("apt ", "apt-get", "pip install", "systemctl", "dbus"):
            self.assertNotIn(forbidden, remote_script)

    def test_doctor_accepts_xvfb_fallback_but_requires_desktop_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rows = self.complete_rows()
            rows.update({
                "x-server-xorg": ("missing", ""),
                "x-server-xvfb": ("ok", "/usr/bin/Xvfb"),
                "x-server": (
                    "ok",
                    "selected=Xvfb Xorg=missing Xvfb=/usr/bin/Xvfb",
                ),
            })
            ok, stdout, _stderr, _capture = self.run_doctor(
                Path(temporary), rows
            )

        self.assertEqual(ok, 0)
        self.assertIn("optional-missing] target:x-server-xorg", stdout)
        self.assertIn("selected=Xvfb", stdout)

        with tempfile.TemporaryDirectory() as temporary:
            rows = self.complete_rows()
            for name in (
                "bash", "xdpyinfo", "x-server", "isolated-python-tkinter"
            ):
                rows[name] = ("missing", "")
            failed, stdout, stderr, _capture = self.run_doctor(
                Path(temporary), rows
            )

        self.assertEqual(failed, 1)
        self.assertIn("[profile-required] target:bash", stdout)
        self.assertIn("isolated-python-tkinter", stderr)

    def test_doctor_build_and_ch347_stages_are_required_and_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rows = self.complete_rows()
            rows["native-build-make"] = ("missing", "")
            rows["native-build-cc"] = ("missing", "")
            rows["native-build-cxx"] = ("missing", "")
            rows["ch347-provider-script"] = ("missing", "/missing/provider")
            rows["ch347-start-script"] = ("missing", "/missing/start")
            rows["ch347-library"] = ("missing", "/missing/library")
            failed, stdout, stderr, _capture = self.run_doctor(
                Path(temporary), rows
            )

        self.assertEqual(failed, 1)
        self.assertIn("[build-required] target:native-build-make", stdout)
        self.assertIn("stage=source-build", stdout)
        self.assertIn("[deploy-required] target:ch347-provider-script", stdout)
        self.assertIn("stage=workspace-sync", stdout)
        self.assertIn("stage=x11display-sync", stdout)
        self.assertIn("will not invoke a package manager", stderr)
        self.assertIn("sync-x11display", stderr)


class DeliveryCommandTests(unittest.TestCase):
    def context(self, root: Path) -> dev.Context:
        return dev.Context(
            root,
            "root@example",
            "/opt/msys-dev",
            "/opt/msys-dev/.runtime/python/bin/python3",
            ssh_key=None,
        )

    def test_default_sync_set_contains_all_runtime_repositories_once(self) -> None:
        self.assertIn("msys-hal", dev.DEFAULT_REPOS)
        self.assertIn("msys-shell-native", dev.DEFAULT_REPOS)
        self.assertIn("msys-settings", dev.DEFAULT_REPOS)
        self.assertNotIn("msys-apps", dev.DEFAULT_REPOS)
        self.assertIn("msys-notes", dev.DEFAULT_REPOS)
        self.assertIn("msys-calculator", dev.DEFAULT_REPOS)
        self.assertIn("msys-device-info", dev.DEFAULT_REPOS)
        self.assertIn("msys-input-touch", dev.DEFAULT_REPOS)
        self.assertIn("msys-openstick-ch347", dev.DEFAULT_REPOS)
        self.assertEqual(dev.DEFAULT_REPOS.count("msys-x11-session"), 1)
        self.assertEqual(dev.DEFAULT_REPOS.count("msys-calculator"), 1)
        self.assertEqual(len(dev.DEFAULT_REPOS), len(set(dev.DEFAULT_REPOS)))

    def test_sync_environment_repositories_override_config_and_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context_repos = (
                " msys-x11-session,msys-apps,msys-core,"
                "msys-apps,msys-x11-session "
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "MSYS_DEV_REPOS": context_repos,
                        "MSYS_DEV_TARGET": "root@example",
                    },
                    clear=False,
                ),
                mock.patch.object(dev, "CONFIG_PATH", root / "missing-config.json"),
                mock.patch.object(dev, "command_sync", return_value=0) as sync,
            ):
                result = dev.main(["sync", "--root", str(root)])

        self.assertEqual(result, 0)
        self.assertEqual(
            sync.call_args.args[1], ["msys-x11-session", "msys-apps", "msys-core"]
        )

    def test_sync_fallback_stages_then_swaps_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "msys-core").mkdir()
            context = self.context(root)
            captures = [
                subprocess.CompletedProcess([], 1, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="built\n"),
            ]
            with (
                mock.patch.object(dev, "ssh_capture", side_effect=captures) as capture,
                mock.patch.object(dev.shutil, "which", return_value=None),
                mock.patch.object(dev, "run_local") as run_local,
                mock.patch.object(dev, "ssh") as ssh,
            ):
                result = dev.command_sync(context, ["msys-core"])

            self.assertEqual(result, 0)
            commands = "\n".join(call.args[1] for call in ssh.call_args_list)
            self.assertIn("/opt/msys-dev/.sync/msys-core.new", commands)
            self.assertIn("/opt/msys-dev/.msys-core.previous", commands)
            self.assertIn(".msys-dev-source.sha256", commands)
            self.assertIn("mv '/opt/msys-dev/.sync/msys-core.new' '/opt/msys-dev/msys-core'", commands)
            self.assertIn("make -j1 -C native", capture.call_args_list[1].args[1])
            self.assertNotIn("rm -rf '/opt/msys-dev/msys-core'", commands)
            self.assertTrue(any(call.args[0][0] == "tar" for call in run_local.call_args_list))

    def test_x11_sync_builds_policy_in_staging_before_repository_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "msys-x11-session").mkdir()
            context = self.context(root)
            captures = [
                subprocess.CompletedProcess([], 1, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="built\n"),
            ]
            with (
                mock.patch.object(dev, "ssh_capture", side_effect=captures) as capture,
                mock.patch.object(dev.shutil, "which", return_value=None),
                mock.patch.object(dev, "run_local"),
                mock.patch.object(dev, "ssh") as ssh,
                mock.patch.object(
                    dev,
                    "_target_native_source_identity",
                    return_value=(
                        "org.msys.x11.session",
                        "0.2.3",
                        root / "msys-x11-session/manifest.json",
                    ),
                ),
                mock.patch.object(
                    dev, "_record_target_native_artifact", return_value={}
                ) as record,
            ):
                result = dev.command_sync(context, ["msys-x11-session"])

        self.assertEqual(result, 0)
        build_command = capture.call_args_list[1].args[1]
        self.assertIn(".sync/msys-x11-session.new", build_command)
        self.assertIn("MAKEFLAGS= MFLAGS= make clean", build_command)
        self.assertIn("MAKEFLAGS= MFLAGS= make all", build_command)
        self.assertLess(
            build_command.index("MAKEFLAGS= MFLAGS= make clean"),
            build_command.index("MAKEFLAGS= MFLAGS= make all"),
        )
        self.assertNotIn("--msys-build-probe", build_command)
        self.assertNotIn("probe_status", build_command)
        self.assertIn("bin/msys-x11-policy", build_command)
        record.assert_called_once()
        self.assertEqual(record.call_args.args[2].package_id, "org.msys.x11.session")
        finalise = ssh.call_args_list[-1].args[1]
        self.assertIn(
            "mv '/opt/msys-dev/.sync/msys-x11-session.new' "
            "'/opt/msys-dev/msys-x11-session'",
            finalise,
        )

    def test_native_shell_and_hal_build_in_target_staging_single_threaded(self) -> None:
        cases = {
            "msys-shell-native": (
                "SDK_DIR='/opt/msys-dev/msys-sdk'",
                "bin/msys-shell-native",
            ),
            "msys-hal": (
                "MSYS_SDK_DIR='/opt/msys-dev/msys-sdk'",
                "files/bin/msys-hal-native",
            ),
        }
        for repository, (sdk_argument, binary) in cases.items():
            with self.subTest(repository=repository), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / repository).mkdir()
                context = self.context(root)
                captures = [
                    subprocess.CompletedProcess([], 1, stdout=""),
                    subprocess.CompletedProcess([], 0, stdout="built\n"),
                ]
                with (
                    mock.patch.object(dev, "ssh_capture", side_effect=captures) as capture,
                    mock.patch.object(dev.shutil, "which", return_value=None),
                    mock.patch.object(dev, "run_local"),
                    mock.patch.object(dev, "ssh") as ssh,
                    mock.patch.object(
                        dev,
                        "_target_native_source_identity",
                        return_value=(
                            dev.TARGET_NATIVE_REPOSITORIES[repository].package_id,
                            "1.2.3",
                            root / repository / "manifest.json",
                        ),
                    ),
                    mock.patch.object(
                        dev, "_record_target_native_artifact", return_value={}
                    ) as record,
                ):
                    result = dev.command_sync(context, [repository])

                self.assertEqual(result, 0)
                build = capture.call_args_list[1].args[1]
                self.assertIn("make -j1", build)
                self.assertIn(sdk_argument, build)
                self.assertIn(binary, build)
                record.assert_called_once()
                if repository == "msys-shell-native":
                    self.assertIn("compiler=cc", build)
                    self.assertIn("compiler=gcc", build)
                    self.assertIn('CC=\"$compiler\"', build)
                finalise = ssh.call_args_list[-1].args[1]
                self.assertIn(f"/{repository}.new", finalise)
                self.assertIn(f"/msys-dev/{repository}", finalise)

    def test_audio_bootstrap_builds_installs_and_inventories_target_elf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = "msys-audio"
            (root / repository).mkdir()
            context = self.context(root)
            captures = [
                subprocess.CompletedProcess([], 1, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="built\n"),
            ]
            with (
                mock.patch.object(dev, "ssh_capture", side_effect=captures) as capture,
                mock.patch.object(dev.shutil, "which", return_value=None),
                mock.patch.object(dev, "run_local"),
                mock.patch.object(dev, "ssh") as ssh,
                mock.patch.object(
                    dev,
                    "_target_native_source_identity",
                    return_value=(
                        "org.msys.audio.bluez",
                        "0.1.6",
                        root / repository / "manifest.json",
                    ),
                ),
                mock.patch.object(
                    dev, "_record_target_native_artifact", return_value={}
                ) as record,
            ):
                result = dev.command_sync(context, [repository])

        self.assertEqual(result, 0)
        spec = dev.TARGET_NATIVE_REPOSITORIES[repository]
        self.assertEqual(spec.package_id, "org.msys.audio.bluez")
        build = capture.call_args_list[1].args[1]
        self.assertIn("make -j1 -C native", build)
        self.assertIn('CC="$compiler" all', build)
        self.assertIn("DESTDIR='/opt/msys-dev/.sync/msys-audio.new/files/runtime/aarch64' install", build)
        self.assertIn(spec.relative_path, build)
        self.assertNotIn("--self-test", build)
        self.assertNotIn("--build-probe", build)
        self.assertIn(str(spec.runtime_inventory_path), build)
        self.assertIn(context.remote_python, build)
        self.assertNotIn("MSYS_SDK_DIR=", build)
        self.assertNotIn("install-manager", build)
        record.assert_called_once()
        self.assertEqual(record.call_args.args[2], spec)
        finalise = ssh.call_args_list[-1].args[1]
        self.assertIn("/msys-audio.new", finalise)
        self.assertIn("/msys-dev/msys-audio", finalise)

    def test_audio_native_manager_is_explicit_and_uses_synced_sdk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = "msys-audio"
            (root / repository).mkdir()
            context = self.context(root)
            captures = [
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="built\n"),
            ]
            with (
                mock.patch.object(dev, "ssh_capture", side_effect=captures) as capture,
                mock.patch.object(dev.shutil, "which", return_value=None),
                mock.patch.object(dev, "run_local"),
                mock.patch.object(dev, "ssh"),
                mock.patch.object(
                    dev,
                    "_target_native_source_identity",
                    return_value=(
                        "org.msys.audio.bluez",
                        "0.1.11",
                        root / repository / "manifest.json",
                    ),
                ),
                mock.patch.object(
                    dev, "_record_target_native_artifacts", return_value={}
                ) as record,
            ):
                result = dev.command_sync(
                    context,
                    [repository],
                    native_audio_manager=True,
                )

        self.assertEqual(result, 0)
        build = capture.call_args_list[1].args[1]
        self.assertIn("MSYS_SDK_DIR='/opt/msys-dev/msys-sdk'", build)
        self.assertIn("/opt/msys-dev/msys-sdk/include/msys/mipc.h", build)
        self.assertIn("/opt/msys-dev/msys-sdk/src/mipc.c", build)
        self.assertIn("manager", build)
        self.assertNotIn("check-manager", build)
        self.assertIn("install-manager", build)
        self.assertIn("msys-audio-manager-native", build)
        record.assert_called_once()
        recorded_specs = record.call_args.args[2]
        self.assertEqual(len(recorded_specs), 2)
        self.assertEqual(
            [spec.relative_path for spec in recorded_specs],
            [
                "files/runtime/aarch64/bin/msys-hci-bootstrap",
                "files/runtime/aarch64/bin/msys-audio-manager-native",
            ],
        )

    def test_x11display_sync_rebuilds_and_verifies_staging_before_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "x11display"
            source.mkdir()
            context = self.context(root)
            with (
                mock.patch.object(dev, "run_local"),
                mock.patch.object(dev, "ssh") as ssh,
            ):
                result = dev.command_sync_x11display(
                    context, source, "/root/x11display"
                )

        self.assertEqual(result, 0)
        deploy = ssh.call_args_list[1].args[1]
        self.assertIn("/root/x11display.new", deploy)
        self.assertIn("MAKEFLAGS= MFLAGS= make clean", deploy)
        self.assertIn("MAKEFLAGS= MFLAGS= make all", deploy)
        for relative in dev.X11DISPLAY_RUNTIME_BINARIES:
            self.assertIn(f"/root/x11display.new/{relative}", deploy)
        self.assertIn('test -x "$artifact"', deploy)
        self.assertIn('test ! -L "$artifact"', deploy)
        syntax = subprocess.run(
            ["sh", "-n"], input=deploy, text=True, capture_output=True
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        build_position = deploy.index("MAKEFLAGS= MFLAGS= make all")
        swap_position = deploy.index(
            "mv '/root/x11display.new' '/root/x11display'"
        )
        self.assertLess(build_position, swap_position)
        self.assertLess(deploy.index("rm -f '/root/x11display.incoming.tar'"), swap_position)

    def test_x11display_build_failure_cleans_transient_paths_without_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "x11display"
            source.mkdir()
            context = self.context(root)
            failed = subprocess.CalledProcessError(2, ["ssh"])
            with (
                mock.patch.object(dev, "run_local"),
                mock.patch.object(
                    dev, "ssh", side_effect=[None, failed, None]
                ) as ssh,
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    dev.command_sync_x11display(
                        context, source, "/root/x11display"
                    )

        failed_deploy = ssh.call_args_list[1].args[1]
        self.assertLess(
            failed_deploy.index("MAKEFLAGS= MFLAGS= make all"),
            failed_deploy.index("mv '/root/x11display.new' '/root/x11display'"),
        )
        cleanup = ssh.call_args_list[2]
        self.assertEqual(
            cleanup.args[1],
            "rm -rf '/root/x11display.new'; rm -f '/root/x11display.incoming.tar'",
        )
        self.assertFalse(cleanup.kwargs["check"])

    def test_run_uses_isolated_python_hal_path_and_canonical_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = self.context(Path(temporary))
            completed = [
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout='{"ready": true}\n'),
                subprocess.CompletedProcess([], 0, stdout="1234\n"),
                subprocess.CompletedProcess([], 0, stdout='{"healthy": true}\n'),
            ]
            with mock.patch.object(dev, "ssh_capture", side_effect=completed) as capture:
                result = dev.command_run(
                    context,
                    "mobile-spi",
                    "/tmp/msys-main",
                    "/tmp/msysd.log",
                    context.remote_python,
                )
            commands = [call.args[1] for call in capture.call_args_list]
            command = commands[2]

        self.assertEqual(result, 0)
        self.assertIn("bin/msys-x11-policy", commands[0])
        self.assertIn("msys-shell-native/bin/msys-shell-native", commands[0])
        self.assertIn("msys-hal/files/bin/msys-hal-native", commands[0])
        self.assertIn("msys_tools.remote_lifecycle 'prepare'", commands[1])
        self.assertIn("msys_tools.remote_lifecycle 'wait-ready'", commands[3])
        self.assertIn("/opt/msys-dev/msys-hal", command)
        self.assertIn("/opt/msys-dev/msys-shell-pyside/manifest.json", command)
        self.assertIn("/opt/msys-dev/msys-shell-native/manifest.json", command)
        self.assertIn("/opt/msys-dev/msys-hal/manifest.json", command)
        canonical = "/opt/msys-dev/msys-x11-session/manifest.json"
        ch347 = "/opt/msys-dev/msys-openstick-ch347/manifest.json"
        install = "/opt/msys-dev/msys-install/manifest.json"
        input_method = "/opt/msys-dev/msys-input-touch/manifest.json"
        self.assertIn(canonical, command)
        self.assertIn(ch347, command)
        self.assertIn(install, command)
        self.assertIn(input_method, command)
        self.assertLess(command.index(canonical), command.index(ch347))
        self.assertLess(command.index(ch347), command.index(install))
        self.assertLess(command.index("--config"), command.index(install))
        self.assertIn(context.remote_python, command)
        self.assertIn("MSYS_PLATFORM_PYTHONPATH=", command)
        self.assertIn("/opt/msys-dev/msys-sdk", command)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", command)
        self.assertIn("MALLOC_ARENA_MAX", command)
        self.assertIn("MALLOC_TRIM_THRESHOLD_=", command)
        self.assertIn("test -S '/tmp/msys-main/control.sock'", command)
        self.assertIn("refusing to start a duplicate msysd", command)
        self.assertNotIn("python3 -m msys_core", command)
        for application in (
            "msys-notes",
            "msys-calculator",
            "msys-device-info",
            "msys-apps",
        ):
            self.assertNotIn(f"/opt/msys-dev/{application}/manifest.json", command)

    def test_compatibility_service_script_uses_private_runtime_and_overlays(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "start-msys.sh"
            with redirect_stdout(io.StringIO()):
                result = dev.command_script(output, "/opt/msys-dev")
            source = output.read_text(encoding="utf-8")
            syntax = subprocess.run(
                ["sh", "-n"], input=source, text=True, capture_output=True
            )

        self.assertEqual(result, 0)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertIn(".runtime/python/bin/python3", source)
        self.assertIn("--manifest", source)
        self.assertIn("msys-hal", source)
        self.assertIn("$MSYS_ROOT/msys-shell-native/manifest.json", source)
        canonical = "$MSYS_ROOT/msys-x11-session/manifest.json"
        ch347 = "$MSYS_ROOT/msys-openstick-ch347/manifest.json"
        install = "$MSYS_ROOT/msys-install/manifest.json"
        input_method = "$MSYS_ROOT/msys-input-touch/manifest.json"
        self.assertIn(canonical, source)
        self.assertIn(ch347, source)
        self.assertIn(install, source)
        self.assertIn(input_method, source)
        self.assertIn('set -- "$@" --manifest "$input_manifest"', source)
        self.assertLess(source.index(canonical), source.index(ch347))
        self.assertLess(source.index(ch347), source.index(install))
        self.assertIn('set -- "$@" --manifest "$install_manifest"', source)
        self.assertIn('test -S "$MSYS_RUNTIME_DIR/control.sock"', source)
        self.assertIn("refusing to start a duplicate msysd", source)
        self.assertIn("MSYS_PLATFORM_PYTHONPATH", source)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", source)
        self.assertIn("MALLOC_ARENA_MAX", source)
        self.assertIn("MALLOC_TRIM_THRESHOLD_=", source)
        self.assertNotIn("exec python3", source)
        for application in (
            "msys-notes",
            "msys-calculator",
            "msys-device-info",
            "msys-apps",
        ):
            self.assertNotIn(f"$MSYS_ROOT/{application}/manifest.json", source)

    def test_application_repository_is_delivered_as_a_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apps = root / "msys-apps"
            apps.mkdir()
            (apps / "manifest.json").write_text(
                json.dumps(
                    {"package": {"id": "org.msys.apps", "version": "0.1.0"}}
                ),
                encoding="utf-8",
            )
            output = root / "dist"
            artifact = output / "org.msys.apps-0.1.0.tar.gz"
            with (
                mock.patch.object(dev, "CONFIG_PATH", root / "missing-config.json"),
                mock.patch.object(
                    dev,
                    "build_package",
                    return_value={
                        "artifact": str(artifact),
                        "package": "org.msys.apps",
                        "version": "0.1.0",
                        "sha256": "a" * 64,
                        "content_sha256": "c" * 64,
                    },
                ) as build,
                mock.patch.object(dev, "command_install_archive", return_value=0) as install,
                redirect_stdout(io.StringIO()),
            ):
                result = dev.main(
                    [
                        "package",
                        "deliver",
                        str(apps),
                        "--output",
                        str(output),
                        "--force",
                        "--root",
                        str(root),
                        "--target",
                        "root@example",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(build.call_args.args[:3], (root, apps, output))
        self.assertTrue(build.call_args.kwargs["force"])
        self.assertEqual(install.call_args.args[1], "/run/msys/main")
        self.assertEqual(install.call_args.args[2], artifact)
        self.assertEqual(install.call_args.kwargs["built"]["sha256"], "a" * 64)

    def test_package_deliver_explicit_maf_format_reaches_build_and_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apps = root / "msys-apps"
            apps.mkdir()
            (apps / "manifest.json").write_text(
                json.dumps(
                    {"package": {"id": "org.msys.apps", "version": "0.1.0"}}
                ),
                encoding="utf-8",
            )
            output = root / "dist"
            artifact = output / "org.msys.apps-0.1.0.maf"
            with (
                mock.patch.object(dev, "CONFIG_PATH", root / "missing-config.json"),
                mock.patch.object(
                    dev,
                    "build_package",
                    return_value={
                        "artifact": str(artifact),
                        "package": "org.msys.apps",
                        "version": "0.1.0",
                        "format": "maf",
                        "sha256": "a" * 64,
                        "content_sha256": "c" * 64,
                    },
                ) as build,
                mock.patch.object(
                    dev, "command_install_archive", return_value=0
                ) as install,
                redirect_stdout(io.StringIO()),
            ):
                result = dev.main([
                    "package",
                    "deliver",
                    str(apps),
                    "--output",
                    str(output),
                    "--format",
                    "maf",
                    "--root",
                    str(root),
                    "--target",
                    "root@example",
                ])

        self.assertEqual(result, 0)
        self.assertEqual(build.call_args.kwargs["artifact_format"], "maf")
        self.assertEqual(install.call_args.args[2], artifact)

    def test_install_archive_rejects_unverified_input_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = self.context(Path(temporary))
            archive = Path(temporary) / "bad.tar.gz"
            stderr = io.StringIO()
            with (
                mock.patch.object(dev, "validate_package", side_effect=PackageFlowError("bad hashes")),
                mock.patch.object(dev, "ssh") as ssh,
                mock.patch.object(dev, "run_local") as run_local,
                redirect_stderr(stderr),
            ):
                result = dev.command_install_archive(context, "/tmp/msys-main", archive)

        self.assertEqual(result, 2)
        ssh.assert_not_called()
        run_local.assert_not_called()
        self.assertIn("bad hashes", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
