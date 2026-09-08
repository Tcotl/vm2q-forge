# VM2Q Forge

VM2Q Forge 是一个把 VMware 工作目录或 ZIP 压缩包转换为 qcow2 的工具，
并内置 Windows/Linux 的 VirtIO、QEMU Guest Agent、WinPE/DISM 和便携包构建流程，
适合离线迁移、批量打包和跨平台使用。

`vmware2qcow2` 是一个基于 Python 标准库的 VMware 工作目录/ZIP 到 qcow2 转换工具。

## 典型场景

- VMware 工作目录或 ZIP → qcow2；
- Windows 7 x64：WinPE/DISM 离线注入 `viostor`、`NetKVM`、`vioserial`，并在发布前完成 QEMU Guest Agent bake；
- Linux：生成 `virtio-net-pci` 启动参数和 DHCP 配置脚本；
- Arm64 Mac：可完成 x86 VMware 镜像的转换；最终的客体启动仍由 x86 QEMU/UTM 配置负责。

## 项目结构

```text
vmware2qcow2/              核心 Python 包：转换编排、驱动/QGA/WinPE/GUI 本地化逻辑
tests/                     单元测试与回归用例
packaging/                 便携包、原生 GUI、驱动资产和 WinPE ISO 构建脚本
assets/winpe/startnet.cmd  注入到 WinPE boot.wim 的离线 DISM 入口
docs/design.md             架构和转换流程设计
docs/winpe.md              WinPE/DISM 辅助镜像构建说明
.github/workflows/         GitHub Actions CI 与 Release 构建流程
```

生成产物不进入源码结构：`outputs/`、`work/`、`build/`、`vm2q_forge.egg-info/` 都由 `.gitignore` 排除。

## Releases

GitHub Releases 按两个维度拆分：

- 版本形态：`online` / `offline`
- 入口形态：`cli` / `gui`

常见文件名格式：

```text
VM2Q-Forge-{os}-{arch}-{edition}-{flavor}.zip
```

含义如下：

| 维度 | 含义 |
| --- | --- |
| `online` | 只带运行程序和宿主运行时，体积更小；适合自行准备 VirtIO / WinPE 资产 |
| `offline` | 额外打包 VirtIO ISO、WinPE ISO 和离线转换所需资产；适合无网环境 |
| `cli` | 命令行入口 |
| `gui` | 图形界面入口 |

当前发布矩阵覆盖：

- `linux-amd64`
- `linux-arm64`
- `macos-amd64`
- `macos-arm64`
- `windows-amd64`
- `windows-arm64`

发布时每个系统/结构都会同时产出 `online` 与 `offline`、`cli` 与 `gui`
四种组合；离线版会把对应平台的 QEMU/驱动/WinPE 资产一并打包。
维护者发布离线版时，需要先准备好 `VM2Q_WINPE_AMD64_URL` 和
`VM2Q_WINPE_AMD64_SHA256` 两个秘密变量。
其中 `windows-11-arm` 属于 GitHub-hosted runner 的预览标签。

### 原生 GUI 发行物

除 ZIP 便携包外，发布流程还生成两个无需安装 Python 的原生 GUI 文件：

| 宿主机 | 文件 | 架构 | 说明 |
| --- | --- | --- | --- |
| Windows | `VM2Q-Forge-windows-amd64-gui-{edition}.exe` | AMD64/x64 | 单文件 GUI，首次运行自动释放内置运行时 |
| macOS | `VM2Q-Forge-macos-arm64-gui-{edition}.dmg` | Apple Silicon/ARM64 | 拖入“应用程序”后运行的 `.app` 磁盘镜像 |

两种 GUI 均内置匹配宿主的 `qemu-img`、`qemu-system-x86_64` 和必需动态库；
`online` 版通过界面选择本地 VirtIO/WinPE 文件，`offline` 版还会内置所选
离线配置文件需要的 VirtIO ISO 与 WinPE ISO。离线通用版体积会显著增大，因为
准备好的 WinPE ISO 本身通常为数 GB。

GUI 顶部提供“中文 / English”下拉框。切换时会保留已填的路径、选项、密码字段
和已有日志，只更新界面标签、按钮、选项显示和文件选择对话框。

## 运行依赖

