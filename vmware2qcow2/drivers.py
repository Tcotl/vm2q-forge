from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .model import VirtioWinVersion
from .resources import application_resource_roots


@dataclass(frozen=True)
class VirtioWinBundle:
    """Metadata for a supported virtio-win bundle.

    The ISO itself is intentionally supplied as an asset rather than embedded
    in the Python package.  This keeps the application small while the version
    selection and expected driver layout remain built in.
    """

    version: str
    filename: str
    label: str
    driver_roots: tuple[str, ...]

    @property
    def expected_paths(self) -> tuple[str, ...]:
        return tuple(
            f"{component}/{root}/amd64"
            for root in self.driver_roots
            for component in ("viostor", "NetKVM")
        )


VIRTIO_WIN_BUNDLES: dict[str, VirtioWinBundle] = {
    "0.1.173-9": VirtioWinBundle(
        version="0.1.173-9",
        filename="virtio-win-0.1.173-9.iso",
        label="Windows 7/legacy",
        driver_roots=("w7",),
    ),
    "0.1.285-1": VirtioWinBundle(
        version="0.1.285-1",
        filename="virtio-win-0.1.285-1.iso",
        label="Windows 8/10/11/modern",
        driver_roots=("w11", "w10", "w8"),
    ),
}


def get_bundle(version: VirtioWinVersion | str) -> VirtioWinBundle:
    if version == "auto":
        raise ValueError("auto is not a concrete virtio-win bundle version")
    try:
        return VIRTIO_WIN_BUNDLES[version]
    except KeyError as exc:
        raise ValueError(f"Unsupported virtio-win bundle: {version}") from exc


def bundled_iso_candidates(version: VirtioWinVersion | str) -> tuple[Path, ...]:
    """Return conventional package-local and user-cache asset locations."""

    bundle = get_bundle(version)
    package_dir = Path(__file__).resolve().parent
    configured_dir = os.environ.get("VM2Q_DRIVER_DIR")
    configured_candidates = (
        (Path(configured_dir).expanduser() / bundle.filename,)
        if configured_dir
        else ()
    )
    frozen_candidates = tuple(
        candidate
        for root in application_resource_roots()
        for candidate in (
            root / "assets" / "drivers" / bundle.filename,
            root / "vmware2qcow2" / "drivers" / bundle.filename,
        )
    )
    candidates = (
        *configured_candidates,
        *frozen_candidates,
        package_dir / "drivers" / bundle.filename,
        package_dir.parent / "drivers" / bundle.filename,
        Path.home() / ".cache" / "vmware2qcow2" / bundle.filename,
    )
    return tuple(dict.fromkeys(candidates))


def bundled_winpe_candidates() -> tuple[Path, ...]:
    """Return conventional locations for a prepared amd64 WinPE ISO."""

    package_dir = Path(__file__).resolve().parent
    configured_dir = os.environ.get("VM2Q_WINPE_DIR")
    configured_candidates = (
        (
            Path(configured_dir).expanduser() / "winpe-amd64.iso",
            Path(configured_dir).expanduser() / "VM2Q-Forge-WinPE-amd64.iso",
            Path(configured_dir).expanduser() / "winpe-arm64.iso",
            Path(configured_dir).expanduser() / "VM2Q-Forge-WinPE-arm64.iso",
        )
        if configured_dir
        else ()
    )
    frozen_candidates = tuple(
        candidate
        for root in application_resource_roots()
        for candidate in (
            root / "assets" / "winpe" / "winpe-amd64.iso",
            root / "assets" / "winpe" / "VM2Q-Forge-WinPE-amd64.iso",
            root / "assets" / "winpe" / "winpe-arm64.iso",
            root / "assets" / "winpe" / "VM2Q-Forge-WinPE-arm64.iso",
            root / "vmware2qcow2" / "drivers" / "winpe-amd64.iso",
            root / "vmware2qcow2" / "drivers" / "winpe-arm64.iso",
        )
    )
    candidates = (
        *configured_candidates,
        *frozen_candidates,
        package_dir / "drivers" / "winpe-amd64.iso",
        package_dir / "drivers" / "winpe-arm64.iso",
        package_dir.parent / "drivers" / "winpe-amd64.iso",
        package_dir.parent / "drivers" / "winpe-arm64.iso",
        package_dir.parent / "assets" / "winpe" / "winpe-amd64.iso",
        package_dir.parent / "assets" / "winpe" / "winpe-arm64.iso",
        Path.home() / ".cache" / "vmware2qcow2" / "winpe-amd64.iso",
        Path.home() / ".cache" / "vmware2qcow2" / "winpe-arm64.iso",
    )
    return tuple(dict.fromkeys(candidates))
