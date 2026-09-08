from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STARTNET = PROJECT_ROOT / "assets" / "winpe" / "startnet.cmd"


def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        check=False,
        text=True,
        capture_output=True,
        **kwargs,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"Command failed with exit status {completed.returncode}: "
            f"{' '.join(argv)}\n{detail}"
        )
    return completed


def _architecture(wimlib: str, boot_wim: Path, image: int) -> str:
    output = _run([wimlib, "info", str(boot_wim), str(image)]).stdout
    match = re.search(r"(?im)^\s*Architecture:\s*(\S+)", output)
    if match is None:
        raise RuntimeError(
            f"Could not determine WinPE architecture from {boot_wim}"
        )
    value = match.group(1).lower()
    if value in {"amd64", "x64", "x86_64"}:
        return "amd64"
    if value in {"arm64", "aarch64"}:
        return "arm64"
    return value


def _extract_boot_wim(
    iso: Path,
    destination: Path,
    xorriso: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run(
            [
                xorriso,
                "-osirrox",
                "on",
                "-indev",
                str(iso),
                "-extract",
                "/sources/boot.wim",
                str(destination),
            ]
        )
        return
    except RuntimeError:
        if not sys.platform == "darwin":
            raise

    mountpoint = destination.parent / "iso-mount"
    mountpoint.mkdir(parents=True, exist_ok=True)
    attached = False
    try:
        _run(
            [
                "hdiutil",
                "attach",
                "-readonly",
                "-nobrowse",
                "-mountpoint",
                str(mountpoint),
                str(iso),
            ]
        )
        attached = True
        source = mountpoint / "sources" / "boot.wim"
        if not source.is_file():
            raise RuntimeError(f"ISO has no sources/boot.wim: {iso}")
        shutil.copy2(source, destination)
    finally:
        if attached:
            subprocess.run(
                ["hdiutil", "detach", str(mountpoint)],
                check=False,
                capture_output=True,
                text=True,
            )


def _update_wim(wimlib: str, boot_wim: Path, startnet: Path) -> None:
    info = _run([wimlib, "info", str(boot_wim)]).stdout
    matches = re.findall(r"(?im)^\s*Index:\s*(\d+)", info)
    images = tuple(dict.fromkeys(int(value) for value in matches))
    if not images:
        raise RuntimeError(f"No images found in {boot_wim}")
    command_source = str(startnet).replace('"', '""')
    command_file = (
        f'add "{command_source}" /Windows/System32/startnet.cmd\n'
    )
    for image in images:
        completed = subprocess.run(
            [wimlib, "update", str(boot_wim), str(image)],
            input=command_file,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"Could not update boot.wim image {image}: {detail}"
            )


def build_winpe_iso(
    base_iso: Path,
    output_iso: Path,
    expected_arch: str,
    wimlib: str = "wimlib-imagex",
    xorriso: str = "xorriso",
    boot_wim: Path | None = None,
) -> Path:
    base_iso = base_iso.expanduser().resolve()
    output_iso = output_iso.expanduser().resolve()
    if not base_iso.is_file():
        raise RuntimeError(f"Base WinPE ISO was not found: {base_iso}")
    if expected_arch not in {"amd64", "arm64"}:
        raise RuntimeError(f"Unsupported WinPE architecture: {expected_arch}")
    if not STARTNET.is_file():
        raise RuntimeError(f"WinPE startnet.cmd is missing: {STARTNET}")
    if output_iso.exists():
        raise RuntimeError(f"Output already exists: {output_iso}")

    output_iso.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vm2qforge-winpe-build-") as temp:
        temp_root = Path(temp)
        local_wim = (
            boot_wim.expanduser().resolve()
            if boot_wim is not None
            else temp_root / "boot.wim"
        )
        if boot_wim is None:
            _extract_boot_wim(base_iso, local_wim, xorriso)
        if not local_wim.is_file():
            raise RuntimeError(f"boot.wim was not found: {local_wim}")
        # xorriso commonly restores files from an ISO as read-only.  wimlib
        # needs a writable WIM container when updating startnet.cmd.
        local_wim.chmod(0o644)
        detected = _architecture(wimlib, local_wim, 1)
        if detected != expected_arch:
            raise RuntimeError(
                f"WinPE architecture mismatch: expected {expected_arch}, "
                f"boot.wim reports {detected}"
            )
        _update_wim(wimlib, local_wim, STARTNET)

        marker = temp_root / f"VM2Q-FORGE-WINPE-{expected_arch.upper()}.TXT"
        marker.write_text(
            "VM2Q Forge WinPE/DISM helper\n"
            f"architecture={expected_arch}\n"
            "startnet=VM2Q Forge offline driver injection\n",
            encoding="ascii",
        )
        _run(
            [
                xorriso,
                "-indev",
                str(base_iso),
                "-outdev",
                str(output_iso),
                "-boot_image",
                "any",
                "replay",
                "-map",
                str(local_wim),
                "/sources/boot.wim",
                "-map",
                str(marker),
                f"/VM2Q-FORGE-WINPE-{expected_arch.upper()}.TXT",
                "-commit",
                "-end",
            ]
        )
    return output_iso


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a VM2Q Forge WinPE ISO with the offline DISM entry point."
    )
    parser.add_argument("base_iso", type=Path)
    parser.add_argument("output_iso", type=Path)
    parser.add_argument("--arch", choices=("amd64", "arm64"), default="amd64")
    parser.add_argument("--wimlib", default="wimlib-imagex")
    parser.add_argument("--xorriso", default="xorriso")
    parser.add_argument(
        "--boot-wim",
        type=Path,
        help="Use an already extracted boot.wim when ISO extraction is unavailable",
    )
    args = parser.parse_args(argv)
    try:
        output = build_winpe_iso(
            args.base_iso,
            args.output_iso,
            args.arch,
            wimlib=args.wimlib,
            xorriso=args.xorriso,
            boot_wim=args.boot_wim,
        )
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2
    print(f"winpe_iso={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