- Python 3.10 或更高版本；
- `qemu-img`：所有实际转换都需要；
- `virt-v2v`/libguestfs：可选，用于 Linux 或 Windows 的增强转换；
- WinPE/DISM 后端需要一份与客体同架构的准备好的 WinPE ISO，以及
  `qemu-system-x86_64`；便携包可以把这些资产一起打包；
- Tkinter：仅 GUI 需要；
- Windows 客体的对应真实 `virtio-win` ISO：可放入 `vmware2qcow2/drivers/`
  或通过参数指定；Windows 转换默认还需要其中的
  `guest-agent/qemu-ga-x86_64.msi`/`qemu-ga-i386.msi`。

在 Arm64 Mac 上，`qemu-img` 可以处理 x86 VMware 磁盘格式；真正启动 amd64
客体还需要 `qemu-system-x86_64` 或 UTM 的 x86 模拟/虚拟化配置。

## 当前能力

- 输入 VMware 工作目录或 ZIP 压缩包。
- 自动读取 VMX，识别固件模式和 VMX 引用的主 VMDK 描述文件。
- 检测 VMware `.lck` 锁，避免读取正在运行的虚拟机。
- 安全解压 ZIP，阻止路径穿越。
- 调用 `qemu-img info`、`convert`、`check`。
- 输出 qcow2、SHA-256、JSON 转换报告、导入说明和回滚脚本。
- 内置 `virtio-win-0.1.173-9` 与 `virtio-win-0.1.285-1` 的版本目录、路径规则和自动选择逻辑。
- 按 VMX 的 `guestOS`/`guestOS.detailedData` 识别 Windows/Linux、架构和 Windows 代际。
- 为 Windows 7/legacy 生成旧版 `viostor`/`NetKVM` 预置 CMD；为新 Windows 生成新版脚本。
- Linux 默认使用内核中的 `virtio_net`，输出 `virtio-net-pci` 启动参数和 DHCP 配置脚本。
- Linux 可选 `virt-v2v` 后端；未安装时自动回退到 `qemu-img`，仍生成 Linux 网卡配置脚本。
- Linux 会根据 VMX 识别结果生成依赖画像，列出 `virtio_pci`、`virtio_blk`、
  `virtio_net`、`virtio_scsi`、initramfs 工具、网络栈、DHCP 客户端和发行版包提示。
- Linux 首启脚本现在同时尝试加载/持久化 VirtIO 模块、刷新
  `update-initramfs`/`dracut`/`mkinitcpio`，并兼容 `dhclient`、`dhcpcd`、
  `udhcpc`、NetworkManager、Netplan、wicked、systemd-networkd 和 ifupdown。
- Windows 默认网络模式使用系统自带的 `e1000` 驱动；启用 QGA 必选时由
  WinPE/DISM 完成预置，`qemu-img-only` 仅在显式 `--no-require-qga` 时可用。
- Windows 可用严格 VirtIO 模式：由 `virt-v2v` 或 WinPE/DISM 离线注入
  `viostor`/`NetKVM`，生成首启 DHCP 脚本，并直接输出 VirtIO Block/
  VirtIO-net 导入参数。
- Windows 转换默认强制使用 WinPE/DISM 预置 `vioserial`，并在发布前启动一次
  Windows 安装 QEMU Guest Agent；只有 QGA 服务已安装并验证后才发布 qcow2。
- 若源 VM 使用 VMware SCSI 控制器，QGA bake 阶段会自动切换到兼容的 QEMU
  SCSI 控制器完成最后一次启动。
- 提供 Tkinter 图形界面。
- GUI 支持中文/English 实时切换，并保留当前转换表单状态。
- 支持便携运行时目录：优先使用程序包内的 `qemu-img`，再回退到系统 `PATH`。
- 原生 EXE/DMG 会从自身嵌入的资源目录自动发现 QEMU、动态库、VirtIO 和 WinPE
  资产，不依赖启动脚本或系统 Python。
- 离线便携包内置 PyInstaller Python 运行时、平台对应的 QEMU 运行库，以及两个
  Windows VirtIO ISO；解压后转换阶段不需要网络。

## VirtIO 版本和资产

程序内置两个版本的选择规则：

