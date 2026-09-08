from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from vmware2qcow2.drivers import (
    bundled_iso_candidates,
    bundled_winpe_candidates,
    get_bundle,
)
from vmware2qcow2.runtime import resolve_runtime
from vmware2qcow2.winpe import infer_winpe_architecture


@dataclass(frozen=True)
class OfflineProfile:
    """Asset requirements for a purpose-built offline portable bundle."""

    name: str
    description: str
    driver_versions: tuple[str, ...]
    requires_winpe: bool
    requires_qemu_system: bool


OFFLINE_PROFILES: dict[str, OfflineProfile] = {
    "universal": OfflineProfile(
        name="universal",
        description=(
            "Windows and Linux conversion bundle with both supported "
            "virtio-win releases and prepared WinPE"
        ),
        driver_versions=("0.1.173-9", "0.1.285-1"),
        requires_winpe=True,
        requires_qemu_system=True,
    ),
    "windows7": OfflineProfile(
        name="windows7",
        description=(
            "Windows 7 legacy bundle with virtio-win-0.1.173-9 and "
            "prepared WinPE/QEMU Guest Agent bake assets"
        ),
        driver_versions=("0.1.173-9",),
        requires_winpe=True,
        requires_qemu_system=True,
    ),
    "linux": OfflineProfile(
        name="linux",
        description=(
            "Linux disk-conversion bundle; VirtIO and DHCP preparation "
            "uses the guest kernel and generated Linux script"
        ),
        driver_versions=(),
        requires_winpe=False,
        requires_qemu_system=False,
    ),
}


def _offline_profile(name: str) -> OfflineProfile:
    try:
        return OFFLINE_PROFILES[name]
    except KeyError as exc:
        choices = ", ".join(sorted(OFFLINE_PROFILES))
        raise RuntimeError(
            f"Unsupported offline profile {name!r}; choose one of: {choices}"
        ) from exc


def _copy_asset(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_driver_asset(
    version: str,
    explicit: str | None,
    driver_dir: str | None,
) -> Path | None:
    bundle = get_bundle(version)
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if not candidate.is_file():
            raise RuntimeError(f"VirtIO ISO was not found: {candidate}")
        return candidate

    candidates: list[Path] = []
    if driver_dir:
        candidates.append(
            Path(driver_dir).expanduser().resolve() / bundle.filename
        )
    candidates.extend(bundled_iso_candidates(version))
    candidates.extend(
        (
            PROJECT_ROOT / "assets" / "drivers" / bundle.filename,
            PROJECT_ROOT / "work" / "driver-assets" / bundle.filename,
        )
    )
    for candidate in dict.fromkeys(candidates):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _resolve_driver_assets(args: argparse.Namespace) -> dict[str, Path | None]:
    return {
        "0.1.173-9": _resolve_driver_asset(
            "0.1.173-9",
            args.virtio_win_old,
            args.driver_dir,
        ),
        "0.1.285-1": _resolve_driver_asset(
            "0.1.285-1",
            args.virtio_win_new,
            args.driver_dir,
        ),
    }


def _resolve_winpe_asset(
    explicit: str | None,
    expected_arch: str = "amd64",
) -> Path | None:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if not candidate.is_file():
            raise RuntimeError(f"WinPE ISO was not found: {candidate}")
        inferred = infer_winpe_architecture(candidate)
        if inferred and inferred != expected_arch:
            raise RuntimeError(
                f"WinPE ISO name indicates {inferred}, expected {expected_arch}: "
                f"{candidate}"
            )
        return candidate
    candidates = [
        *bundled_winpe_candidates(),
        PROJECT_ROOT / "assets" / "winpe" / "winpe-amd64.iso",
        PROJECT_ROOT / "work" / "winpe-assets" / "winpe-amd64.iso",
    ]
    for candidate in dict.fromkeys(candidates):
        if (
            candidate.is_file()
            and (
                infer_winpe_architecture(candidate) is None
                or infer_winpe_architecture(candidate) == expected_arch
            )
        ):
            return candidate.resolve()
    return None


def _command_output(argv: list[str]) -> str:
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"Runtime dependency command failed: {' '.join(argv)}\n{detail}"
        )
    return completed.stdout


