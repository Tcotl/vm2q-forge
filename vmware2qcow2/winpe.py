from __future__ import annotations

import os
import socket
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


RESULT_REQUEST_NAME = "VM2QREQ.TXT"
RESULT_NAME = "VM2QRES.TXT"
RESULT_LOG_NAME = "VM2QLOG.TXT"
RESULT_NETWORK_SCRIPT_NAME = "VM2QNET.CMD"
RESULT_QGA_SCRIPT_NAME = "VM2QGA.CMD"
RESULT_QGA_AUTOLOGON_NAME = "VM2QALOG.REG"
RESULT_DISK_SIZE = 32 * 1024 * 1024
WINPE_MEMORY_MB = 8192
FAT_BYTES_PER_SECTOR = 512
FAT_RESERVED_SECTORS = 1
FAT_COUNT = 2
FAT_ROOT_ENTRIES = 512
FAT_SECTORS_PER_FAT = 256
FAT_SECTORS_PER_CLUSTER = 1


class WinPeError(RuntimeError):
    """Raised when the WinPE helper cannot complete offline servicing."""


@dataclass(frozen=True)
class WinPeResult:
    status: str
    fields: dict[str, str]
    raw: str
    log: str

    @property
    def succeeded(self) -> bool:
        return self.status.lower() == "success"


@dataclass(frozen=True)
class WinPeExecution:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    result: WinPeResult | None


def infer_winpe_architecture(path: Path) -> str | None:
    """Infer the architecture from a conventional WinPE asset name."""

    name = path.name.lower()
    if any(
        token in name
        for token in ("arm64", "aarch64", "arm-64", "a64fre", "a64")
    ):
        return "arm64"
    if any(token in name for token in ("amd64", "x64", "x86_64", "winpe64")):
        return "amd64"
    return None


def _short_name(filename: str) -> bytes:
    if "." in filename:
        stem, suffix = filename.rsplit(".", 1)
    else:
        stem, suffix = filename, ""
    if not stem or len(stem) > 8 or len(suffix) > 3:
        raise WinPeError(f"Result disk file is not an 8.3 name: {filename}")
    return f"{stem.upper():<8}{suffix.upper():<3}".encode("ascii")


def _fat_layout(total_sectors: int) -> tuple[int, int, int]:
    root_dir_sectors = (
        FAT_ROOT_ENTRIES * 32 + FAT_BYTES_PER_SECTOR - 1
    ) // FAT_BYTES_PER_SECTOR
    root_dir_sector = (
        FAT_RESERVED_SECTORS + FAT_COUNT * FAT_SECTORS_PER_FAT
    )
    data_sector = root_dir_sector + root_dir_sectors
    data_sectors = total_sectors - data_sector
    clusters = data_sectors // FAT_SECTORS_PER_CLUSTER
    if clusters < 2 or clusters >= 0xFFF6:
        raise WinPeError(f"Unsupported result disk size: {total_sectors} sectors")
    return root_dir_sector, data_sector, clusters


def _write_fat16_entry(fat: bytearray, cluster: int, value: int) -> None:
    struct.pack_into("<H", fat, cluster * 2, value & 0xFFFF)


