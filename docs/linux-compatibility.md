# Linux 客体兼容性与依赖

VM2Q Forge 可以复用同一套 VMware VMDK → qcow2 转换流程，但 Linux 客体的
VirtIO 启动和 DHCP 依赖会随内核、initramfs 工具和网络栈变化。转换器现在会
根据 VMX 的 `guestOS`/`guestOS.detailedData` 生成 Linux 依赖画像，并把画像写入
JSON 报告、导入说明和 `configure-linux-virtio-net.sh`。

## 宿主机依赖

| 功能 | 必需组件 | 说明 |
| --- | --- | --- |
| 基础磁盘转换 | Python 3.10+、`qemu-img` | `qemu-img` 只复制磁盘扇区，不改变文件系统 UUID |
| Linux firstboot 注入 | `virt-v2v`、libguestfs appliance | `--linux-backend virt-v2v` 会把生成脚本作为 firstboot 脚本传入 |
| Linux 启动验证 | 对应的 `qemu-system-x86_64` 或 `qemu-system-aarch64` | ARM64 宿主机运行 amd64 客体时使用 x86_64 模拟 |
| macOS/Windows 无 libguestfs | `qemu-img` + 生成的脚本 | 脚本需要在客体首次启动时执行；qemu-img 路径不会离线修改 initramfs |

## 客体依赖画像

所有 Linux VirtIO 目标至少需要以下内核模块：

```text
virtio_pci
virtio_blk
virtio_net
virtio_scsi（使用 VirtIO SCSI 时）
```

老客体最常见的缺口是模块存在于 `/lib/modules`，但没有进入启动 initramfs。
此时切换到 VirtIO Block 后可能在挂载根文件系统前停住。生成的脚本会尝试：

1. `modprobe` 加载模块；
2. 写入 `/etc/modules-load.d/vm2q-forge-virtio.conf`，旧系统回退到 `/etc/modules`；
3. 按客体环境调用 `update-initramfs`、`dracut`、`mkinitcpio` 或提示使用 `mkinitrd`；
4. 配置可用的网络栈和 DHCP 客户端；
5. 写入 `/var/lib/vm2q-forge/linux-virtio-ready` 和日志。

### 发行版画像

| VMX 识别结果 | 代际判断 | initramfs 候选 | 网络栈/DHCP 候选 | 迁移建议 |
| --- | --- | --- | --- | --- |
| Debian/Kali/Ubuntu | 旧版本标记为 `legacy` | `update-initramfs`、`dracut` | NetworkManager、Netplan、ifupdown、`dhclient`/`dhcpcd`/`udhcpc` | 老版本先执行脚本，再切换 VirtIO |
| RHEL/CentOS/Rocky/Alma/Oracle/Fedora/Amazon | 7 及更早通常为 `legacy` | `dracut`、`update-initramfs` | NetworkManager、ifcfg、`dhclient`/`dhcpcd`/`udhcpc` | 检查 dracut 是否把模块加入 initramfs |
| SLES/openSUSE | SLES 12 及更早通常为 `legacy` | `dracut`、`mkinitrd` | wicked、NetworkManager、systemd-networkd | 优先保留源网络配置，首次启动执行 DHCP |
| Arch/Gentoo | 版本通常为 `unknown` | `mkinitcpio`、`dracut` | NetworkManager、systemd-networkd、`dhcpcd` | 以实际内核和 initramfs 配置为准 |
| `linux26`/`otherlinux`/未知 Linux | `legacy` 或 `unknown` | 三类工具全部列出 | 通用候选全部列出 | 使用 E1000 或 IDE/SCSI 兼容启动完成检查 |

版本画像是保守提示，不替代客体内验证。VMX 只写 `ubuntu-64`、`archlinux-64`
等无版本字符串时，报告会将代际标记为 `unknown`。

## 转换后端行为

### `qemu-img`

```text
VMDK → qcow2
       └── 输出 VirtIO/DHCP 准备脚本和依赖画像
```

该模式适合已经包含 VirtIO 内核模块的 Linux 客体。脚本不会假设目标环境有
网络，也不会自动调用客体包管理器。老系统应在 VMware 中先安装/更新内核和
initramfs 工具，或用兼容网卡首启后执行脚本。

### `virt-v2v`

```text
VMware → virt-v2v → qcow2
                 └── --firstboot configure-linux-virtio-net.sh
```

当 `virt-v2v` 可用时，`--linux-backend auto` 优先选择它，生成脚本会在客体
首次启动时运行。该路径仍不替客体安装新内核；模块缺失时需要在源客体中补齐
内核/包后重新转换。

## QEMU Guest Agent

Linux QGA 不属于 VirtIO 网卡依赖。需要集群文件浏览、`guest-exec` 或电源管理
时，客体还要安装发行版对应的 `qemu-guest-agent` 包，并由目标平台提供：

```text
virtio-serial-pci
org.qemu.guest_agent.0
```

不同发行版的包名和服务单元细节可能不同，Linux 转换报告只记录依赖画像，不
伪造 QGA 已安装状态。

## UEFI、磁盘 UUID 与回退策略

- `qemu-img convert` 不重写分区表、文件系统 UUID 或 GPT GUID；
- BIOS/UEFI 应沿用源 VMX 的固件模式；
- UEFI 客体要保留 EFI System Partition，并在目标环境挂载对应 NVRAM/固件；
- 老客体第一次启动建议使用 IDE/SCSI 兼容磁盘和 E1000，确认模块与 DHCP 后再
  切换 VirtIO；
- qcow2 不保存虚拟网卡，目标 QEMU/UTM/Proxmox 仍需显式添加 VirtIO 网卡和
  提供 DHCP 的 NAT/桥接网络。

## 首次启动检查

```bash
uname -m
modprobe virtio_pci virtio_blk virtio_net
grep -E 'virtio_(pci|blk|net|scsi)' /proc/modules
ip -br addr
ip route
```

如果根文件系统已经通过 `/dev/vda` 挂载、网卡获得 DHCP 地址，再将镜像标记为
VirtIO-ready。目标网络的 DHCP 服务仍需单独验证。

## 官方参考

- [Linux kernel VirtIO documentation](https://docs.kernel.org/driver-api/virtio/virtio.html)
- [dracut configuration and driver inclusion](https://man7.org/linux/man-pages/man5/dracut.conf.5.html)
- [Netplan DHCP configuration](https://netplan.readthedocs.io/en/latest/netplan-yaml/)
- [NetworkManager connection settings](https://networkmanager.dev/docs/api/latest/settings-ipv4.html)
