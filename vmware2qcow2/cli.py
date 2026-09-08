from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from .core import ConversionError, convert
from .model import ConversionOptions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vmware2qcow2",
        description="Convert a VMware work directory or ZIP archive to qcow2.",
    )
    parser.add_argument(
        "input",
        type=Path,
        help="VMware work directory or ZIP archive",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="Output qcow2 path",
    )
    parser.add_argument(
        "--driver-iso",
        type=Path,
        help="virtio-win ISO or extracted directory override",
    )
    parser.add_argument(
        "--driver-mode",
        choices=("none", "plan"),
        default="plan",
        help="Driver handling mode; plan generates a Windows driver staging script",
    )
    parser.add_argument(
        "--virtio-win-old",
        type=Path,
        help="Path to virtio-win-0.1.173-9.iso or its extracted directory",
    )
    parser.add_argument(
        "--virtio-win-new",
        type=Path,
        help="Path to virtio-win-0.1.285-1.iso or its extracted directory",
    )
    parser.add_argument(
        "--virtio-win-version",
        choices=("auto", "0.1.173-9", "0.1.285-1"),
        default="auto",
        help="Select a built-in virtio-win bundle or let VMX guestOS choose it",
    )
    parser.add_argument(
        "--guest-os",
        choices=("auto", "windows", "linux"),
        default="auto",
        help="Guest OS family; auto reads VMX guestOS and guestOS.detailedData",
    )
    parser.add_argument(
        "--windows-backend",
        choices=("auto", "qemu-img", "virt-v2v", "winpe-dism"),
        default="auto",
        help=(
            "Windows conversion backend; virt-v2v or WinPE/DISM can inject "
            "selected virtio-win drivers"
        ),
    )
    parser.add_argument(
        "--linux-backend",
        choices=("auto", "qemu-img", "virt-v2v"),
        default="auto",
        help="Linux conversion backend; auto uses virt-v2v when installed",
    )
    parser.add_argument(
        "--network-model",
        choices=("auto", "e1000", "virtio-net-pci"),
        default="auto",
        help=(
            "Generated QEMU/UTM network model; Windows auto uses built-in "
            "e1000 for portable DHCP"
        ),
    )
    parser.add_argument(
        "--require-virtio",
        action="store_true",
        help=(
            "Windows only: require offline VirtIO driver injection and generate "
            "a first-boot DHCP script; never fall back to qemu-img"
        ),
    )
    parser.add_argument(
        "--require-qga",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Windows only: require QEMU Guest Agent MSI staging and "
            "vioserial injection; use --no-require-qga for disk-only conversion"
        ),
    )
    parser.add_argument(
        "--qga-install-mode",
        choices=("bake", "firstboot"),
        default="bake",
        help=(
            "Windows QEMU Guest Agent mode; bake installs it before qcow2 "
            "publication, firstboot preserves deferred installation"
        ),
    )
    parser.add_argument(
        "--windows-bake-username",
        help="Windows local/domain account used only during the private QGA bake boot",
    )
    parser.add_argument(
        "--windows-bake-password",
        help=(
            "Windows bake password; VM2Q_WINDOWS_BAKE_PASSWORD or an interactive "
            "prompt is preferred"
        ),
    )
    parser.add_argument(
        "--windows-bake-domain",
        help="Optional Windows bake account domain; defaults to the local machine",
    )
    parser.add_argument(
        "--winpe-iso",
        type=Path,
        help=(
            "Prepared WinPE ISO with VM2Q Forge startnet.cmd; used by "
            "the winpe-dism backend"
        ),
    )
    parser.add_argument(
        "--winpe-arch",
        choices=("auto", "amd64", "arm64"),
        default="auto",
        help="Architecture of the prepared WinPE ISO; auto reads its filename",
    )
    parser.add_argument(
        "--firmware",
        choices=("auto", "bios", "uefi"),
        default="auto",
    )
    parser.add_argument("--qemu-img", default="qemu-img")
    parser.add_argument("--virt-v2v", default="virt-v2v")
    parser.add_argument(
        "--qemu-system",
        default="qemu-system-x86_64",
        help="QEMU system emulator used to boot WinPE for offline DISM injection",
    )
    parser.add_argument(
        "--winpe-timeout",
        type=int,
        default=900,
        help="Maximum WinPE/DISM helper runtime in seconds",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="Portable runtime directory containing qemu-img/qemu-system/virt-v2v",
    )
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--keep-workdir", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bake_username = (
        args.windows_bake_username
        or os.environ.get("VM2Q_WINDOWS_BAKE_USERNAME")
    )
    bake_password = (
        args.windows_bake_password
        or os.environ.get("VM2Q_WINDOWS_BAKE_PASSWORD")
    )
    if (
        args.require_qga
        and args.qga_install_mode == "bake"
        and not args.dry_run
        and bake_password is None
    ):
        bake_password = getpass.getpass("Windows QGA bake password: ")
    options = ConversionOptions(
        input_path=args.input,
        output_path=args.output,
        qemu_img=args.qemu_img,
        runtime_dir=args.runtime_dir,
        firmware=args.firmware,
        driver_iso=args.driver_iso,
        driver_mode=args.driver_mode,
        overwrite=args.overwrite,
        keep_workdir=args.keep_workdir,
        workdir=args.workdir,
        dry_run=args.dry_run,
        guest_os=args.guest_os,
        windows_backend=args.windows_backend,
        linux_backend=args.linux_backend,
        network_model=args.network_model,
        require_virtio=args.require_virtio,
        require_qga=args.require_qga,
        qga_install_mode=args.qga_install_mode,
        windows_bake_username=bake_username,
        windows_bake_password=bake_password,
        windows_bake_domain=args.windows_bake_domain,
        virtio_win_old=args.virtio_win_old,
        virtio_win_new=args.virtio_win_new,
        virtio_win_version=args.virtio_win_version,
        virt_v2v=args.virt_v2v,
        winpe_iso=args.winpe_iso,
        winpe_arch=args.winpe_arch,
        qemu_system=args.qemu_system,
        winpe_timeout=args.winpe_timeout,
    )

    try:
        result = convert(options, progress=lambda message: print(f"[+] {message}"))
    except (ConversionError, OSError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2

    print()
    print(f"qcow2:     {result.output_path}")
    print(f"report:    {result.report_path}")
    print(f"import doc:{result.import_doc_path}")
    print(f"rollback:  {result.rollback_path}")
    print(f"sha256:    {result.sha256_path}")
    return 0