| 客体 | 默认版本 |
| --- | --- |
| Windows 7/Vista/legacy | `0.1.173-9` |
| Windows 8/10/11/未知 Windows | `0.1.285-1` |
| Linux | 不使用 `virtio-win` |

ISO 二进制不放入 Python 源码包。将两个 ISO 放到以下任一位置即可自动发现：

```text
vmware2qcow2/drivers/virtio-win-0.1.173-9.iso
vmware2qcow2/drivers/virtio-win-0.1.285-1.iso
~/.cache/vmware2qcow2/virtio-win-0.1.173-9.iso
~/.cache/vmware2qcow2/virtio-win-0.1.285-1.iso
```

也可以显式传入路径。显式 `--driver-iso` 优先级最高，随后是对应版本参数：

```bash
--virtio-win-old /path/to/virtio-win-0.1.173-9.iso
--virtio-win-new /path/to/virtio-win-0.1.285-1.iso
```

默认 Windows 转换要求真实 `virtio-win` ISO、WinPE/DISM 后端和一次临时的
Windows 自动登录安装阶段。临时凭证只写入临时 FAT 结果盘，QGA 安装完成后
从镜像删除。Windows 的
`--network-model auto` 仍选择 `e1000`，因为 Windows 7 自带该网卡驱动；
QEMU Guest Agent 不负责提供网卡。

在安装了 `virt-v2v` 的 Linux 转换主机上，也可以启用：

```bash
--windows-backend virt-v2v
```

当显式使用 `--no-require-qga` 时，工具才会允许 `virt-v2v` 或
`qemu-img` 的旧路径；该兼容模式只生成驱动脚本，不把 QEMU Guest Agent
安装到 Windows。

如果要求生成的 Windows 镜像首启就使用 VirtIO，不允许回退到
`qemu-img`，使用严格模式：

```bash
python3 -m vmware2qcow2 "/path/to/windows7-vm" \
  --output "/path/to/Win7-virtio.qcow2" \
  --windows-backend auto \
  --driver-iso "/path/to/virtio-win-0.1.173-9.iso" \
  --require-virtio \
  --network-model virtio-net-pci
```

严格模式要求匹配的 `virtio-win` ISO。由于 QEMU Guest Agent 默认是必选项，
该命令会选择 WinPE/DISM：先离线注入 `viostor`、`NetKVM`、`vioserial`，
再用临时 QEMU Windows 启动阶段执行 QGA MSI。打包脚本会把
`QEMU-GA` 服务显式设置为自动启动（`Start=0x2`），并在结果盘报告
`qga_installed=1`、`qga_service_start=auto` 且服务进入运行状态后才发布 qcow2。

### WinPE/DISM 后端（无需宿主机原生 DISM）

先制作一次与客体同架构的 WinPE ISO：

```bash
python packaging/build_winpe_iso.py \
  /path/to/WinPE_amd64.iso \
  /path/to/winpe-amd64.iso \
  --arch amd64
```

然后在 ARM64 Mac 上执行：

```bash
python3 -m vmware2qcow2 \
  "/path/to/windows7-vm" \
  --output "/path/to/Win7-winpe-virtio.qcow2" \
  --driver-iso "/path/to/virtio-win-0.1.173-9.iso" \
  --winpe-iso "/path/to/winpe-amd64.iso" \
  --windows-backend winpe-dism \
  --require-virtio \
  --network-model virtio-net-pci \
  --qemu-system qemu-system-x86_64
```

WinPE/DISM 后端不调用 macOS 的 `dism`；离线驱动注入阶段不要求把源 Windows
启动到桌面。QGA bake 模式随后由 QEMU 无界面启动一次已注入的 Windows，
完成 MSI 安装后关机。便携包构建时使用
`--require-winpe-assets --winpe-iso ...` 可把 WinPE ISO、QEMU system
emulator、Python 运行时和两个 VirtIO ISO 一起放入离线包。

如果目标是“任何便携运行环境都能首启 DHCP”，使用
`--network-model auto`；仍需通过 WinPE/DISM 完成 QGA 必选流程：

