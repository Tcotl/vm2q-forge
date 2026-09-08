from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


Firmware = Literal["auto", "bios", "uefi"]
DriverMode = Literal["none", "plan"]
GuestOS = Literal["auto", "windows", "linux"]
GuestFamily = Literal["windows", "linux", "unknown"]
Architecture = Literal["amd64", "x86", "arm64", "unknown"]
WindowsBackend = Literal["auto", "qemu-img", "virt-v2v", "winpe-dism"]
LinuxBackend = Literal["auto", "qemu-img", "virt-v2v"]
NetworkModel = Literal["auto", "e1000", "virtio-net-pci"]
VirtioWinVersion = Literal["auto", "0.1.173-9", "0.1.285-1"]
WinPeArchitecture = Literal["auto", "amd64", "arm64"]
QgaInstallMode = Literal["bake", "firstboot"]
LinuxGeneration = Literal["legacy", "intermediate", "modern", "unknown"]


@dataclass(frozen=True)
class VmxMetadata:
    path: Path
    firmware: Literal["bios", "uefi"]
    disk_interface: str | None
    disk_filename: str | None
    hardware_version: str | None
    guest_os: str | None = None
    guest_os_detailed: str | None = None
    guest_os_family: GuestFamily = "unknown"
    architecture: Architecture = "unknown"
    values: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceSelection:
    root: Path
    vmx: Path | None
    vmdk_descriptor: Path
    vmx_metadata: VmxMetadata | None
    extracted_from_zip: bool


@dataclass(frozen=True)
class GuestProfile:
    requested: GuestOS
    family: GuestFamily
    architecture: Architecture
    detected_guest_os: str | None
    detected_detailed_data: str | None
    detection_reason: str


@dataclass(frozen=True)
class DriverPlan:
    mode: DriverMode
    iso: Path | None
    expected_paths: tuple[str, ...]
    generated_script: Path | None
    status: str
    version: str | None = None
    selection_reason: str = ""
    guest_os_family: GuestFamily = "unknown"


@dataclass(frozen=True)
class NetworkPlan:
    requested_model: NetworkModel
    selected_model: str
    first_boot_model: str
    driver_required: bool
    config_script: Path | None
    status: str
    qemu_arguments: tuple[str, ...]
    virtio_ready: bool = False
    dhcp_ready: bool = False


@dataclass(frozen=True)
class LinuxDependencyPlan:
    """Guest-side prerequisites for booting Linux with QEMU VirtIO devices."""

    distribution: str
    generation: LinuxGeneration
    required_modules: tuple[str, ...]
    initramfs_tools: tuple[str, ...]
    network_stacks: tuple[str, ...]
    dhcp_clients: tuple[str, ...]
    package_hints: tuple[str, ...]
    script: Path | None
    status: str
    warnings: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class GuestAgentPlan:
    required: bool
    architecture: str | None
    msi_relative_path: str | None
    install_script: Path | None
    status: str
    channel: str = "virtio-serial"
    staged: bool = False
    install_mode: QgaInstallMode = "bake"
    installed: bool = False


@dataclass(frozen=True)
class RuntimeInfo:
    host_os: str
    host_architecture: str
    runtime_dir: str | None
    qemu_img: str | None
    qemu_img_source: str | None
    virt_v2v: str | None
    virt_v2v_source: str | None
    qemu_system: str | None = None
    qemu_system_source: str | None = None


@dataclass(frozen=True)
class ConversionOptions:
    input_path: Path
    output_path: Path
    qemu_img: str = "qemu-img"
    firmware: Firmware = "auto"
    driver_iso: Path | None = None
    driver_mode: DriverMode = "plan"
    overwrite: bool = False
    keep_workdir: bool = False
    workdir: Path | None = None
    report_path: Path | None = None
    import_doc_path: Path | None = None
    rollback_path: Path | None = None
    dry_run: bool = False
    runtime_dir: Path | None = None
    guest_os: GuestOS = "auto"
    windows_backend: WindowsBackend = "auto"
    linux_backend: LinuxBackend = "auto"
    network_model: NetworkModel = "auto"
    virtio_win_old: Path | None = None
    virtio_win_new: Path | None = None
    virtio_win_version: VirtioWinVersion = "auto"
    virt_v2v: str = "virt-v2v"
    require_virtio: bool = False
    require_qga: bool = True
    winpe_iso: Path | None = None
    winpe_arch: WinPeArchitecture = "auto"
    qemu_system: str = "qemu-system-x86_64"
    winpe_timeout: int = 900
    qga_install_mode: QgaInstallMode = "bake"
    windows_bake_username: str | None = None
    windows_bake_password: str | None = None
    windows_bake_domain: str | None = None


@dataclass(frozen=True)
class CommandRecord:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    environment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversionResult:
    selection: SourceSelection
    output_path: Path
    report_path: Path
    import_doc_path: Path
    rollback_path: Path
    sha256_path: Path
    guest_profile: GuestProfile
    driver_plan: DriverPlan
    linux_dependency_plan: LinuxDependencyPlan
    guest_agent_plan: GuestAgentPlan
    network_plan: NetworkPlan
    storage_backend: str
    runtime_info: RuntimeInfo
    commands: tuple[CommandRecord, ...]
    source_info: dict[str, Any]
    output_info: dict[str, Any]
