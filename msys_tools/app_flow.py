from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .package_flow import PackageFlowError, load_installer_api, resolve_source_manifest


PACKAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$")
COMPONENT_ID_RE = re.compile(r"^[a-z][a-z0-9._-]*$")
VERSION_RE = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
TEMPLATES = ("tk", "python", "c", "cpp", "qt", "electron")
SCAFFOLD_SCHEMA = "msys.dev-app-scaffold.v1"


class AppFlowError(RuntimeError):
    """A developer application could not be created or selected safely."""


@dataclass(frozen=True, slots=True)
class ProjectFile:
    text: str
    executable: bool = False


def _validate_text(value: str, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AppFlowError(f"{label} must contain 1..{maximum} characters")
    if "\x00" in value or any(ord(character) < 32 for character in value):
        raise AppFlowError(f"{label} contains a control character")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise AppFlowError(f"{label} is not valid UTF-8") from exc
    if label == "name" and any(character in value for character in "{}"):
        raise AppFlowError("name cannot contain i18n placeholder braces")
    return value


def _validate_package_id(value: str) -> str:
    if len(value) > 128 or PACKAGE_ID_RE.fullmatch(value) is None:
        raise AppFlowError(
            "app id must be a lower-case reverse-domain id such as org.example.hello"
        )
    return value


def _validate_component(value: str) -> str:
    if len(value) > 64 or COMPONENT_ID_RE.fullmatch(value) is None:
        raise AppFlowError(
            "component must start with a lower-case letter and contain only "
            "lower-case letters, digits, '.', '_' or '-'"
        )
    return value


def _default_name(package_id: str) -> str:
    leaf = package_id.rsplit(".", 1)[-1].replace("_", " ").replace("-", " ")
    return " ".join(part.capitalize() for part in leaf.split()) or "MSYS App"


def _manifest(
    package_id: str,
    name: str,
    version: str,
    component: str,
    template: str,
) -> dict[str, Any]:
    if template in {"python", "tk"}:
        runtime = template
        command = ["python", "@package/files/app/main.py"]
        timeout = 5000
        environment = {"PYTHONUNBUFFERED": "1"}
    elif template in {"c", "cpp"}:
        runtime = template
        command = ["@package/files/bin/app"]
        timeout = 5000
        environment = {}
    elif template == "qt":
        runtime = "qt"
        command = ["@package/files/bin/app"]
        timeout = 8000
        environment = {
            "QT_QPA_PLATFORM": "xcb",
            "QT_PLUGIN_PATH": "files/runtime/qt/plugins",
            "LD_LIBRARY_PATH": "files/runtime/qt/lib",
        }
    else:
        runtime = "electron"
        command = [
            "python",
            "-m",
            "msys_sdk.stdio_bridge",
            "--",
            "@package/files/runtime/electron/electron",
            "--no-sandbox",
            "@package/files/app",
        ]
        timeout = 10000
        environment = {"ELECTRON_ENABLE_LOGGING": "1"}

    item: dict[str, Any] = {
        "id": component,
        "name": name,
        "runtime": runtime,
        "exec": command,
        "lifecycle": "manual",
        "restart": "never",
        "readiness": {"mode": "mipc-ready", "timeout_ms": timeout},
        "provides": [
            {
                "interface": "org.msys.application-navigation.v1",
                "exclusive": False,
                "priority": 100,
            }
        ],
        "isolation": "baseline",
        "windowing": {
            "system": "x11",
            "display": "inherit",
            "mode": "window",
            "title": name,
            "identity": {
                "app_id": package_id,
                "x11_wm_class": package_id,
            },
        },
        "activation": {"launchable": True, "intents": []},
        "permissions": ["display:x11"],
    }
    if environment:
        item["env"] = environment
    return {
        "schema": "msys.manifest.v1",
        "package": {
            "id": package_id,
            "name": name,
            "version": version,
            "kind": "application",
            "summary": f"{name}, created from the MSYS {template} application template",
            "x-msys-i18n": {
                "catalog": "files/share/i18n/catalog.json",
                "name_key": "app.name",
                "summary_key": "app.summary",
            },
        },
        "components": [item],
    }


def _catalog(package_id: str, name: str) -> dict[str, Any]:
    return {
        "$schema": "https://msys.local/schemas/i18n-catalog.v1.json",
        "schema": "msys.i18n.catalog.v1",
        "id": package_id,
        "description": f"Application-local messages for {name}",
        "default_locale": "en-US",
        "messages": {
            "en-US": {
                "app.name": name,
                "app.summary": f"{name}, an MSYS application",
                "app.title": name,
                "app.message": "Your MSYS application is running.",
                "app.close": "Close",
            },
            "zh": {
                "app.name": name,
                "app.summary": f"{name}，一个 MSYS 应用",
                "app.title": name,
                "app.message": "MSYS 应用正在运行。",
                "app.close": "关闭",
            },
        },
    }


PYTHON_I18N = '''from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping


CATALOG = Path(__file__).resolve().parents[1] / "share" / "i18n" / "catalog.json"
ENVIRONMENT_LOCALE_KEYS = ("MSYS_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG")


def normalize_locale(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    raw = raw.split("@", 1)[0].split(".", 1)[0]
    if raw.upper() in {"C", "POSIX"}:
        return None
    parts = raw.replace("_", "-").split("-")
    language = parts[0] if parts else ""
    if not (2 <= len(language) <= 8 and language.isascii() and language.isalpha()):
        return None
    canonical = [language.lower()]
    index = 1
    if (
        index < len(parts)
        and len(parts[index]) == 4
        and parts[index].isascii()
        and parts[index].isalpha()
    ):
        canonical.append(parts[index].title())
        index += 1
    if index < len(parts) and (
        (
            len(parts[index]) == 2
            and parts[index].isascii()
            and parts[index].isalpha()
        )
        or (len(parts[index]) == 3 and parts[index].isascii() and parts[index].isdigit())
    ):
        canonical.append(parts[index].upper())
        index += 1
    for part in parts[index:]:
        if not (
            part.isascii()
            and part.isalnum()
            and (5 <= len(part) <= 8 or (len(part) == 4 and part[0].isdigit()))
        ):
            return None
        canonical.append(part.lower())
    return "-".join(canonical)


def locale_from_environment(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    values = os.environ if environ is None else environ
    for name in ENVIRONMENT_LOCALE_KEYS:
        value = values.get(name)
        if value is not None and str(value).strip():
            return normalize_locale(str(value))
    return None


def locale_candidates(requested: str | None, default_locale: str) -> list[str]:
    raw = normalize_locale(requested) if requested is not None else None
    current = raw or default_locale
    candidates: list[str] = []
    while current:
        if current not in candidates:
            candidates.append(current)
        current = current.rpartition("-")[0]
    if default_locale not in candidates:
        candidates.append(default_locale)
    return candidates


def load_messages(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    document = json.loads(CATALOG.read_text(encoding="utf-8"))
    default_locale = document["default_locale"]
    catalogs = document["messages"]
    selected: dict[str, str] = {}
    chain = locale_candidates(locale_from_environment(environ), default_locale)
    for locale in reversed(chain):
        messages = catalogs.get(locale)
        if isinstance(messages, dict):
            selected.update(
                (str(key), str(value)) for key, value in messages.items()
            )
    return selected
'''


PYTHON_UI_FONTS = '''from __future__ import annotations

import os
from typing import Any
from tkinter import TclError
from tkinter import font as tkfont


PREFERRED_FAMILIES = (
    "Noto Sans CJK SC",
    "Noto Sans SC",
    "Noto Sans CJK",
    "Source Han Sans SC",
    "WenQuanYi Micro Hei",
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "PingFang SC",
    "Hiragino Sans GB",
    "Noto Sans",
    "DejaVu Sans",
    "Liberation Sans",
    "Arial",
)

NAMED_FONTS = (
    "TkDefaultFont", "TkTextFont", "TkFixedFont", "TkMenuFont",
    "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont",
    "TkIconFont", "TkTooltipFont",
)


def _tk_pixel_size(size: int) -> int:
    value = int(size)
    if value < 0:
        return -max(12, -value)
    return -max(12, (value * 4 + 1) // 3)


def configure_tk_fonts(root: Any, default_size: int = 10) -> str | None:
    try:
        available = tkfont.families(root=root)
    except (TclError, RuntimeError):
        available = ()
    installed = {
        str(value).strip().casefold(): str(value).strip()
        for value in available if str(value).strip()
    }
    requested = str(
        os.environ.get("MSYS_UI_FONT_FAMILY", "")
        or os.environ.get("MSYS_TK_FONT_FAMILY", "")
    ).strip()
    candidates = ((requested,) if requested else ()) + PREFERRED_FAMILIES
    family = next(
        (installed[value.casefold()] for value in candidates if value.casefold() in installed),
        None,
    )
    if family is None:
        try:
            family = str(
                tkfont.nametofont("TkDefaultFont", root=root).actual("family")
            ).strip() or None
        except (TclError, RuntimeError):
            family = None
    if family is None:
        return None
    for name in NAMED_FONTS:
        try:
            options = {"family": family}
            options["size"] = _tk_pixel_size(default_size)
            tkfont.nametofont(name, root=root).configure(**options)
        except (TclError, RuntimeError):
            continue
    try:
        actual = str(
            tkfont.nametofont("TkDefaultFont", root=root).actual("family")
        ).strip()
        if actual:
            family = actual
    except (TclError, RuntimeError):
        pass
    root._msys_tk_font_family = family
    return family


def font_spec(widget: Any, size: int, *modifiers: str) -> tuple[object, ...]:
    try:
        root = widget._root()
    except (AttributeError, RuntimeError):
        root = widget
    return (
        getattr(root, "_msys_tk_font_family", "sans-serif"),
        _tk_pixel_size(size),
        *modifiers,
    )
'''


def _python_main(package_id: str, component: str) -> str:
    return f'''from __future__ import annotations

import os
import tkinter as tk

from msys_sdk import ComponentChannel, application_navigation_handler

from i18n import load_messages
from ui_fonts import configure_tk_fonts, font_spec


TEXT = load_messages()
IDENTITY = os.environ.get("MSYS_WINDOW_IDENTITY", {json.dumps(package_id)})

root = tk.Tk(className=IDENTITY)
configure_tk_fonts(root)
root.title(TEXT["app.title"])
root.configure(bg="#f4f6fa")
root.geometry("320x420")
root.minsize(240, 240)

viewport = tk.Frame(root, bg="#f4f6fa")
viewport.pack(fill="both", expand=True)
scrollbar = tk.Scrollbar(viewport, orient="vertical")
scrollbar.pack(side="right", fill="y")
canvas = tk.Canvas(
    viewport,
    bg="#f4f6fa",
    highlightthickness=0,
    yscrollcommand=scrollbar.set,
)
canvas.pack(side="left", fill="both", expand=True)
scrollbar.configure(command=canvas.yview)

page = tk.Frame(canvas, bg="#f4f6fa")
page_window = canvas.create_window((0, 0), window=page, anchor="nw")

header = tk.Frame(page, bg="#315fbd")
header.pack(fill="x")
title_label = tk.Label(
    header,
    text=TEXT["app.title"],
    bg="#315fbd",
    fg="white",
    justify="left",
    anchor="w",
    font=font_spec(root, 19, "bold"),
)
title_label.pack(fill="x", padx=22, pady=(26, 22))

card = tk.Frame(page, bg="white", highlightthickness=1, highlightbackground="#dce1eb")
card.pack(fill="x", padx=18, pady=18)
message_label = tk.Label(
    card,
    text=TEXT["app.message"],
    bg="white",
    fg="#202124",
    justify="left",
    anchor="w",
    font=font_spec(root, 13),
)
message_label.pack(fill="x", padx=20, pady=(32, 18))
identity_label = tk.Label(
    card,
    text={json.dumps(package_id + ':' + component)},
    bg="white",
    fg="#6b7280",
    justify="left",
    anchor="w",
    font=font_spec(root, 9),
)
identity_label.pack(fill="x", padx=20)
tk.Button(
    card,
    text=TEXT["app.close"],
    command=root.destroy,
    relief="flat",
    bg="#315fbd",
    activebackground="#244c9d",
    fg="white",
    activeforeground="white",
    padx=22,
    pady=8,
).pack(anchor="e", padx=20, pady=28)


def resize_page(event: tk.Event) -> None:
    width = max(1, int(event.width))
    canvas.itemconfigure(page_window, width=width)
    title_label.configure(wraplength=max(80, width - 44))
    message_label.configure(wraplength=max(80, width - 78))
    identity_label.configure(wraplength=max(80, width - 78))


def update_scroll_region(_event: tk.Event | None = None) -> None:
    canvas.configure(scrollregion=canvas.bbox("all"))


def wheel_scroll(event: tk.Event) -> str:
    delta = getattr(event, "delta", 0)
    if delta:
        canvas.yview_scroll(-1 if delta > 0 else 1, "units")
    else:
        canvas.yview_scroll(-1 if getattr(event, "num", 0) == 4 else 1, "units")
    return "break"


canvas.bind("<Configure>", resize_page)
page.bind("<Configure>", update_scroll_region)
root.bind_all("<MouseWheel>", wheel_scroll)
root.bind_all("<Button-4>", wheel_scroll)
root.bind_all("<Button-5>", wheel_scroll)


def navigate_back() -> bool:
    """Consume Back inside the app; replace this when adding nested pages."""

    return False


def on_component_message(message: dict[str, object]) -> None:
    if message.get("type") in {{"shutdown", "eof"}}:
        root.after(0, root.destroy)


channel = ComponentChannel.from_environment()
if channel is not None:
    channel.handshake()
    channel.start(
        on_component_message,
        call_handler=application_navigation_handler(navigate_back),
    )

root.mainloop()
'''


COMPILE_I18N = '''#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


def c_bytes(value: str) -> str:
    return '"' + "".join("\\\\x%02x" % byte for byte in value.encode("utf-8")) + '"'


source = Path(sys.argv[1])
target = Path(sys.argv[2])
document = json.loads(source.read_text(encoding="utf-8"))
if document.get("schema") != "msys.i18n.catalog.v1":
    raise SystemExit("invalid i18n catalog schema")
messages = document.get("messages")
default_locale = document.get("default_locale")
if (
    not isinstance(messages, dict)
    or not isinstance(default_locale, str)
    or not isinstance(messages.get(default_locale), dict)
):
    raise SystemExit("catalog has no complete default locale")


def locale_chain(locale: str) -> list[str]:
    chain: list[str] = []
    current = locale
    while current:
        if current not in chain:
            chain.append(current)
        current = current.rpartition("-")[0]
    if default_locale not in chain:
        chain.append(default_locale)
    return chain


def merged_messages(locale: str) -> dict[str, str]:
    selected: dict[str, str] = {}
    for candidate in reversed(locale_chain(locale)):
        overlay = messages.get(candidate)
        if isinstance(overlay, dict):
            selected.update(
                (str(key), str(value))
                for key, value in overlay.items()
                if isinstance(value, str)
            )
    return selected


english = merged_messages(default_locale)
chinese = merged_messages("zh")
chinese_cn = merged_messages("zh-CN")
required = ("app.title", "app.message")
for key in required:
    if not all(isinstance(catalog.get(key), str) for catalog in (english, chinese, chinese_cn)):
        raise SystemExit("catalog is missing " + key)


def getter(name: str, key: str) -> str:
    base = english[key]
    zh = chinese[key]
    zh_cn = chinese_cn[key]
    lines = ["static const char *msys_text_%s(void) {" % name]
    if zh_cn != zh or zh != base:
        lines.append("    enum msys_locale_kind locale = msys_selected_locale();")
    if zh_cn != zh:
        lines.append(
            "    if (locale == MSYS_LOCALE_ZH_CN) return %s;" % c_bytes(zh_cn)
        )
    if zh != base:
        lines.append(
            "    if (locale == MSYS_LOCALE_ZH || locale == MSYS_LOCALE_ZH_CN) return %s;"
            % c_bytes(zh)
        )
    lines.append("    return %s;" % c_bytes(base))
    lines.append("}")
    return "\\n".join(lines) + "\\n"

header = """#ifndef MSYS_APP_I18N_H
#define MSYS_APP_I18N_H
#include <stdlib.h>
#include <string.h>

enum msys_locale_kind {
    MSYS_LOCALE_DEFAULT = 0,
    MSYS_LOCALE_ZH = 1,
    MSYS_LOCALE_ZH_CN = 2
};

static int msys_ascii_space(char value) {
    return value == ' ' || value == '\\\\t' || value == '\\\\r' || value == '\\\\n' ||
           value == '\\\\f' || value == '\\\\v';
}

static int msys_ascii_alpha(char value) {
    return (value >= 'A' && value <= 'Z') || (value >= 'a' && value <= 'z');
}

static int msys_ascii_digit(char value) {
    return value >= '0' && value <= '9';
}

static int msys_ascii_alnum(char value) {
    return msys_ascii_alpha(value) || msys_ascii_digit(value);
}

static char msys_ascii_lower(char value) {
    return value >= 'A' && value <= 'Z' ? (char)(value + ('a' - 'A')) : value;
}

static int msys_token_equal(const char *value, const char *expected) {
    while (*value && *expected) {
        if (msys_ascii_lower(*value) != msys_ascii_lower(*expected)) return 0;
        ++value;
        ++expected;
    }
    return !*value && !*expected;
}

static size_t msys_token_length(const char *value) {
    size_t length = 0;
    while (value[length]) ++length;
    return length;
}

static const char *msys_environment_locale(void) {
    static const char *const names[] = {
        "MSYS_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG"
    };
    size_t index;
    for (index = 0; index < sizeof(names) / sizeof(names[0]); ++index) {
        const char *value = getenv(names[index]);
        const char *cursor = value;
        if (!cursor) continue;
        while (*cursor && msys_ascii_space(*cursor)) ++cursor;
        if (*cursor) return value;
    }
    return NULL;
}

static enum msys_locale_kind msys_selected_locale(void) {
    const char *value = msys_environment_locale();
    const char *start;
    const char *end;
    char normalized[64];
    char *parts[16];
    size_t length;
    size_t index;
    size_t count = 0;
    size_t cursor = 1;
    int has_script = 0;
    int has_cn_region = 0;

    if (!value) return MSYS_LOCALE_DEFAULT;
    start = value;
    while (*start && msys_ascii_space(*start)) ++start;
    end = start + strlen(start);
    while (end > start && msys_ascii_space(end[-1])) --end;
    {
        const char *separator = start;
        while (separator < end && *separator != '.' && *separator != '@') ++separator;
        end = separator;
    }
    length = (size_t)(end - start);
    if (!length || length >= sizeof(normalized)) return MSYS_LOCALE_DEFAULT;
    for (index = 0; index < length; ++index)
        normalized[index] = start[index] == '_' ? '-' : start[index];
    normalized[length] = '\\\\0';
    if (msys_token_equal(normalized, "C") || msys_token_equal(normalized, "POSIX"))
        return MSYS_LOCALE_DEFAULT;

    parts[count++] = normalized;
    for (index = 0; index < length; ++index) {
        if (normalized[index] != '-') continue;
        normalized[index] = '\\\\0';
        if (count >= sizeof(parts) / sizeof(parts[0])) return MSYS_LOCALE_DEFAULT;
        parts[count++] = normalized + index + 1;
    }
    length = msys_token_length(parts[0]);
    if (length < 2 || length > 8) return MSYS_LOCALE_DEFAULT;
    for (index = 0; index < length; ++index)
        if (!msys_ascii_alpha(parts[0][index])) return MSYS_LOCALE_DEFAULT;

    if (cursor < count && msys_token_length(parts[cursor]) == 4 &&
        msys_ascii_alpha(parts[cursor][0])) {
        length = msys_token_length(parts[cursor]);
        for (index = 0; index < length; ++index)
            if (!msys_ascii_alpha(parts[cursor][index])) return MSYS_LOCALE_DEFAULT;
        has_script = 1;
        ++cursor;
    }
    if (cursor < count) {
        length = msys_token_length(parts[cursor]);
        if ((length == 2 && msys_ascii_alpha(parts[cursor][0]) &&
             msys_ascii_alpha(parts[cursor][1])) ||
            (length == 3 && msys_ascii_digit(parts[cursor][0]) &&
             msys_ascii_digit(parts[cursor][1]) && msys_ascii_digit(parts[cursor][2]))) {
            has_cn_region = !has_script && msys_token_equal(parts[cursor], "CN");
            ++cursor;
        }
    }
    while (cursor < count) {
        length = msys_token_length(parts[cursor]);
        if (!((length >= 5 && length <= 8) ||
              (length == 4 && msys_ascii_digit(parts[cursor][0]))))
            return MSYS_LOCALE_DEFAULT;
        for (index = 0; index < length; ++index)
            if (!msys_ascii_alnum(parts[cursor][index])) return MSYS_LOCALE_DEFAULT;
        ++cursor;
    }
    if (!msys_token_equal(parts[0], "zh")) return MSYS_LOCALE_DEFAULT;
    return has_cn_region ? MSYS_LOCALE_ZH_CN : MSYS_LOCALE_ZH;
}
"""
header += getter("app_title", "app.title")
header += getter("app_message", "app.message")
header += "#endif\\n"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(header, encoding="ascii")
'''


NATIVE_MIPC_SUPPORT = r'''static int navigate_back(void)
{
    /* Root-page default. Replace this function when adding nested pages. */
    return 0;
}

static int start_component_channel(msys_mipc_client *client, char *packet)
{
    char type[32];
    int result = msys_mipc_client_from_env(client);
    if (result == MSYS_MIPC_OK)
        result = msys_mipc_send_hello_from_env(client);
    if (result == MSYS_MIPC_OK)
        result = msys_mipc_recv_json(
            client, packet, MSYS_MIPC_RECV_CAPACITY, 3000, NULL);
    if (result == MSYS_MIPC_OK)
        result = msys_mipc_json_get_string(
            packet, "type", type, sizeof(type), NULL);
    if (result == MSYS_MIPC_OK && strcmp(type, "welcome") != 0)
        result = MSYS_MIPC_INVALID_JSON;
    if (result == MSYS_MIPC_OK)
        result = msys_mipc_send_ready(client);
    return result;
}

static int handle_component_channel(msys_mipc_client *client, char *packet)
{
    char type[32];
    char method[96];
    uint64_t request_id;
    int result = msys_mipc_recv_json(
        client, packet, MSYS_MIPC_RECV_CAPACITY, 0, NULL);
    if (result == MSYS_MIPC_EOF) return 0;
    if (result != MSYS_MIPC_OK) return -1;
    if (msys_mipc_json_get_string(
            packet, "type", type, sizeof(type), NULL) != MSYS_MIPC_OK)
        return 1;
    if (strcmp(type, "shutdown") == 0) return 0;
    if (strcmp(type, "call") != 0) return 1;
    if (msys_mipc_json_get_u64(packet, "id", &request_id) != MSYS_MIPC_OK ||
        msys_mipc_json_get_string(
            packet, "method", method, sizeof(method), NULL) != MSYS_MIPC_OK)
        return 1;
    if (strcmp(method, MSYS_NAVIGATION_BACK_METHOD) == 0)
        result = msys_mipc_send_navigation_back_result(
            client, request_id, navigate_back());
    else
        result = msys_mipc_send_error(
            client, request_id, "NO_METHOD", method);
    return result == MSYS_MIPC_OK ? 1 : -1;
}
'''


def _native_source(package_id: str, component: str, *, cpp: bool) -> str:
    instance = json.dumps(component)
    app_id = json.dumps(package_id)
    if not cpp:
        return f'''#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <errno.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <msys/mipc.h>
#include "i18n_catalog.h"

{NATIVE_MIPC_SUPPORT}

int main(void) {{
    Display *display = XOpenDisplay(NULL);
    msys_mipc_client client;
    char *packet;
    int running = 1;
    if (!display) {{
        fputs("cannot open inherited X11 display\\n", stderr);
        return 2;
    }}
    packet = (char *)malloc(MSYS_MIPC_RECV_CAPACITY);
    if (packet == NULL) {{
        XCloseDisplay(display);
        return 3;
    }}
    int screen = DefaultScreen(display);
    Window window = XCreateSimpleWindow(
        display, RootWindow(display, screen), 0, 0, 320, 400, 0,
        BlackPixel(display, screen), WhitePixel(display, screen));
    XClassHint hint = {{.res_name = {instance}, .res_class = {app_id}}};
    XSetClassHint(display, window, &hint);
    XStoreName(display, window, msys_text_app_title());
    Atom close_atom = XInternAtom(display, "WM_DELETE_WINDOW", False);
    XSetWMProtocols(display, window, &close_atom, 1);
    XSelectInput(display, window, ExposureMask | StructureNotifyMask);
    XMapWindow(display, window);
    if (start_component_channel(&client, packet) != MSYS_MIPC_OK) {{
        fputs("cannot initialize inherited MSYS component channel\\n", stderr);
        free(packet);
        XDestroyWindow(display, window);
        XCloseDisplay(display);
        return 3;
    }}
    while (running) {{
        struct pollfd descriptors[2] = {{
            {{ConnectionNumber(display), POLLIN, 0}},
            {{msys_mipc_client_fd(&client), POLLIN, 0}},
        }};
        int polled = poll(descriptors, 2, -1);
        if (polled < 0) {{
            if (errno == EINTR) continue;
            break;
        }}
        if (descriptors[1].revents & (POLLIN | POLLHUP | POLLERR)) {{
            int active = handle_component_channel(&client, packet);
            if (active <= 0) running = 0;
        }}
        while (running && XPending(display)) {{
            XEvent event;
            XNextEvent(display, &event);
            if (event.type == Expose) {{
                const char *message = msys_text_app_message();
                XDrawString(display, window, DefaultGC(display, screen), 24, 64,
                            message, (int)strlen(message));
            }} else if (event.type == ClientMessage &&
                       (Atom)event.xclient.data.l[0] == close_atom) {{
                running = 0;
            }}
        }}
    }}
    msys_mipc_client_close(&client);
    free(packet);
    XDestroyWindow(display, window);
    XCloseDisplay(display);
    return 0;
}}
'''
    return f'''#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <poll.h>

#include <msys/mipc.h>
#include "i18n_catalog.h"

{NATIVE_MIPC_SUPPORT}

int main() {{
    Display *display = XOpenDisplay(nullptr);
    msys_mipc_client client{{}};
    char *packet = static_cast<char *>(std::malloc(MSYS_MIPC_RECV_CAPACITY));
    bool running = true;
    if (!display) {{
        std::cerr << "cannot open inherited X11 display\\n";
        return 2;
    }}
    if (packet == nullptr) {{
        XCloseDisplay(display);
        return 3;
    }}
    const int screen = DefaultScreen(display);
    Window window = XCreateSimpleWindow(
        display, RootWindow(display, screen), 0, 0, 320, 400, 0,
        BlackPixel(display, screen), WhitePixel(display, screen));
    XClassHint hint{{const_cast<char *>({instance}), const_cast<char *>({app_id})}};
    XSetClassHint(display, window, &hint);
    XStoreName(display, window, msys_text_app_title());
    Atom close_atom = XInternAtom(display, "WM_DELETE_WINDOW", False);
    XSetWMProtocols(display, window, &close_atom, 1);
    XSelectInput(display, window, ExposureMask | StructureNotifyMask);
    XMapWindow(display, window);
    if (start_component_channel(&client, packet) != MSYS_MIPC_OK) {{
        std::cerr << "cannot initialize inherited MSYS component channel\\n";
        std::free(packet);
        XDestroyWindow(display, window);
        XCloseDisplay(display);
        return 3;
    }}
    while (running) {{
        struct pollfd descriptors[2] = {{
            {{ConnectionNumber(display), POLLIN, 0}},
            {{msys_mipc_client_fd(&client), POLLIN, 0}},
        }};
        const int polled = poll(descriptors, 2, -1);
        if (polled < 0) {{
            if (errno == EINTR) continue;
            break;
        }}
        if (descriptors[1].revents & (POLLIN | POLLHUP | POLLERR)) {{
            const int active = handle_component_channel(&client, packet);
            if (active <= 0) running = false;
        }}
        while (running && XPending(display)) {{
            XEvent event{{}};
            XNextEvent(display, &event);
            if (event.type == Expose) {{
                const char *message = msys_text_app_message();
                XDrawString(display, window, DefaultGC(display, screen), 24, 64,
                            message, static_cast<int>(std::strlen(message)));
            }} else if (event.type == ClientMessage &&
                       static_cast<Atom>(event.xclient.data.l[0]) == close_atom) {{
                running = false;
            }}
        }}
    }}
    msys_mipc_client_close(&client);
    std::free(packet);
    XDestroyWindow(display, window);
    XCloseDisplay(display);
    return 0;
}}
'''


def _makefile(*, cpp: bool) -> str:
    compiler = "CXX" if cpp else "CC"
    flags_name = "CXXFLAGS" if cpp else "CFLAGS"
    default_flags = "-O2 -Wall -Wextra -std=c++17" if cpp else "-O2 -Wall -Wextra -std=c11"
    source = "src/main.cpp" if cpp else "src/main.c"
    cxx_setup = '''ifeq ($(origin CXX), default)
CXX := aarch64-linux-gnu-g++
endif
''' if cpp else ""
    return f'''ifeq ($(origin CC), default)
CC := aarch64-linux-gnu-gcc
endif
{cxx_setup}{flags_name} ?= {default_flags}
CPPFLAGS ?=
LDFLAGS ?=
X11_LIBS ?= -lX11
MSYS_SDK ?= ../msys-sdk
TARGET := files/bin/app
I18N_HEADER := build/i18n_catalog.h
MIPC_OBJECT := build/msys_mipc.o
MIPC_HEADER := $(MSYS_SDK)/include/msys/mipc.h
MIPC_SOURCE := $(MSYS_SDK)/src/mipc.c

.PHONY: all clean
all: $(TARGET)

$(I18N_HEADER): files/share/i18n/catalog.json tools/compile_i18n.py
\tpython3 tools/compile_i18n.py $< $@

$(MIPC_OBJECT): $(MIPC_SOURCE) $(MIPC_HEADER)
\tmkdir -p build
\t$(CC) $(CFLAGS) $(CPPFLAGS) -I$(MSYS_SDK)/include -c $(MIPC_SOURCE) -o $@

$(TARGET): {source} $(I18N_HEADER) $(MIPC_OBJECT)
\tmkdir -p files/bin
\t$({compiler}) $({flags_name}) $(CPPFLAGS) -Ibuild -I$(MSYS_SDK)/include $< \\
\t\t$(MIPC_OBJECT) -o $@ $(LDFLAGS) $(X11_LIBS)

clean:
\trm -rf build $(TARGET)
'''


QT_I18N = '''#ifndef MSYS_APP_QT_I18N_H
#define MSYS_APP_QT_I18N_H

#include <QCoreApplication>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QRegularExpression>
#include <QStringList>

namespace msys_app_i18n {

static QString normalizeLocale(QString value) {
    value = value.trimmed();
    if (value.isEmpty()) return {};
    value = value.section('@', 0, 0).section('.', 0, 0);
    if (value.compare("C", Qt::CaseInsensitive) == 0 ||
        value.compare("POSIX", Qt::CaseInsensitive) == 0)
        return {};
    static const QRegularExpression portable(
        "^[A-Za-z]{2,8}(?:[-_][A-Za-z]{4})?"
        "(?:[-_](?:[A-Za-z]{2}|[0-9]{3}))?"
        "(?:[-_](?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*$");
    if (!portable.match(value).hasMatch()) return {};
    value.replace('_', '-');
    QStringList parts = value.split('-');
    parts[0] = parts[0].toLower();
    int index = 1;
    if (index < parts.size() && parts[index].size() == 4 &&
        !parts[index].at(0).isDigit()) {
        parts[index] = parts[index].left(1).toUpper() + parts[index].mid(1).toLower();
        ++index;
    }
    if (index < parts.size() &&
        (parts[index].size() == 2 || parts[index].size() == 3)) {
        parts[index] = parts[index].toUpper();
        ++index;
    }
    while (index < parts.size()) {
        parts[index] = parts[index].toLower();
        ++index;
    }
    return parts.join('-');
}

static QString environmentLocale() {
    static const char *const keys[] = {
        "MSYS_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG"
    };
    for (const char *key : keys) {
        const QString value = qEnvironmentVariable(key);
        if (!value.trimmed().isEmpty()) return normalizeLocale(value);
    }
    return {};
}

static QStringList localeCandidates(const QString &requested,
                                    const QString &defaultLocale) {
    QString current = normalizeLocale(requested);
    if (current.isEmpty()) current = defaultLocale;
    QStringList candidates;
    while (!current.isEmpty()) {
        if (!candidates.contains(current)) candidates.append(current);
        const int split = current.lastIndexOf('-');
        if (split < 0) break;
        current.truncate(split);
    }
    if (!candidates.contains(defaultLocale)) candidates.append(defaultLocale);
    return candidates;
}

static QJsonObject loadMessages() {
    QString root = qEnvironmentVariable("MSYS_PACKAGE_ROOT");
    if (root.isEmpty()) root = QCoreApplication::applicationDirPath() + "/../..";
    QFile file(root + "/files/share/i18n/catalog.json");
    if (!file.open(QIODevice::ReadOnly)) return {};
    const QJsonObject catalog = QJsonDocument::fromJson(file.readAll()).object();
    const QJsonObject all = catalog.value("messages").toObject();
    const QString defaultLocale =
        catalog.value("default_locale").toString("en-US");
    QJsonObject selected = all.value(defaultLocale).toObject();
    const QStringList chain = localeCandidates(environmentLocale(), defaultLocale);
    for (auto iterator = chain.crbegin(); iterator != chain.crend(); ++iterator) {
        const QJsonObject overlay = all.value(*iterator).toObject();
        for (auto item = overlay.begin(); item != overlay.end(); ++item) {
            selected.insert(item.key(), item.value());
        }
    }
    return selected;
}

}  // namespace msys_app_i18n

#endif
'''


def _qt_source(package_id: str, name: str) -> str:
    return f'''#include <QApplication>
#include <QFont>
#include <QFrame>
#include <QJsonObject>
#include <QLabel>
#include <QMainWindow>
#include <QScrollArea>
#include <QSizePolicy>
#include <QSocketNotifier>
#include <QVBoxLayout>
#include <QWidget>
#include <cstdint>
#include <cstdlib>
#include <cstring>

#include <msys/mipc.h>
#include "i18n.h"

{NATIVE_MIPC_SUPPORT}

int main(int argc, char **argv) {{
    QApplication app(argc, argv);
    app.setApplicationName({json.dumps(package_id)});
    QFont uiFont = app.font();
    const QString requestedFont = qEnvironmentVariable("MSYS_UI_FONT_FAMILY").trimmed();
    if (!requestedFont.isEmpty()) uiFont.setFamily(requestedFont);
    uiFont.setPixelSize(14);
    uiFont.setStyleStrategy(
        static_cast<QFont::StyleStrategy>(
            static_cast<int>(uiFont.styleStrategy()) |
            static_cast<int>(QFont::PreferAntialias) |
            static_cast<int>(QFont::NoSubpixelAntialias)
        )
    );
    app.setFont(uiFont);
    const QJsonObject text = msys_app_i18n::loadMessages();
    QMainWindow window;
    window.setWindowTitle(text.value("app.title").toString({json.dumps(name)}));
    auto *page = new QWidget;
    page->setObjectName("page");
    page->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Preferred);
    auto *layout = new QVBoxLayout(page);
    layout->setContentsMargins(18, 22, 18, 22);
    layout->setSpacing(16);
    auto *title = new QLabel(text.value("app.title").toString({json.dumps(name)}));
    title->setObjectName("title");
    title->setWordWrap(true);
    title->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);
    auto *message = new QLabel(text.value("app.message").toString());
    message->setWordWrap(true);
    message->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);
    layout->addWidget(title);
    layout->addWidget(message);
    layout->addStretch();
    auto *scroll = new QScrollArea;
    scroll->setObjectName("pageScroll");
    scroll->setFrameShape(QFrame::NoFrame);
    scroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    scroll->setWidgetResizable(true);
    scroll->setWidget(page);
    window.setCentralWidget(scroll);
    window.resize(320, 420);
    window.setMinimumSize(240, 240);
    app.setStyleSheet(
        "QMainWindow {{ background: #f4f6fa; }}"
        "QWidget#page, QScrollArea#pageScroll {{ background: #f4f6fa; }}"
        "QWidget {{ color: #202124; font-size: 14px; }}"
        "QLabel#title {{ color: #315fbd; font-size: 22px; font-weight: 700; }}"
    );
    window.show();
    msys_mipc_client channel{{}};
    char *packet = static_cast<char *>(std::malloc(MSYS_MIPC_RECV_CAPACITY));
    if (packet == nullptr ||
        start_component_channel(&channel, packet) != MSYS_MIPC_OK) {{
        std::free(packet);
        return 3;
    }}
    QSocketNotifier control(msys_mipc_client_fd(&channel), QSocketNotifier::Read);
    QObject::connect(
        &control,
        &QSocketNotifier::activated,
        [&app, &channel, packet](auto...) {{
            if (handle_component_channel(&channel, packet) <= 0) app.quit();
        }}
    );
    const int result = app.exec();
    msys_mipc_client_close(&channel);
    std::free(packet);
    return result;
}}
'''


QT_CMAKE = '''cmake_minimum_required(VERSION 3.16)
project(msys_app LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(MSYS_SDK_ROOT "${CMAKE_SOURCE_DIR}/../msys-sdk" CACHE PATH "MSYS SDK source root")
if(NOT EXISTS "${MSYS_SDK_ROOT}/include/msys/mipc.h" OR
   NOT EXISTS "${MSYS_SDK_ROOT}/src/mipc.c")
  message(FATAL_ERROR "Set MSYS_SDK_ROOT to the msys-sdk repository")
endif()
find_package(QT NAMES Qt6 Qt5 REQUIRED COMPONENTS Widgets)
find_package(Qt${QT_VERSION_MAJOR} REQUIRED COMPONENTS Widgets)
add_library(msys-mipc STATIC "${MSYS_SDK_ROOT}/src/mipc.c")
target_include_directories(msys-mipc PUBLIC "${MSYS_SDK_ROOT}/include")
add_executable(msys-app src/main.cpp)
target_link_libraries(msys-app PRIVATE Qt${QT_VERSION_MAJOR}::Widgets msys-mipc)
set_target_properties(msys-app PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/files/bin"
    BUILD_RPATH "$ORIGIN/../runtime/qt/lib"
    INSTALL_RPATH "$ORIGIN/../runtime/qt/lib")
'''


ELECTRON_I18N = '''"use strict";

const fs = require("node:fs");
const path = require("node:path");

const ENVIRONMENT_LOCALE_KEYS = ["MSYS_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG"];

function normalizeLocale(value) {
  if (typeof value !== "string") return null;
  let raw = value.trim();
  if (!raw) return null;
  raw = raw.split("@", 1)[0].split(".", 1)[0];
  if (raw.toUpperCase() === "C" || raw.toUpperCase() === "POSIX") return null;
  raw = raw.replace(/_/g, "-");
  if (!/^[A-Za-z]{2,8}(?:-[A-Za-z]{4})?(?:-(?:[A-Za-z]{2}|[0-9]{3}))?(?:-(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*$/.test(raw)) {
    return null;
  }
  const parts = raw.split("-");
  parts[0] = parts[0].toLowerCase();
  let index = 1;
  if (index < parts.length && /^[A-Za-z]{4}$/.test(parts[index])) {
    parts[index] = parts[index][0].toUpperCase() + parts[index].slice(1).toLowerCase();
    index += 1;
  }
  if (index < parts.length && /^(?:[A-Za-z]{2}|[0-9]{3})$/.test(parts[index])) {
    parts[index] = parts[index].toUpperCase();
    index += 1;
  }
  while (index < parts.length) {
    parts[index] = parts[index].toLowerCase();
    index += 1;
  }
  return parts.join("-");
}

function localeFromEnvironment(environ = process.env) {
  for (const name of ENVIRONMENT_LOCALE_KEYS) {
    const value = environ[name];
    if (value !== undefined && String(value).trim()) {
      return normalizeLocale(String(value));
    }
  }
  return null;
}

function localeCandidates(requested, defaultLocale) {
  let current = normalizeLocale(requested) || defaultLocale;
  const candidates = [];
  while (current) {
    if (!candidates.includes(current)) candidates.push(current);
    const split = current.lastIndexOf("-");
    if (split < 0) break;
    current = current.slice(0, split);
  }
  if (!candidates.includes(defaultLocale)) candidates.push(defaultLocale);
  return candidates;
}

function mergeMessages(catalog, environ = process.env) {
  const all = catalog.messages || {};
  const defaultLocale = catalog.default_locale;
  const selected = Object.assign({}, all[defaultLocale] || {});
  const chain = localeCandidates(localeFromEnvironment(environ), defaultLocale);
  for (const locale of chain.slice().reverse()) {
    if (all[locale] && typeof all[locale] === "object") {
      Object.assign(selected, all[locale]);
    }
  }
  return selected;
}

function loadMessages(environ = process.env, filename = null) {
  const root = environ.MSYS_PACKAGE_ROOT || path.resolve(__dirname, "../..");
  const source = filename || path.join(root, "files", "share", "i18n", "catalog.json");
  return mergeMessages(JSON.parse(fs.readFileSync(source, "utf8")), environ);
}

module.exports = {
  ENVIRONMENT_LOCALE_KEYS,
  loadMessages,
  localeCandidates,
  localeFromEnvironment,
  mergeMessages,
  normalizeLocale,
};
'''


def _electron_main(package_id: str, name: str) -> str:
    return f'''"use strict";

const {{ app, BrowserWindow }} = require("electron");
const readline = require("node:readline");
const {{ loadMessages }} = require("./i18n");

app.setName({json.dumps(package_id)});
app.commandLine.appendSwitch("class", {json.dumps(package_id)});
app.commandLine.appendSwitch("disable-lcd-text");

const component = process.env.MSYS_COMPONENT_ID || {json.dumps(package_id + ':main')};
const generation = Number.parseInt(process.env.MSYS_GENERATION || "0", 10);
let protocolWelcomed = false;
let uiReady = false;
let readySent = false;

function send(message) {{
  process.stdout.write(`${{JSON.stringify(message)}}\\n`);
}}

function navigateBack() {{
  // Root-page default. Replace this function when adding nested pages.
  return false;
}}

function maybeReady() {{
  if (protocolWelcomed && uiReady && !readySent) {{
    readySent = true;
    send({{ type: "ready" }});
  }}
}}

const protocol = readline.createInterface({{
  input: process.stdin,
  crlfDelay: Infinity,
  terminal: false,
}});

protocol.on("line", line => {{
  let message;
  try {{
    message = JSON.parse(line);
  }} catch (error) {{
    console.error(`invalid mIPC input: ${{error.message}}`);
    app.quit();
    return;
  }}
  if (message.type === "welcome") {{
    protocolWelcomed = true;
    maybeReady();
  }} else if (message.type === "shutdown" || message.type === "eof") {{
    app.quit();
  }} else if (message.type === "call") {{
    if (message.method === "navigation_back") {{
      send({{
        type: "return",
        id: message.id,
        payload: {{ handled: navigateBack() === true }},
      }});
    }} else {{
      send({{
        type: "error",
        id: message.id,
        code: "NO_METHOD",
        message: String(message.method || ""),
      }});
    }}
  }}
}});

send({{ type: "hello", component, generation }});

function escapeHtml(value) {{
  return String(value).replace(/[&<>"']/g, character => ({{
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }})[character]);
}}

app.whenReady().then(() => {{
  const text = loadMessages();
  const uiFont = (process.env.MSYS_UI_FONT_FAMILY || "").trim();
  const window = new BrowserWindow({{
    width: 320,
    height: 420,
    minWidth: 240,
    minHeight: 240,
    autoHideMenuBar: true,
    backgroundColor: "#f4f6fa",
    webPreferences: {{ contextIsolation: true, nodeIntegration: false, sandbox: true }}
  }});
  window.setTitle(text["app.title"] || {json.dumps(name)});
  const html = `<!doctype html><meta charset="utf-8"><style>
    * {{ box-sizing:border-box; }}
    html {{ min-width:0; background:#f4f6fa; }}
    body {{ margin:0; min-height:100vh; overflow-x:hidden; overflow-y:auto;
            background:#f4f6fa; color:#202124; font:14px sans-serif; }}
    header {{ max-width:100%; background:#315fbd; color:white; padding:30px 22px 22px;
              font-size:22px; font-weight:700; overflow-wrap:anywhere; word-break:break-word; }}
    main {{ max-width:calc(100% - 36px); margin:18px; padding:24px; background:white;
            border-radius:14px; box-shadow:0 2px 10px #0002; white-space:pre-wrap;
            overflow-wrap:anywhere; word-break:break-word; }}
  </style><header>${{escapeHtml(text["app.title"] || {json.dumps(name)})}}</header>
  <main>${{escapeHtml(text["app.message"] || "")}}</main>`;
  window.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(html));
  if (uiFont) {{
    window.webContents.insertCSS(
      `body {{ font-family: ${{JSON.stringify(uiFont)}}, sans-serif; }}`);
  }}
  uiReady = true;
  maybeReady();
}});

app.on("window-all-closed", () => app.quit());
'''


def _readme(
    package_id: str,
    name: str,
    template: str,
    component: str,
) -> str:
    if template in {"python", "tk"}:
        preparation = '''This starter runs immediately with MSYS's isolated Python runtime.  It uses
the public `msys_sdk` shipped by MSYS plus the standard library (and `_tkinter`
for the window), so it does not run `pip` or inherit host site-packages.  For a portable third-party release,
bundle a Python/Tk runtime under `files/runtime/`, change manifest `exec` to
that package-owned interpreter, and keep dependencies inside that tree.
`ui_fonts.py` selects an installed CJK-capable family for Tk named and explicit
fonts; set `MSYS_UI_FONT_FAMILY` to override it.  This selection does not add a
font rasterizer, so anti-aliased text still requires an Xft-enabled Tk runtime.'''
        build = "No build step is required."
    elif template in {"c", "cpp"}:
        preparation = '''The manifest deliberately references `files/bin/app`, which does not exist
until you compile it for the target ABI.  This makes an incomplete package fail
validation instead of silently using a host binary.  The resulting executable
and any private loader/libraries belong below `files/`; nothing is installed
into `/usr`.'''
        make_command = (
            "make CXX=aarch64-linux-gnu-g++"
            if template == "cpp"
            else "make CC=aarch64-linux-gnu-gcc"
        )
        build = f'''Use a pre-provisioned cross compiler and X11 development sysroot:

```sh
{make_command}
```

The default `MSYS_SDK=../msys-sdk` reuses the public SDK source without copying
the protocol into this project. Set `MSYS_SDK=/path/to/msys-sdk`, `CPPFLAGS`,
`LDFLAGS` and `X11_LIBS` when your workspace or sysroot is non-standard.
The Makefile does not download anything or invoke a package manager.'''
    elif template == "qt":
        preparation = '''The compiled target must be `files/bin/app`.  Bundle the matching aarch64
Qt libraries below `files/runtime/qt/lib` and the xcb platform plugin below
`files/runtime/qt/plugins/platforms`.  A static build is also valid.  Never
fall back to target `/usr` as an implicit deployment step.'''
        build = '''Build against an already-provisioned Qt 5 or Qt 6 cross toolchain:

```sh
cmake -S . -B build -DMSYS_SDK_ROOT=/path/to/msys-sdk \
  -DCMAKE_TOOLCHAIN_FILE=/path/to/aarch64-toolchain.cmake
cmake --build build
```

Copy the runtime libraries/plugins from that same toolchain into `files/runtime/qt`.
No download or package-manager command is part of this project.'''
    else:
        preparation = '''Place a complete matching aarch64 Electron distribution below
`files/runtime/electron`; its executable must be
`files/runtime/electron/electron` and executable.  Application JavaScript is
already under `files/app` and has no npm dependencies. The manifest uses the
public `msys_sdk.stdio_bridge`; stdout is therefore reserved for its JSON-lines
component channel and diagnostics belong on stderr. Do not run npm or a
target package manager merely to launch this starter.'''
        build = '''There is no JavaScript build step.  Copy an Electron runtime obtained through
your controlled workstation/runtime pipeline, preserve its licenses and modes,
then validate the complete package.  `app run` never downloads Electron.'''

    return f'''# {name}

MSYS application `{package_id}:{component}` generated from the `{template}` template.

## Runtime boundary

{preparation}

## Build or prepare

{build}

## i18n

`files/share/i18n/catalog.json` is the language-neutral
`msys.i18n.catalog.v1` source of UI text.  It contains complete `en-US` and
language-wide `zh` maps. Add a partial locale such as `zh-CN` only when text
really differs by region; do not copy the complete `zh` map. Lookup
samples `MSYS_LOCALE`, `LC_ALL`, `LC_MESSAGES`, then `LANG`; normalizes common
POSIX spellings such as `zh_Hans_CN.UTF-8`; and merges every available parent
from the default through `zh` to the most specific locale.  C/C++ performs the
same merge when generating its allocation-free header, so a regional overlay
does not duplicate the complete Chinese catalog.  Validate the source with the
dependency-free tool in `msys-contracts`:

```sh
python3 -m tools.i18n_tool validate /path/to/project/files/share/i18n/catalog.json
```

The Tk/Python, Qt, and Electron pages wrap long text and provide vertical
scrolling at small window sizes without adding a UI service or framework
dependency.

## Application Back

The component declares `org.msys.application-navigation.v1` and becomes ready
only after its private mIPC handshake. The generated root page answers
`navigation_back` with `{{"handled":false}}`, allowing the window manager to
restore the previous task or Home. When adding nested pages, replace the
clearly marked `navigate_back` function and return true only after consuming
Back inside the application. Tk/Qt UI state must be marshalled to the toolkit
thread; the transport callback itself may run on a component I/O thread.

## Touch input

Editable applications may declare `mipc.call:role:input-method` and call the
selected `role:input-method` provider's
`show({{"mode":"en"|"zh"|"numeric"|"symbols"}})` method when a field receives
focus. Call `hide({{}})` when leaving the editing view. Address the role rather
than a package id so the same application works with the stock floating
keyboard or a replacement implemented in Qt, C/C++, Electron, or another
toolkit.

## Validate, deliver and start

From this project directory, with the target stored by `msys-dev config set`:

```sh
msys-dev app run .
```

The command composes the existing strict validate, content-hashed build,
verified `install-archive`, and exact `start-component` flows.  Useful variants:

```sh
msys-dev app run . --no-start
msys-dev app run . --output ../dist --component {component}
```

It does not modify `DEFAULT_REPOS`, install host/target packages, or add this
project to a system release automatically.  Initialize source control when
ready with `git init`.
'''


def _project_files(
    package_id: str,
    name: str,
    version: str,
    component: str,
    template: str,
) -> dict[str, ProjectFile]:
    manifest = _manifest(package_id, name, version, component, template)
    catalog = _catalog(package_id, name)
    files: dict[str, ProjectFile] = {
        "manifest.json": ProjectFile(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ),
        "files/share/i18n/catalog.json": ProjectFile(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
        ),
        "README.md": ProjectFile(
            _readme(package_id, name, template, component)
        ),
        ".gitignore": ProjectFile("build/\ndist/\n__pycache__/\n*.py[cod]\n"),
    }
    if template in {"python", "tk"}:
        files["files/app/i18n.py"] = ProjectFile(PYTHON_I18N)
        files["files/app/ui_fonts.py"] = ProjectFile(PYTHON_UI_FONTS)
        files["files/app/main.py"] = ProjectFile(
            _python_main(package_id, component)
        )
        files[".msys-packageignore"] = ProjectFile(".gitignore\n")
    elif template in {"c", "cpp"}:
        cpp = template == "cpp"
        extension = "cpp" if cpp else "c"
        files[f"src/main.{extension}"] = ProjectFile(
            _native_source(package_id, component, cpp=cpp)
        )
        files["tools/compile_i18n.py"] = ProjectFile(COMPILE_I18N, executable=True)
        files["Makefile"] = ProjectFile(_makefile(cpp=cpp))
        files["files/bin/.gitkeep"] = ProjectFile("")
        files[".msys-packageignore"] = ProjectFile(
            ".gitignore\nsrc/\ntools/\nbuild/\nMakefile\n"
        )
    elif template == "qt":
        files["src/main.cpp"] = ProjectFile(_qt_source(package_id, name))
        files["src/i18n.h"] = ProjectFile(QT_I18N)
        files["CMakeLists.txt"] = ProjectFile(QT_CMAKE)
        files["files/bin/.gitkeep"] = ProjectFile("")
        files["files/runtime/qt/lib/.gitkeep"] = ProjectFile("")
        files["files/runtime/qt/plugins/platforms/.gitkeep"] = ProjectFile("")
        files[".msys-packageignore"] = ProjectFile(
            ".gitignore\nsrc/\nbuild/\nCMakeLists.txt\n"
        )
    else:
        files["files/app/main.js"] = ProjectFile(
            _electron_main(package_id, name)
        )
        files["files/app/i18n.js"] = ProjectFile(ELECTRON_I18N)
        files["files/app/package.json"] = ProjectFile(
            json.dumps(
                {
                    "name": package_id.replace(".", "-"),
                    "version": version,
                    "private": True,
                    "main": "main.js",
                },
                indent=2,
            ) + "\n"
        )
        files["files/runtime/electron/.gitkeep"] = ProjectFile("")
        files[".msys-packageignore"] = ProjectFile(".gitignore\n")
    return files


def _safe_destination(path: Path) -> Path:
    raw = os.fspath(path)
    if not raw or "\x00" in raw or any(ord(character) < 32 for character in raw):
        raise AppFlowError("project path is invalid")
    candidate = path.expanduser().resolve()
    if candidate.exists() or candidate.is_symlink():
        raise AppFlowError(f"project path already exists; refusing to overwrite: {candidate}")
    if candidate.name in {"", ".", ".."}:
        raise AppFlowError("project path must name a new directory")
    return candidate


def _write_project(destination: Path, files: dict[str, ProjectFile]) -> None:
    created_files: list[Path] = []
    created_directories: list[Path] = []
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(mode=0o755)
        created_directories.append(destination)
        for relative, specification in sorted(files.items()):
            parts = relative.split("/")
            if any(part in {"", ".", ".."} for part in parts):
                raise AppFlowError(f"template contains an unsafe path: {relative}")
            target = destination.joinpath(*parts)
            missing: list[Path] = []
            parent = target.parent
            while parent != destination and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            target.parent.mkdir(parents=True, exist_ok=True)
            created_directories.extend(reversed(missing))
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(specification.text)
            created_files.append(target)
            target.chmod(0o755 if specification.executable else 0o644)
    except Exception as exc:
        for path in reversed(created_files):
            try:
                path.unlink()
            except OSError:
                pass
        for path in sorted(set(created_directories), key=lambda item: len(item.parts), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass
        if isinstance(exc, AppFlowError):
            raise
        raise AppFlowError(f"cannot create project {destination}: {exc}") from exc


def create_app(
    workspace: Path,
    destination: Path,
    *,
    template: str,
    package_id: str,
    name: str | None = None,
    version: str = "0.1.0",
    component: str = "main",
) -> dict[str, Any]:
    if template not in TEMPLATES:
        raise AppFlowError(f"template must be one of: {', '.join(TEMPLATES)}")
    package_id = _validate_package_id(package_id)
    component = _validate_component(component)
    if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
        raise AppFlowError("version must be a semantic version such as 0.1.0")
    selected_name = _validate_text(
        name if name is not None else _default_name(package_id),
        "name",
        maximum=128,
    )
    target = _safe_destination(destination)
    files = _project_files(
        package_id,
        selected_name,
        version,
        component,
        template,
    )
    manifest = json.loads(files["manifest.json"].text)
    try:
        load_installer_api(workspace).validate_manifest(manifest)
    except Exception as exc:
        raise AppFlowError(f"generated manifest did not pass the MSYS contract: {exc}") from exc
    _write_project(target, files)
    return {
        "schema": SCAFFOLD_SCHEMA,
        "path": str(target),
        "template": template,
        "package": package_id,
        "version": version,
        "component": f"{package_id}:{component}",
        "files": sorted(files),
    }


def _load_validated_manifest(
    workspace: Path,
    package_dir: Path,
    manifest_path: Path | None,
) -> dict[str, Any]:
    manifest = resolve_source_manifest(package_dir, manifest_path)
    try:
        if manifest.stat().st_size > 1024 * 1024:
            raise AppFlowError("manifest is too large")
        document = json.loads(manifest.read_text(encoding="utf-8-sig"))
        return load_installer_api(workspace).validate_manifest(document)
    except AppFlowError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AppFlowError(f"cannot read app manifest: {exc}") from exc
    except Exception as exc:
        raise AppFlowError(f"invalid app manifest: {exc}") from exc


def select_app_component(
    workspace: Path,
    package_dir: Path,
    requested: str | None,
    *,
    manifest_path: Path | None = None,
) -> str:
    source = package_dir.expanduser().resolve()
    document = _load_validated_manifest(workspace, source, manifest_path)
    package_id = str(document["package"]["id"])
    components = {str(item["id"]): item for item in document["components"]}
    if requested:
        if ":" in requested:
            requested_package, component = requested.split(":", 1)
            if requested_package != package_id:
                raise AppFlowError(
                    f"component package {requested_package!r} does not match {package_id!r}"
                )
        else:
            component = requested
        if component not in components:
            raise AppFlowError(f"component {component!r} is not declared by {package_id}")
        return f"{package_id}:{component}"

    launchable = [
        component
        for component, item in components.items()
        if isinstance(item.get("activation"), dict)
        and item["activation"].get("launchable") is True
    ]
    if len(launchable) != 1:
        raise AppFlowError(
            "app run needs --component because the manifest does not have exactly "
            "one launchable component"
        )
    return f"{package_id}:{launchable[0]}"