def _macho_dependencies(path: Path) -> tuple[str, ...]:
    lines = _command_output(["otool", "-L", str(path)]).splitlines()
    return tuple(
        line.strip().split(" (", 1)[0]
        for line in lines[1:]
        if line.strip()
    )


def _macho_rpaths(path: Path) -> tuple[str, ...]:
    lines = _command_output(["otool", "-l", str(path)]).splitlines()
    rpaths: list[str] = []
    for index, line in enumerate(lines):
        if "cmd LC_RPATH" not in line:
            continue
        for candidate in lines[index + 1 : index + 5]:
            match = re.search(r"path (.+) \(offset", candidate)
            if match:
                rpaths.append(match.group(1))
                break
    return tuple(rpaths)


def _macho_resolve_dependency(
    dependency: str,
    loader: Path,
    executable: Path,
    rpaths: tuple[str, ...],
) -> Path | None:
    if dependency.startswith("/"):
        candidate = Path(dependency)
        return candidate if candidate.exists() else None

    if dependency.startswith("@loader_path/"):
        candidate = loader.parent / dependency.removeprefix("@loader_path/")
        return candidate if candidate.exists() else None
    if dependency.startswith("@executable_path/"):
        candidate = executable.parent / dependency.removeprefix("@executable_path/")
        return candidate if candidate.exists() else None
    if dependency.startswith("@rpath/"):
        suffix = dependency.removeprefix("@rpath/")
        for rpath in rpaths:
            base = rpath.replace("@loader_path", str(loader.parent))
            base = base.replace("@executable_path", str(executable.parent))
            candidate = Path(base) / suffix
            if candidate.exists():
                return candidate
        return None
    return None


def _is_macos_system_library(path: Path) -> bool:
    return str(path).startswith(("/usr/lib/", "/System/Library/"))


def _bundle_macos_dependencies(
    executable: Path,
    library_dir: Path,
) -> tuple[str, ...]:
    library_dir.mkdir(parents=True, exist_ok=True)
    executable = executable.resolve()
    executable.chmod(executable.stat().st_mode | 0o200)
    pending: list[Path] = [executable]
    seen: set[Path] = set()
    copied: dict[Path, Path] = {}
    dependency_records: list[tuple[Path, Path, tuple[str, ...]]] = []

    while pending:
        current = pending.pop()
        current = current.resolve()
        if current in seen:
            continue
        seen.add(current)
        dependencies = _macho_dependencies(current)
        rpaths = _macho_rpaths(current)
        destination = executable if current == executable else copied[current]
        dependency_records.append((current, destination, dependencies))
        for dependency in dependencies:
            resolved = _macho_resolve_dependency(
                dependency,
                current,
                executable,
                rpaths,
            )
            if resolved is None or _is_macos_system_library(resolved):
                continue
            resolved = resolved.resolve()
            if resolved not in copied:
                destination = library_dir / resolved.name
                existing = next(
                    (
                        source
                        for source, candidate in copied.items()
                        if candidate == destination and source != resolved
                    ),
                    None,
                )
                if existing is not None:
                    raise RuntimeError(
                        "macOS runtime library name collision: "
                        f"{existing} and {resolved}"
                    )
                shutil.copy2(resolved, destination)
                destination.chmod(destination.stat().st_mode | 0o200)
                copied[resolved] = destination
                pending.append(resolved)

    for current, destination, dependencies in dependency_records:
        if destination == executable:
            subprocess.run(
                [
                    "install_name_tool",
                    "-add_rpath",
                    "@loader_path/../lib",
                    str(destination),
                ],
                check=True,
            )
        else:
            subprocess.run(
                [
                    "install_name_tool",
                    "-id",
                    f"@rpath/{destination.name}",
                    str(destination),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "install_name_tool",
                    "-add_rpath",
                    "@loader_path",
                    str(destination),
                ],
                check=True,
            )

        rpaths = _macho_rpaths(current)
        for dependency in dependencies:
            resolved = _macho_resolve_dependency(
                dependency,
                current,
                executable,
                rpaths,
            )
            if resolved is None:
                continue
            resolved = resolved.resolve()
            replacement = copied.get(resolved)
            if replacement is None:
                continue
            subprocess.run(
                [
                    "install_name_tool",
                    "-change",
                    dependency,
                    f"@rpath/{replacement.name}",
                    str(destination),
                ],
                check=True,
            )

    for library in sorted(copied.values()):
        subprocess.run(
            ["codesign", "--force", "--sign", "-", str(library)],
            check=True,
            capture_output=True,
            text=True,
        )
    subprocess.run(
        ["codesign", "--force", "--sign", "-", str(executable)],
        check=True,
        capture_output=True,
        text=True,
    )

    return tuple(path.name for path in sorted(copied.values()))