```bash
python3 -m vmware2qcow2 "/path/to/windows7-vm" \
  --output "/path/to/Win7-dhcp.qcow2" \
  --driver-iso "/path/to/virtio-win-0.1.173-9.iso" \
  --winpe-iso "/path/to/winpe-amd64.iso" \
  --windows-backend winpe-dism \
  --network-model auto
```

该模式使用 IDE 磁盘和 Windows 7 自带的 `e1000` 网卡驱动；目标环境提供
DHCP 后即可直接联网，同时 QGA 已在镜像发布前安装。
如只需旧版磁盘转换，可显式使用 `--no-require-qga`，此时报告会标记
QGA 为 `disabled`。

QGA 安装阶段默认使用：

```text
--qga-install-mode bake
```

转换时需要提供临时 Windows 管理员账号。密码优先从环境变量读取，也可以由
CLI 交互输入：

```bash
export VM2Q_WINDOWS_BAKE_USERNAME=ACCOUNT
export VM2Q_WINDOWS_BAKE_PASSWORD=SECRET
```

如果只需要保留旧的首启安装行为，可显式使用
`--qga-install-mode firstboot`；该模式不会把 QGA 标记为镜像内已安装。

## Linux 和网卡

qcow2 只保存磁盘内容，不保存 QEMU/UTM 的虚拟网卡设备。Linux 客体通常使用内核自带的 `virtio_net`；本工具会：

1. 输出 `-device virtio-net-pci,netdev=net0 -netdev user,id=net0`；
2. 生成 `configure-linux-virtio-net.sh`，同时处理 VirtIO 模块、模块持久化、
   initramfs 刷新和 DHCP；
3. 如果检测到 `virt-v2v`，`--linux-backend auto` 会使用它并把网卡脚本作为 first-boot 脚本传入；
4. 如果没有 `virt-v2v`，使用 `qemu-img` 转换并保留上述网卡配置脚本。

转换报告新增 `linux_dependency_plan`，按 Debian/Ubuntu/Kali、
RHEL/CentOS/Rocky/Alma/Fedora、SLES/openSUSE、Arch/Gentoo 和未知 Linux
给出保守的依赖候选。老版本或 VMX 版本未知时，状态为
`legacy-or-unknown-firstboot-check`，不会把未验证的客体标记为直接 VirtIO-ready。

`virt-v2v` 负责客体转换和脚本注入，但不会替旧 Linux 客体安装新的内核
virtio 驱动；老系统应先确认内核包含 `virtio_pci`、`virtio_blk` 和
`virtio_net`，否则先在 VMware 中更新内核或选择 `e1000` 首次启动。
详细矩阵见 [`docs/linux-compatibility.md`](docs/linux-compatibility.md)。

Windows 严格模式与 Linux 脚本模式都只负责镜像内部的驱动/客体配置；
目标虚拟机仍必须把网卡设备配置为 `virtio-net-pci`，并连接到目标环境的
NAT、桥接或其他网络后端。

## 跨平台便携构建

转换磁盘格式本身与客体 CPU 架构无关，但运行程序和 `qemu-img` 必须匹配
宿主系统/架构。因此发布时使用以下形式的独立包：

```text
VM2Q-Forge-linux-amd64/
VM2Q-Forge-linux-arm64/
VM2Q-Forge-macos-amd64/
VM2Q-Forge-macos-arm64/
VM2Q-Forge-windows-amd64/
VM2Q-Forge-windows-arm64/
```

每个包自带：

```text
app/                       PyInstaller 运行程序（cli 或 gui）和嵌入式 Python
runtime/<os>-<arch>/bin/   qemu-img、qemu-system-x86_64，可选 virt-v2v
runtime/<os>-<arch>/lib/   qemu-img 的平台动态库（适用时）
assets/drivers/            virtio-win-0.1.173-9.iso
                           virtio-win-0.1.285-1.iso
assets/winpe/              winpe-amd64.iso（启用 WinPE/DISM 时）
run-vm2qforge.sh           macOS/Linux 启动器
run-vm2qforge.cmd          Windows 启动器
portable-manifest.json     离线资产、哈希和运行时清单
```

在线包只包含程序和宿主运行时；离线包再附带 driver ISO 和 WinPE ISO。
构建机联网准备一次即可，生成的 ZIP 随后可复制到无网络主机。构建在线包：

