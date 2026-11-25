"""Builds the wheels: one per platform with rbridge inside, and a pure one.

    python3 scripts/build_wheels.py --binaries ../rbridge/bin --out dist

--binaries is a folder of rbridge builds named as ``make cross`` in the
rbridge repository names them: rbridge-linux-amd64, rbridge-windows-arm64.exe
and so on. A platform with no binary there gets no wheel. The pure wheel
(py3-none-any) has no binary and no rbridge command; pip falls back to it on
platforms without a build, and the SDK then uses an rbridge installed
separately.

Wheels are written directly (they are zip files with a fixed layout), so the
build needs only the standard library and Python 3.11 or later for tomllib.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import os
import sys
import tomllib
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(HERE, "src", "rbridge")

# (Go os, Go arch, wheel platform tag). Go builds are static, so one Linux
# wheel serves both glibc and musl systems.
TARGETS = [
    ("linux", "amd64", "manylinux_2_17_x86_64.manylinux2014_x86_64.musllinux_1_1_x86_64"),
    ("linux", "arm64", "manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64"),
    ("darwin", "amd64", "macosx_10_12_x86_64"),
    ("darwin", "arm64", "macosx_11_0_arm64"),
    ("windows", "amd64", "win_amd64"),
    ("windows", "arm64", "win_arm64"),
]
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def metadata(project: dict) -> str:
    lines = [
        "Metadata-Version: 2.1",
        f"Name: {project['name']}",
        f"Version: {project['version']}",
        f"Summary: {project['description']}",
        f"Requires-Python: {project['requires-python']}",
        f"License: {project['license']['text']}",
    ]
    for label, url in project.get("urls", {}).items():
        lines.append(f"Project-URL: {label}, {url}")
    lines.append("Description-Content-Type: text/markdown")
    with open(os.path.join(HERE, project["readme"]), encoding="utf-8") as f:
        return "\n".join(lines) + "\n\n" + f.read()


class Wheel:
    def __init__(self, path: str):
        self.zip = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
        self.records: list[tuple[str, str, int]] = []

    def add(self, name: str, data: bytes, executable: bool = False) -> None:
        info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED
        # A regular file (S_IFREG) with its mode, made on Unix: pip keeps the
        # executable bit only when all three are there.
        info.create_system = 3
        info.external_attr = (0o100755 if executable else 0o100644) << 16
        self.zip.writestr(info, data)
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        self.records.append((name, f"sha256={digest}", len(data)))

    def close(self, record_name: str) -> None:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        for row in self.records:
            writer.writerow(row)
        writer.writerow((record_name, "", ""))
        info = zipfile.ZipInfo(record_name, date_time=FIXED_TIME)
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        self.zip.writestr(info, buf.getvalue())
        self.zip.close()


def build(out: str, project: dict, tag: str, binary: str | None, exe_name: str) -> str:
    name, version = project["name"], project["version"]
    dist_info = f"{name}-{version}.dist-info"
    path = os.path.join(out, f"{name}-{version}-py3-none-{tag}.whl")
    w = Wheel(path)
    for entry in sorted(os.listdir(PACKAGE)):
        if entry.endswith(".py") or entry == "py.typed":
            with open(os.path.join(PACKAGE, entry), "rb") as f:
                w.add(f"rbridge/{entry}", f.read())
    if binary:
        with open(binary, "rb") as f:
            w.add(f"rbridge/_bin/{exe_name}", f.read(), executable=True)
        w.add(f"{dist_info}/entry_points.txt", b"[console_scripts]\nrbridge = rbridge._cli:main\n")
    w.add(f"{dist_info}/METADATA", metadata(project).encode())
    tags = "".join(f"Tag: py3-none-{t}\n" for t in tag.split("."))
    w.add(f"{dist_info}/WHEEL", f"Wheel-Version: 1.0\nGenerator: rbridge build_wheels\nRoot-Is-Purelib: {'false' if binary else 'true'}\n{tags}".encode())
    with open(os.path.join(HERE, "LICENSE"), "rb") as f:
        w.add(f"{dist_info}/LICENSE", f.read())
    w.close(f"{dist_info}/RECORD")
    return path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--binaries", required=True, help="folder with rbridge-<os>-<arch>[.exe] builds")
    p.add_argument("--out", default=os.path.join(HERE, "dist"))
    p.add_argument("--only", help="build only this platform, as os-arch, such as linux-amd64")
    a = p.parse_args()
    with open(os.path.join(HERE, "pyproject.toml"), "rb") as f:
        project = tomllib.load(f)["project"]
    os.makedirs(a.out, exist_ok=True)
    built = []
    for goos, goarch, tag in TARGETS:
        if a.only and a.only != f"{goos}-{goarch}":
            continue
        ext = ".exe" if goos == "windows" else ""
        binary = os.path.join(a.binaries, f"rbridge-{goos}-{goarch}{ext}")
        if not os.path.isfile(binary):
            print(f"skip {goos}/{goarch}: no {binary}", file=sys.stderr)
            continue
        built.append(build(a.out, project, tag, binary, "rbridge" + ext))
    built.append(build(a.out, project, "any", None, ""))
    for path in built:
        print(os.path.relpath(path, HERE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