def _linux_dependencies(path: Path) -> tuple[Path, ...]:
    output = _command_output(["ldd", str(path)])
    dependencies: list[Path] = []
    for line in output.splitlines():
        match = re.search(r"=>\s+(/[^\s]+)", line)
        if match is None:
            match = re.match(r"\s*(/[^\s]+)\s+\(", line)
        if match is None:
            continue
        candidate = Path(match.group(1))
        if not candidate.exists():
            continue
        name = candidate.name
        if name.startswith(
            (
                "ld-linux",
                "libc.so",
                "libm.so",
                "libpthread.so",
                "libdl.so",
                "librt.so",
                "libresolv.so",
                "libutil.so",
                "linux-vdso",
            )
        ):
            continue
        dependencies.append(candidate.resolve())
    return tuple(dict.fromkeys(dependencies))


def _bundle_linux_dependencies(
    executable: Path,
    library_dir: Path,
) -> tuple[str, ...]:
    library_dir.mkdir(parents=True, exist_ok=True)
    pending = [executable.resolve()]
    seen: set[Path] = set()
    copied: dict[Path, Path] = {}
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for dependency in _linux_dependencies(current):
            destination = copied.get(dependency)
            if destination is None:
                destination = library_dir / dependency.name
                copied[dependency] = destination
                if not destination.exists():
                    shutil.copy2(dependency, destination)
                pending.append(dependency)
    return tuple(path.name for path in sorted(copied.values()))


def _bundle_windows_dependencies(
    executable: Path,
    library_dir: Path,
) -> tuple[str, ...]:
    library_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for dependency in sorted(executable.parent.glob("*.dll")):
        destination = library_dir / dependency.name
        shutil.copy2(dependency, destination)
        copied.append(destination.name)
    return tuple(copied)


def _bundle_runtime_dependencies(
    executable: Path,
    runtime_root: Path,
    host_os: str,
) -> tuple[str, ...]:
    library_dir = runtime_root / "lib"
    if host_os == "macos":
        return _bundle_macos_dependencies(executable, library_dir)
    if host_os == "linux":
        return _bundle_linux_dependencies(executable, library_dir)
    if host_os == "windows":
        return _bundle_windows_dependencies(executable, library_dir)
    return ()