```bash
python -m pip install ".[build]"
python packaging/build_portable.py \
  --qemu-img /path/to/qemu-img \
  --edition online \
  --flavor cli
```

构建离线 CLI 包：

```bash
python packaging/build_portable.py \
  --qemu-img /path/to/qemu-img \
  --edition offline \
  --flavor cli \
  --virtio-win-old /path/to/virtio-win-0.1.173-9.iso \
  --virtio-win-new /path/to/virtio-win-0.1.285-1.iso \
  --winpe-iso /path/to/winpe-amd64.iso \
  --driver-dir /path/to/driver-assets
```

`--edition offline` 会要求 driver ISO 和 WinPE 资产都存在。PyInstaller 会把
Python 解释器和标准库放进 `app/`；启动器还会把平台运行库加入搜索路径。
若只为一个已知客体制作体积更小的离线包，可选择离线配置文件：

```bash
# Windows 7：只需要 legacy VirtIO ISO、准备好的 WinPE 和 qemu-system-x86_64
python packaging/build_portable.py \
  --edition offline \
  --offline-profile windows7 \
  --flavor cli \
  --virtio-win-old /path/to/virtio-win-0.1.173.iso \
  --winpe-iso /path/to/winpe-amd64.iso

# Linux/Kali：只打包 Python、qemu-img、平台运行库和可用的 qemu-system
python packaging/build_portable.py \
  --edition offline \
  --offline-profile linux \
  --flavor cli
```

`windows7` 包会自动选择 `virtio-win-0.1.173-9.iso` 和包内 WinPE；
`linux` 包不携带 Windows 驱动或 WinPE。macOS Linux 包会额外附带
`extract-vmware-7z.sh`，可用系统自带的 `bsdtar` 解开 Kali 的 `.7z` 工作目录。
GitHub Actions 会在 Linux amd64/arm64、macOS amd64/arm64 和
Windows amd64/arm64 上生成 `online` / `offline`、`cli` / `gui` 的组合包。

离线 Windows 包默认把 QEMU Guest Agent 与 WinPE 注入流程一起打包；如果只做
磁盘-only 或纯在线包，可保持 `--edition online`。

使用离线便携包：

```bash
./run-vm2qforge.sh /path/to/windows7-vm.zip \
  --output /path/to/Win7-ready.qcow2
```

GUI 版直接运行对应的 `run-vm2qforge-gui.sh` 或 `run-vm2qforge-gui.cmd`。

程序会从 `assets/drivers/` 自动选择 Windows 7 的
`virtio-win-0.1.173-9.iso`。Linux 客体不使用这两个 ISO，但它们仍随包提供，
便于同一套介质处理 Windows 客体。

注意：一个二进制包不能同时覆盖所有宿主系统和 CPU 架构；应下载与宿主机
匹配的包。x86/amd64 VMware 客体可以在 Arm64 主机上转换，启动时仍需
目标平台的 x86 模拟器或虚拟化配置。

离线包默认使用 `qemu-img` 转换。Windows 7 的默认 `e1000` 网络模式不要求
`virt-v2v`，但 QEMU Guest Agent 必选流程仍需要准备好的 WinPE ISO 和
`qemu-system-x86_64`；运行阶段不需要 macOS 原生安装 DISM 或 Python。

手动指定：

```bash
--guest-os linux
--linux-backend qemu-img
# 或
--linux-backend virt-v2v
```

## CLI

先确保 `qemu-img` 在 `PATH` 中：

```bash
python3 -m vmware2qcow2 \
  "/Users/shellcode/WorkDocument/AWD-Windows/Vulnstack-Win7-x64-PhpMyAdmin" \
  --output "/Users/shellcode/Documents/Codex/2026-08-03/x/outputs/Win7-ready.qcow2" \
  --virtio-win-old "/path/to/virtio-win-0.1.173-9.iso" \
  --winpe-iso "/path/to/winpe-amd64.iso" \
  --windows-backend winpe-dism
```

Windows 7 要求直接使用 VirtIO 时：

```bash
python3 -m vmware2qcow2 \
  "/path/to/windows7-vm" \
  --output "/path/to/Win7-virtio.qcow2" \
  --virtio-win-old "/path/to/virtio-win-0.1.173-9.iso" \
  --windows-backend winpe-dism \
  --winpe-iso "/path/to/winpe-amd64.iso" \
  --require-virtio \
  --network-model virtio-net-pci
```