def create_result_disk(
    path: Path,
    network_script: str | None = None,
    qga_script: str | None = None,
    size_bytes: int = RESULT_DISK_SIZE,
    qga_autologon_reg: str | None = None,
) -> None:
    """Create a small FAT16 disk WinPE can use for result files.

    The disk deliberately uses short 8.3 file names so the WinPE batch file
    can find it without depending on volume labels or drive-letter ordering.
    """

    if size_bytes % FAT_BYTES_PER_SECTOR:
        raise WinPeError("Result disk size must be a multiple of 512 bytes")
    total_sectors = size_bytes // FAT_BYTES_PER_SECTOR
    partition_start = 1
    partition_sectors = total_sectors - partition_start
    root_dir_sector, data_sector, clusters = _fat_layout(partition_sectors)
    path.parent.mkdir(parents=True, exist_ok=True)

    files: dict[str, bytes] = {
        RESULT_REQUEST_NAME: b"VM2Q Forge result request\r\n",
    }
    if network_script is not None:
        files[RESULT_NETWORK_SCRIPT_NAME] = network_script.encode("utf-8")
    if qga_script is not None:
        files[RESULT_QGA_SCRIPT_NAME] = qga_script.encode("utf-8")
    if qga_autologon_reg is not None:
        files[RESULT_QGA_AUTOLOGON_NAME] = qga_autologon_reg.encode("utf-8")

    fat_size = FAT_SECTORS_PER_FAT * FAT_BYTES_PER_SECTOR
    fat = bytearray(fat_size)
    _write_fat16_entry(fat, 0, 0xFFF8)
    _write_fat16_entry(fat, 1, 0xFFFF)
    root = bytearray(
        FAT_ROOT_ENTRIES * 32
    )
    next_cluster = 2
    data_blocks: list[tuple[int, bytes]] = []

    for index, (filename, content) in enumerate(files.items()):
        if index >= FAT_ROOT_ENTRIES:
            raise WinPeError("Result disk root directory is full")
        name = _short_name(filename)
        cluster_count = max(
            1,
            (len(content) + FAT_BYTES_PER_SECTOR - 1) // FAT_BYTES_PER_SECTOR,
        )
        first_cluster = next_cluster
        last_cluster = first_cluster + cluster_count - 1
        if last_cluster >= clusters + 2:
            raise WinPeError("Result disk is too small for WinPE helper files")
        for cluster in range(first_cluster, last_cluster + 1):
            _write_fat16_entry(
                fat,
                cluster,
                0xFFFF if cluster == last_cluster else cluster + 1,
            )
        entry_offset = index * 32
        root[entry_offset : entry_offset + 11] = name
        root[entry_offset + 11] = 0x20
        struct.pack_into("<H", root, entry_offset + 26, first_cluster)
        struct.pack_into("<I", root, entry_offset + 28, len(content))
        for cluster_index in range(cluster_count):
            start = cluster_index * FAT_BYTES_PER_SECTOR
            block = content[start : start + FAT_BYTES_PER_SECTOR]
            data_blocks.append((first_cluster + cluster_index, block))
        next_cluster = last_cluster + 1

    boot = bytearray(FAT_BYTES_PER_SECTOR)
    boot[0:3] = b"\xEB\x3C\x90"
    boot[3:11] = b"VM2QFAT "
    struct.pack_into("<H", boot, 11, FAT_BYTES_PER_SECTOR)
    boot[13] = FAT_SECTORS_PER_CLUSTER
    struct.pack_into("<H", boot, 14, FAT_RESERVED_SECTORS)
    boot[16] = FAT_COUNT
    struct.pack_into("<H", boot, 17, FAT_ROOT_ENTRIES)
    struct.pack_into("<H", boot, 19, 0)
    boot[21] = 0xF8
    struct.pack_into("<H", boot, 22, FAT_SECTORS_PER_FAT)
    struct.pack_into("<H", boot, 24, 32)
    struct.pack_into("<H", boot, 26, 64)
    struct.pack_into("<I", boot, 28, 0)
    struct.pack_into("<I", boot, 32, total_sectors)
    boot[36] = 0x80
    boot[38] = 0x29
    struct.pack_into("<I", boot, 39, 0x564D3251)
    boot[43:54] = b"VM2Q RESULT"
    boot[54:62] = b"FAT16   "
    boot[510:512] = b"\x55\xAA"
    mbr = bytearray(FAT_BYTES_PER_SECTOR)
    mbr[446] = 0x00
    mbr[450] = 0x06
    struct.pack_into("<I", mbr, 454, partition_start)
    struct.pack_into("<I", mbr, 458, partition_sectors)
    mbr[510:512] = b"\x55\xAA"

    with path.open("w+b") as stream:
        stream.truncate(size_bytes)
        stream.seek(0)
        stream.write(mbr)
        stream.seek(partition_start * FAT_BYTES_PER_SECTOR)
        stream.write(boot)
        fat_offset = (
            partition_start + FAT_RESERVED_SECTORS
        ) * FAT_BYTES_PER_SECTOR
        for fat_index in range(FAT_COUNT):
            stream.seek(fat_offset + fat_index * fat_size)
            stream.write(fat)
        stream.seek(
            (partition_start + root_dir_sector) * FAT_BYTES_PER_SECTOR
        )
        stream.write(root)
        for cluster, block in data_blocks:
            offset = (
                partition_start + data_sector
                + (cluster - 2) * FAT_SECTORS_PER_CLUSTER
            ) * FAT_BYTES_PER_SECTOR
            stream.seek(offset)
            stream.write(block)
        stream.flush()


