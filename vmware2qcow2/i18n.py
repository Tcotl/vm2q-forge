from __future__ import annotations

from typing import Final, Literal


Language = Literal["zh_CN", "en_US"]

DEFAULT_LANGUAGE: Final[Language] = "zh_CN"
LANGUAGE_LABELS: Final[dict[Language, str]] = {
    "zh_CN": "中文",
    "en_US": "English",
}

TRANSLATIONS: Final[dict[Language, dict[str, str]]] = {
    "zh_CN": {
        "app_title": "VM2Q Forge · VMware → qcow2",
        "language": "界面语言",
        "input_path": "VMware 目录或 ZIP",
        "output_path": "输出 qcow2",
        "runtime_dir": "便携运行时目录（可选）",
        "driver_iso": "VirtIO ISO 覆盖（可选）",
        "driver_old": "VirtIO 旧版 0.1.173-9（可选）",
        "driver_new": "VirtIO 新版 0.1.285-1（可选）",
        "winpe_iso": "WinPE ISO（WinPE/DISM，可选）",
        "windows_bake_username": "QGA bake 用户名",
        "windows_bake_password": "QGA bake 密码",
        "guest_os": "客体系统",
        "windows_backend": "Windows 转换后端",
        "linux_backend": "Linux 转换后端",
        "network_model": "目标网卡模型",
        "winpe_arch": "WinPE 架构",
        "qemu_system": "WinPE QEMU 模拟器",
        "qga_install_mode": "QGA 安装阶段",
        "choose": "选择",
        "choose_directory": "目录",
        "choose_zip": "ZIP",
        "start_conversion": "开始转换",
        "require_virtio": (
            "Windows 严格 VirtIO 模式（需要 virt-v2v；生成后直接使用 VirtIO）"
        ),
        "require_qga": "Windows 必须集成 QEMU Guest Agent（需要 WinPE/DISM）",
        "status_ready": "就绪",
        "status_running": "运行中",
        "status_failed": "失败",
        "status_completed": "完成",
        "missing_input_title": "缺少输入",
        "missing_input_message": "请选择 VMware 目录或 ZIP，并指定输出 qcow2。",
        "conversion_failed_title": "转换失败",
        "select_vmware_directory": "选择 VMware 工作目录",
        "select_vmware_zip": "选择 VMware ZIP",
        "select_output": "选择输出 qcow2",
        "select_runtime_directory": "选择便携运行时目录",
        "select_virtio_iso": "选择 virtio-win ISO",
        "select_old_virtio_iso": "选择 virtio-win-0.1.173-9 ISO",
        "select_new_virtio_iso": "选择 virtio-win-0.1.285-1 ISO",
        "select_winpe_iso": "选择准备好的 WinPE ISO",
        "filetype_zip": "ZIP 压缩包",
        "filetype_iso": "ISO 镜像",
        "filetype_qcow2": "qcow2 镜像",
        "filetype_all": "所有文件",
        "log_output": "qcow2：{output_path}",
        "choice_auto": "自动",
        "choice_windows": "Windows",
        "choice_linux": "Linux",
        "choice_qemu_img": "qemu-img",
        "choice_virt_v2v": "virt-v2v",
        "choice_winpe_dism": "WinPE/DISM",
        "choice_e1000": "E1000（兼容）",
        "choice_virtio_net_pci": "VirtIO 网卡（virtio-net-pci）",
        "choice_bake": "转换时内置（bake）",
        "choice_firstboot": "首次启动安装（firstboot）",
        "choice_amd64": "AMD64",
        "choice_arm64": "ARM64",
    },
    "en_US": {
        "app_title": "VM2Q Forge · VMware → qcow2",
        "language": "Language",
        "input_path": "VMware directory or ZIP",
        "output_path": "Output qcow2",
        "runtime_dir": "Portable runtime directory (optional)",
        "driver_iso": "VirtIO ISO override (optional)",
        "driver_old": "Legacy VirtIO 0.1.173-9 (optional)",
        "driver_new": "Modern VirtIO 0.1.285-1 (optional)",
        "winpe_iso": "WinPE ISO (WinPE/DISM, optional)",
        "windows_bake_username": "QGA bake username",
        "windows_bake_password": "QGA bake password",
        "guest_os": "Guest operating system",
        "windows_backend": "Windows conversion backend",
        "linux_backend": "Linux conversion backend",
        "network_model": "Target network adapter",
        "winpe_arch": "WinPE architecture",
        "qemu_system": "WinPE QEMU emulator",
        "qga_install_mode": "QGA installation phase",
        "choose": "Browse",
        "choose_directory": "Directory",
        "choose_zip": "ZIP",
        "start_conversion": "Start conversion",
        "require_virtio": (
            "Strict Windows VirtIO mode (requires virt-v2v; boots with VirtIO)"
        ),
        "require_qga": "Require QEMU Guest Agent in Windows image (requires WinPE/DISM)",
        "status_ready": "Ready",
        "status_running": "Running",
        "status_failed": "Failed",
        "status_completed": "Completed",
        "missing_input_title": "Missing input",
        "missing_input_message": (
            "Choose a VMware directory or ZIP archive and an output qcow2 path."
        ),
        "conversion_failed_title": "Conversion failed",
        "select_vmware_directory": "Choose VMware working directory",
        "select_vmware_zip": "Choose VMware ZIP archive",
        "select_output": "Choose output qcow2",
        "select_runtime_directory": "Choose portable runtime directory",
        "select_virtio_iso": "Choose virtio-win ISO",
        "select_old_virtio_iso": "Choose virtio-win-0.1.173-9 ISO",
        "select_new_virtio_iso": "Choose virtio-win-0.1.285-1 ISO",
        "select_winpe_iso": "Choose prepared WinPE ISO",
        "filetype_zip": "ZIP archive",
        "filetype_iso": "ISO image",
        "filetype_qcow2": "qcow2 image",
        "filetype_all": "All files",
        "log_output": "qcow2: {output_path}",
        "choice_auto": "Automatic",
        "choice_windows": "Windows",
        "choice_linux": "Linux",
        "choice_qemu_img": "qemu-img",
        "choice_virt_v2v": "virt-v2v",
        "choice_winpe_dism": "WinPE/DISM",
        "choice_e1000": "E1000 (compatible)",
        "choice_virtio_net_pci": "VirtIO network (virtio-net-pci)",
        "choice_bake": "Bake during conversion",
        "choice_firstboot": "Install at first boot",
        "choice_amd64": "AMD64",
        "choice_arm64": "ARM64",
    },
}


def supported_languages() -> tuple[Language, ...]:
    return tuple(LANGUAGE_LABELS)


def normalize_language(language: str | None) -> Language:
    if language in TRANSLATIONS:
        return language
    return DEFAULT_LANGUAGE


def language_label(language: str) -> str:
    return LANGUAGE_LABELS[normalize_language(language)]


def translate(language: str, key: str, **values: object) -> str:
    normalized = normalize_language(language)
    text = TRANSLATIONS[normalized].get(key)
    if text is None:
        text = TRANSLATIONS["en_US"].get(key, key)
    return text.format(**values)
