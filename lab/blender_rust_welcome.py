# SPDX-License-Identifier: GPL-2.0-or-later
"""First-run setup for the portable Mesh Rust Studio Windows packages."""

import io
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import sys

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from _blendfile_header import BlendFileHeader, BlendHeaderError, BlockHeader


MAX_PREFS_BYTES = 8 * 1024 * 1024
PROFILE_VERSION = re.compile(r"[0-9]+\.[0-9]+\Z")
# Exact native DNA names and their public RNA equivalents. No script-related settings.
VALUE_FIELDS = {
    "ui_scale": ("view", "ui_scale", "f", 0.5, 3.0),
    "dbl_click_time": ("inputs", "mouse_double_click_time", "h", 1, 1000),
    "undosteps": ("edit", "undo_steps", "h", 0, 256),
    "undomemory": ("edit", "undo_memory_limit", "i", 0, 65536),
    "versions": ("filepaths", "save_version", "h", 0, 32),
    "savetime": ("filepaths", "auto_save_time", "B", 1, 60),
}
# From eUserpref_UI_Flag and eUserPref_Flag in DNA_userdef_types.h.
BIT_FIELDS = {
    "uiflag": (("inputs", "use_rotate_around_active", 1 << 14),
               ("inputs", "use_zoom_to_mouse", 1 << 20)),
    "flag": (("inputs", "use_mouse_emulate_3_button", 1 << 12),),
}
WANTED_DNA = {b"UserDef", b"bUserAssetLibrary", b"bUserExtensionRepo"}


def _local_path(path):
    """Resolve existing local paths without probing a network path or following a redirect."""
    path = Path(path)
    if sys.platform != "win32" or not path.is_absolute() or not re.fullmatch(r"[A-Za-z]:", path.drive):
        return None
    if any(part in {".", ".."} or ":" in part for part in path.parts[1:]):
        return None

    import ctypes
    if ctypes.windll.kernel32.GetDriveTypeW(path.anchor) not in {2, 3}:
        return None

    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            # Reject junctions and symlinks before stat/open can follow their targets.
            if current.is_junction() or current.is_symlink():
                return None
        return current if current.exists() else None
    except OSError:
        return None


def _profile_file(root):
    path = _local_path(Path(root) / "config" / "userpref.blend")
    return path if path is not None and path.is_file() else None


def _latest_profile():
    roaming = os.environ.get("BLENDER_RUST_ORIGINAL_APPDATA")
    if not roaming:
        return ""
    root = _local_path(Path(roaming) / "Blender Foundation" / "Blender")
    if root is None or not root.is_dir():
        return ""
    try:
        versions = [(tuple(map(int, item.name.split("."))), item)
                    for item in root.iterdir() if PROFILE_VERSION.fullmatch(item.name)]
    except OSError:
        return ""
    for version, folder in sorted(versions, reverse=True):
        if (4, 2) <= version <= bpy.app.version[:2] and _profile_file(folder):
            return str(folder)
    return ""


def _dna_fields(data, endian, pointer_size):
    """Decode just enough SDNA to find the sizes and offsets of allowlisted fields."""
    cursor = 0

    def expect(token):
        nonlocal cursor
        if data[cursor:cursor + 4] != token:
            raise ValueError("Invalid Blender SDNA")
        cursor += 4

    def number(code):
        nonlocal cursor
        value, = struct.unpack_from(endian + code, data, cursor)
        cursor += struct.calcsize(code)
        return value

    def names(token):
        nonlocal cursor
        expect(token)
        count = number("I")
        if count > min(65536, len(data)):
            raise ValueError("Invalid Blender SDNA name count")
        result = []
        for _ in range(count):
            end = data.index(0, cursor)
            result.append(data[cursor:end])
            cursor = end + 1
        cursor = (cursor + 3) & ~3
        return result

    expect(b"SDNA")
    identifiers = names(b"NAME")
    types = names(b"TYPE")
    expect(b"TLEN")
    sizes = [number("H") for _ in types]
    cursor = (cursor + 3) & ~3
    expect(b"STRC")
    count = number("I")
    if count > len(data) // 4:
        raise ValueError("Invalid Blender SDNA struct count")
    result = {}
    for index in range(count):
        type_index = number("H")
        fields_count = number("H")
        target = types[type_index]
        if target not in WANTED_DNA:
            if (cursor := cursor + fields_count * 4) > len(data):
                raise ValueError("Invalid Blender SDNA fields")
            continue
        fields = {}
        offset = 0
        for _ in range(fields_count):
            field_type = number("H")
            identifier = identifiers[number("H")]
            if len(identifier) > 512:
                raise ValueError("Invalid Blender SDNA field name")
            match = re.search(rb"[a-zA-Z_][a-zA-Z_0-9]*", identifier)
            if not match:
                raise ValueError("Invalid Blender SDNA field name")
            elements = math.prod(int(size) for size in re.findall(rb"\[([0-9]+)\]", identifier))
            size = (pointer_size if b"*" in identifier.split(b")")[0] else sizes[field_type]) * elements
            fields[match[0].decode("ascii")] = (offset, size)
            offset += size
        if offset != sizes[type_index]:
            raise ValueError("Unsupported Blender SDNA layout")
        result[index] = (target, fields, offset)
    return result