def _read_fat16_file(path: Path, filename: str) -> bytes | None:
    try:
        with path.open("rb") as stream:
            first_sector = stream.read(FAT_BYTES_PER_SECTOR)
            boot = first_sector
            if len(boot) != FAT_BYTES_PER_SECTOR:
                return None

            def valid_boot_sector(candidate: bytes) -> bool:
                return (
                    len(candidate) == FAT_BYTES_PER_SECTOR
                    and struct.unpack_from("<H", candidate, 11)[0]
                    == FAT_BYTES_PER_SECTOR
                    and candidate[13] != 0
                    and candidate[16] != 0
                    and candidate[510:512] == b"\x55\xAA"
                )

            base_sector = 0
            if not valid_boot_sector(boot):
                if first_sector[510:512] != b"\x55\xAA":
                    return None
                partition_type = first_sector[446 + 4]
                if partition_type not in (0x04, 0x06, 0x0E):
                    return None
                base_sector = struct.unpack_from(
                    "<I", first_sector, 446 + 8
                )[0]
                stream.seek(base_sector * FAT_BYTES_PER_SECTOR)
                boot = stream.read(FAT_BYTES_PER_SECTOR)
                if not valid_boot_sector(boot):
                    return None
            bytes_per_sector = struct.unpack_from("<H", boot, 11)[0]
            sectors_per_cluster = boot[13]
            reserved = struct.unpack_from("<H", boot, 14)[0]
            fat_count = boot[16]
            root_entries = struct.unpack_from("<H", boot, 17)[0]
            sectors_per_fat = struct.unpack_from("<H", boot, 22)[0]
            if (
                bytes_per_sector != FAT_BYTES_PER_SECTOR
                or sectors_per_cluster == 0
                or reserved == 0
                or fat_count == 0
                or root_entries == 0
                or sectors_per_fat == 0
            ):
                return None
            root_dir_sectors = (
                root_entries * 32 + bytes_per_sector - 1
            ) // bytes_per_sector
            root_dir_sector = reserved + fat_count * sectors_per_fat
            data_sector = root_dir_sector + root_dir_sectors
            wanted = _short_name(filename)
            first_cluster = None
            size = 0
            stream.seek(
                (base_sector + root_dir_sector) * bytes_per_sector
            )
            for _ in range(root_entries):
                entry = stream.read(32)
                if len(entry) != 32 or entry[0] == 0:
                    break
                if entry[0] in (0xE5, 0x2E) or entry[11] == 0x0F:
                    continue
                if entry[:11] != wanted:
                    continue
                first_cluster = struct.unpack_from("<H", entry, 26)[0]
                size = struct.unpack_from("<I", entry, 28)[0]
                break
            if first_cluster is None:
                return None
            if size == 0:
                return b""

            fat_offset = (
                base_sector + reserved
            ) * bytes_per_sector
            stream.seek(fat_offset)
            fat = stream.read(sectors_per_fat * bytes_per_sector)
            output = bytearray()
            cluster = first_cluster
            seen: set[int] = set()
            while (
                cluster >= 2
                and cluster < 0xFFF8
                and cluster not in seen
                and len(output) < size
            ):
                seen.add(cluster)
                offset = (
                    base_sector + data_sector
                    + (cluster - 2) * sectors_per_cluster
                ) * bytes_per_sector
                stream.seek(offset)
                output.extend(
                    stream.read(sectors_per_cluster * bytes_per_sector)
                )
                fat_entry_offset = cluster * 2
                if fat_entry_offset + 2 > len(fat):
                    break
                cluster = struct.unpack_from("<H", fat, fat_entry_offset)[0]
            return bytes(output[:size])
    except OSError:
        return None


