from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Iterable, MutableMapping

from .model import RuntimeInfo
from .resources import application_resource_roots


def host_os() -> str:
    if os.name == "nt" or sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform.lower().replace("/", "-")


def host_architecture() -> str:
    value = platform.machine().lower()
    if value in {"amd64", "x86_64", "x64"}:
        return "amd64"
    if value in {"arm64", "aarch64", "armv8", "armv8l"}:
        return "arm64"
    if value in {"i386", "i686", "x86", "x86_32"}:
        return "x86"
    return value.replace("/", "-") or "unknown"


def _unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        try:
            normalized = str(path.expanduser().resolve())
        except OSError:
            normalized = str(path.expanduser())
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(Path(normalized))
    return tuple(result)


def _runtime_roots(runtime_dir: Path | None) -> tuple[Path, ...]:
    current_os = host_os()
    current_arch = host_architecture()
    bases: list[Path] = []
    if runtime_dir:
        bases.append(runtime_dir.expanduser())
    environment_dir = os.environ.get("VM2Q_RUNTIME_DIR")
    if environment_dir:
        bases.append(Path(environment_dir).expanduser())
    bases.extend(application_resource_roots())

    executable = Path(sys.executable).resolve()
    launcher = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else executable
    package_root = Path(__file__).resolve().parent
    bases.extend(
        (
            executable.parent,
            launcher.parent,
            package_root,
            package_root.parent,
            Path.cwd(),
        )
    )

    roots: list[Path] = []
    for base in bases:
        roots.extend(
            (
                base,
                base / "bin",
                base / "runtime",
                base / "runtime" / f"{current_os}-{current_arch}",
                base / "runtime" / f"{current_os}-{current_arch}" / "bin",
                base / "runtime" / current_os / current_arch,
                base / "runtime" / current_os / current_arch / "bin",
                base / f"{current_os}-{current_arch}",
                base / current_os / current_arch,
            )
        )
    return _unique_paths(roots)


def _runtime_bundle_roots(info: RuntimeInfo) -> tuple[Path, ...]:
    roots: list[Path] = []
    for executable in (info.qemu_img, info.qemu_system, info.virt_v2v):
        if not executable:
            continue
        candidate = Path(executable).expanduser().resolve()
        if candidate.parent.name == "bin":
            roots.append(candidate.parent.parent)
        else:
            roots.append(candidate.parent)
    return _unique_paths(roots)


def _prepend_environment_path(
    environment: MutableMapping[str, str],
    key: str,
    paths: Iterable[Path],
) -> None:
    additions = [str(path) for path in paths if path.is_dir()]
    if not additions:
        return
    existing = environment.get(key, "")
    existing_values = [value for value in existing.split(os.pathsep) if value]
    merged = list(dict.fromkeys((*additions, *existing_values)))
    environment[key] = os.pathsep.join(merged)


def configure_runtime_environment(
    info: RuntimeInfo,
    environment: MutableMapping[str, str] | None = None,
) -> None:
    """Prepare library and QEMU-data lookup for an embedded runtime bundle."""

    target = environment if environment is not None else os.environ
    roots = _runtime_bundle_roots(info)
    bin_dirs = tuple(root / "bin" for root in roots)
    library_dirs = tuple(root / "lib" for root in roots)
    if info.host_os == "windows":
        _prepend_environment_path(target, "PATH", (*bin_dirs, *library_dirs))
    elif info.host_os == "macos":
        _prepend_environment_path(target, "PATH", bin_dirs)
        _prepend_environment_path(target, "DYLD_LIBRARY_PATH", library_dirs)
    elif info.host_os == "linux":
        _prepend_environment_path(target, "PATH", bin_dirs)
        _prepend_environment_path(target, "LD_LIBRARY_PATH", library_dirs)
    else:
        _prepend_environment_path(target, "PATH", bin_dirs)

    if not target.get("VM2Q_QEMU_DATA_DIR"):
        for root in roots:
            data_dir = root / "share" / "qemu"
            if data_dir.is_dir():
                target["VM2Q_QEMU_DATA_DIR"] = str(data_dir)
                break


def _requested_path(name: str) -> Path | None:
    path = Path(name).expanduser()
    if path.is_absolute() or path.parent != Path("."):
        return path
    return None


def _executable_names(name: str) -> tuple[str, ...]:
    if host_os() == "windows" and not name.lower().endswith(".exe"):
        return (name, f"{name}.exe")
    return (name,)


def _find_executable(
    name: str,
    roots: Iterable[Path],
) -> tuple[str | None, str | None]:
    direct = _requested_path(name)
    if direct:
        for candidate in (direct, *[Path(f"{direct}.exe")]):
            if candidate.is_file():
                return str(candidate.resolve()), "explicit-path"
        return None, None

    for root in roots:
        for executable_name in _executable_names(name):
            candidate = root / executable_name
            if candidate.is_file():
                return str(candidate.resolve()), "runtime-bundle"

    discovered = shutil.which(name)
    if discovered:
        return str(Path(discovered).resolve()), "PATH"
    return None, None


def resolve_runtime(
    qemu_img: str,
    virt_v2v: str,
    runtime_dir: Path | None = None,
    dry_run: bool = False,
    qemu_system: str = "qemu-system-x86_64",
) -> RuntimeInfo:
    roots = _runtime_roots(runtime_dir)
    environment_dir = os.environ.get("VM2Q_RUNTIME_DIR")
    qemu_path, qemu_source = _find_executable(qemu_img, roots)
    virt_v2v_path, virt_v2v_source = _find_executable(virt_v2v, roots)
    qemu_system_path, qemu_system_source = _find_executable(qemu_system, roots)

    if dry_run:
        qemu_path = qemu_path or qemu_img
        qemu_source = qemu_source or "dry-run-placeholder"
        virt_v2v_path = virt_v2v_path or virt_v2v
        virt_v2v_source = virt_v2v_source or "dry-run-placeholder"
        qemu_system_path = qemu_system_path or qemu_system
        qemu_system_source = qemu_system_source or "dry-run-placeholder"

    if runtime_dir:
        configured_runtime = str(runtime_dir.expanduser().resolve())
    elif environment_dir:
        configured_runtime = str(Path(environment_dir).expanduser().resolve())
    else:
        configured_runtime = None
    return RuntimeInfo(
        host_os=host_os(),
        host_architecture=host_architecture(),
        runtime_dir=configured_runtime,
        qemu_img=qemu_path,
        qemu_img_source=qemu_source,
        virt_v2v=virt_v2v_path,
        virt_v2v_source=virt_v2v_source,
        qemu_system=qemu_system_path,
        qemu_system_source=qemu_system_source,
    )
