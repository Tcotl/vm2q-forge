from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PACKAGING_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGING_ROOT))

from build_portable import (  # noqa: E402
    _bundle_runtime_dependencies,
    _copy_asset,
    _offline_profile,
    _resolve_driver_assets,
    _resolve_qemu_data_dir,
    _resolve_winpe_asset,
    _sha256_file,
)
from vmware2qcow2.drivers import get_bundle  # noqa: E402
from vmware2qcow2.runtime import resolve_runtime  # noqa: E402


@dataclass(frozen=True)
class PreparedPayload:
    root: Path
    runtime_name: str
    edition: str
    offline_profile: str
    qemu_img: Path
    qemu_system: Path
    virt_v2v: Path | None
    qemu_data_dir: Path | None
    runtime_libraries: tuple[str, ...]
    driver_assets: tuple[dict[str, object], ...]
    winpe_asset: dict[str, object] | None


@dataclass(frozen=True)
class GuiReleaseResult:
    artifact: Path
    manifest: Path
    sha256: Path
    verification: Path


def _release_basename(runtime_name: str, edition: str) -> str:
    return f"VM2Q-Forge-{runtime_name}-gui-{edition}"


def _required_assets(
    edition: str,
    offline_profile: str,
) -> tuple[tuple[str, ...], bool]:
    if edition != "offline":
        return (), False
    profile = _offline_profile(offline_profile)
    return profile.driver_versions, profile.requires_winpe


def _validate_native_host(host_os: str, host_architecture: str) -> None:
    if host_os == "macos" and host_architecture == "arm64":
        return
    if host_os == "windows" and host_architecture == "amd64":
        return
    raise RuntimeError(
        "Native GUI releases must be built on their destination platform: "
        "macOS arm64 for the DMG or Windows amd64 for the EXE. "
        f"Detected {host_os}/{host_architecture}."
    )


def _validate_tkinter() -> None:
    try:
        import tkinter
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "The build Python does not include tkinter. Install a Python build "
            "with Tk support before creating a GUI release."
        ) from exc
    try:
        tkinter.Tcl()
    except tkinter.TclError as exc:
        raise RuntimeError(
            "The build Python has tkinter but cannot initialize Tcl/Tk."
        ) from exc