def _read_preferences(path):
    """Read fixed-format data only; never load foreign preferences into Blender."""
    with path.open("rb") as stream:
        contents = stream.read(MAX_PREFS_BYTES + 1)
    if len(contents) > MAX_PREFS_BYTES:
        raise ValueError("Blender preferences exceed the import size limit")
    stream = io.BytesIO(contents)
    header = BlendFileHeader(stream)
    current = bpy.app.version[0] * 100 + bpy.app.version[1]
    if not 402 <= header.version <= current:
        raise ValueError("Unsupported Blender preferences version")
    endian = "<" if header.is_little_endian else ">"
    layout = header.create_block_header_struct()
    records = []
    dna = None
    while stream.tell() < len(contents):
        block = BlockHeader(stream, layout)
        if block.code == b"ENDB":
            break
        start = stream.tell()
        if not 0 <= block.size <= len(contents) - start:
            raise ValueError("Invalid Blender preference block")
        if block.code == b"DNA1":
            dna = contents[start:start + block.size]
        elif block.count == 1 and block.code in {b"USER", b"DATA"}:
            if len(records) >= 16384:
                raise ValueError("Too many Blender preference blocks")
            records.append((block.code, block.sdna_index, start, block.size))
        stream.seek(start + block.size)
    if dna is None:
        raise ValueError("Blender preference schema is missing")

    schema = _dna_fields(dna, endian, header.pointer_size)
    settings = {}
    assets = []
    repos = []
    found = False
    for code, index, start, size in records:
        if index not in schema:
            continue
        name, fields, expected = schema[index]
        if size != expected:
            continue

        def text(field):
            if field not in fields:
                return ""
            offset, length = fields[field]
            return contents[start + offset:start + offset + length].split(b"\0", 1)[0].decode(
                "utf-8", errors="replace")

        def number(field, format):
            if field not in fields:
                return None
            offset, length = fields[field]
            if length != struct.calcsize(format):
                return None
            return struct.unpack_from(endian + format, contents, start + offset)[0]

        if code == b"USER" and name == b"UserDef":
            if found:
                raise ValueError("Duplicate Blender preference block")
            found = True
            for field, (section, property, format, minimum, maximum) in VALUE_FIELDS.items():
                value = number(field, format)
                if value is not None and minimum <= value <= maximum:
                    settings[section, property] = value
            for field, flags in BIT_FIELDS.items():
                value = number(field, "I")
                if value is not None:
                    settings.update({(section, property): bool(value & flag)
                                     for section, property, flag in flags})
        elif code == b"DATA" and name == b"bUserAssetLibrary" and not text("remote_url"):
            assets.append((text("name"), text("dirpath") or text("path")))
        elif code == b"DATA" and name == b"bUserExtensionRepo":
            directory = text("custom_dirpath")
            module = text("module")
            if not directory and re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]{0,47}", module):
                directory = str(path.parent.parent / "extensions" / module)
            repos.append((text("name"), directory))
    if not found:
        raise ValueError("No Blender user preferences found")
    return settings, assets[:128], repos[:64]