Windows 7 只转换磁盘、不集成 QEMU Guest Agent：

```bash
python3 -m vmware2qcow2 "/path/to/windows7-vm" \
  --output "/path/to/Win7-disk-only.qcow2" \
  --no-require-qga \
  --windows-backend qemu-img \
  --network-model auto
```

Windows 新版和 Linux：

```bash
python3 -m vmware2qcow2 "/path/to/vmware-vm" \
  --output "/path/to/linux-ready.qcow2" \
  --guest-os linux \
  --linux-backend auto \
  --network-model virtio-net-pci
```

ZIP 输入：

```bash
python3 -m vmware2qcow2 \
  "/path/to/windows7-vm.zip" \
  --output "/path/to/Win7-ready.qcow2"
```

Dry run：

```bash
python3 -m vmware2qcow2 \
  "/path/to/vmware-vm" \
  --output "/path/to/Win7-ready.qcow2" \
  --dry-run
```

## GUI

```bash
python3 -m vmware2qcow2.gui
```

界面顶部的 **界面语言 / Language** 下拉框可以在中文和 English 之间切换。
在线 GUI 版遇到 Windows 的 WinPE/DISM 流程时，在对应字段选择本地
`virtio-win` 与准备好的 WinPE ISO；离线 GUI 版则会自动从程序内部资源中发现
这些文件。

### 构建原生 GUI 文件

原生 GUI 必须在目标宿主平台上构建：macOS ARM64 生成 DMG，Windows AMD64
生成 EXE。构建机只需要 Python 3.10+（并带 Tkinter）、PyInstaller 和对应平台的
QEMU。Homebrew macOS 构建机可安装 `python-tk@3.13`：

```bash
python -m pip install ".[build]"

# macOS ARM64：输出 VM2Q-Forge-macos-arm64-gui-online.dmg
python packaging/build_gui_release.py \
  --edition online \
  --output-dir dist/gui

# 离线通用 GUI：额外内置两版 VirtIO 与准备好的 WinPE
python packaging/build_gui_release.py \
  --edition offline \
  --offline-profile universal \
  --driver-dir /path/to/driver-assets \
  --winpe-iso /path/to/winpe-amd64.iso \
  --output-dir dist/gui
```

每次原生构建都会生成四个可校验角色：

```text
VM2Q-Forge-...gui-....dmg / .exe       可执行发行物
VM2Q-Forge-...gui-...-manifest.json    内置运行时和资产清单
VM2Q-Forge-...gui-....sha256           SHA-256 校验和
VM2Q-Forge-...gui-...-verification.txt 构建与验证记录
```

macOS 文件使用临时 ad-hoc 签名以便本机验证；没有 Developer ID 公证的第三方
构建在首次打开时仍可能需要用户按住 Control 点击“打开”。

## 输出角色

工具会输出：

```text
Win7-ready.qcow2
Win7-ready.qcow2.sha256
Win7-ready-conversion-report.json
Win7-ready-import.md
Win7-ready-rollback.sh
install-virtio-win7.cmd
install-virtio-win-modern.cmd
configure-linux-virtio-net.sh
install-qemu-guest-agent.cmd
Win7-ready-winpe-dism.log（WinPE/DISM 后端）
Win7-ready-qga-bake.log（QGA 镜像内安装阶段）
```

Windows 客体只会生成与所选 bundle 对应的一个 `install-virtio-*.cmd`；
Linux 客体生成 `configure-linux-virtio-net.sh`；严格 Windows 模式另外生成
`configure-windows-virtio-net.cmd`；QGA 必选模式生成
`install-qemu-guest-agent.cmd`；WinPE/DISM 成功后还保留
`*-winpe-dism.log`。

Windows QGA bake 模式第一次启动目标虚拟机时，使用报告中的
IDE/`e1000` 或 `if=virtio`/`virtio-net-pci` 参数；QGA 已在镜像中安装。
Proxmox 还要启用 QEMU Guest Agent/virtio-serial 通道。
`qcow2` 本身都不存储虚拟网卡和 QGA 通道设备，目标环境仍需提供相应配置。