def _resolve_qemu_data_dir(executable: Path) -> Path | None:
    configured_value = os.environ.get("VM2Q_QEMU_DATA_DIR")
    configured = Path(configured_value).expanduser() if configured_value else None
    candidates = ((configured,) if configured else ()) + (
        executable.resolve().parent.parent / "share" / "qemu",
        executable.resolve().parent / ".." / "share" / "qemu",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return None


def _has_embedded_python(app_dir: Path) -> bool:
    return any(
        path.name.startswith(("libpython", "python"))
        or path.name == "base_library.zip"
        for path in app_dir.rglob("*")
        if path.is_file()
    )


def _build_variant(flavor: str) -> tuple[str, str]:
    if flavor == "cli":
        return (
            "vm2qforge-cli",
            "from vmware2qcow2.cli import main\nraise SystemExit(main())\n",
        )
    if flavor == "gui":
        return (
            "vm2qforge-gui",
            "from vmware2qcow2.gui import main\nraise SystemExit(main())\n",
        )
    raise RuntimeError(f"Unsupported portable flavor: {flavor}")


def _write_launchers(
    bundle_dir: Path,
    runtime_name: str,
    executable_name: str,
    flavor: str,
) -> None:
    for suffix in ("", f"-{flavor}"):
        posix = bundle_dir / f"run-vm2qforge{suffix}.sh"
        posix.write_text(
            f"""#!/bin/sh
set -eu
BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export VM2Q_RUNTIME_DIR="$BASE/runtime/{runtime_name}"
export VM2Q_DRIVER_DIR="$BASE/assets/drivers"
export VM2Q_WINPE_DIR="$BASE/assets/winpe"
export PATH="$BASE/runtime/{runtime_name}/bin:$PATH"
if [ -d "$BASE/runtime/{runtime_name}/lib" ]; then
    export LD_LIBRARY_PATH="$BASE/runtime/{runtime_name}/lib${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"
    export DYLD_LIBRARY_PATH="$BASE/runtime/{runtime_name}/lib${{DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}}"
fi
exec "$BASE/app/{executable_name}" "$@"
""",
            encoding="utf-8",
        )
        posix.chmod(0o755)

        windows = bundle_dir / f"run-vm2qforge{suffix}.cmd"
        windows.write_text(
            f"""@echo off
setlocal
set "BASE=%~dp0"
set "VM2Q_RUNTIME_DIR=%BASE%runtime\\{runtime_name}"
set "VM2Q_DRIVER_DIR=%BASE%assets\\drivers"
set "VM2Q_WINPE_DIR=%BASE%assets\\winpe"
set "PATH=%BASE%runtime\\{runtime_name}\\bin;%BASE%runtime\\{runtime_name}\\lib;%PATH%"
"%BASE%app\\{executable_name}.exe" %*
""",
            encoding="utf-8",
        )


def _profile_usage(profile: OfflineProfile, flavor: str) -> str:
    launcher = f"./run-vm2qforge-{flavor}.sh"
    if profile.name == "windows7":
        return f"""Windows 7 offline conversion:
  export VM2Q_WINDOWS_BAKE_USERNAME=Administrator
  export VM2Q_WINDOWS_BAKE_PASSWORD='SET_THE_GUEST_PASSWORD'
  {launcher} /path/to/windows7-vm \\
    --output /path/to/Win7-ready.qcow2 \\
    --windows-backend auto \\
    --network-model auto

The package auto-selects its legacy VirtIO ISO and prepared WinPE image.
"""
    if profile.name == "linux":
        return f"""Linux/Kali offline conversion:
  {launcher} /path/to/kali-vmware-directory \\
    --output /path/to/kali-ready.qcow2 \\
    --guest-os linux \\
    --linux-backend qemu-img \\
    --network-model virtio-net-pci

For a Kali .7z archive on macOS, first run:
  ./extract-vmware-7z.sh /path/to/kali-linux-vmware.7z /path/to/extracted
"""
    return f"""Universal offline conversion:
  {launcher} /path/to/vmware-directory-or-zip \\
    --output /path/to/converted.qcow2
"""


def _write_profile_helpers(
    bundle_dir: Path,
    runtime_name: str,
    profile: OfflineProfile,
) -> None:
    if profile.name != "linux":
        return
    helper = bundle_dir / "extract-vmware-7z.sh"
    helper.write_text(
        """#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 /path/to/vmware.7z /path/to/extract-directory" >&2
    exit 64
fi

ARCHIVE=$1
DESTINATION=$2
BSDTAR=/usr/bin/bsdtar
if [ ! -x "$BSDTAR" ]; then
    BSDTAR=$(command -v bsdtar || true)
fi
if [ -z "$BSDTAR" ] || [ ! -x "$BSDTAR" ]; then
    echo "bsdtar is required to extract a .7z archive on this host." >&2
    exit 127
fi

mkdir -p "$DESTINATION"
exec "$BSDTAR" -xf "$ARCHIVE" -C "$DESTINATION"
""",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    (bundle_dir / "PROFILE-RUNTIME.txt").write_text(
        f"runtime={runtime_name}\nprofile={profile.name}\n",
        encoding="utf-8",
    )


def build_portable(args: argparse.Namespace) -> Path:
    runtime = resolve_runtime(
        args.qemu_img,
        args.virt_v2v,
        Path(args.runtime_dir) if args.runtime_dir else None,
        dry_run=args.dry_run,
        qemu_system=getattr(args, "qemu_system", "qemu-system-x86_64"),
    )
    if runtime.qemu_img is None:
        raise RuntimeError(
            f"qemu-img was not found for {runtime.host_os}/{runtime.host_architecture}; "
            "pass --qemu-img or --runtime-dir"
        )

    runtime_name = f"{runtime.host_os}-{runtime.host_architecture}"
    flavor = getattr(args, "flavor", "cli")
    edition = getattr(args, "edition", "online")
    offline_profile = _offline_profile(
        getattr(args, "offline_profile", "universal")
    )
    executable_name, entry_source = _build_variant(flavor)
    profile_suffix = (
        f"-{offline_profile.name}"
        if edition == "offline" and offline_profile.name != "universal"
        else ""
    )
    bundle_name = (
        f"VM2Q-Forge-{runtime_name}-{edition}{profile_suffix}-{flavor}"
    )
    output_root = Path(args.output_dir).expanduser().resolve()
    bundle_dir = output_root / bundle_name
    driver_assets = _resolve_driver_assets(args)
    winpe_arch = getattr(args, "winpe_arch", "amd64")
    if winpe_arch not in {"amd64", "arm64"}:
        raise RuntimeError(f"Unsupported WinPE architecture: {winpe_arch}")
    winpe_asset = _resolve_winpe_asset(
        getattr(args, "winpe_iso", None),
        expected_arch=winpe_arch,
    )
    require_qga_assets = getattr(args, "require_qga_assets", False)
    required_driver_versions = (
        offline_profile.driver_versions
        if edition == "offline"
        else (
            tuple(driver_assets)
            if getattr(args, "require_driver_assets", False)
            else ()
        )
    )
    require_winpe_assets = (
        getattr(args, "require_winpe_assets", False)
        or require_qga_assets
        or (edition == "offline" and offline_profile.requires_winpe)
    )
    require_qemu_system = (
        require_winpe_assets
        or (edition == "offline" and offline_profile.requires_qemu_system)
    )
    require_driver_assets = bool(required_driver_versions)
    if (
        getattr(args, "require_driver_assets", False)
        and edition != "offline"
    ):
        required_driver_versions = tuple(driver_assets)
    missing_driver_assets = [
        version
        for version in required_driver_versions
        if driver_assets[version] is None
    ]
    if require_driver_assets and missing_driver_assets:
        missing = ", ".join(missing_driver_assets)
        raise RuntimeError(
            "Offline bundle is missing required VirtIO ISO assets: "
            f"{missing}. Pass --driver-dir or the version-specific paths."
        )
    if require_winpe_assets and winpe_asset is None:
        raise RuntimeError(
            "QEMU Guest Agent/WinPE offline bundle requires a prepared "
            "winpe-amd64.iso; "
            "pass --winpe-iso."
        )
    if (
        require_qemu_system
        and runtime.qemu_system is None
    ):
        raise RuntimeError(
            "WinPE/DISM offline bundle requires qemu-system-x86_64; "
            "pass --qemu-system or --runtime-dir."
        )
    if args.dry_run:
        print(json.dumps(
            {
                "bundle": str(bundle_dir),
                "runtime": runtime_name,
                "edition": edition,
                "offline_profile": offline_profile.name,
                "offline_profile_description": offline_profile.description,
                "flavor": flavor,
                "qemu_img": runtime.qemu_img,
                "virt_v2v": runtime.virt_v2v,
                "qemu_system": runtime.qemu_system,
                "pyinstaller": args.pyinstaller,
                "driver_assets": {
                    version: str(path) if path else None
                    for version, path in driver_assets.items()
                },
                "winpe_iso": str(winpe_asset) if winpe_asset else None,
                "required_driver_versions": list(required_driver_versions),
                "winpe_required": require_winpe_assets,
                "qemu_system_required": require_qemu_system,
                "offline_ready": (
                    edition == "offline"
                    and not missing_driver_assets
                    and (
                        not require_winpe_assets
                        or winpe_asset is not None
                    )
                    and (
                        not require_qemu_system
                        or runtime.qemu_system is not None
                    )
                ),
                "winpe_dism_ready": bool(winpe_asset and runtime.qemu_system),
            },
            indent=2,
        ))
        return bundle_dir

    pyinstaller = shutil.which(args.pyinstaller) or args.pyinstaller
    if shutil.which(args.pyinstaller) is None and not Path(args.pyinstaller).is_file():
        raise RuntimeError(
            f"PyInstaller executable was not found: {args.pyinstaller}. "
            "Install the build extras first."
        )

    output_root.mkdir(parents=True, exist_ok=True)
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    pyinstaller_dist = output_root / ".pyinstaller-dist"
    pyinstaller_work = output_root / ".pyinstaller-work"
    pyinstaller_spec = output_root / ".pyinstaller-spec"
    for directory in (pyinstaller_dist, pyinstaller_work, pyinstaller_spec):
        if directory.exists():
            shutil.rmtree(directory)

    with tempfile.TemporaryDirectory(prefix="vm2qforge-entry-") as temp:
        entry = Path(temp) / "vm2qforge_entry.py"
        entry.write_text(entry_source, encoding="utf-8")
        pyinstaller_args = [
            pyinstaller,
            "--noconfirm",
            "--clean",
            "--onedir",
            "--name",
            executable_name,
            "--paths",
            str(PROJECT_ROOT),
            "--distpath",
            str(pyinstaller_dist),
            "--workpath",
            str(pyinstaller_work),
            "--specpath",
            str(pyinstaller_spec),
        ]
        if flavor == "gui" and runtime.host_os == "windows":
            pyinstaller_args.append("--windowed")
        pyinstaller_args.append(str(entry))
        subprocess.run(
            pyinstaller_args,
            check=True,
        )

    app_source = pyinstaller_dist / executable_name
    if sys.platform.startswith("win"):
        app_source = pyinstaller_dist / executable_name
    if not app_source.is_dir():
        raise RuntimeError(f"PyInstaller output was not found: {app_source}")
    shutil.copytree(app_source, bundle_dir / "app")
    python_runtime_embedded = _has_embedded_python(bundle_dir / "app")
    if not python_runtime_embedded:
        raise RuntimeError(
            "PyInstaller output does not contain an embedded Python runtime"
        )

    runtime_root = bundle_dir / "runtime" / runtime_name
    runtime_bin = runtime_root / "bin"
    runtime_bin.mkdir(parents=True, exist_ok=True)
    qemu_destination = runtime_bin / Path(runtime.qemu_img).name
    _copy_asset(Path(runtime.qemu_img), qemu_destination)
    runtime_libraries = _bundle_runtime_dependencies(
        qemu_destination,
        runtime_root,
        runtime.host_os,
    )
    qemu_system_libraries: tuple[str, ...] = ()
    qemu_data_source = (
        _resolve_qemu_data_dir(Path(runtime.qemu_system))
        if runtime.qemu_system
        else None
    )
    qemu_data_destination: Path | None = None
    if runtime.qemu_system:
        qemu_system_destination = runtime_bin / Path(runtime.qemu_system).name
        _copy_asset(Path(runtime.qemu_system), qemu_system_destination)
        qemu_system_libraries = _bundle_runtime_dependencies(
            qemu_system_destination,
            runtime_root,
            runtime.host_os,
        )
    if qemu_data_source:
        qemu_data_destination = runtime_root / "share" / "qemu"
        shutil.copytree(
            qemu_data_source,
            qemu_data_destination,
            dirs_exist_ok=True,
        )
    if runtime.virt_v2v:
        _copy_asset(Path(runtime.virt_v2v), runtime_bin / Path(runtime.virt_v2v).name)

    driver_dir = bundle_dir / "assets" / "drivers"
    driver_dir.mkdir(parents=True, exist_ok=True)
    versions_to_copy = (
        required_driver_versions
        if edition == "offline"
        else tuple(driver_assets)
    )
    for version in versions_to_copy:
        source = driver_assets[version]
        if source:
            _copy_asset(source, driver_dir / get_bundle(version).filename)
    if winpe_asset:
        winpe_dir = bundle_dir / "assets" / "winpe"
        winpe_dir.mkdir(parents=True, exist_ok=True)
        _copy_asset(winpe_asset, winpe_dir / f"winpe-{winpe_arch}.iso")

    _write_launchers(bundle_dir, runtime_name, executable_name, flavor)
    _write_profile_helpers(bundle_dir, runtime_name, offline_profile)
    copied_driver_assets = [
        {
            "filename": path.name,
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        for path in sorted(driver_dir.iterdir())
        if path.is_file()
    ]
    offline_assets = [f"- {item['filename']}" for item in copied_driver_assets]
    if winpe_asset:
        offline_assets.append(f"- winpe-{winpe_arch}.iso")
    offline_assets_text = "\n  ".join(offline_assets) if offline_assets else "- none"
    copied_driver_versions = {
        path["filename"].removeprefix("virtio-win-").removesuffix(".iso")
        for path in copied_driver_assets
    }
    assets_ready = (
        all(version in copied_driver_versions for version in required_driver_versions)
        and (not require_winpe_assets or winpe_asset is not None)
        and (not require_qemu_system or runtime.qemu_system is not None)
    )
    (bundle_dir / "PORTABLE-README.txt").write_text(
        f"""VM2Q Forge portable bundle

Host runtime: {runtime_name}
Edition: {edition}
Flavor: {flavor}
Offline profile: {offline_profile.name}
Profile purpose: {offline_profile.description}
Offline assets:
  {offline_assets_text}
Launch:
  macOS/Linux: ./run-vm2qforge.sh /path/to/vmware.zip --output /path/to/out.qcow2
  Windows:     run-vm2qforge.cmd C:\\path\\vmware.zip --output C:\\path\\out.qcow2
  Flavor-specific: ./run-vm2qforge-{flavor}.sh
                  run-vm2qforge-{flavor}.cmd

The bundle uses its own qemu-img through VM2Q_RUNTIME_DIR.
The bundled driver assets are discovered through VM2Q_DRIVER_DIR.
The PyInstaller app contains its Python runtime; no Python installation is
required on the target host.
After extraction, conversion uses local files only and does not download
drivers or Python packages.

{_profile_usage(offline_profile, flavor)}
""",
        encoding="utf-8",
    )
    (bundle_dir / "portable-manifest.json").write_text(
        json.dumps(
            {
                "name": "VM2Q Forge",
                "runtime": runtime_name,
                "edition": edition,
                "offline_profile": offline_profile.name,
                "offline_profile_description": offline_profile.description,
                "flavor": flavor,
                "host_os": runtime.host_os,
                "host_architecture": runtime.host_architecture,
                "qemu_img": Path(runtime.qemu_img).name,
                "virt_v2v": Path(runtime.virt_v2v).name
                if runtime.virt_v2v
                else None,
                "qemu_system": Path(runtime.qemu_system).name
                if runtime.qemu_system
                else None,
                "qemu_data_dir": (
                    str(qemu_data_destination.relative_to(bundle_dir))
                    if qemu_data_destination
                    else None
                ),
                "python_runtime": (
                    "embedded-in-pyinstaller-app"
                    if python_runtime_embedded
                    else None
                ),
                "runtime_libraries": list(
                    dict.fromkeys(
                        (*runtime_libraries, *qemu_system_libraries)
                    )
                ),
                "offline_ready": (
                    edition == "offline"
                    and python_runtime_embedded
                    and assets_ready
                ),
                "winpe_dism_ready": bool(
                    python_runtime_embedded
                    and winpe_asset
                    and runtime.qemu_system
                ),
                "driver_assets": copied_driver_assets,
                "required_driver_versions": list(required_driver_versions),
                "winpe_required": require_winpe_assets,
                "qemu_system_required": require_qemu_system,
                "winpe_asset": (
                    {
                        "filename": f"winpe-{winpe_arch}.iso",
                        "bytes": (
                            bundle_dir
                            / "assets"
                            / "winpe"
                            / f"winpe-{winpe_arch}.iso"
                        ).stat().st_size,
                        "sha256": _sha256_file(
                            bundle_dir
                            / "assets"
                            / "winpe"
                            / f"winpe-{winpe_arch}.iso"
                        ),
                    }
                    if winpe_asset
                    else None
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    archive = shutil.make_archive(
        str(output_root / bundle_name),
        "zip",
        root_dir=output_root,
        base_dir=bundle_name,
    )
    for directory in (pyinstaller_dist, pyinstaller_work, pyinstaller_spec):
        shutil.rmtree(directory, ignore_errors=True)
    print(f"portable_bundle={bundle_dir}")
    print(f"portable_archive={archive}")
    return bundle_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a host-specific VM2Q Forge portable bundle."
    )
    parser.add_argument("--output-dir", default="dist/portable")
    parser.add_argument(
        "--edition",
        choices=("online", "offline"),
        default="online",
        help="Choose a lightweight online bundle or a fully offline bundle",
    )
    parser.add_argument(
        "--offline-profile",
        choices=tuple(sorted(OFFLINE_PROFILES)),
        default="universal",
        help=(
            "Offline asset set: universal includes both Windows VirtIO ISOs "
            "and WinPE; windows7 includes the legacy ISO and WinPE; linux "
            "contains only the Linux conversion runtime"
        ),
    )
    parser.add_argument(
        "--flavor",
        choices=("cli", "gui"),
        default="cli",
        help="Build a CLI or GUI executable bundle",
    )
    parser.add_argument("--qemu-img", default="qemu-img")
    parser.add_argument("--virt-v2v", default="virt-v2v")
    parser.add_argument("--qemu-system", default="qemu-system-x86_64")
    parser.add_argument("--runtime-dir")
    parser.add_argument(
        "--driver-dir",
        help="Directory containing both versioned VirtIO ISO assets",
    )
    parser.add_argument("--virtio-win-old")
    parser.add_argument("--virtio-win-new")
    parser.add_argument(
        "--winpe-iso",
        help="Prepared WinPE ISO to include for offline DISM injection",
    )
    parser.add_argument(
        "--winpe-arch",
        choices=("amd64", "arm64"),
        default="amd64",
        help="Architecture of the prepared WinPE ISO",
    )
    parser.add_argument(
        "--require-driver-assets",
        action="store_true",
        help="Fail unless both VirtIO ISO assets are included in the bundle",
    )
    parser.add_argument(
        "--require-winpe-assets",
        action="store_true",
        help="Fail unless a prepared WinPE ISO and QEMU system emulator are included",
    )
    parser.add_argument(
        "--require-qga-assets",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Require WinPE/QEMU assets for offline Windows bundles; use "
            "--no-require-qga-assets for online or disk-only bundles"
        ),
    )
    parser.add_argument("--pyinstaller", default="pyinstaller")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        build_portable(args)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