def _own_config():
    explicit = os.environ.get("BLENDER_USER_RESOURCES")
    if not explicit:
        return None
    expected = os.path.normcase(os.path.normpath(str(Path(explicit) / "config")))
    actual = os.path.normcase(os.path.normpath(bpy.utils.user_resource("CONFIG")))
    if expected != actual:
        return None
    root = _local_path(explicit)
    if root is None or not root.is_dir():
        return None
    config = root / "config"
    if config.is_symlink() or config.is_junction() or (config.exists() and not config.is_dir()):
        return None
    preferences = config / "userpref.blend"
    if preferences.is_symlink() or preferences.is_junction():
        return None
    return config


def _directory_key(path):
    return os.path.normcase(os.path.normpath(str(path))) if path else ""


def _attach_assets(preferences, items):
    libraries = preferences.filepaths.asset_libraries
    known = {_directory_key(library.path) for library in libraries}
    added = 0
    for name, location in items:
        path = _local_path(location)
        if path is None or not path.is_dir() or _directory_key(path) in known:
            continue
        name = "".join(character for character in name if character.isprintable()).strip()
        libraries.new(name=(name or path.name)[:60], directory=str(path))
        known.add(_directory_key(path))
        added += 1
    return added


def _attach_extensions(preferences, items):
    # New module identifiers ensure that the old profile's enabled add-ons are not restored.
    repos = preferences.extensions.repos
    known = {_directory_key(repo.directory) for repo in repos}
    added = 0
    for name, location in items:
        path = _local_path(location)
        if path is None or not path.is_dir() or _directory_key(path) in known:
            continue
        # Empty folders are not installed repositories. Never import remote URLs or tokens.
        try:
            found = any((child / "blender_manifest.toml").is_file() for child in path.iterdir()
                        if child.is_dir() and not child.is_symlink() and not child.is_junction())
        except OSError:
            continue
        if not found:
            continue
        name = "".join(character for character in name if character.isprintable()).strip()
        created = repos.new(name=("Shared " + (name or path.name))[:60],
                            module="rust_shared", custom_directory=str(path))
        created.use_sync_on_startup = False
        known.add(_directory_key(path))
        added += 1
    return added


def _enabled():
    return sys.platform == "win32" and os.environ.get("BLENDER_RUST_PUBLIC") == "1"


class BLENDER_RUST_OT_default_apps(bpy.types.Operator):
    bl_idname = "blender_rust.default_apps"
    bl_label = "Choose default app"

    @classmethod
    def poll(cls, _context):
        return _enabled() and os.environ.get("BLENDER_RUST_PUBLIC_EDITION") == "work"

    def execute(self, _context):
        launcher = Path(bpy.app.binary_path).with_name("Mesh Rust Studio.exe")
        if not launcher.is_file():
            self.report({"ERROR"}, "The Work launcher is missing")
            return {"CANCELLED"}
        try:
            subprocess.Popen([str(launcher), "--default-apps"], shell=False)
        except OSError:
            self.report({"ERROR"}, "Windows Default apps could not be opened")
            return {"CANCELLED"}
        return {"FINISHED"}


