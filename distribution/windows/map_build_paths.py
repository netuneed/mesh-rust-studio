# SPDX-License-Identifier: GPL-2.0-or-later
"""Remove local build-root prefixes from read-only data in unsigned project binaries."""

import re
import struct


def map_paths(path, roots):
    data = bytearray(path.read_bytes())
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[:2] != b"MZ" or data[pe : pe + 4] != b"PE\0\0":
        raise ValueError(f"Invalid Windows executable {path.name}")
    count = struct.unpack_from("<H", data, pe + 6)[0]
    optional = pe + 24
    table = optional + struct.unpack_from("<H", data, pe + 20)[0]
    magic = struct.unpack_from("<H", data, optional)[0]
    dirs = optional + {0x20B: 112, 0x10B: 96}[magic]
    if any(struct.unpack_from("<II", data, dirs + 4 * 8)):
        raise ValueError(f"Signed input cannot be changed: {path.name}")
    sections = []
    for index in range(count):
        s = table + 40 * index
        size, offset = struct.unpack_from("<II", data, s + 16)
        flags = struct.unpack_from("<I", data, s + 36)[0]
        if flags & 0x40000000 and not flags & 0xA0000000:
            sections.append((offset, offset + size))
    changed = 0
    for root in sorted(set(roots), key=lambda s: -len(str(s))):
        for sep in ("\\", "/"):
            raw = str(root).replace("\\", sep).replace("/", sep).rstrip(sep)
            if len(raw) < 4 or raw[1:3] != ":" + sep:
                raise ValueError("Map only absolute build directory prefixes")
            stem = "X:" + sep + ("src" + sep if len(raw) >= 7 else "")
            repl = stem.ljust(len(raw), "_")
            for enc in ("ascii", "utf-16-le"):
                pattern = re.compile(re.escape(raw.encode(enc)), re.I)
                replace = repl.encode(enc)
                for match in pattern.finditer(data):
                    a, b = match.span()
                    if not any(start <= a and b <= end for start, end in sections):
                        raise ValueError(
                            f"Build path outside read-only data in {path.name}"
                        )
                    data[a:b] = replace
                    changed += 1
    if changed:
        path.write_bytes(data)
    return changed