def _prepare_payload(
    args: argparse.Namespace,
    root: Path,
) -> PreparedPayload:
    runtime = resolve_runtime(
        args.qemu_img,
        args.virt_v2v,
        Path(args.runtime_dir) if args.runtime_dir else None,
        qemu_system=args.qemu_system,
    )
    _validate_native_host(runtime.host_os, runtime.host_architecture)
    if runtime.qemu_img is None:
        raise RuntimeError(
            "qemu-img was not found; pass --qemu-img or --runtime-dir."
        )
    if runtime.qemu_system is None:
        raise RuntimeError(
            "qemu-system-x86_64 was not found; pass --qemu-system or "
            "--runtime-dir so the GUI can perform WinPE/DISM conversions."
        )

    runtime_name = f"{runtime.host_os}-{runtime.host_architecture}"
    required_driver_versions, winpe_required = _required_assets(
        args.edition,
        args.offline_profile,
    )
    driver_assets = _resolve_driver_assets(args)
    missing_drivers = [
        version
        for version in required_driver_versions
        if driver_assets[version] is None
    ]
    if missing_drivers:
        raise RuntimeError(
            "Offline GUI release is missing VirtIO ISO assets: "
            f"{', '.join(missing_drivers)}. Pass --driver-dir or the "
            "version-specific paths."
        )
    winpe_asset = _resolve_winpe_asset(
        args.winpe_iso,
        expected_arch=args.winpe_arch,
    )
    if winpe_required and winpe_asset is None:
        raise RuntimeError(
            "Offline GUI release requires a prepared winpe-amd64.iso; "
            "pass --winpe-iso."
        )

    runtime_root = root / "runtime" / runtime_name
    runtime_bin = runtime_root / "bin"
    runtime_bin.mkdir(parents=True, exist_ok=True)

    qemu_img_source = Path(runtime.qemu_img)
    qemu_img_destination = runtime_bin / qemu_img_source.name
    _copy_asset(qemu_img_source, qemu_img_destination)
    runtime_libraries = list(
        _bundle_runtime_dependencies(
            qemu_img_destination,
            runtime_root,
            runtime.host_os,
        )
    )

    qemu_system_source = Path(runtime.qemu_system)
    qemu_system_destination = runtime_bin / qemu_system_source.name
    _copy_asset(qemu_system_source, qemu_system_destination)
    runtime_libraries.extend(
        _bundle_runtime_dependencies(
            qemu_system_destination,
            runtime_root,
            runtime.host_os,
        )
    )

    qemu_data_source = _resolve_qemu_data_dir(qemu_system_source)
    qemu_data_destination: Path | None = None
    if qemu_data_source:
        qemu_data_destination = runtime_root / "share" / "qemu"
        shutil.copytree(
            qemu_data_source,
            qemu_data_destination,
            dirs_exist_ok=True,
        )

    virt_v2v_destination: Path | None = None
    if runtime.virt_v2v:
        virt_v2v_source = Path(runtime.virt_v2v)
        virt_v2v_destination = runtime_bin / virt_v2v_source.name
        _copy_asset(virt_v2v_source, virt_v2v_destination)

    driver_dir = root / "assets" / "drivers"
    copied_drivers: list[dict[str, object]] = []
    for version in required_driver_versions:
        source = driver_assets[version]
        if source is None:
            continue
        destination = driver_dir / get_bundle(version).filename
        _copy_asset(source, destination)
        copied_drivers.append(
            {
                "version": version,
                "filename": destination.name,
                "bytes": destination.stat().st_size,
                "sha256": _sha256_file(destination),
            }
        )

    copied_winpe: dict[str, object] | None = None
    if winpe_asset is not None and winpe_required:
        destination = root / "assets" / "winpe" / f"winpe-{args.winpe_arch}.iso"
        _copy_asset(winpe_asset, destination)
        copied_winpe = {
            "filename": destination.name,
            "bytes": destination.stat().st_size,
            "sha256": _sha256_file(destination),
        }

    return PreparedPayload(
        root=root,
        runtime_name=runtime_name,
        edition=args.edition,
        offline_profile=args.offline_profile,
        qemu_img=qemu_img_destination,
        qemu_system=qemu_system_destination,
        virt_v2v=virt_v2v_destination,
        qemu_data_dir=qemu_data_destination,
        runtime_libraries=tuple(dict.fromkeys(runtime_libraries)),
        driver_assets=tuple(copied_drivers),
        winpe_asset=copied_winpe,
    )


def _add_data_argument(source: Path, destination: str) -> str:
    return f"{source}{os.pathsep}{destination}"


def _pyinstaller_launcher(requested: str) -> tuple[str, ...]:
    discovered = shutil.which(requested)
    candidates = (
        Path(discovered) if discovered else None,
        Path(requested).expanduser(),
    )
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return (str(candidate),)
    if importlib.util.find_spec("PyInstaller") is not None:
        return (sys.executable, "-m", "PyInstaller")
    raise RuntimeError(
        f"PyInstaller executable was not found: {requested}. "
        'Install the project build extras with python -m pip install ".[build]".'
    )


def _pyinstaller_command(
    args: argparse.Namespace,
    payload: PreparedPayload,
    entry: Path,
    dist_dir: Path,
    work_dir: Path,
    spec_dir: Path,
    name: str,
) -> list[str]:
    command = [
        *_pyinstaller_launcher(args.pyinstaller),
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        name,
        "--paths",
        str(PROJECT_ROOT),
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(spec_dir),
        "--add-data",
        _add_data_argument(
            payload.root / "runtime" / payload.runtime_name,
            f"runtime/{payload.runtime_name}",
        ),
    ]
    driver_dir = payload.root / "assets" / "drivers"
    if driver_dir.is_dir():
        command.extend(
            (
                "--add-data",
                _add_data_argument(driver_dir, "assets/drivers"),
            )
        )
    winpe_dir = payload.root / "assets" / "winpe"
    if winpe_dir.is_dir():
        command.extend(
            (
                "--add-data",
                _add_data_argument(winpe_dir, "assets/winpe"),
            )
        )
    if payload.runtime_name == "macos-arm64":
        command.extend(
            (
                "--onedir",
                "--osx-bundle-identifier",
                "io.github.tcotl.vm2qforge",
            )
        )
    else:
        command.append("--onefile")
    command.append(str(entry))
    return command