class BLENDER_RUST_OT_setup(bpy.types.Operator):
    bl_idname = "blender_rust.setup"
    bl_label = "Mesh Rust Studio Setup"

    language: EnumProperty(
        name="Language", items=(("EN", "English", ""), ("RU", "Русский", "")), default="EN")
    source: StringProperty(name="Blender profile", subtype="DIR_PATH")
    import_preferences: BoolProperty(name="Import interface and navigation settings", default=True)
    attach_assets: BoolProperty(name="Link existing local asset libraries", default=True)
    attach_extensions: BoolProperty(name="Link installed local extension repositories", default=False)
    extra_assets: StringProperty(name="Extra asset folder", subtype="DIR_PATH")

    @classmethod
    def poll(cls, _context):
        return _enabled() and _own_config() is not None

    def invoke(self, context, _event):
        self.source = _latest_profile()
        if not self.source:
            self.import_preferences = False
            self.attach_assets = False
        return context.window_manager.invoke_props_dialog(self, width=535, confirm_text="Apply")

    def draw(self, context):
        layout = self.layout
        russian = self.language == "RU"

        def translated(english, russian_text):
            return russian_text if russian else english

        layout.prop(self, "language", expand=True)
        layout.label(text=translated("Use an existing local Blender profile",
                                     "Подключите существующий профиль Blender"))
        layout.prop(self, "source", text=translated("Profile folder", "Папка профиля"))
        layout.prop(self, "import_preferences", text=translated(
            "Import display scale, navigation, Undo and backups", "Импорт масштаба, навигации, Undo и копий"))
        layout.prop(self, "attach_assets", text=translated(
            "Link local asset libraries", "Подключить локальные библиотеки ассетов"))
        layout.prop(self, "extra_assets", text=translated("Extra asset folder", "Дополнительная папка ассетов"))
        layout.prop(self, "attach_extensions", text=translated(
            "Link trusted local extension repositories", "Подключить доверенные локальные репозитории"))
        if self.attach_extensions:
            warning = layout.box()
            warning.label(text=translated("Enable add-ons manually after reviewing them.",
                                          "Проверяйте аддоны и включайте их вручную."))
            warning.label(text=translated("Removing shared extensions affects both installations.",
                                          "Удаление общего расширения затронет обе установки."))
        if BLENDER_RUST_OT_default_apps.poll(context):
            layout.operator("blender_rust.default_apps", icon="PREFERENCES",
                            text=translated("Choose default app in Windows", "Приложение по умолчанию в Windows"))
        layout.label(text=translated("Assets and extensions stay in their existing folders.",
                                     "Ассеты и расширения остаются в своих папках."))

    def execute(self, context):
        config = _own_config()
        if config is None:
            return {"CANCELLED"}
        preferences = context.preferences
        settings, assets, repos = {}, [], []
        if self.import_preferences or self.attach_assets or self.attach_extensions:
            source = _profile_file(self.source) if self.source else None
            if source is None or _directory_key(source.parent) == _directory_key(config):
                detail = ("Выберите локальную папку предыдущего профиля Blender." if self.language == "RU"
                          else "Choose a local folder from an earlier Blender profile.")
                self.report({"ERROR"}, detail)
                return {"CANCELLED"}
            try:
                settings, assets, repos = _read_preferences(source)
            except (OSError, ValueError, IndexError, struct.error, BlendHeaderError):
                detail = ("Не удалось прочитать настройки Blender 4.2 или новее." if self.language == "RU"
                          else "Could not read preferences from Blender 4.2 or newer.")
                self.report({"ERROR"}, detail)
                return {"CANCELLED"}
        extra = _local_path(self.extra_assets) if self.extra_assets else None
        if self.extra_assets and (extra is None or not extra.is_dir()):
            self.report({"ERROR"}, "Choose an existing local asset folder.")
            return {"CANCELLED"}

        if self.import_preferences:
            for (section, property), value in settings.items():
                setattr(getattr(preferences, section), property, value)
        linked_assets = assets if self.attach_assets else []
        if extra is not None:
            linked_assets.append((extra.name, str(extra)))
        added_assets = _attach_assets(preferences, linked_assets)
        added_repos = _attach_extensions(preferences, repos) if self.attach_extensions else 0
        if self.import_preferences or added_assets or added_repos:
            if bpy.ops.wm.save_userpref() != {"FINISHED"}:
                self.report({"ERROR"}, "Could not save the Mesh Rust Studio profile.")
                return {"CANCELLED"}
        message = (f"Подключено библиотек ассетов {added_assets}, репозиториев {added_repos}."
                   if self.language == "RU" else
                   f"Linked {added_assets} asset libraries and {added_repos} extension repositories.")
        self.report({"INFO"}, message)
        return {"FINISHED"}


def _draw_setup(self, _context):
    layout = self.layout
    layout.separator()
    layout.operator_context = "INVOKE_DEFAULT"
    layout.operator("blender_rust.setup", icon="PREFERENCES")


def register():
    if not _enabled():
        return
    bpy.utils.register_class(BLENDER_RUST_OT_default_apps)
    bpy.utils.register_class(BLENDER_RUST_OT_setup)
    bpy.types.WM_MT_splash_quick_setup.append(_draw_setup)
    bpy.types.WM_MT_splash.append(_draw_setup)


def unregister():
    if not _enabled():
        return
    bpy.types.WM_MT_splash.remove(_draw_setup)
    bpy.types.WM_MT_splash_quick_setup.remove(_draw_setup)
    bpy.utils.unregister_class(BLENDER_RUST_OT_setup)
    bpy.utils.unregister_class(BLENDER_RUST_OT_default_apps)
