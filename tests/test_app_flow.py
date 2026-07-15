from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from msys_tools import dev
from msys_tools.app_flow import (
    AppFlowError,
    TEMPLATES,
    create_app,
    select_app_component,
)
from msys_tools.package_flow import PackageFlowError, parse_overlay_spec, validate_package


WORKSPACE = Path(__file__).resolve().parents[2]


def load_i18n_validator():
    path = WORKSPACE / "msys-contracts" / "tools" / "i18n_tool.py"
    spec = importlib.util.spec_from_file_location("msys_test_i18n_tool", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load i18n contract validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_catalog


class AppFlowTests(unittest.TestCase):
    def test_all_offline_templates_have_strict_manifest_files_and_i18n(self) -> None:
        validate_catalog = load_i18n_validator()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for template in TEMPLATES:
                with self.subTest(template=template):
                    package_id = f"org.example.scaffold-{template}"
                    destination = root / template
                    result = create_app(
                        WORKSPACE,
                        destination,
                        template=template,
                        package_id=package_id,
                        name=f"Example {template}",
                    )
                    self.assertEqual(result["component"], f"{package_id}:main")
                    self.assertTrue((destination / "files").is_dir())
                    manifest = json.loads(
                        (destination / "manifest.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(manifest["schema"], "msys.manifest.v1")
                    self.assertEqual(
                        manifest["package"]["x-msys-i18n"],
                        {
                            "catalog": "files/share/i18n/catalog.json",
                            "name_key": "app.name",
                            "summary_key": "app.summary",
                        },
                    )
                    validate_package(WORKSPACE, destination / "manifest.json")
                    catalog = json.loads(
                        (destination / "files/share/i18n/catalog.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(validate_catalog(catalog), [])
                    for locale in ("en-US", "zh"):
                        self.assertIn("app.name", catalog["messages"][locale])
                        self.assertIn("app.summary", catalog["messages"][locale])
                    self.assertEqual(
                        set(catalog["messages"]["zh"]),
                        set(catalog["messages"]["en-US"]),
                    )
                    self.assertNotIn("zh-CN", catalog["messages"])
                    readme = (destination / "README.md").read_text(encoding="utf-8")
                    self.assertIn("msys-dev app run", readme)
                    self.assertIn("mipc.call:role:input-method", readme)
                    self.assertIn("role:input-method", readme)
                    self.assertNotIn("apt install", readme)
                    self.assertNotIn("pip install", readme)
                    if template in {"c", "cpp", "qt"}:
                        executable = destination / "files/bin/app"
                        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                        executable.chmod(0o755)
                    elif template == "electron":
                        executable = destination / "files/runtime/electron/electron"
                        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                        executable.chmod(0o755)
                    checked = validate_package(WORKSPACE, destination)
                    self.assertEqual(checked["package"], package_id)

    def test_python_and_tk_are_immediately_package_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for template in ("python", "tk"):
                destination = root / template
                create_app(
                    WORKSPACE,
                    destination,
                    template=template,
                    package_id=f"org.example.{template}",
                )
                checked = validate_package(WORKSPACE, destination)
                self.assertEqual(checked["package"], f"org.example.{template}")
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "py_compile",
                        str(destination / "files/app/main.py"),
                        str(destination / "files/app/i18n.py"),
                        str(destination / "files/app/ui_fonts.py"),
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                main_source = (destination / "files/app/main.py").read_text(
                    encoding="utf-8"
                )
                font_source = (destination / "files/app/ui_fonts.py").read_text(
                    encoding="utf-8"
                )
                self.assertIn("configure_tk_fonts(root)", main_source)
                self.assertNotIn('font=("Sans",', main_source)
                self.assertIn("MSYS_UI_FONT_FAMILY", font_source)
                self.assertIn("Noto Sans CJK SC", font_source)
                self.assertIn("tk.Scrollbar", main_source)
                self.assertIn("tk.Canvas", main_source)
                self.assertIn("wraplength", main_source)

    def test_all_templates_serve_the_application_back_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for template in TEMPLATES:
                with self.subTest(template=template):
                    destination = root / template
                    create_app(
                        WORKSPACE,
                        destination,
                        template=template,
                        package_id=f"org.example.back-{template}",
                    )
                    manifest = json.loads(
                        (destination / "manifest.json").read_text(encoding="utf-8")
                    )
                    component = manifest["components"][0]
                    self.assertEqual(component["readiness"]["mode"], "mipc-ready")
                    self.assertEqual(
                        component["provides"],
                        [
                            {
                                "interface": "org.msys.application-navigation.v1",
                                "exclusive": False,
                                "priority": 100,
                            }
                        ],
                    )
                    readme = (destination / "README.md").read_text(encoding="utf-8")
                    self.assertIn("org.msys.application-navigation.v1", readme)
                    self.assertIn('navigation_back', readme)
                    self.assertIn('{"handled":false}', readme)

                    if template in {"python", "tk"}:
                        source = (destination / "files/app/main.py").read_text(
                            encoding="utf-8"
                        )
                        self.assertIn(
                            "from msys_sdk import ComponentChannel, application_navigation_handler",
                            source,
                        )
                        self.assertIn("channel.handshake()", source)
                        self.assertIn("call_handler=application_navigation_handler", source)
                        self.assertIn("def navigate_back() -> bool:", source)
                        self.assertIn("return False", source)
                    elif template in {"c", "cpp"}:
                        suffix = "cpp" if template == "cpp" else "c"
                        source = (destination / f"src/main.{suffix}").read_text(
                            encoding="utf-8"
                        )
                        makefile = (destination / "Makefile").read_text(encoding="utf-8")
                        self.assertIn("#include <msys/mipc.h>", source)
                        self.assertIn("MSYS_NAVIGATION_BACK_METHOD", source)
                        self.assertIn("msys_mipc_send_navigation_back_result", source)
                        self.assertIn("static int navigate_back(void)", source)
                        self.assertIn("return 0;", source)
                        self.assertIn("MSYS_SDK ?= ../msys-sdk", makefile)
                        self.assertIn("$(MSYS_SDK)/src/mipc.c", makefile)
                    elif template == "qt":
                        source = (destination / "src/main.cpp").read_text(encoding="utf-8")
                        cmake = (destination / "CMakeLists.txt").read_text(encoding="utf-8")
                        self.assertIn("QSocketNotifier", source)
                        self.assertIn("MSYS_NAVIGATION_BACK_METHOD", source)
                        self.assertIn("static int navigate_back(void)", source)
                        self.assertIn("MSYS_SDK_ROOT", cmake)
                        self.assertIn("src/mipc.c", cmake)
                    else:
                        source = (destination / "files/app/main.js").read_text(
                            encoding="utf-8"
                        )
                        self.assertEqual(
                            component["exec"][:5],
                            [
                                "python",
                                "-m",
                                "msys_sdk.stdio_bridge",
                                "--",
                                "@package/files/runtime/electron/electron",
                            ],
                        )
                        self.assertIn('message.method === "navigation_back"', source)
                        self.assertIn("function navigateBack()", source)
                        self.assertIn("return false;", source)
                        self.assertIn('send({ type: "hello", component, generation })', source)

    @unittest.skipUnless(
        os.name == "posix"
        and shutil.which("make") is not None
        and shutil.which("cc") is not None
        and shutil.which("c++") is not None
        and Path("/usr/include/X11/Xlib.h").is_file(),
        "requires a POSIX C/C++ toolchain with X11 headers",
    )
    def test_native_back_templates_build_against_the_public_sdk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for template in ("c", "cpp"):
                with self.subTest(template=template):
                    destination = root / template
                    create_app(
                        WORKSPACE,
                        destination,
                        template=template,
                        package_id=f"org.example.build-{template}",
                    )
                    subprocess.run(
                        [
                            "make",
                            "CC=cc",
                            "CXX=c++",
                            f"MSYS_SDK={WORKSPACE / 'msys-sdk'}",
                        ],
                        cwd=destination,
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    self.assertTrue((destination / "files/bin/app").is_file())

    def test_python_i18n_merges_parent_and_partial_locale_overlays(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "python"
            create_app(
                WORKSPACE,
                destination,
                template="python",
                package_id="org.example.python-locale",
            )
            catalog_path = destination / "files/share/i18n/catalog.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["messages"]["zh"]["app.title"] = "通用中文标题"
            catalog["messages"]["zh"]["app.message"] = "通用中文消息"
            catalog["messages"]["zh-CN"] = {"app.message": "中国地区消息"}
            catalog_path.write_text(
                json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
            )

            module_path = destination / "files/app/i18n.py"
            spec = importlib.util.spec_from_file_location(
                "generated_msys_python_i18n", module_path
            )
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader if spec is not None else None)
            module = importlib.util.module_from_spec(spec)
            assert spec is not None and spec.loader is not None
            spec.loader.exec_module(module)

            self.assertEqual(
                module.normalize_locale("zh_Hans_CN.UTF-8"), "zh-Hans-CN"
            )
            self.assertEqual(
                module.locale_candidates("zh_Hans_CN.UTF-8", "en-US"),
                ["zh-Hans-CN", "zh-Hans", "zh", "en-US"],
            )
            self.assertIsNone(module.normalize_locale("C.UTF-8"))
            self.assertIsNone(module.normalize_locale("POSIX"))

            parent = module.load_messages(
                {
                    "MSYS_LOCALE": "zh_Hans_CN.UTF-8",
                    "LC_ALL": "en_US.UTF-8",
                    "LC_MESSAGES": "zh_CN.UTF-8",
                    "LANG": "en_US.UTF-8",
                }
            )
            self.assertEqual(parent["app.title"], "通用中文标题")
            self.assertEqual(parent["app.message"], "通用中文消息")

            partial = module.load_messages(
                {
                    "LC_ALL": "",
                    "LC_MESSAGES": "zh_CN.UTF-8",
                    "LANG": "en_US.UTF-8",
                }
            )
            self.assertEqual(partial["app.title"], "通用中文标题")
            self.assertEqual(partial["app.message"], "中国地区消息")

            precedence = module.load_messages(
                {
                    "LC_ALL": "en_US.UTF-8",
                    "LC_MESSAGES": "zh_CN.UTF-8",
                    "LANG": "zh_CN.UTF-8",
                }
            )
            self.assertEqual(precedence["app.message"], "Your MSYS application is running.")
            posix_default = module.load_messages(
                {"MSYS_LOCALE": "C.UTF-8", "LC_ALL": "zh_CN.UTF-8"}
            )
            self.assertEqual(
                posix_default["app.message"], "Your MSYS application is running."
            )

    def test_bundled_templates_fail_cleanly_until_artifact_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for template in ("c", "cpp", "qt", "electron"):
                destination = root / template
                create_app(
                    WORKSPACE,
                    destination,
                    template=template,
                    package_id=f"org.example.bundle-{template}",
                )
                with self.assertRaisesRegex(PackageFlowError, "missing|invalid"):
                    validate_package(WORKSPACE, destination)
                readme = (destination / "README.md").read_text(encoding="utf-8")
                self.assertIn("files/", readme)
                self.assertTrue(
                    "compile" in readme.casefold()
                    or "electron distribution" in readme.casefold()
                )
                if template == "qt":
                    source = (destination / "src/main.cpp").read_text(encoding="utf-8")
                    i18n_source = (destination / "src/i18n.h").read_text(
                        encoding="utf-8"
                    )
                    self.assertIn("MSYS_UI_FONT_FAMILY", source)
                    self.assertIn("setPixelSize(14)", source)
                    self.assertIn("static_cast<QFont::StyleStrategy>", source)
                    self.assertIn("PreferAntialias", source)
                    self.assertIn("NoSubpixelAntialias", source)
                    self.assertIn("QScrollArea", source)
                    self.assertIn("setWordWrap(true)", source)
                    self.assertIn("ScrollBarAlwaysOff", source)
                    self.assertIn("chain.crbegin()", i18n_source)
                    environment_keys = i18n_source[
                        i18n_source.index("static const char *const keys[]") :
                    ]
                    self.assertLess(
                        environment_keys.index('"MSYS_LOCALE"'),
                        environment_keys.index('"LC_ALL"'),
                    )
                    self.assertLess(
                        environment_keys.index('"LC_ALL"'),
                        environment_keys.index('"LC_MESSAGES"'),
                    )
                    self.assertLess(
                        environment_keys.index('"LC_MESSAGES"'),
                        environment_keys.index('"LANG"'),
                    )
                if template == "electron":
                    source = (destination / "files/app/main.js").read_text(
                        encoding="utf-8"
                    )
                    i18n_source = (destination / "files/app/i18n.js").read_text(
                        encoding="utf-8"
                    )
                    self.assertIn("MSYS_UI_FONT_FAMILY", source)
                    self.assertIn('appendSwitch("disable-lcd-text")', source)
                    self.assertIn('require("./i18n")', source)
                    self.assertIn("overflow-y:auto", source)
                    self.assertIn("overflow-wrap:anywhere", source)
                    self.assertIn(
                        '["MSYS_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG"]',
                        i18n_source,
                    )
                    self.assertIn("chain.slice().reverse()", i18n_source)

    def test_electron_i18n_matches_python_locale_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "electron"
            create_app(
                WORKSPACE,
                destination,
                template="electron",
                package_id="org.example.electron-locale",
            )
            catalog_path = destination / "files/share/i18n/catalog.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["messages"]["zh"]["app.title"] = "通用中文标题"
            catalog["messages"]["zh"]["app.message"] = "通用中文消息"
            catalog["messages"]["zh-CN"] = {"app.message": "中国地区消息"}
            catalog_path.write_text(
                json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
            )
            module_path = destination / "files/app/i18n.js"
            node = shutil.which("node")
            if node is None:
                self.assertIn(
                    "function normalizeLocale",
                    module_path.read_text(encoding="utf-8"),
                )
                return
            subprocess.run(
                [node, "--check", str(destination / "files/app/main.js")],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            script = r'''
const i18n = require(process.argv[1]);
const source = process.argv[2];
const result = {
  normalized: i18n.normalizeLocale("zh_Hans_CN.UTF-8"),
  chain: i18n.localeCandidates("zh_Hans_CN.UTF-8", "en-US"),
  parent: i18n.loadMessages({
    MSYS_LOCALE: "zh_Hans_CN.UTF-8", LC_ALL: "en_US.UTF-8",
    LC_MESSAGES: "zh_CN.UTF-8", LANG: "en_US.UTF-8"
  }, source),
  partial: i18n.loadMessages({
    LC_ALL: "", LC_MESSAGES: "zh_CN.UTF-8", LANG: "en_US.UTF-8"
  }, source),
  precedence: i18n.loadMessages({
    LC_ALL: "en_US.UTF-8", LC_MESSAGES: "zh_CN.UTF-8", LANG: "zh_CN.UTF-8"
  }, source),
  posix: i18n.loadMessages({
    MSYS_LOCALE: "C.UTF-8", LC_ALL: "zh_CN.UTF-8"
  }, source)
};
process.stdout.write(JSON.stringify(result));
'''
            completed = subprocess.run(
                [node, "-e", script, str(module_path), str(catalog_path)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            result = json.loads(completed.stdout)
            self.assertEqual(result["normalized"], "zh-Hans-CN")
            self.assertEqual(
                result["chain"], ["zh-Hans-CN", "zh-Hans", "zh", "en-US"]
            )
            self.assertEqual(result["parent"]["app.title"], "通用中文标题")
            self.assertEqual(result["parent"]["app.message"], "通用中文消息")
            self.assertEqual(result["partial"]["app.title"], "通用中文标题")
            self.assertEqual(result["partial"]["app.message"], "中国地区消息")
            self.assertEqual(
                result["precedence"]["app.message"],
                "Your MSYS application is running.",
            )
            self.assertEqual(
                result["posix"]["app.message"],
                "Your MSYS application is running.",
            )

    def test_c_i18n_example_compiles_catalog_without_third_party_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "c-app"
            create_app(
                WORKSPACE,
                destination,
                template="c",
                package_id="org.example.c-i18n",
                name="C i18n",
            )
            catalog_path = destination / "files/share/i18n/catalog.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["messages"]["zh"]["app.title"] = "通用中文标题"
            catalog["messages"]["zh"]["app.message"] = "通用中文消息"
            catalog["messages"]["zh-CN"] = {"app.message": "中国地区消息"}
            catalog_path.write_text(
                json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
            )
            output = destination / "build/i18n_catalog.h"
            subprocess.run(
                [
                    sys.executable,
                    str(destination / "tools/compile_i18n.py"),
                    str(catalog_path),
                    str(output),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            header = output.read_text(encoding="ascii")
            self.assertIn("msys_text_app_title", header)
            self.assertIn("\\xe4", header)
            self.assertNotIn("\x00", header)
            self.assertIn("value == '\\n'", header)
            self.assertIn("normalized[length] = '\\0';", header)
            self.assertIn("MSYS_LOCALE_ZH_CN", header)
            self.assertIn('"MSYS_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG"', header)
            encoded_generic_title = "".join(
                "\\x%02x" % byte for byte in "通用中文标题".encode("utf-8")
            )
            encoded_generic_message = "".join(
                "\\x%02x" % byte for byte in "通用中文消息".encode("utf-8")
            )
            encoded_region_message = "".join(
                "\\x%02x" % byte for byte in "中国地区消息".encode("utf-8")
            )
            self.assertEqual(header.count(encoded_generic_title), 1)
            self.assertEqual(header.count(encoded_generic_message), 1)
            self.assertEqual(header.count(encoded_region_message), 1)

    def test_new_refuses_invalid_identity_and_every_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for package_id in ("Example.App", "single", "org.example.bad!", "org..bad"):
                with self.subTest(package_id=package_id), self.assertRaises(AppFlowError):
                    create_app(
                        WORKSPACE,
                        root / package_id.replace("!", "x"),
                        template="tk",
                        package_id=package_id,
                    )

            existing = root / "existing"
            existing.mkdir()
            marker = existing / "owned.txt"
            marker.write_text("developer data", encoding="utf-8")
            with self.assertRaisesRegex(AppFlowError, "refusing to overwrite"):
                create_app(
                    WORKSPACE,
                    existing,
                    template="tk",
                    package_id="org.example.safe",
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "developer data")

            with self.assertRaisesRegex(AppFlowError, "control character"):
                create_app(
                    WORKSPACE,
                    root / "control",
                    template="tk",
                    package_id="org.example.safe",
                    name="bad\nname",
                )

    def test_component_selection_is_exact_and_defaults_only_when_unambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "app"
            create_app(
                WORKSPACE,
                destination,
                template="tk",
                package_id="org.example.select",
                component="window",
            )
            self.assertEqual(
                select_app_component(WORKSPACE, destination, None),
                "org.example.select:window",
            )
            self.assertEqual(
                select_app_component(WORKSPACE, destination, "window"),
                "org.example.select:window",
            )
            self.assertEqual(
                select_app_component(
                    WORKSPACE, destination, "org.example.select:window"
                ),
                "org.example.select:window",
            )
            with self.assertRaisesRegex(AppFlowError, "does not match"):
                select_app_component(WORKSPACE, destination, "org.other:window")
            with self.assertRaisesRegex(AppFlowError, "not declared"):
                select_app_component(WORKSPACE, destination, "missing")

    def test_app_run_composes_existing_operations_and_honors_no_start(self) -> None:
        context = dev.Context(
            root=WORKSPACE,
            target="root@example",
            remote="/opt/msys-dev",
            remote_python="/opt/msys-dev/.runtime/python/bin/python3",
        )
        package = Path("/work/example")
        output = Path("/work/dist")
        artifact = "/work/dist/org.example.app-0.1.0.tar.gz"
        events: list[str] = []

        with (
            mock.patch.object(
                dev,
                "validate_package",
                side_effect=lambda *_args, **_kwargs: events.append("validate")
                or {"package": "org.example.app", "version": "0.1.0"},
            ),
            mock.patch.object(
                dev,
                "select_app_component",
                side_effect=lambda *_args, **_kwargs: events.append("select")
                or "org.example.app:main",
            ),
            mock.patch.object(
                dev,
                "build_package",
                side_effect=lambda *_args, **_kwargs: events.append("build")
                or {"artifact": artifact},
            ) as build,
            mock.patch.object(
                dev,
                "command_install_archive",
                side_effect=lambda *_args, **_kwargs: events.append("install") or 0,
            ) as install,
            mock.patch.object(
                dev,
                "command_start_component",
                side_effect=lambda *_args, **_kwargs: events.append("start") or 0,
            ) as start,
            redirect_stdout(io.StringIO()),
        ):
            result = dev.command_app_run(
                context,
                WORKSPACE,
                package,
                output,
                runtime_dir="/tmp/msys-main",
                state_dir="/opt/msys-state",
                component="main",
                artifact_format="maf",
            )

        self.assertEqual(result, 0)
        self.assertEqual(events, ["validate", "select", "build", "install", "start"])
        self.assertEqual(build.call_args.kwargs["artifact_format"], "maf")
        self.assertEqual(install.call_args.kwargs["state_dir"], "/opt/msys-state")
        start.assert_called_once_with(
            context, "/tmp/msys-main", "org.example.app:main"
        )

        events.clear()
        with (
            mock.patch.object(
                dev,
                "validate_package",
                return_value={"package": "org.example.app", "version": "0.1.0"},
            ),
            mock.patch.object(
                dev, "select_app_component", return_value="org.example.app:main"
            ) as select,
            mock.patch.object(dev, "build_package", return_value={"artifact": artifact}),
            mock.patch.object(dev, "command_install_archive", return_value=0),
            mock.patch.object(dev, "command_start_component") as start,
            redirect_stdout(io.StringIO()),
        ):
            result = dev.command_app_run(
                context,
                WORKSPACE,
                package,
                output,
                runtime_dir="/tmp/msys-main",
                state_dir="/opt/msys-state",
                no_start=True,
            )
        self.assertEqual(result, 0)
        select.assert_not_called()
        start.assert_not_called()

    def test_app_run_overlay_can_supply_a_declared_compiled_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "native"
            create_app(
                WORKSPACE,
                package,
                template="c",
                package_id="org.example.overlay-app",
            )
            executable = root / "compiled-app"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            overlay = parse_overlay_spec(
                WORKSPACE, f"{executable}=files/bin/app"
            )
            context = dev.Context(
                root=WORKSPACE,
                target="root@example",
                remote="/opt/msys-dev",
                remote_python="/opt/msys-dev/.runtime/python/bin/python3",
            )
            with (
                mock.patch.object(dev, "command_install_archive", return_value=0) as install,
                mock.patch.object(dev, "command_start_component", return_value=0) as start,
                redirect_stdout(io.StringIO()),
            ):
                result = dev.command_app_run(
                    context,
                    WORKSPACE,
                    package,
                    root / "dist",
                    runtime_dir="/tmp/msys-main",
                    state_dir="/opt/msys-state",
                    overlays=[overlay],
                )
            self.assertEqual(result, 0)
            archive = Path(install.call_args.args[2])
            self.assertTrue(archive.is_file())
            self.assertEqual(
                validate_package(WORKSPACE, archive, require_content_hashes=True)[
                    "package"
                ],
                "org.example.overlay-app",
            )
            start.assert_called_once_with(
                context, "/tmp/msys-main", "org.example.overlay-app:main"
            )

    def test_cli_new_is_local_and_errors_are_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "hello"
            with (
                mock.patch.object(dev, "CONFIG_PATH", root / "missing-config.json"),
                redirect_stdout(io.StringIO()) as stdout,
            ):
                result = dev.main(
                    [
                        "app",
                        "new",
                        str(destination),
                        "--root",
                        str(WORKSPACE),
                        "--id",
                        "org.example.hello",
                        "--template",
                        "tk",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertIn("msys.dev-app-scaffold.v1", stdout.getvalue())
            self.assertTrue((destination / "manifest.json").is_file())

            stderr = io.StringIO()
            with (
                mock.patch.object(dev, "CONFIG_PATH", root / "missing-config.json"),
                redirect_stderr(stderr),
            ):
                result = dev.main(
                    [
                        "app",
                        "new",
                        str(destination),
                        "--root",
                        str(WORKSPACE),
                        "--id",
                        "org.example.hello",
                        "--template",
                        "tk",
                    ]
                )
            self.assertEqual(result, 2)
            self.assertIn("refusing to overwrite", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_run_routes_practical_options_and_incomplete_build_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package"
            output = root / "artifacts"
            with (
                mock.patch.object(dev, "CONFIG_PATH", root / "missing-config.json"),
                mock.patch.object(dev, "command_app_run", return_value=0) as run,
            ):
                result = dev.main(
                    [
                        "app",
                        "run",
                        str(package),
                        "--root",
                        str(WORKSPACE),
                        "--target",
                        "root@example",
                        "--output",
                        str(output),
                        "--component",
                        "main",
                        "--no-start",
                        "--format",
                        "maf",
                        "--runtime-dir",
                        "/tmp/custom-msys",
                        "--state-dir",
                        "/srv/msys-state",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(run.call_args.args[2], package)
            self.assertEqual(run.call_args.args[3], output)
            self.assertEqual(run.call_args.kwargs["component"], "main")
            self.assertTrue(run.call_args.kwargs["no_start"])
            self.assertEqual(run.call_args.kwargs["artifact_format"], "maf")
            self.assertEqual(run.call_args.kwargs["runtime_dir"], "/tmp/custom-msys")
            self.assertEqual(run.call_args.kwargs["state_dir"], "/srv/msys-state")

            native = root / "native"
            create_app(
                WORKSPACE,
                native,
                template="c",
                package_id="org.example.incomplete",
            )
            stderr = io.StringIO()
            with (
                mock.patch.object(dev, "CONFIG_PATH", root / "missing-config.json"),
                redirect_stderr(stderr),
                redirect_stdout(io.StringIO()),
            ):
                result = dev.main(
                    [
                        "app",
                        "run",
                        str(native),
                        "--root",
                        str(WORKSPACE),
                        "--target",
                        "root@example",
                    ]
                )
            self.assertEqual(result, 2)
            self.assertIn("files/bin/app", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