def _stage_macos_app(app_source: Path, app_stage: Path) -> Path:
    if app_stage.exists():
        shutil.rmtree(app_stage)
    app_stage.mkdir(parents=True)
    staged_app = app_stage / app_source.name
    shutil.copytree(
        app_source,
        staged_app,
        symlinks=True,
    )
    applications = app_stage / "Applications"
    applications.symlink_to("/Applications")
    return staged_app


def _build_macos_dmg(
    output_dir: Path,
    basename: str,
    app_source: Path,
) -> Path:
    app_stage = output_dir / ".dmg-stage"
    _stage_macos_app(app_source, app_stage)

    artifact = output_dir / f"{basename}.dmg"
    if artifact.exists():
        artifact.unlink()
    subprocess.run(
        [
            "hdiutil",
            "create",
            "-volname",
            "VM2Q Forge",
            "-srcfolder",
            str(app_stage),
            "-ov",
            "-format",
            "UDZO",
            str(artifact),
        ],
        check=True,
    )
    subprocess.run(["hdiutil", "verify", str(artifact)], check=True)
    shutil.rmtree(app_stage)
    return artifact


def _write_release_records(
    output_dir: Path,
    artifact: Path,
    payload: PreparedPayload,
    verification_lines: tuple[str, ...],
) -> GuiReleaseResult:
    digest = _sha256_file(artifact)
    sha256_path = artifact.with_suffix(artifact.suffix + ".sha256")
    # Keep checksum files portable: Windows' default text newline would add
    # CRLF, which some POSIX checksum tools treat as part of the filename.
    with sha256_path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(f"{digest}  {artifact.name}\n")
    manifest_path = artifact.with_name(artifact.stem + "-manifest.json")
    manifest_path.write_text(
        json.dumps(
            {
                "name": "VM2Q Forge",
                "artifact": artifact.name,
                "bytes": artifact.stat().st_size,
                "sha256": digest,
                "runtime": payload.runtime_name,
                "edition": payload.edition,
                "offline_profile": payload.offline_profile,
                "qemu_img": str(
                    payload.qemu_img.relative_to(payload.root)
                ),
                "qemu_system": str(
                    payload.qemu_system.relative_to(payload.root)
                ),
                "virt_v2v": (
                    str(payload.virt_v2v.relative_to(payload.root))
                    if payload.virt_v2v
                    else None
                ),
                "qemu_data_dir": (
                    str(payload.qemu_data_dir.relative_to(payload.root))
                    if payload.qemu_data_dir
                    else None
                ),
                "runtime_libraries": list(payload.runtime_libraries),
                "driver_assets": list(payload.driver_assets),
                "winpe_asset": payload.winpe_asset,
                "language_options": ["zh_CN", "en_US"],
                "offline_ready": payload.edition == "offline",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    verification_path = artifact.with_name(
        artifact.stem + "-verification.txt"
    )
    verification_path.write_text(
        "\n".join(
            (
                f"artifact={artifact}",
                f"bytes={artifact.stat().st_size}",
                f"sha256={digest}",
                *verification_lines,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return GuiReleaseResult(
        artifact=artifact,
        manifest=manifest_path,
        sha256=sha256_path,
        verification=verification_path,
    )


def build_gui_release(args: argparse.Namespace) -> GuiReleaseResult | None:
    output_dir = Path(args.output_dir).expanduser().resolve()
    _validate_tkinter()
    with tempfile.TemporaryDirectory(prefix="vm2qforge-gui-") as temporary:
        temporary_root = Path(temporary)
        payload = _prepare_payload(args, temporary_root / "payload")
        basename = _release_basename(payload.runtime_name, args.edition)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "artifact": str(
                            output_dir
                            / f"{basename}{'.dmg' if payload.runtime_name == 'macos-arm64' else '.exe'}"
                        ),
                        "runtime": payload.runtime_name,
                        "edition": payload.edition,
                        "offline_profile": payload.offline_profile,
                        "driver_assets": list(payload.driver_assets),
                        "winpe_asset": payload.winpe_asset,
                        "qemu_img": str(payload.qemu_img),
                        "qemu_system": str(payload.qemu_system),
                        "language_options": ["zh_CN", "en_US"],
                    },
                    indent=2,
                )
            )
            return None

        output_dir.mkdir(parents=True, exist_ok=True)
        dist_dir = temporary_root / "dist"
        work_dir = temporary_root / "work"
        spec_dir = temporary_root / "spec"
        entry = temporary_root / "vm2qforge_gui_entry.py"
        entry.write_text(
            "from vmware2qcow2.gui import main\nmain()\n",
            encoding="utf-8",
        )
        app_name = "VM2Q Forge" if payload.runtime_name == "macos-arm64" else basename
        command = _pyinstaller_command(
            args,
            payload,
            entry,
            dist_dir,
            work_dir,
            spec_dir,
            app_name,
        )
        subprocess.run(command, check=True)

        verification_lines = [
            f"pyinstaller={' '.join(command)}",
            f"runtime_qemu_img={payload.qemu_img}",
            f"runtime_qemu_system={payload.qemu_system}",
        ]
        if payload.runtime_name == "macos-arm64":
            app_source = dist_dir / f"{app_name}.app"
            if not app_source.is_dir():
                raise RuntimeError(
                    f"PyInstaller macOS app was not found: {app_source}"
                )
            subprocess.run(
                [
                    "codesign",
                    "--force",
                    "--deep",
                    "--sign",
                    "-",
                    str(app_source),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "codesign",
                    "--verify",
                    "--deep",
                    "--strict",
                    str(app_source),
                ],
                check=True,
            )
            verification_lines.extend(
                (
                    f"codesign_verify={app_source}",
                    "hdiutil_verify=passed",
                )
            )
            artifact = _build_macos_dmg(output_dir, basename, app_source)
        else:
            source = dist_dir / f"{app_name}.exe"
            if not source.is_file():
                raise RuntimeError(
                    f"PyInstaller Windows EXE was not found: {source}"
                )
            artifact = output_dir / f"{basename}.exe"
            shutil.copy2(source, artifact)
            verification_lines.append(f"pyinstaller_exe={source}")
        return _write_release_records(
            output_dir,
            artifact,
            payload,
            tuple(verification_lines),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a native bilingual VM2Q Forge GUI release for macOS arm64 "
            "or Windows amd64."
        )
    )
    parser.add_argument("--output-dir", default="dist/gui")
    parser.add_argument(
        "--edition",
        choices=("online", "offline"),
        default="online",
        help=(
            "online embeds QEMU; offline also embeds required VirtIO and "
            "prepared WinPE assets"
        ),
    )
    parser.add_argument(
        "--offline-profile",
        choices=("universal", "windows7", "linux"),
        default="universal",
        help="Guest asset set used by an offline release",
    )
    parser.add_argument("--qemu-img", default="qemu-img")
    parser.add_argument("--qemu-system", default="qemu-system-x86_64")
    parser.add_argument("--virt-v2v", default="virt-v2v")
    parser.add_argument("--runtime-dir")
    parser.add_argument("--driver-dir")
    parser.add_argument("--virtio-win-old")
    parser.add_argument("--virtio-win-new")
    parser.add_argument("--winpe-iso")
    parser.add_argument(
        "--winpe-arch",
        choices=("amd64", "arm64"),
        default="amd64",
    )
    parser.add_argument("--pyinstaller", default="pyinstaller")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = build_gui_release(args)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2
    if result is not None:
        print(f"gui_artifact={result.artifact}")
        print(f"gui_manifest={result.manifest}")
        print(f"gui_sha256={result.sha256}")
        print(f"gui_verification={result.verification}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
