from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import asdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .drivers import bundled_iso_candidates, bundled_winpe_candidates, get_bundle
from .model import (
    Architecture,
    CommandRecord,
    ConversionOptions,
    ConversionResult,
    DriverPlan,
    GuestAgentPlan,
    GuestFamily,
    GuestProfile,
    LinuxDependencyPlan,
    NetworkPlan,
    RuntimeInfo,
    SourceSelection,
    VmxMetadata,
)
from .runtime import configure_runtime_environment, resolve_runtime
from .winpe import (
    WinPeError,
    create_result_disk,
    infer_winpe_architecture,
    run_winpe,
    run_windows_bake,
)


class ConversionError(RuntimeError):
    """Raised when a conversion precondition or command fails."""


Progress = Callable[[str], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _log(progress: Progress, message: str) -> None:
    progress(message)


def _safe_zip_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            member_path = (root / member.filename).resolve()
            if os.path.commonpath((str(root), str(member_path))) != str(root):
                raise ConversionError(f"ZIP contains an unsafe path: {member.filename}")
            if member.is_dir():
                member_path.mkdir(parents=True, exist_ok=True)
                continue
            member_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as source, member_path.open("wb") as target:
                shutil.copyfileobj(source, target)


def _is_vmdk_descriptor(path: Path) -> bool:
    try:
        sample = path.read_text(encoding="utf-8", errors="ignore")[:8192]
    except OSError:
        return False
    return (
        "# Disk DescriptorFile" in sample
        and "createType=" in sample
        and bool(re.search(r"(?m)^\s*RW\s+\d+\s+\S+", sample))
    )


def _classify_guest_os(guest_os: str | None, detailed_data: str | None) -> GuestFamily:
    value = " ".join(part for part in (guest_os, detailed_data) if part).lower()
    if not value:
        return "unknown"
    if re.search(
        r"(windows|winxp|winvista|win7|win8|win10|win11|win2008|win2012|"
        r"win2016|win2019|win2022|win2025)",
        value,
    ):
        return "windows"
    if re.search(
        r"(linux|linux26|ubuntu|debian|centos|rhel|redhat|fedora|suse|"
        r"opensuse|sles|oraclelinux|rocky|alma|arch|gentoo|kali|"
        r"amazonlinux|photon|clearlinux|otherlinux)",
        value,
    ):
        return "linux"
    return "unknown"


def _detect_architecture(guest_os: str | None, detailed_data: str | None) -> Architecture:
    value = " ".join(part for part in (guest_os, detailed_data) if part).lower()
    if re.search(r"(arm64|aarch64|arm-64)", value):
        return "arm64"
    if re.search(r"(amd64|x86_64|x64|64[-_ ]?bit|-64\b)", value):
        return "amd64"
    if re.search(r"(i[3-6]86|x86|32[-_ ]?bit|-32\b)", value):
        return "x86"
    return "unknown"


def _windows_generation(guest_os: str | None, detailed_data: str | None) -> str:
    value = " ".join(part for part in (guest_os, detailed_data) if part).lower()
    if re.search(r"(windows7|win7|winvista|windowsvista|winxp|windowsxp|"
                 r"windows2000|win2000|win2003|windows2003|win2008|windows2008)",
                 value):
        return "legacy"
    if re.search(r"(windows11|win11|windows10|win10|windows12|win12|"
                 r"windows2012|win2012|windows2016|win2016|windows2019|"
                 r"win2019|windows2022|win2022|windows2025|win2025)",
                 value):
        return "modern"
    if re.search(r"(windows8|win8|windows9|win9|windows2012|win2012)", value):
        return "modern"
    return "unknown"


def _parse_vmx(path: Path) -> VmxMetadata:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"')
        values[key] = value

    firmware = "uefi" if values.get("firmware", "").lower() == "efi" else "bios"
    guest_os = values.get("guestOS")
    guest_os_detailed = values.get("guestOS.detailedData")
    disk_interface: str | None = None
    disk_filename: str | None = None
    preferred_keys = (
        "scsi0:0.fileName",
        "sata0:0.fileName",
        "ide0:0.fileName",
        "nvme0:0.fileName",
    )
    for key in preferred_keys:
        if key in values:
            disk_interface = key.split(":", 1)[0]
            disk_filename = values[key]
            break
    if disk_filename is None:
        for key, value in values.items():
            if key.endswith(".fileName") and value.lower().endswith(".vmdk"):
                disk_interface = key.split(":", 1)[0]
                disk_filename = value
                break

    return VmxMetadata(
        path=path,
        firmware=firmware,
        disk_interface=disk_interface,
        disk_filename=disk_filename,
        hardware_version=values.get("virtualHW.version"),
        guest_os=guest_os,
        guest_os_detailed=guest_os_detailed,
        guest_os_family=_classify_guest_os(guest_os, guest_os_detailed),
        architecture=_detect_architecture(guest_os, guest_os_detailed),
        values=values,
    )


def _find_vmx(root: Path) -> Path | None:
    vmx_files = sorted(root.rglob("*.vmx"))
    return vmx_files[0] if vmx_files else None


def _select_vmdk(root: Path, vmx: Path | None) -> tuple[Path, VmxMetadata | None]:
    metadata = _parse_vmx(vmx) if vmx else None
    if metadata and metadata.disk_filename:
        referenced = (vmx.parent / metadata.disk_filename).resolve()
        if referenced.exists() and _is_vmdk_descriptor(referenced):
            return referenced, metadata

    descriptors = [
        path
        for path in sorted(root.rglob("*.vmdk"))
        if _is_vmdk_descriptor(path)
    ]
    if not descriptors:
        raise ConversionError(f"No VMware VMDK descriptor found under {root}")
    if len(descriptors) > 1:
        names = ", ".join(str(path.relative_to(root)) for path in descriptors)
        raise ConversionError(
            "Multiple VMDK descriptors found; select a VMX-referenced disk or "
            f"pass a directory containing one VM. Candidates: {names}"
        )
    return descriptors[0], metadata


def _assert_unlocked(root: Path) -> None:
    locks = sorted(root.rglob("*.lck"))
    if locks:
        names = ", ".join(str(path.relative_to(root)) for path in locks[:8])
        suffix = " ..." if len(locks) > 8 else ""
        raise ConversionError(
            f"VMware lock entries are present; shut down the source VM first: {names}{suffix}"
        )


def _prepare_source(input_path: Path, workdir: Path | None) -> tuple[SourceSelection, Path | None]:
    if not input_path.exists():
        raise ConversionError(f"Input does not exist: {input_path}")

    if input_path.is_dir():
        root = input_path.resolve()
        extracted = False
        cleanup_root = None
    elif input_path.is_file() and input_path.suffix.lower() == ".zip":
        cleanup_root = Path(
            tempfile.mkdtemp(prefix="vmware2qcow2-", dir=str(workdir) if workdir else None)
        )
        _safe_zip_extract(input_path.resolve(), cleanup_root)
        roots = [path for path in cleanup_root.iterdir() if path.is_dir()]
        root = roots[0] if len(roots) == 1 else cleanup_root
        extracted = True
    else:
        raise ConversionError("Input must be a VMware work directory or a .zip archive")

    _assert_unlocked(root)
    vmx = _find_vmx(root)
    vmdk, metadata = _select_vmdk(root, vmx)
    return (
        SourceSelection(
            root=root,
            vmx=vmx,
            vmdk_descriptor=vmdk,
            vmx_metadata=metadata,
            extracted_from_zip=extracted,
        ),
        cleanup_root,
    )


def _run(
    argv: list[str],
    commands: list[CommandRecord],
    dry_run: bool,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> CommandRecord:
    if dry_run:
        record = CommandRecord(
            tuple(argv),
            0,
            "",
            "",
            {"VIRTIO_WIN": env["VIRTIO_WIN"]}
            if env and "VIRTIO_WIN" in env
            else {},
        )
        commands.append(record)
        return record
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    record = CommandRecord(
        tuple(argv),
        completed.returncode,
        completed.stdout,
        completed.stderr,
        {"VIRTIO_WIN": env["VIRTIO_WIN"]}
        if env and "VIRTIO_WIN" in env
        else {},
    )
    commands.append(record)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ConversionError(
            f"Command failed with exit status {completed.returncode}: "
            f"{' '.join(argv)}\n{detail}"
        )
    return record


def _json_command(
    argv: list[str],
    commands: list[CommandRecord],
    dry_run: bool,
) -> dict:
    record = _run(argv, commands, dry_run)
    if dry_run:
        return {"dry_run": True}
    try:
        return json.loads(record.stdout)
    except json.JSONDecodeError as exc:
        raise ConversionError(f"Expected JSON from {' '.join(argv)}") from exc


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _guest_profile(selection: SourceSelection, requested: str) -> GuestProfile:
    metadata = selection.vmx_metadata
    detected_family: GuestFamily = metadata.guest_os_family if metadata else "unknown"
    detected_architecture: Architecture = (
        metadata.architecture if metadata else "unknown"
    )
    detected_guest_os = metadata.guest_os if metadata else None
    detailed_data = metadata.guest_os_detailed if metadata else None

    if requested == "auto":
        family = detected_family
        reason = (
            f"VMX guestOS={detected_guest_os!r}, "
            f"guestOS.detailedData={detailed_data!r}"
        )
    else:
        family = requested  # type: ignore[assignment]
        reason = f"guest OS explicitly set to {requested}"

    return GuestProfile(
        requested=requested,  # type: ignore[arg-type]
        family=family,
        architecture=detected_architecture,
        detected_guest_os=detected_guest_os,
        detected_detailed_data=detailed_data,
        detection_reason=reason,
    )


def _select_virtio_version(
    profile: GuestProfile, requested_version: str
) -> tuple[str | None, str]:
    if profile.family != "windows":
        return None, "virtio-win is only applicable to Windows guests"
    if requested_version != "auto":
        get_bundle(requested_version)
        return requested_version, "virtio-win version explicitly selected"

    generation = _windows_generation(
        profile.detected_guest_os, profile.detected_detailed_data
    )
    if generation == "legacy":
        return "0.1.173-9", "VMX identifies Windows 7/Vista/legacy Windows"
    if generation == "modern":
        return "0.1.285-1", "VMX identifies Windows 8/10/11/modern Windows"
    return (
        "0.1.285-1",
        "Windows was detected but its generation was not explicit; selected modern bundle",
    )


def _driver_source(
    options: ConversionOptions, version: str
) -> tuple[Path | None, str]:
    if options.driver_iso is not None:
        source = options.driver_iso.expanduser().resolve()
        if not source.exists():
            raise ConversionError(f"Driver ISO or directory does not exist: {source}")
        return source, "driver-iso override"

    version_path = (
        options.virtio_win_old
        if version == "0.1.173-9"
        else options.virtio_win_new
    )
    if version_path is not None:
        source = version_path.expanduser().resolve()
        if not source.exists():
            raise ConversionError(
                f"virtio-win {version} ISO or directory does not exist: {source}"
            )
        return source, "version-specific driver path"

    for candidate in bundled_iso_candidates(version):
        if candidate.exists():
            return candidate, "automatic package/cache discovery"
    return None, "no driver asset found in configured or package locations"


def _resolve_winpe_source(
    options: ConversionOptions,
    profile: GuestProfile,
) -> tuple[Path | None, str | None]:
    """Find and validate a prepared WinPE image for the guest architecture."""

    source = options.winpe_iso.expanduser().resolve() if options.winpe_iso else None
    if source is None:
        source = next(
            (candidate.resolve() for candidate in bundled_winpe_candidates()
             if candidate.is_file()),
            None,
        )
    if source is None:
        return None, None
    if not source.is_file():
        raise ConversionError(f"WinPE ISO does not exist: {source}")

    selected_arch = options.winpe_arch
    inferred_arch = infer_winpe_architecture(source)
    if selected_arch == "auto":
        selected_arch = inferred_arch
    if selected_arch not in ("amd64", "arm64"):
        raise ConversionError(
            "WinPE architecture could not be verified from the ISO name; "
            "pass --winpe-arch amd64 or --winpe-arch arm64"
        )
    if inferred_arch and options.winpe_arch != "auto" and inferred_arch != selected_arch:
        raise ConversionError(
            f"WinPE ISO name indicates {inferred_arch}, but "
            f"--winpe-arch selected {selected_arch}: {source}"
        )
    if selected_arch == "arm64":
        raise ConversionError(
            "winpe-dism currently boots amd64 WinPE with "
            "qemu-system-x86_64; use a prepared winpe-amd64.iso"
        )
    if (
        profile.architecture in ("amd64", "x86", "arm64")
        and selected_arch != profile.architecture
    ):
        raise ConversionError(
            f"WinPE architecture {selected_arch} does not match guest "
            f"architecture {profile.architecture}"
        )
    return source, selected_arch


def _driver_roots(profile: GuestProfile, version: str) -> tuple[str, ...]:
    bundle = get_bundle(version)
    if version == "0.1.173-9":
        return bundle.driver_roots

    value = " ".join(
        part
        for part in (profile.detected_guest_os, profile.detected_detailed_data)
        if part
    ).lower()
    if "windows11" in value or "win11" in value:
        return ("w11", "w10", "w8")
    if "windows8" in value or "win8" in value:
        return ("w8", "w10", "w11")
    return ("w10", "w11", "w8")


def _write_windows_driver_script(
    path: Path,
    version: str,
    roots: tuple[str, ...],
    architecture: Architecture,
    source: Path | None,
) -> None:
    arch_dir = "x86" if architecture == "x86" else "amd64"
    lines = [
        "@echo off",
        "setlocal",
        "rem Generated by vmware2qcow2; review the ISO drive letter before running.",
        "set ISO_DRIVE=E:",
        f"set DRIVER_ARCH={arch_dir}",
        f"echo Selected virtio-win bundle: {version}",
        "set FOUND=0",
        "set RC=0",
    ]
    if source:
        lines.append(f"rem Asset selected by the converter: {source}")
    else:
        lines.append(
            "echo Mount the matching virtio-win ISO and set ISO_DRIVE if it is not E:."
        )
    for root in roots:
        lines.extend(
            [
                f'if exist "%ISO_DRIVE%\\viostor\\{root}\\%DRIVER_ARCH%\\viostor.inf" (',
                f'  echo Installing viostor from {root}...',
                f'  pnputil -a "%ISO_DRIVE%\\viostor\\{root}\\%DRIVER_ARCH%\\viostor.inf" || set RC=1',
                f'  echo Installing NetKVM from {root}...',
                f'  pnputil -a "%ISO_DRIVE%\\NetKVM\\{root}\\%DRIVER_ARCH%\\netkvm.inf" || set RC=1',
                "  set FOUND=1",
                "  goto :driver_done",
                ")",
            ]
        )
    lines.extend(
        [
            ":driver_done",
            'if "%FOUND%"=="0" (',
            "  echo No matching viostor/NetKVM INF files were found.",
            "  exit /b 2",
            ")",
            "echo Driver staging finished with errorlevel %RC%.",
            "exit /b %RC%",
        ]
    )
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


def _driver_plan(
    options: ConversionOptions, report_dir: Path, profile: GuestProfile
) -> DriverPlan:
    if options.driver_mode == "none":
        return DriverPlan(
            "none",
            None,
            (),
            None,
            "skipped",
            guest_os_family=profile.family,
        )

    if profile.family != "windows":
        return DriverPlan(
            "none",
            None,
            (),
            None,
            "not-applicable",
            selection_reason="Linux uses virtio drivers from the guest kernel, not virtio-win",
            guest_os_family=profile.family,
        )

    version, selection_reason = _select_virtio_version(
        profile, options.virtio_win_version
    )
    if version is None:
        raise ConversionError("A Windows guest requires a concrete virtio-win version")
    get_bundle(version)
    roots = _driver_roots(profile, version)
    expected_paths = tuple(
        f"{component}\\{root}\\{'x86' if profile.architecture == 'x86' else 'amd64'}"
        for root in roots
        for component in ("viostor", "NetKVM")
    )
    iso, source_reason = _driver_source(options, version)
    script_name = (
        "install-virtio-win7.cmd" if version == "0.1.173-9"
        else "install-virtio-win-modern.cmd"
    )
    script_path = report_dir / script_name
    _write_windows_driver_script(
        script_path, version, roots, profile.architecture, iso
    )

    if iso is None:
        status = "missing-driver-iso"
    elif iso.is_dir():
        available = any((iso / Path(path.replace("\\", "/")) / "viostor.inf").exists()
                        for path in expected_paths if path.startswith("viostor\\"))
        status = "source-checked" if available else "prepared-script"
    else:
        status = "prepared-script"
    return DriverPlan(
        "plan",
        iso,
        expected_paths,
        script_path,
        status,
        version=version,
        selection_reason=f"{selection_reason}; {source_reason}",
        guest_os_family=profile.family,
    )


def _reg_sz_hex(value: str) -> str:
    encoded = value.encode("utf-16le") + b"\x00\x00"
    return "hex(1):" + ",".join(f"{byte:02x}" for byte in encoded)


def _windows_bake_registry(
    username: str,
    password: str,
    domain: str | None,
) -> str:
    """Build a temporary autologon registry import for the bake boot."""

    local_domain = domain or "."
    return (
        "Windows Registry Editor Version 5.00\r\n"
        "\r\n"
        "[HKEY_LOCAL_MACHINE\\VM2Q_QGA_SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon]\r\n"
        '"AutoAdminLogon"="1"\r\n'
        f'"DefaultUserName"={_reg_sz_hex(username)}\r\n'
        f'"DefaultPassword"={_reg_sz_hex(password)}\r\n'
        f'"DefaultDomainName"={_reg_sz_hex(local_domain)}\r\n'
    )


def _windows_bake_storage_controller(selection: SourceSelection) -> str:
    """Map VMware's boot disk controller to a QEMU controller for bake boots."""

    metadata = selection.vmx_metadata
    if metadata is None or metadata.disk_interface is None:
        return "ide"

    controller = metadata.disk_interface
    if not controller.startswith("scsi"):
        return "ide"

    virtual_dev = metadata.values.get(f"{controller}.virtualDev", "").lower()
    if virtual_dev == "lsisas1068":
        return "mptsas1068"
    if virtual_dev in {"lsilogic", "lsilogic-sas"}:
        return "lsi53c895a"
    if virtual_dev == "pvscsi":
        return "pvscsi"
    if virtual_dev in {"megasas", "megasas-gen2"}:
        return virtual_dev
    return "ide"


def _write_windows_qga_script(
    path: Path,
    msi_name: str,
    bake: bool = False,
) -> None:
    service_autostart_bake = r"""sc config QEMU-GA start= auto >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] Failed to set QEMU-GA service to automatic start >> "%LOG%"
    set "RC=14"
    goto :failed
)
sc qc QEMU-GA >> "%LOG%" 2>&1
set "SERVICE_START="
for /f "tokens=3" %%S in ('reg query "HKLM\SYSTEM\CurrentControlSet\Services\QEMU-GA" /v Start 2^>nul ^| findstr /i "Start"') do set "SERVICE_START=%%S"
if /I not "%SERVICE_START%"=="0x2" (
    echo [%date% %time%] QEMU-GA service is not configured for automatic start >> "%LOG%"
    set "RC=15"
    goto :failed
)
    """
    service_autostart_firstboot = r"""sc config QEMU-GA start= auto >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] Failed to set QEMU-GA service to automatic start >> "%LOG%"
    set "RC=14"
) else (
    sc qc QEMU-GA >> "%LOG%" 2>&1
    set "SERVICE_START="
    for /f "tokens=3" %%S in ('reg query "HKLM\SYSTEM\CurrentControlSet\Services\QEMU-GA" /v Start 2^>nul ^| findstr /i "Start"') do set "SERVICE_START=%%S"
    if /I not "!SERVICE_START!"=="0x2" (
        echo [%date% %time%] QEMU-GA service is not configured for automatic start >> "%LOG%"
        set "RC=15"
    )
)
"""
    if bake:
        content = f"""@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Generated by vmware2qcow2.
rem This script runs during the private QGA bake boot.
set "BASE=%ProgramData%\\VM2Q-Forge"
set "MSI=%BASE%\\{msi_name}"
set "LOG=%BASE%\\qemu-ga-install.log"
set "RESULT="
if not exist "%BASE%" mkdir "%BASE%" >nul 2>&1
for %%D in (B C D E F G H I J K L M N O P Q R S T U V W Y Z) do (
    if not defined RESULT if exist "%%D:\\VM2QREQ.TXT" set "RESULT=%%D:"
)
echo [%date% %time%] QEMU Guest Agent image bake started > "%LOG%"
if not exist "%MSI%" (
    echo [%date% %time%] Missing MSI: %MSI% >> "%LOG%"
    goto :failed
)
msiexec.exe /i "%MSI%" /qn /norestart /l*v "%LOG%"
set "RC=%ERRORLEVEL%"
if "%RC%"=="3010" set "RC=0"
echo [%date% %time%] msiexec exit code: %RC% >> "%LOG%"
if not "%RC%"=="0" goto :failed
sc query QEMU-GA >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] QEMU-GA service was not registered >> "%LOG%"
    set "RC=11"
    goto :failed
)
{service_autostart_bake}
sc start QEMU-GA >> "%LOG%" 2>&1
sc query QEMU-GA >> "%LOG%" 2>&1
sc query QEMU-GA | find /I "RUNNING" >nul
if errorlevel 1 (
    echo [%date% %time%] QEMU-GA service did not reach RUNNING state >> "%LOG%"
    set "RC=13"
    goto :failed
)
if not defined RESULT (
    echo [%date% %time%] Result disk was not found >> "%LOG%"
    set "RC=12"
    goto :failed
)
rem Re-arm the VirtIO-net DHCP helper for the published target boot.  The
rem private bake boot has no network device, so any earlier RunOnce network
rem entry may have already been consumed.
if exist "%BASE%\\configure-windows-virtio-net.cmd" (
    reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\RunOnce" /v VM2QForgeVirtioNet /t REG_EXPAND_SZ /d "cmd.exe /c %%ProgramData%%\\VM2Q-Forge\\configure-windows-virtio-net.cmd" /f >> "%LOG%" 2>&1
)
rem Remove the temporary autologon values before publishing the image.
reg delete "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon" /v AutoAdminLogon /f >nul 2>&1
reg delete "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon" /v DefaultPassword /f >nul 2>&1
reg delete "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon" /v DefaultUserName /f >nul 2>&1
reg delete "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon" /v DefaultDomainName /f >nul 2>&1
echo status=installed>"%RESULT%\\VM2QRES.TXT"
echo qga_installed=1 >>"%RESULT%\\VM2QRES.TXT"
echo qga_service=running>>"%RESULT%\\VM2QRES.TXT"
echo qga_service_start=auto>>"%RESULT%\\VM2QRES.TXT"
echo qga_msi={msi_name}>>"%RESULT%\\VM2QRES.TXT"
echo status=installed>"%RESULT%\\VM2QLOG.TXT"
del /f /q "%MSI%" >nul 2>&1
del /f /q "%~f0" >nul 2>&1
shutdown.exe /s /t 0 /f
exit /b 0

:failed
if not defined RESULT exit /b %RC%
echo status=failed>"%RESULT%\\VM2QRES.TXT"
echo qga_installed=0 >>"%RESULT%\\VM2QRES.TXT"
echo qga_rc=%RC%>>"%RESULT%\\VM2QRES.TXT"
shutdown.exe /s /t 0 /f
exit /b %RC%
"""
    else:
        content = f"""@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem Generated by vmware2qcow2.
rem This script is staged offline and runs once after the first Windows logon.
set "BASE=%ProgramData%\\VM2Q-Forge"
set "MSI=%BASE%\\{msi_name}"
set "LOG=%BASE%\\qemu-ga-install.log"
if not exist "%BASE%" mkdir "%BASE%" >nul 2>&1
echo [%date% %time%] QEMU Guest Agent installation started > "%LOG%"
if not exist "%MSI%" (
    echo [%date% %time%] Missing MSI: %MSI% >> "%LOG%"
    exit /b 10
)
msiexec.exe /i "%MSI%" /qn /norestart /l*v "%LOG%"
set "RC=%ERRORLEVEL%"
echo [%date% %time%] msiexec exit code: %RC% >> "%LOG%"
if "%RC%"=="0" (
    sc query QEMU-GA >> "%LOG%" 2>&1
    if errorlevel 1 (
        echo [%date% %time%] QEMU-GA service was not registered >> "%LOG%"
        set "RC=11"
    ) else (
        {service_autostart_firstboot}
        if "!RC!"=="0" sc start QEMU-GA >> "%LOG%" 2>&1
    )
)
exit /b %RC%
"""
    path.write_text(content, encoding="utf-8")


def _guest_agent_plan(
    options: ConversionOptions,
    report_dir: Path,
    profile: GuestProfile,
    driver_plan: DriverPlan,
) -> GuestAgentPlan:
    if profile.family != "windows":
        return GuestAgentPlan(
            required=False,
            architecture=None,
            msi_relative_path=None,
            install_script=None,
            status="not-applicable",
            install_mode=options.qga_install_mode,
        )

    if not options.require_qga:
        return GuestAgentPlan(
            required=False,
            architecture=(
                "x86" if profile.architecture == "x86" else "amd64"
                if profile.architecture == "amd64"
                else None
            ),
            msi_relative_path=None,
            install_script=None,
            status="disabled",
            install_mode=options.qga_install_mode,
        )

    if driver_plan.iso is None:
        raise ConversionError(
            "QEMU Guest Agent is required: provide a real virtio-win ISO "
            "containing guest-agent/qemu-ga-x86_64.msi or qemu-ga-i386.msi"
        )
    if profile.architecture == "amd64":
        architecture = "amd64"
        msi_name = "qemu-ga-x86_64.msi"
    elif profile.architecture == "x86":
        architecture = "x86"
        msi_name = "qemu-ga-i386.msi"
    else:
        raise ConversionError(
            "QEMU Guest Agent is required, but the Windows guest architecture "
            "could not select an x86 or x86_64 MSI"
        )

    install_script = report_dir / "install-qemu-guest-agent.cmd"
    _write_windows_qga_script(
        install_script,
        msi_name,
        bake=options.qga_install_mode == "bake",
    )
    return GuestAgentPlan(
        required=True,
        architecture=architecture,
        msi_relative_path=f"guest-agent/{msi_name}",
        install_script=install_script,
        status=(
            "required-bake-before-publish"
            if options.qga_install_mode == "bake"
            else "required-staged-firstboot"
        ),
        channel="virtio-serial",
        staged=False,
        install_mode=options.qga_install_mode,
    )


def _linux_profile_text(profile: GuestProfile) -> str:
    return " ".join(
        part
        for part in (profile.detected_guest_os, profile.detected_detailed_data)
        if part
    ).lower()


def _linux_distribution(profile: GuestProfile) -> str:
    value = _linux_profile_text(profile)
    patterns = (
        ("kali", r"\bkali(?:[-_ ]|(?=\d)|$)"),
        ("debian", r"\bdebian(?:[-_ ]|(?=\d)|$)"),
        ("ubuntu", r"\bubuntu(?:[-_ ]|(?=\d)|$)"),
        ("rhel", r"\b(?:rhel|redhat)(?:[-_ ]|(?=\d)|$)"),
        ("centos", r"\bcentos(?:[-_ ]|(?=\d)|$)"),
        ("rocky", r"\brocky(?:[-_ ]|(?=\d)|$)"),
        ("alma", r"\balma(?:[-_ ]|(?=\d)|$)"),
        ("oraclelinux", r"\boraclelinux(?:[-_ ]|(?=\d)|$)"),
        ("fedora", r"\bfedora(?:[-_ ]|(?=\d)|$)"),
        ("sles", r"\b(?:sles|suse)(?:[-_ ]|(?=\d)|$)"),
        ("opensuse", r"\bopensuse(?:[-_ ]|(?=\d)|$)"),
        ("arch", r"\barch(?:linux)?(?:[-_ ]|(?=\d)|$)"),
        ("gentoo", r"\bgentoo(?:[-_ ]|(?=\d)|$)"),
        ("amazon", r"\bamazon(?:linux)?(?:[-_ ]|(?=\d)|$)"),
        ("photon", r"\bphoton(?:[-_ ]|(?=\d)|$)"),
    )
    for name, pattern in patterns:
        if re.search(pattern, value):
            return name
    if "linux26" in value:
        return "generic-linux-legacy"
    if "otherlinux" in value or "linux" in value:
        return "generic-linux"
    return "unknown-linux"


def _linux_major_version(profile: GuestProfile, distribution: str) -> int | None:
    value = _linux_profile_text(profile)
    if distribution == "ubuntu" and re.search(r"ubuntu[-_ ]?64\b", value):
        return None
    aliases = {
        "rhel": r"(?:rhel|redhat)",
        "sles": r"(?:sles|suse)",
        "amazon": r"amazon(?:linux)?",
        "kali": r"kali(?:[-_ ]linux)?",
        "generic-linux-legacy": r"linux",
        "generic-linux": r"linux",
    }
    name = aliases.get(distribution, distribution)
    match = re.search(rf"{name}(?:[-_ ]?)(\d+)", value)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _linux_generation(
    profile: GuestProfile,
    distribution: str,
) -> str:
    value = _linux_profile_text(profile)
    if distribution == "generic-linux-legacy" or "linux26" in value:
        return "legacy"
    major = _linux_major_version(profile, distribution)
    if distribution in {"debian", "kali"}:
        if major is None:
            return "unknown"
        if major <= (9 if distribution == "debian" else 2020):
            return "legacy"
        if major <= (11 if distribution == "debian" else 2022):
            return "intermediate"
        return "modern"
    if distribution == "ubuntu":
        if major is None:
            return "unknown"
        if major <= 16:
            return "legacy"
        if major <= 20:
            return "intermediate"
        return "modern"
    if distribution in {
        "rhel",
        "centos",
        "rocky",
        "alma",
        "oraclelinux",
    }:
        if major is None:
            return "unknown"
        if major <= 7:
            return "legacy"
        if major == 8:
            return "intermediate"
        return "modern"
    if distribution == "sles":
        if major is None:
            return "unknown"
        if major <= 12:
            return "legacy"
        if major == 15:
            return "modern"
        return "intermediate"
    if distribution == "fedora":
        if major is None:
            return "unknown"
        if major <= 28:
            return "legacy"
        if major <= 34:
            return "intermediate"
        return "modern"
    if distribution == "amazon":
        if major is None:
            return "unknown"
        return "legacy" if major <= 2 else "modern"
    if distribution in {"arch", "gentoo", "photon", "opensuse"}:
        return "unknown"
    return "unknown"


def _linux_dependency_plan(
    report_dir: Path,
    profile: GuestProfile,
) -> LinuxDependencyPlan:
    if profile.family != "linux":
        return LinuxDependencyPlan(
            distribution="not-applicable",
            generation="unknown",
            required_modules=(),
            initramfs_tools=(),
            network_stacks=(),
            dhcp_clients=(),
            package_hints=(),
            script=None,
            status="not-applicable",
        )

    distribution = _linux_distribution(profile)
    generation = _linux_generation(profile, distribution)
    if distribution in {"debian", "ubuntu", "kali"}:
        initramfs_tools = ("update-initramfs", "dracut")
        network_stacks = (
            "NetworkManager",
            "netplan",
            "ifupdown",
            "systemd-networkd",
        )
        dhcp_clients = ("dhclient", "dhcpcd", "udhcpc")
        package_hints = (
            "initramfs-tools",
            "network-manager or ifupdown",
            "isc-dhcp-client or dhcpcd",
        )
    elif distribution in {
        "rhel",
        "centos",
        "rocky",
        "alma",
        "oraclelinux",
        "fedora",
        "amazon",
    }:
        initramfs_tools = ("dracut", "update-initramfs")
        network_stacks = (
            "NetworkManager",
            "ifcfg scripts",
            "systemd-networkd",
        )
        dhcp_clients = ("dhclient", "dhcpcd", "udhcpc")
        package_hints = (
            "dracut",
            "NetworkManager",
            "dhclient or dhcpcd",
        )
    elif distribution in {"sles", "opensuse"}:
        initramfs_tools = ("dracut", "mkinitrd")
        network_stacks = ("wicked", "NetworkManager", "systemd-networkd")
        dhcp_clients = ("dhclient", "dhcpcd")
        package_hints = ("dracut", "wicked or NetworkManager", "dhcp-client")
    elif distribution in {"arch", "gentoo"}:
        initramfs_tools = ("mkinitcpio", "dracut")
        network_stacks = (
            "NetworkManager",
            "systemd-networkd",
            "dhcpcd",
            "ifupdown",
        )
        dhcp_clients = ("dhcpcd", "dhclient", "udhcpc")
        package_hints = (
            "mkinitcpio or dracut",
            "networkmanager or systemd-networkd",
            "dhcpcd or dhclient",
        )
    else:
        initramfs_tools = ("update-initramfs", "dracut", "mkinitcpio")
        network_stacks = (
            "NetworkManager",
            "netplan",
            "wicked",
            "systemd-networkd",
            "ifupdown",
        )
        dhcp_clients = ("dhclient", "dhcpcd", "udhcpc")
        package_hints = (
            "one of initramfs-tools, dracut, or mkinitcpio",
            "one of NetworkManager, systemd-networkd, or ifupdown",
            "one of dhclient, dhcpcd, or udhcpc",
        )

    warnings = [
        "qemu-img converts sectors but does not inspect or rebuild the guest initramfs",
        "package hints are reported only; offline qemu-img mode does not install guest packages",
    ]
    if generation in {"legacy", "unknown"}:
        warnings.append(
            "verify virtio_pci, virtio_blk, and virtio_net are available in the boot initramfs"
        )
    if distribution == "unknown-linux":
        warnings.append(
            "VMX does not identify a known distribution; keep an e1000 fallback for the first boot"
        )
    if profile.architecture == "unknown":
        warnings.append(
            "guest architecture is unknown; verify the target emulator and initramfs architecture"
        )

    script = report_dir / "configure-linux-virtio-net.sh"
    plan = LinuxDependencyPlan(
        distribution=distribution,
        generation=generation,
        required_modules=("virtio_pci", "virtio_blk", "virtio_net", "virtio_scsi"),
        initramfs_tools=initramfs_tools,
        network_stacks=network_stacks,
        dhcp_clients=dhcp_clients,
        package_hints=package_hints,
        script=script,
        status=(
            "legacy-or-unknown-firstboot-check"
            if generation in {"legacy", "unknown"}
            else "portable-kernel-and-network-preflight"
        ),
        warnings=tuple(warnings),
        verification_commands=(
            "uname -m",
            "modprobe virtio_pci virtio_blk virtio_net",
            "grep -E 'virtio_(pci|blk|net|scsi)' /proc/modules",
            "ip -br addr",
            "ip route",
        ),
    )
    _write_linux_network_script(script, plan)
    return plan


def _write_linux_network_script(
    path: Path,
    dependency_plan: LinuxDependencyPlan | None = None,
) -> None:
    distribution = dependency_plan.distribution if dependency_plan else "generic-linux"
    generation = dependency_plan.generation if dependency_plan else "unknown"
    initramfs_hints = (
        ", ".join(dependency_plan.initramfs_tools)
        if dependency_plan
        else "update-initramfs, dracut, mkinitcpio"
    )
    network_hints = (
        ", ".join(dependency_plan.network_stacks)
        if dependency_plan
        else "NetworkManager, Netplan, systemd-networkd, ifupdown"
    )
    dhcp_hints = (
        ", ".join(dependency_plan.dhcp_clients)
        if dependency_plan
        else "dhclient, dhcpcd, udhcpc"
    )
    template = """#!/bin/sh
set -u

# Generated by vmware2qcow2.
# Distribution hint: @DISTRO@
# Compatibility generation: @GENERATION@
# Initramfs candidates: @INITRAMFS@
# Network candidates: @NETWORK@
# DHCP candidates: @DHCP@

LOG_DIR=/var/log/vm2q-forge
LOG_FILE="$LOG_DIR/linux-virtio-firstboot.log"
mkdir -p "$LOG_DIR" 2>/dev/null || true

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE"
}

log "starting Linux VirtIO preparation"

MODULES="virtio_pci virtio_blk virtio_net virtio_scsi"
if command -v modprobe >/dev/null 2>&1; then
    for module in $MODULES; do
        if modprobe "$module" >>"$LOG_FILE" 2>&1; then
            log "loaded $module"
        else
            log "module $module was not loadable; continuing"
        fi
    done
else
    log "modprobe is missing; install the matching kmod/module-init-tools package"
fi

if mkdir -p /etc/modules-load.d 2>/dev/null; then
    printf '%s\n' $MODULES >/etc/modules-load.d/vm2q-forge-virtio.conf
elif [ -w /etc/modules ] || { [ ! -e /etc/modules ] && [ -w /etc ]; }; then
    for module in $MODULES; do
        if ! grep -qx "$module" /etc/modules 2>/dev/null; then
            printf '%s\n' "$module" >>/etc/modules
        fi
    done
    log "persisted modules in /etc/modules"
else
    log "could not persist module names; check /etc/modules-load.d or /etc/modules"
fi

if command -v dracut >/dev/null 2>&1; then
    mkdir -p /etc/dracut.conf.d 2>/dev/null || true
    printf '%s\n' 'add_drivers+=" virtio_pci virtio_blk virtio_net virtio_scsi "' \
        >/etc/dracut.conf.d/90-vm2q-forge-virtio.conf 2>/dev/null || true
fi

INITRAMFS_RC=0
if command -v update-initramfs >/dev/null 2>&1; then
    update-initramfs -u -k all >>"$LOG_FILE" 2>&1 || INITRAMFS_RC=$?
elif command -v dracut >/dev/null 2>&1; then
    dracut -f --regenerate-all >>"$LOG_FILE" 2>&1 || \
        dracut -f >>"$LOG_FILE" 2>&1 || INITRAMFS_RC=$?
elif command -v mkinitcpio >/dev/null 2>&1; then
    mkinitcpio -P >>"$LOG_FILE" 2>&1 || INITRAMFS_RC=$?
elif command -v mkinitrd >/dev/null 2>&1; then
    log "mkinitrd is present; regenerate the distribution initramfs before switching the boot disk"
else
    INITRAMFS_RC=127
    log "no supported initramfs generator was found"
fi
if [ "$INITRAMFS_RC" -ne 0 ]; then
    log "initramfs refresh returned $INITRAMFS_RC"
else
    log "initramfs preparation completed or was not required"
fi

IFACE="${1:-}"
if [ -z "$IFACE" ]; then
    for entry in /sys/class/net/*; do
        name="${entry##*/}"
        [ "$name" = "lo" ] && continue
        IFACE="$name"
        break
    done
fi

if [ -z "$IFACE" ]; then
    log "no non-loopback network interface was found"
    exit 2
fi

if command -v ip >/dev/null 2>&1; then
    ip link set "$IFACE" up >>"$LOG_FILE" 2>&1 || true
elif command -v ifconfig >/dev/null 2>&1; then
    ifconfig "$IFACE" up >>"$LOG_FILE" 2>&1 || true
fi

if [ -d /etc/netplan ] && command -v netplan >/dev/null 2>&1; then
    cat >/etc/netplan/99-vmware2qcow2-virtio.yaml <<EOF
network:
  version: 2
  ethernets:
    vmware2qcow2-virtio:
      match:
        name: "$IFACE"
      dhcp4: true
      dhcp6: false
EOF
    netplan generate >>"$LOG_FILE" 2>&1 || true
    netplan apply >>"$LOG_FILE" 2>&1 || true
elif command -v nmcli >/dev/null 2>&1; then
    nmcli connection add type ethernet ifname "$IFACE" \
        con-name vmware2qcow2-virtio ipv4.method auto ipv6.method auto \
        >>"$LOG_FILE" 2>&1 || true
    nmcli connection modify vmware2qcow2-virtio connection.autoconnect yes \
        ipv4.method auto ipv6.method auto >>"$LOG_FILE" 2>&1 || true
    nmcli connection up vmware2qcow2-virtio >>"$LOG_FILE" 2>&1 || true
elif command -v wicked >/dev/null 2>&1; then
    wicked ifup "$IFACE" >>"$LOG_FILE" 2>&1 || true
elif [ -d /etc/systemd/network ] && command -v networkctl >/dev/null 2>&1; then
    cat >/etc/systemd/network/80-vmware2qcow2-virtio.network <<EOF
[Match]
Name=$IFACE

[Network]
DHCP=yes
EOF
    networkctl reload >>"$LOG_FILE" 2>&1 || true
    networkctl reconfigure "$IFACE" >>"$LOG_FILE" 2>&1 || true
elif [ -f /etc/network/interfaces ] && command -v ifup >/dev/null 2>&1; then
    if ! grep -qE "^[[:space:]]*(allow-hotplug|auto)[[:space:]]+$IFACE([[:space:]]|$)" \
        /etc/network/interfaces 2>/dev/null; then
        printf '\nauto %s\nallow-hotplug %s\niface %s inet dhcp\n' \
            "$IFACE" "$IFACE" "$IFACE" >>/etc/network/interfaces
    fi
    command -v ifdown >/dev/null 2>&1 && ifdown "$IFACE" >>"$LOG_FILE" 2>&1 || true
    ifup "$IFACE" >>"$LOG_FILE" 2>&1 || true
elif command -v dhclient >/dev/null 2>&1; then
    dhclient "$IFACE" >>"$LOG_FILE" 2>&1 || true
elif command -v dhcpcd >/dev/null 2>&1; then
    dhcpcd "$IFACE" >>"$LOG_FILE" 2>&1 || true
elif command -v udhcpc >/dev/null 2>&1; then
    udhcpc -i "$IFACE" -q -n >>"$LOG_FILE" 2>&1 || \
        udhcpc -i "$IFACE" >>"$LOG_FILE" 2>&1 || true
else
    log "no supported DHCP/network manager command was found"
fi

if command -v ip >/dev/null 2>&1; then
    ip -br addr >>"$LOG_FILE" 2>&1 || true
    ip route >>"$LOG_FILE" 2>&1 || true
fi
mkdir -p /var/lib/vm2q-forge 2>/dev/null || true
printf 'distribution=%s\ngeneration=%s\ninterface=%s\n' \
    "@DISTRO@" "@GENERATION@" "$IFACE" \
    >/var/lib/vm2q-forge/linux-virtio-ready 2>/dev/null || true
log "Linux VirtIO preparation finished"
exit 0
"""
    content = (
        template
        .replace("@DISTRO@", distribution)
        .replace("@GENERATION@", generation)
        .replace("@INITRAMFS@", initramfs_hints)
        .replace("@NETWORK@", network_hints)
        .replace("@DHCP@", dhcp_hints)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_windows_network_script(path: Path) -> None:
    path.write_text(
        """@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem Generated by vmware2qcow2 for a VirtIO-net first boot.
rem The script waits for NetKVM to appear, then enables DHCP.
set "LOG_DIR=%ProgramData%\\VM2Q-Forge"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
set "LOG_FILE=%LOG_DIR%\\virtio-net-firstboot.log"
echo [%date% %time%] starting VirtIO-net first boot configuration > "%LOG_FILE%"
set "IFACE="
set "DRIVER_RC=0"
sc query viostor >nul 2>&1 || set "DRIVER_RC=1"
sc query netkvm >nul 2>&1 || set "DRIVER_RC=1"
if "%DRIVER_RC%"=="0" (
    echo [%date% %time%] viostor and NetKVM services are present >> "%LOG_FILE%"
) else (
    echo [%date% %time%] viostor or NetKVM service was not found >> "%LOG_FILE%"
)

for /l %%T in (1,1,60) do (
    if not defined IFACE (
        for /f "tokens=2 delims==" %%A in ('wmic path Win32_NetworkAdapter where "PhysicalAdapter=TRUE" get NetConnectionID /value 2^>nul ^| find "="') do (
            if not defined IFACE set "IFACE=%%A"
        )
    )
    if defined IFACE goto :configure
    timeout /t 2 /nobreak >nul
)

echo [%date% %time%] no enabled physical adapter was found >> "%LOG_FILE%"
exit /b 0

:configure
echo [%date% %time%] configuring interface "!IFACE!" >> "%LOG_FILE%"
netsh interface set interface name="!IFACE!" admin=enabled >> "%LOG_FILE%" 2>&1
netsh interface ipv4 set address name="!IFACE!" source=dhcp >> "%LOG_FILE%" 2>&1
netsh interface ipv4 set dnsserver name="!IFACE!" source=dhcp >> "%LOG_FILE%" 2>&1
ipconfig /renew "!IFACE!" >> "%LOG_FILE%" 2>&1
echo [%date% %time%] VirtIO-net DHCP configuration finished >> "%LOG_FILE%"
exit /b 0
""",
        encoding="utf-8",
    )


def _network_plan(
    options: ConversionOptions,
    report_dir: Path,
    profile: GuestProfile,
    storage_backend: str | None = None,
    linux_dependency_plan: LinuxDependencyPlan | None = None,
) -> NetworkPlan:
    direct_virtio_requested = options.require_virtio or (
        options.network_model == "virtio-net-pci"
        and storage_backend == "winpe-dism"
    )
    if options.network_model == "auto":
        # Windows 7 already contains the e1000 driver. Use it as the
        # cross-platform DHCP guarantee unless strict VirtIO injection was
        # explicitly requested.
        selected_model = (
            "virtio-net-pci"
            if direct_virtio_requested and profile.family == "windows"
            else "e1000"
            if profile.family == "windows"
            else "virtio-net-pci"
        )
    else:
        selected_model = options.network_model
    if (
        direct_virtio_requested
        and profile.family == "windows"
        and selected_model != "virtio-net-pci"
    ):
        raise ConversionError(
            "Offline VirtIO injection requires --network-model virtio-net-pci "
            "or the default auto selection"
        )
    driver_required = profile.family == "windows" and selected_model == "virtio-net-pci"
    if profile.family == "windows":
        if not driver_required:
            first_boot_model = selected_model
            config_script = None
            status = "built-in-e1000-dhcp"
            virtio_ready = False
            dhcp_ready = True
        elif direct_virtio_requested:
            first_boot_model = selected_model
            config_script = report_dir / "configure-windows-virtio-net.cmd"
            _write_windows_network_script(config_script)
            status = "virtio-driver-injected-firstboot-dhcp"
            virtio_ready = True
            dhcp_ready = True
        else:
            first_boot_model = "e1000" if driver_required else selected_model
            status = (
                "use-e1000-first-then-switch-to-virtio"
                if driver_required
                else "direct-network-model"
            )
            config_script = None
            virtio_ready = False
            dhcp_ready = first_boot_model == "e1000"
    elif profile.family == "linux" and selected_model == "virtio-net-pci":
        first_boot_model = selected_model
        config_script = report_dir / "configure-linux-virtio-net.sh"
        _write_linux_network_script(config_script, linux_dependency_plan)
        status = "kernel-driver-plus-guest-network-script"
        virtio_ready = True
        dhcp_ready = False
    else:
        first_boot_model = selected_model
        config_script = None
        status = "direct-network-model"
        virtio_ready = False
        dhcp_ready = selected_model == "e1000"

    qemu_arguments = (
        "-device",
        f"{selected_model},netdev=net0",
        "-netdev",
        "user,id=net0",
    )
    return NetworkPlan(
        requested_model=options.network_model,
        selected_model=selected_model,
        first_boot_model=first_boot_model,
        driver_required=driver_required,
        config_script=config_script,
        status=status,
        qemu_arguments=qemu_arguments,
        virtio_ready=virtio_ready,
        dhcp_ready=dhcp_ready,
    )


def _virt_v2v_available(runtime_info: RuntimeInfo) -> bool:
    return runtime_info.virt_v2v is not None


def _resolve_storage_backend(
    options: ConversionOptions,
    profile: GuestProfile,
    driver_plan: DriverPlan,
    runtime_info: RuntimeInfo,
    winpe_source: Path | None = None,
) -> str:
    if winpe_source is None and profile.family == "windows":
        winpe_source, _ = _resolve_winpe_source(options, profile)

    def require_winpe() -> str:
        if profile.family != "windows":
            raise ConversionError(
                "--windows-backend winpe-dism is only supported for Windows guests"
            )
        if options.driver_mode == "none" or driver_plan.iso is None:
            raise ConversionError(
                "winpe-dism needs the matching virtio-win ISO and driver plan"
            )
        if not driver_plan.iso.is_file():
            raise ConversionError(
                "winpe-dism needs a virtio-win ISO file, not an extracted directory"
            )
        if winpe_source is None:
            raise ConversionError(
                "winpe-dism needs a prepared WinPE ISO. Pass --winpe-iso "
                "or place winpe-amd64.iso in the portable asset directory."
            )
        if runtime_info.qemu_system is None:
            raise ConversionError(
                f"qemu system emulator was not found for "
                f"{runtime_info.host_os}/{runtime_info.host_architecture}: "
                f"{options.qemu_system}"
            )
        return "winpe-dism"

    if profile.family == "windows" and options.require_qga:
        if options.windows_backend in ("qemu-img", "virt-v2v"):
            raise ConversionError(
                "QEMU Guest Agent is mandatory for Windows conversion; "
                "use --windows-backend winpe-dism or auto with a prepared "
                "WinPE ISO so vioserial, the QGA MSI, and the first-boot "
                "installer can be staged"
            )
        return require_winpe()

    if options.require_virtio:
        if profile.family != "windows":
            raise ConversionError(
                "--require-virtio is currently supported for Windows guests only"
            )
        if options.driver_mode == "none" or driver_plan.iso is None:
            raise ConversionError(
                "--require-virtio needs the matching virtio-win ISO and driver plan"
            )
        if options.windows_backend == "qemu-img":
            raise ConversionError(
                "--require-virtio requires --windows-backend virt-v2v, "
                "winpe-dism, or auto; qemu-img cannot inject drivers into a "
                "Windows disk"
            )
        if options.windows_backend == "winpe-dism":
            return require_winpe()
        if _virt_v2v_available(runtime_info):
            return "virt-v2v"
        if options.windows_backend == "virt-v2v":
            raise ConversionError(
                f"--require-virtio requires virt-v2v for "
                f"{runtime_info.host_os}/{runtime_info.host_architecture}: "
                f"{options.virt_v2v}"
            )
        return require_winpe()

    if profile.family == "linux":
        requested = options.linux_backend
    elif profile.family == "windows":
        requested = options.windows_backend
    else:
        return "qemu-img"

    if requested == "qemu-img":
        return "qemu-img"
    if requested == "virt-v2v":
        if not _virt_v2v_available(runtime_info):
            raise ConversionError(
                f"virt-v2v executable not found for "
                f"{runtime_info.host_os}/{runtime_info.host_architecture}: "
                f"{options.virt_v2v}"
            )
        return "virt-v2v"
    if requested == "winpe-dism":
        return require_winpe()

    if profile.family == "windows" and driver_plan.iso is None:
        return "qemu-img"
    if _virt_v2v_available(runtime_info):
        return "virt-v2v"
    if profile.family == "windows" and winpe_source is not None:
        return require_winpe()
    return "qemu-img"


def _virt_v2v_convert(
    options: ConversionOptions,
    selection: SourceSelection,
    output_path: Path,
    qemu_img: str,
    profile: GuestProfile,
    driver_plan: DriverPlan,
    network_plan: NetworkPlan,
    runtime_info: RuntimeInfo,
    commands: list[CommandRecord],
) -> None:
    staging = Path(
        tempfile.mkdtemp(prefix="vmware2qcow2-v2v-", dir=str(output_path.parent))
    )
    try:
        if selection.vmx:
            input_args = ["-i", "vmx", str(selection.vmx)]
        else:
            input_args = ["-i", "disk", str(selection.vmdk_descriptor)]
        if runtime_info.virt_v2v is None:
            raise ConversionError(
                f"virt-v2v executable not found for "
                f"{runtime_info.host_os}/{runtime_info.host_architecture}"
            )
        command = [
            runtime_info.virt_v2v,
            *input_args,
            "-o",
            "local",
            "-os",
            str(staging),
            "-of",
            "qcow2",
        ]
        if profile.family == "windows":
            command.extend(["--block-driver", "virtio-blk"])
        if network_plan.config_script:
            command.extend(["--firstboot", str(network_plan.config_script)])
        env = None
        if profile.family == "windows" and driver_plan.iso:
            env = os.environ.copy()
            env["VIRTIO_WIN"] = str(driver_plan.iso)
        _run(command, commands, options.dry_run, env=env)
        if options.dry_run:
            return

        qcow2_candidates: list[Path] = []
        for candidate in sorted(staging.rglob("*")):
            if not candidate.is_file():
                continue
            try:
                info = _json_command(
                    [qemu_img, "info", "--output=json", str(candidate)],
                    commands,
                    dry_run=False,
                )
            except ConversionError:
                continue
            if info.get("format") == "qcow2":
                qcow2_candidates.append(candidate)

        if len(qcow2_candidates) != 1:
            names = ", ".join(str(path) for path in qcow2_candidates)
            raise ConversionError(
                "virt-v2v did not produce exactly one qcow2 disk; "
                f"candidates: {names or 'none'}"
            )
        shutil.move(str(qcow2_candidates[0]), str(output_path))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _winpe_dism_convert(
    options: ConversionOptions,
    selection: SourceSelection,
    output_path: Path,
    qemu_img: str,
    profile: GuestProfile,
    driver_plan: DriverPlan,
    guest_agent_plan: GuestAgentPlan,
    network_plan: NetworkPlan,
    runtime_info: RuntimeInfo,
    winpe_source: Path,
    commands: list[CommandRecord],
) -> tuple[
    Path | None,
    dict[str, str] | None,
    Path | None,
    dict[str, str] | None,
]:
    """Convert, service in WinPE, and optionally bake QGA before publishing."""

    if runtime_info.qemu_system is None:
        raise ConversionError(
            f"qemu system emulator not found for "
            f"{runtime_info.host_os}/{runtime_info.host_architecture}: "
            f"{options.qemu_system}"
        )
    if driver_plan.iso is None or not driver_plan.iso.is_file():
        raise ConversionError(
            "winpe-dism needs a local virtio-win ISO file"
        )

    staging = Path(
        tempfile.mkdtemp(prefix="vmware2qcow2-winpe-", dir=str(output_path.parent))
    )
    target_disk = staging / "serviced.qcow2"
    result_disk = staging / "result.img"
    network_script = (
        network_plan.config_script.read_text(encoding="utf-8")
        if network_plan.config_script and network_plan.config_script.exists()
        else None
    )
    qga_script = (
        guest_agent_plan.install_script.read_text(encoding="utf-8")
        if guest_agent_plan.install_script
        and guest_agent_plan.install_script.exists()
        else None
    )
    qga_bake = (
        guest_agent_plan.required
        and guest_agent_plan.install_mode == "bake"
    )
    bake_firmware = (
        options.firmware
        if options.firmware != "auto"
        else (
            selection.vmx_metadata.firmware
            if selection.vmx_metadata
            else "bios"
        )
    )
    bake_storage_controller = _windows_bake_storage_controller(selection)
    qga_autologon_reg: str | None = None
    if qga_bake and not options.dry_run:
        if (
            options.windows_bake_username is None
            or options.windows_bake_password is None
        ):
            raise ConversionError(
                "QEMU Guest Agent image bake requires Windows administrator "
                "credentials; pass --windows-bake-username and provide the "
                "password through --windows-bake-password or "
                "VM2Q_WINDOWS_BAKE_PASSWORD"
            )
        qga_autologon_reg = _windows_bake_registry(
            options.windows_bake_username,
            options.windows_bake_password,
            options.windows_bake_domain,
        )
    convert_command = [
        qemu_img,
        "convert",
        "-p",
        "-f",
        "vmdk",
        "-O",
        "qcow2",
        "-o",
        "compat=1.1,lazy_refcounts=on",
        str(selection.vmdk_descriptor),
        str(target_disk),
    ]
    try:
        _run(convert_command, commands, options.dry_run)
        if not options.dry_run:
            create_result_disk(
                result_disk,
                network_script=network_script,
                qga_script=qga_script,
                qga_autologon_reg=qga_autologon_reg,
            )
        execution = run_winpe(
            runtime_info.qemu_system,
            winpe_source,
            driver_plan.iso,
            target_disk,
            result_disk,
            staging,
            timeout=options.winpe_timeout,
            dry_run=options.dry_run,
        )
        commands.append(
            CommandRecord(
                execution.argv,
                execution.returncode,
                execution.stdout,
                execution.stderr,
            )
        )
        if options.dry_run:
            if qga_bake:
                bake_execution = run_windows_bake(
                    runtime_info.qemu_system,
                    target_disk,
                    result_disk,
                staging,
                timeout=options.winpe_timeout,
                firmware=bake_firmware,
                storage_controller=bake_storage_controller,
                dry_run=True,
            )
                commands.append(
                    CommandRecord(
                        bake_execution.argv,
                        bake_execution.returncode,
                        bake_execution.stdout,
                        bake_execution.stderr,
                    )
                )
                return None, None, None, {"status": "dry-run"}
            return None, None, None, None
        if execution.returncode != 0:
            detail = execution.stdout.strip()[-2000:]
            raise ConversionError(
                "WinPE helper exited with status "
                f"{execution.returncode}: {detail}"
            )
        result = execution.result
        if result is None:
            raise ConversionError(
                "WinPE helper exited without VM2QRES.TXT; "
                "the prepared ISO must run the VM2Q Forge startnet.cmd"
            )
        log_path = output_path.with_name(
            output_path.stem + "-winpe-dism.log"
        )
        log_path.write_text(
            result.log or result.raw,
            encoding="utf-8",
        )
        if not result.succeeded:
            detail = result.log.strip()[-2000:] or result.raw.strip()
            raise ConversionError(
                f"DISM offline injection failed with status "
                f"{result.status or 'unknown'}: {detail}"
            )
        bake_log_path: Path | None = None
        bake_result_fields: dict[str, str] | None = None
        if qga_bake:
            # The WinPE helper leaves its own VM2QRES.TXT/VM2QLOG.TXT on the
            # result disk.  Recreate the disk before the private Windows boot
            # so QGA verification cannot accidentally read the stale DISM
            # marker when Windows exits before writing its bake result.
            create_result_disk(result_disk)
            bake_execution = run_windows_bake(
                runtime_info.qemu_system,
                target_disk,
                result_disk,
                staging,
                timeout=options.winpe_timeout,
                firmware=bake_firmware,
                storage_controller=bake_storage_controller,
            )
            commands.append(
                CommandRecord(
                    bake_execution.argv,
                    bake_execution.returncode,
                    bake_execution.stdout,
                    bake_execution.stderr,
                )
            )
            bake_result = bake_execution.result
            if bake_result is None:
                raise ConversionError(
                    "Windows QGA bake boot exited without a result marker"
                )
            bake_log_path = output_path.with_name(
                output_path.stem + "-qga-bake.log"
            )
            bake_log_path.write_text(
                bake_result.log or bake_result.raw,
                encoding="utf-8",
            )
            bake_result_fields = bake_result.fields
            if (
                bake_execution.returncode != 0
                or bake_result.status.lower() != "installed"
                or bake_result.fields.get("qga_installed") != "1"
                or bake_result.fields.get("qga_service_start") != "auto"
            ):
                detail = bake_result.log.strip()[-2000:] or bake_result.raw.strip()
                raise ConversionError(
                    "QEMU Guest Agent was not installed in the image: "
                    f"{detail}"
                )
        shutil.move(str(target_disk), str(output_path))
        return log_path, result.fields, bake_log_path, bake_result_fields
    except WinPeError as exc:
        raise ConversionError(str(exc)) from exc
    finally:
        if not options.keep_workdir:
            shutil.rmtree(staging, ignore_errors=True)


def _write_rollback(path: Path, output_paths: Iterable[Path]) -> None:
    lines = [
        "#!/bin/sh",
        "set -eu",
        "# Generated by vmware2qcow2. Review paths before execution.",
    ]
    for output in output_paths:
        lines.append(f"rm -f -- {shlex.quote(str(output))}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def _write_import_doc(
    path: Path,
    selection: SourceSelection,
    output: Path,
    firmware: str,
    profile: GuestProfile,
    driver_plan: DriverPlan,
    linux_dependency_plan: LinuxDependencyPlan,
    guest_agent_plan: GuestAgentPlan,
    network_plan: NetworkPlan,
    storage_backend: str,
    runtime_info: RuntimeInfo,
) -> None:
    source_interface = (
        selection.vmx_metadata.disk_interface
        if selection.vmx_metadata
        else "unknown"
    )
    direct_virtio_storage = network_plan.virtio_ready or (
        profile.family == "linux"
        and network_plan.selected_model == "virtio-net-pci"
    )
    storage_interface = "virtio" if direct_virtio_storage else "ide"
    boot_network_model = (
        network_plan.selected_model
        if network_plan.virtio_ready
        else network_plan.first_boot_model
    )
    if profile.family == "windows" and network_plan.virtio_ready:
        first_boot = (
            "Windows：VirtIO Block 与 VirtIO-net 驱动已由 virt-v2v 注入转换流程；"
            "首次启动时等待 Windows 自动完成驱动注册和可能的多次重启，"
            "生成的 DHCP 脚本会在 NetKVM 出现后配置网卡。"
        )
    elif profile.family == "windows":
        if network_plan.selected_model == "e1000":
            first_boot = (
                "Windows 7：使用 IDE/源 SCSI 兼容磁盘控制器和内置 E1000 网卡，"
                "目标环境提供 DHCP 后即可直接联网；VirtIO 驱动安装属于可选优化。"
            )
        else:
            first_boot = (
                "Windows：保持 IDE/源 SCSI 兼容磁盘控制器和 E1000 网卡，"
                "确认驱动进入 Driver Store 后再切换到 VirtIO Block 与 VirtIO-net。"
            )
    else:
        if linux_dependency_plan.generation in {"legacy", "unknown"}:
            first_boot = (
                "Linux 老版本或版本未知：先使用兼容磁盘控制器启动，运行生成的 "
                "configure-linux-virtio-net.sh，确认 virtio_pci、virtio_blk、"
                "virtio_net 已进入 initramfs，再切换到 VirtIO Block 与 VirtIO-net。"
            )
        else:
            first_boot = (
                "Linux：使用 VirtIO Block 与 VirtIO-net；如发行版没有自动配置 DHCP，"
                "运行生成的 configure-linux-virtio-net.sh。"
            )
    if not guest_agent_plan.required:
        qga_text = "QEMU Guest Agent 未纳入本次转换。"
    elif guest_agent_plan.installed:
        qga_text = (
            "QEMU Guest Agent 已在转换阶段通过一次性 Windows 引导安装到镜像；"
            "服务已设置为自动启动；目标平台仍要启用 "
            "virtio-serial/QEMU Guest Agent 通道。"
        )
    elif guest_agent_plan.install_mode == "bake":
        qga_text = (
            "QEMU Guest Agent 将在发布镜像前通过一次性 Windows 引导安装；"
            "目标平台仍要启用 virtio-serial/QEMU Guest Agent 通道。"
        )
    else:
        qga_text = (
            "Windows 首次登录时通过 RunOnce 静默安装 QEMU Guest Agent；"
            "目标平台还要启用 virtio-serial/QEMU Guest Agent 通道。"
        )
    path.write_text(
        "\n".join(
            [
                "# qcow2 导入说明",
                "",
                f"- 镜像：`{output.name}`",
                f"- 客体系统：`{profile.family}`",
                f"- 客体架构：`{profile.architecture}`；本工具不改变客体 CPU 架构。",
                f"- 识别依据：`{profile.detection_reason}`",
                f"- 源固件：`{selection.vmx_metadata.firmware if selection.vmx_metadata else 'unknown'}`",
                f"- 请求固件：`{firmware}`",
                f"- 源磁盘接口：`{source_interface}`",
                f"- 存储转换后端：`{storage_backend}`",
                f"- 运行平台：`{runtime_info.host_os}/{runtime_info.host_architecture}`",
                f"- qemu-img 来源：`{runtime_info.qemu_img_source}`",
                f"- VirtIO 驱动版本：`{driver_plan.version or '不适用'}`",
                f"- 驱动计划：`{driver_plan.status}`",
                f"- QEMU Guest Agent：`{'required' if guest_agent_plan.required else 'not-required'}`",
                f"- QGA 状态：`{guest_agent_plan.status}`",
                f"- QGA MSI：`{guest_agent_plan.msi_relative_path or '无'}`",
                f"- QGA 安装脚本：`{guest_agent_plan.install_script or '无'}`",
                f"- 网卡模型：首次启动=`{network_plan.first_boot_model}`，目标=`{network_plan.selected_model}`",
                f"- 网卡配置：`{network_plan.status}`",
                f"- VirtIO 就绪：`{str(network_plan.virtio_ready).lower()}`",
                f"- DHCP 就绪：`{str(network_plan.dhcp_ready).lower()}`",
                f"- Linux 发行版画像：`{linux_dependency_plan.distribution}`",
                f"- Linux 兼容代际：`{linux_dependency_plan.generation}`",
                f"- Linux 必需内核模块：`{', '.join(linux_dependency_plan.required_modules) or '无'}`",
                f"- initramfs 工具候选：`{', '.join(linux_dependency_plan.initramfs_tools) or '无'}`",
                f"- 网络栈候选：`{', '.join(linux_dependency_plan.network_stacks) or '无'}`",
                f"- DHCP 客户端候选：`{', '.join(linux_dependency_plan.dhcp_clients) or '无'}`",
                f"- Linux 依赖脚本：`{linux_dependency_plan.script or '无'}`",
                "",
                "## 首次启动",
                "",
                first_boot,
                "",
                "## Linux 依赖画像",
                "",
                f"- 发行版：`{linux_dependency_plan.distribution}`",
                f"- 兼容代际：`{linux_dependency_plan.generation}`",
                f"- 必需内核模块：`{', '.join(linux_dependency_plan.required_modules) or '无'}`",
                f"- initramfs 工具：`{', '.join(linux_dependency_plan.initramfs_tools) or '无'}`",
                f"- 网络栈：`{', '.join(linux_dependency_plan.network_stacks) or '无'}`",
                f"- DHCP 客户端：`{', '.join(linux_dependency_plan.dhcp_clients) or '无'}`",
                f"- 建议包：`{'; '.join(linux_dependency_plan.package_hints) or '无'}`",
                "qemu-img-only 模式不会在宿主机上安装客体软件包；老版本或版本未知的客体应先运行依赖脚本并检查 initramfs。",
                "",
                "## QEMU Guest Agent",
                "",
                qga_text,
                "",
                "Proxmox VE：启用 QEMU Guest Agent，并为虚拟机提供 virtio-serial 通道。",
                "",
                "## 示例",
                "",
                "```bash",
                f"qemu-system-x86_64 -machine q35 -m 4096 -smp 2 "
                f"-drive file='{output}',format=qcow2,if={storage_interface} "
                f"-device {boot_network_model},netdev=net0 "
                "-netdev user,id=net0",
                "```",
                "",
                "目标网卡参数：",
                "",
                f"`{' '.join(network_plan.qemu_arguments)}`",
                "",
                "驱动/配置脚本：",
                "",
                f"`{driver_plan.generated_script or linux_dependency_plan.script or network_plan.config_script or '无'}`",
                "",
            ]
        ),
        encoding="utf-8",
    )


def convert(options: ConversionOptions, progress: Progress | None = None) -> ConversionResult:
    progress = progress or (lambda _message: None)
    input_path = options.input_path.expanduser().resolve()
    output_path = options.output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists() and not options.overwrite:
        raise ConversionError(f"Output already exists: {output_path}")

    report_path = (
        options.report_path.expanduser().resolve()
        if options.report_path
        else output_path.with_name(output_path.stem + "-conversion-report.json")
    )
    import_doc_path = (
        options.import_doc_path.expanduser().resolve()
        if options.import_doc_path
        else output_path.with_name(output_path.stem + "-import.md")
    )
    rollback_path = (
        options.rollback_path.expanduser().resolve()
        if options.rollback_path
        else output_path.with_name(output_path.stem + "-rollback.sh")
    )
    sha256_path = output_path.with_name(output_path.name + ".sha256")

    workdir = options.workdir.expanduser().resolve() if options.workdir else None
    commands: list[CommandRecord] = []
    selection, cleanup_root = _prepare_source(input_path, workdir)
    _log(progress, f"Selected VMDK: {selection.vmdk_descriptor}")
    profile = _guest_profile(selection, options.guest_os)
    _log(
        progress,
        f"Guest detected: {profile.family}/{profile.architecture} "
        f"({profile.detected_guest_os or 'unknown'})",
    )

    runtime_info = resolve_runtime(
        options.qemu_img,
        options.virt_v2v,
        options.runtime_dir,
        options.dry_run,
        options.qemu_system,
    )
    configure_runtime_environment(runtime_info)
    if runtime_info.qemu_img is None:
        raise ConversionError(
            f"qemu-img executable not found for "
            f"{runtime_info.host_os}/{runtime_info.host_architecture}: "
            f"{options.qemu_img}. Set --runtime-dir or VM2Q_RUNTIME_DIR."
        )
    qemu_img = runtime_info.qemu_img
    _log(
        progress,
        f"Runtime: {runtime_info.host_os}/{runtime_info.host_architecture}; "
        f"qemu-img={runtime_info.qemu_img_source}",
    )
    source_info = _json_command(
        [qemu_img, "info", "--output=json", str(selection.vmdk_descriptor)],
        commands,
        options.dry_run,
    )
    _log(progress, "Source metadata collected")

    winpe_source, winpe_arch = (
        _resolve_winpe_source(options, profile)
        if profile.family == "windows"
        else (None, None)
    )
    driver_plan = _driver_plan(options, output_path.parent, profile)
    _log(progress, f"Driver plan: {driver_plan.status}")
    guest_agent_plan = _guest_agent_plan(
        options,
        output_path.parent,
        profile,
        driver_plan,
    )
    _log(progress, f"QEMU Guest Agent plan: {guest_agent_plan.status}")
    linux_dependency_plan = _linux_dependency_plan(
        output_path.parent,
        profile,
    )
    _log(progress, f"Linux dependency plan: {linux_dependency_plan.status}")
    storage_backend = _resolve_storage_backend(
        options,
        profile,
        driver_plan,
        runtime_info,
        winpe_source,
    )
    _log(progress, f"Storage backend: {storage_backend}")
    network_plan = _network_plan(
        options,
        output_path.parent,
        profile,
        storage_backend=storage_backend,
        linux_dependency_plan=linux_dependency_plan,
    )
    _log(progress, f"Network plan: {network_plan.status}")
    winpe_dism_log_path: Path | None = None
    winpe_dism_result: dict[str, str] | None = None
    qga_bake_log_path: Path | None = None
    qga_bake_result: dict[str, str] | None = None

    if storage_backend == "virt-v2v":
        _virt_v2v_convert(
            options,
            selection,
            output_path,
            qemu_img,
            profile,
            driver_plan,
            network_plan,
            runtime_info,
            commands,
        )
        network_plan = replace(
            network_plan,
            status="virt-v2v-firstboot-network-script"
            if network_plan.config_script
            else network_plan.status,
            dhcp_ready=(
                True
                if profile.family == "linux" and network_plan.config_script
                else network_plan.dhcp_ready
            ),
        )
        if profile.family == "windows" and driver_plan.iso:
            driver_plan = replace(
                driver_plan,
                status="virt-v2v-driver-injection-command-completed",
            )
            if options.require_virtio:
                network_plan = replace(
                    network_plan,
                    first_boot_model=network_plan.selected_model,
                    status="virtio-driver-injected-firstboot-dhcp",
                    virtio_ready=True,
                    dhcp_ready=True,
                )
    elif storage_backend == "winpe-dism":
        if winpe_source is None:
            raise ConversionError("winpe-dism selected without a WinPE ISO")
        (
            winpe_dism_log_path,
            winpe_dism_result,
            qga_bake_log_path,
            qga_bake_result,
        ) = _winpe_dism_convert(
            options,
            selection,
            output_path,
            qemu_img,
            profile,
            driver_plan,
            guest_agent_plan,
            network_plan,
            runtime_info,
            winpe_source,
            commands,
        )
        driver_plan = replace(
            driver_plan,
            status="winpe-dism-offline-injection-completed",
        )
        if guest_agent_plan.required:
            guest_agent_plan = replace(
                guest_agent_plan,
                status=(
                    "dry-run-qga-bake"
                    if options.dry_run and guest_agent_plan.install_mode == "bake"
                    else "dry-run-qga-staging"
                    if options.dry_run
                    else "qga-installed-in-image"
                    if guest_agent_plan.install_mode == "bake"
                    else "winpe-dism-qga-staged-firstboot"
                ),
                staged=not options.dry_run,
                installed=(
                    guest_agent_plan.install_mode == "bake"
                    and not options.dry_run
                ),
            )
        network_plan = replace(
            network_plan,
            status="winpe-dism-firstboot-network-script"
            if network_plan.config_script
            else network_plan.status,
            virtio_ready=(
                network_plan.selected_model == "virtio-net-pci"
            ),
            dhcp_ready=True,
        )
    else:
        convert_command = [
            qemu_img,
            "convert",
            "-p",
            "-f",
            "vmdk",
            "-O",
            "qcow2",
            "-o",
            "compat=1.1,lazy_refcounts=on",
            str(selection.vmdk_descriptor),
            str(output_path),
        ]
        _run(convert_command, commands, options.dry_run)
    _log(progress, "qcow2 conversion finished")

    output_info = _json_command(
        [qemu_img, "info", "--output=json", str(output_path)],
        commands,
        options.dry_run,
    )
    _run([qemu_img, "check", str(output_path)], commands, options.dry_run)
    _log(progress, "qcow2 integrity check finished")

    if options.dry_run:
        digest = "dry-run"
        sha256_path.write_text(f"{digest}  {output_path}\n", encoding="utf-8")
    else:
        digest = sha256_file(output_path)
        sha256_path.write_text(f"{digest}  {output_path}\n", encoding="utf-8")

    _write_import_doc(
        import_doc_path,
        selection,
        output_path,
        options.firmware,
        profile,
        driver_plan,
        linux_dependency_plan,
        guest_agent_plan,
        network_plan,
        storage_backend,
        runtime_info,
    )
    _write_rollback(
        rollback_path,
        (
            output_path,
            report_path,
            import_doc_path,
            rollback_path,
            sha256_path,
            *(
                (guest_agent_plan.install_script,)
                if guest_agent_plan.install_script is not None
                else ()
            ),
            *(
                (winpe_dism_log_path,)
                if winpe_dism_log_path is not None
                else ()
            ),
            *(
                (qga_bake_log_path,)
                if qga_bake_log_path is not None
                else ()
            ),
        ),
    )

    report = {
        "started_at": _utc_now(),
        "input": str(input_path),
        "source_root": str(selection.root),
        "source_vmx": str(selection.vmx) if selection.vmx else None,
        "source_vmdk": str(selection.vmdk_descriptor),
        "source_firmware": (
            selection.vmx_metadata.firmware if selection.vmx_metadata else None
        ),
        "requested_firmware": options.firmware,
        "guest_profile": {
            "requested": profile.requested,
            "family": profile.family,
            "architecture": profile.architecture,
            "detected_guest_os": profile.detected_guest_os,
            "detected_detailed_data": profile.detected_detailed_data,
            "detection_reason": profile.detection_reason,
        },
        "output": str(output_path),
        "sha256": digest,
        "storage_backend": storage_backend,
        "runtime": {
            "host_os": runtime_info.host_os,
            "host_architecture": runtime_info.host_architecture,
            "runtime_dir": runtime_info.runtime_dir,
            "qemu_img": runtime_info.qemu_img,
            "qemu_img_source": runtime_info.qemu_img_source,
            "virt_v2v": runtime_info.virt_v2v,
            "virt_v2v_source": runtime_info.virt_v2v_source,
            "qemu_system": runtime_info.qemu_system,
            "qemu_system_source": runtime_info.qemu_system_source,
            "winpe_iso": str(winpe_source) if winpe_source else None,
            "winpe_architecture": winpe_arch,
            "winpe_dism_log": (
                str(winpe_dism_log_path)
                if winpe_dism_log_path
                else None
            ),
            "winpe_dism_result": winpe_dism_result,
            "qga_bake_log": (
                str(qga_bake_log_path)
                if qga_bake_log_path
                else None
            ),
            "qga_bake_result": qga_bake_result,
        },
        "driver_plan": {
            "mode": driver_plan.mode,
            "iso": str(driver_plan.iso) if driver_plan.iso else None,
            "expected_paths": list(driver_plan.expected_paths),
            "generated_script": (
                str(driver_plan.generated_script)
                if driver_plan.generated_script
                else None
            ),
            "status": driver_plan.status,
            "version": driver_plan.version,
            "selection_reason": driver_plan.selection_reason,
            "guest_os_family": driver_plan.guest_os_family,
        },
        "linux_dependency_plan": {
            "distribution": linux_dependency_plan.distribution,
            "generation": linux_dependency_plan.generation,
            "required_modules": list(linux_dependency_plan.required_modules),
            "initramfs_tools": list(linux_dependency_plan.initramfs_tools),
            "network_stacks": list(linux_dependency_plan.network_stacks),
            "dhcp_clients": list(linux_dependency_plan.dhcp_clients),
            "package_hints": list(linux_dependency_plan.package_hints),
            "script": (
                str(linux_dependency_plan.script)
                if linux_dependency_plan.script
                else None
            ),
            "status": linux_dependency_plan.status,
            "warnings": list(linux_dependency_plan.warnings),
            "verification_commands": list(
                linux_dependency_plan.verification_commands
            ),
        },
        "qemu_guest_agent": {
            "required": guest_agent_plan.required,
            "architecture": guest_agent_plan.architecture,
            "msi_relative_path": guest_agent_plan.msi_relative_path,
            "install_script": (
                str(guest_agent_plan.install_script)
                if guest_agent_plan.install_script
                else None
            ),
            "status": guest_agent_plan.status,
            "channel": guest_agent_plan.channel,
            "staged": guest_agent_plan.staged,
            "install_mode": guest_agent_plan.install_mode,
            "installed": guest_agent_plan.installed,
            "first_boot_install": (
                guest_agent_plan.required
                and guest_agent_plan.install_mode == "firstboot"
            ),
        },
        "network_plan": {
            "requested_model": network_plan.requested_model,
            "selected_model": network_plan.selected_model,
            "first_boot_model": network_plan.first_boot_model,
            "driver_required": network_plan.driver_required,
            "virtio_ready": network_plan.virtio_ready,
            "dhcp_ready": network_plan.dhcp_ready,
            "config_script": (
                str(network_plan.config_script)
                if network_plan.config_script
                else None
            ),
            "status": network_plan.status,
            "qemu_arguments": list(network_plan.qemu_arguments),
        },
        "source_info": source_info,
        "output_info": output_info,
        "commands": [asdict(record) for record in commands],
        "notes": [
            "Driver plan generation does not claim that Windows has already loaded the drivers.",
            "Windows uses the legacy virtio-win bundle for Windows 7 and the modern bundle for newer Windows guests.",
            "Linux does not use virtio-win; the selected Linux backend can inject a first-boot network script.",
            (
                "Linux dependency planning records the kernel modules, initramfs "
                "generators, network stacks, DHCP clients, and package hints needed "
                "by older or unknown guests; qemu-img mode leaves the script for "
                "manual or first-boot execution."
                if profile.family == "linux"
                else "Linux dependency planning is not applicable to this guest."
            ),
            "The qcow2 file stores disks, not a virtual NIC. Use the recorded QEMU/UTM network model when launching it.",
            (
                "Strict VirtIO mode requires virt-v2v, injects Windows VirtIO drivers, "
                "or uses WinPE/DISM, and adds a first-boot DHCP script."
                if options.require_virtio
                else (
                    "Windows auto network mode uses the built-in e1000 driver; "
                    "qemu-img-only conversion is available only when QGA is "
                    "explicitly disabled."
                ),
            ),
            (
                "Windows conversion installs QEMU Guest Agent in-image before "
                "publish by default; the target VM must expose a virtio-serial "
                "guest-agent channel."
                if guest_agent_plan.required
                and guest_agent_plan.installed
                else (
                    "Windows conversion stages QEMU Guest Agent for first logon; "
                    "the target VM must expose a virtio-serial guest-agent channel."
                    if guest_agent_plan.required
                    else "QEMU Guest Agent staging was explicitly disabled."
                )
            ),
        ],
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    if cleanup_root and not options.keep_workdir:
        shutil.rmtree(cleanup_root, ignore_errors=True)

    return ConversionResult(
        selection=selection,
        output_path=output_path,
        report_path=report_path,
        import_doc_path=import_doc_path,
        rollback_path=rollback_path,
        sha256_path=sha256_path,
        guest_profile=profile,
        driver_plan=driver_plan,
        linux_dependency_plan=linux_dependency_plan,
        guest_agent_plan=guest_agent_plan,
        network_plan=network_plan,
        storage_backend=storage_backend,
        runtime_info=runtime_info,
        commands=tuple(commands),
        source_info=source_info,
        output_info=output_info,
    )