def read_result(path: Path) -> WinPeResult | None:
    raw_bytes = _read_fat16_file(path, RESULT_NAME)
    if not raw_bytes:
        return None
    raw = raw_bytes.decode("utf-8", errors="replace")
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip().lower()] = value.strip()
    status = fields.get("status", "")
    log_bytes = _read_fat16_file(path, RESULT_LOG_NAME) or b""
    return WinPeResult(
        status=status,
        fields=fields,
        raw=raw,
        log=log_bytes.decode("utf-8", errors="replace"),
    )


def _qmp_quit(socket_path: Path, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(0.5)
                channel.connect(str(socket_path))
                try:
                    channel.recv(4096)
                except OSError:
                    pass
                channel.sendall(b'{"execute":"qmp_capabilities"}\r\n')
                try:
                    channel.recv(4096)
                except OSError:
                    pass
                channel.sendall(b'{"execute":"quit"}\r\n')
                return
        except OSError:
            time.sleep(0.1)


def _qemu_drive(path: Path, details: str) -> str:
    escaped = str(path).replace("\\", "\\\\").replace(",", "\\,")
    return f"file={escaped},{details}"


def _qemu_data_dir(qemu_system: str) -> Path | None:
    configured_value = os.environ.get("VM2Q_QEMU_DATA_DIR")
    configured = Path(configured_value).expanduser() if configured_value else None
    candidates = ((configured,) if configured else ()) + (
        Path(qemu_system).resolve().parent.parent / "share" / "qemu",
        Path(qemu_system).resolve().parent / ".." / "share" / "qemu",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return None


def _uefi_firmware_arguments(qemu_system: str, workdir: Path) -> tuple[str, ...]:
    data_dir = _qemu_data_dir(qemu_system)
    if data_dir is None:
        raise WinPeError(
            "UEFI bake boot needs QEMU firmware data; set VM2Q_QEMU_DATA_DIR "
            "or include the QEMU share/qemu directory"
        )

    code_candidates = (
        "edk2-x86_64-code.fd",
        "edk2-i386-code.fd",
        "OVMF_CODE_4M.fd",
        "OVMF_CODE.fd",
    )
    vars_candidates = (
        "edk2-i386-vars.fd",
        "edk2-x86_64-vars.fd",
        "OVMF_VARS_4M.fd",
        "OVMF_VARS.fd",
    )
    code = next(
        (data_dir / name for name in code_candidates if (data_dir / name).is_file()),
        None,
    )
    variables = next(
        (
            data_dir / name
            for name in vars_candidates
            if (data_dir / name).is_file()
        ),
        None,
    )
    if code is None or variables is None:
        raise WinPeError(
            "UEFI bake boot needs both an x86_64 firmware code file and "
            "a writable variables template in QEMU share/qemu"
        )

    workdir.mkdir(parents=True, exist_ok=True)
    writable_vars = workdir / "vm2q-uefi-vars.fd"
    if not writable_vars.exists():
        shutil.copy2(variables, writable_vars)
    return (
        "-drive",
        _qemu_drive(
            code,
            "if=pflash,format=raw,readonly=on",
        ),
        "-drive",
        _qemu_drive(
            writable_vars,
            "if=pflash,format=raw",
        ),
    )


def run_winpe(
    qemu_system: str,
    winpe_iso: Path,
    driver_iso: Path,
    target_disk: Path,
    result_disk: Path,
    workdir: Path,
    timeout: int,
    dry_run: bool = False,
) -> WinPeExecution:
    """Boot the prepared WinPE ISO and wait for its DISM result marker."""

    workdir.mkdir(parents=True, exist_ok=True)
    qmp_socket = workdir / "vm2q-qmp.sock"
    qemu_log = workdir / "vm2q-qemu.log"
    argv = [
        qemu_system,
    ]
    data_dir = _qemu_data_dir(qemu_system)
    if data_dir:
        argv.extend(["-L", str(data_dir)])
    argv.extend(
        [
            "-machine",
            "q35,accel=tcg",
            "-cpu",
            "max",
            "-m",
            str(WINPE_MEMORY_MB),
            "-smp",
            "2",
            "-display",
            "none",
            "-monitor",
            "none",
            "-serial",
            f"file:{qemu_log}",
            "-qmp",
            f"unix:{qmp_socket},server=on,wait=off",
            "-no-reboot",
            "-boot",
            "order=d,menu=off",
            "-nic",
            "none",
            "-drive",
            _qemu_drive(
                target_disk,
                "format=qcow2,if=ide,index=0,media=disk",
            ),
            "-drive",
            _qemu_drive(
                result_disk,
                "format=raw,if=ide,index=1,media=disk",
            ),
            "-drive",
            _qemu_drive(
                winpe_iso,
                "media=cdrom,readonly=on,if=ide,index=2",
            ),
            "-drive",
            _qemu_drive(
                driver_iso,
                "media=cdrom,readonly=on,if=ide,index=3",
            ),
        ]
    )
    if not dry_run:
        argv.extend(_uefi_firmware_arguments(qemu_system, workdir))
    if dry_run:
        return WinPeExecution(
            argv=tuple(argv),
            returncode=0,
            stdout="",
            stderr="",
            result=WinPeResult(
                status="dry-run",
                fields={"status": "dry-run"},
                raw="status=dry-run\n",
                log="",
            ),
        )

    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    result: WinPeResult | None = None
    qemu_log_text = ""
    try:
        with qemu_log.open("w+b") as log_stream:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                cwd=workdir,
            )
            result_seen_at: float | None = None
            while True:
                result = read_result(result_disk)
                now = time.monotonic()
                if result is not None:
                    result_seen_at = result_seen_at or now
                    if now - result_seen_at >= 3.0:
                        if process.poll() is None:
                            _qmp_quit(qmp_socket)
                        break
                returncode = process.poll()
                if returncode is not None:
                    break
                if now - started > timeout:
                    _qmp_quit(qmp_socket)
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                    raise WinPeError(
                        f"WinPE helper timed out after {timeout} seconds"
                    )
                time.sleep(0.5)

            if process.poll() is None:
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    _qmp_quit(qmp_socket)
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
            final_returncode = process.returncode
            log_stream.flush()
        try:
            qemu_log_text = qemu_log.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            qemu_log_text = ""
        result = read_result(result_disk) or result
        return WinPeExecution(
            argv=tuple(argv),
            returncode=final_returncode if final_returncode is not None else 1,
            stdout=qemu_log_text,
            stderr="",
            result=result,
        )
    except OSError as exc:
        raise WinPeError(f"Could not start WinPE helper: {exc}") from exc


def run_windows_bake(
    qemu_system: str,
    target_disk: Path,
    result_disk: Path,
    workdir: Path,
    timeout: int,
    firmware: str = "bios",
    storage_controller: str = "ide",
    dry_run: bool = False,
) -> WinPeExecution:
    """Boot Windows once so the staged QGA MSI is installed before publish."""

    workdir.mkdir(parents=True, exist_ok=True)
    qmp_socket = workdir / "vm2q-bake-qmp.sock"
    qga_socket = workdir / "vm2q-bake-qga.sock"
    qemu_log = workdir / "vm2q-windows-bake.log"
    argv = [
        qemu_system,
    ]
    data_dir = _qemu_data_dir(qemu_system)
    if data_dir:
        argv.extend(["-L", str(data_dir)])
    normalized_controller = storage_controller.lower()
    if normalized_controller in {
        "mptsas1068",
        "lsi53c895a",
        "pvscsi",
        "megasas",
        "megasas-gen2",
    }:
        target_disk_args = [
            "-device",
            f"{normalized_controller},id=vm2qscsi",
            "-drive",
            _qemu_drive(
                target_disk,
                "format=qcow2,if=none,id=vm2qdisk",
            ),
            "-device",
            "scsi-hd,drive=vm2qdisk,bus=vm2qscsi.0,bootindex=0",
        ]
    else:
        target_disk_args = [
            "-drive",
            _qemu_drive(
                target_disk,
                "format=qcow2,if=ide,index=0,media=disk",
            ),
        ]

    argv.extend(
        [
            "-machine",
            "pc,accel=tcg",
            "-cpu",
            "max",
            "-m",
            "2048",
            "-smp",
            "2",
            "-display",
            "none",
            "-monitor",
            "none",
            "-serial",
            f"file:{qemu_log}",
            "-qmp",
            f"unix:{qmp_socket},server=on,wait=off",
            "-no-reboot",
            "-boot",
            "order=c,menu=off",
            "-nic",
            "none",
            *target_disk_args,
            "-drive",
            _qemu_drive(
                result_disk,
                "format=raw,if=ide,index=1,media=disk",
            ),
            "-chardev",
            (
                f"socket,id=vm2qga,path={qga_socket},"
                "server=on,wait=off"
            ),
            "-device",
            "virtio-serial-pci",
            "-device",
            "virtserialport,chardev=vm2qga,name=org.qemu.guest_agent.0",
        ]
    )
    if firmware.lower() == "uefi" and not dry_run:
        argv.extend(_uefi_firmware_arguments(qemu_system, workdir))

    if dry_run:
        return WinPeExecution(
            argv=tuple(argv),
            returncode=0,
            stdout="",
            stderr="",
            result=WinPeResult(
                status="dry-run",
                fields={"status": "dry-run"},
                raw="status=dry-run\n",
                log="",
            ),
        )

    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    result: WinPeResult | None = None
    qemu_log_text = ""
    try:
        with qemu_log.open("w+b") as log_stream:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                cwd=workdir,
            )
            result_seen_at: float | None = None
            while True:
                result = read_result(result_disk)
                now = time.monotonic()
                if result is not None and (
                    result.fields.get("qga_installed") == "1"
                    or result.status.lower() == "failed"
                ):
                    result_seen_at = result_seen_at or now
                    if now - result_seen_at >= 3.0:
                        if process.poll() is None:
                            _qmp_quit(qmp_socket)
                        break
                returncode = process.poll()
                if returncode is not None:
                    break
                if now - started > timeout:
                    _qmp_quit(qmp_socket)
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                    raise WinPeError(
                        f"Windows QGA bake boot timed out after {timeout} seconds"
                    )
                time.sleep(0.5)

            if process.poll() is None:
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    _qmp_quit(qmp_socket)
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
            final_returncode = process.returncode
            log_stream.flush()
        try:
            qemu_log_text = qemu_log.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            qemu_log_text = ""
        result = read_result(result_disk) or result
        return WinPeExecution(
            argv=tuple(argv),
            returncode=final_returncode if final_returncode is not None else 1,
            stdout=qemu_log_text,
            stderr="",
            result=result,
        )
    except OSError as exc:
        raise WinPeError(f"Could not start Windows QGA bake boot: {exc}") from exc
