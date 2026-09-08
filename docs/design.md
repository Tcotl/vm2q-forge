# vmware2qcow2 设计

## 目标

用户提供以下任一输入：

1. VMware 工作目录；
2. 包含完整 VMware 工作目录的 ZIP 压缩包。

工具输出：

- qcow2 磁盘镜像；
- SHA-256 校验文件；
- JSON 转换报告；
- UTM/QEMU 导入说明；
- 回滚脚本；
- Windows 7 x64 VirtIO 驱动预置脚本。
- Windows 7 QEMU Guest Agent 镜像内安装阶段和 DHCP 导入配置。

## 分层

```text
Tkinter GUI（中文 / English） / CLI
        │
        ▼
Conversion Orchestrator
        ├── InputResolver       ZIP 解包、目录检查、锁检测
        ├── VMwareInspector      VMX/VMDK 识别、固件和控制器读取
        ├── RuntimeResolver      宿主系统/架构识别、便携/冻结工具发现
        ├── DriverSelector       Windows 双版本选择、资产发现、脚本计划
        ├── DriverInjector       驱动后端接口
        ├── GuestAgentPlanner    QGA MSI、vioserial、RunOnce、镜像内安装计划
        ├── NetworkPlanner       VirtIO-net QEMU 参数和客体配置脚本
        ├── QemuImgRunner        info / convert / check
        ├── VirtV2VRunner        Linux 转换、Windows 驱动注入和 first-boot 注入
        ├── WinPeDismRunner      QEMU WinPE、FAT 结果盘、DISM 离线注入
        ├── ArtifactWriter       报告、清单、说明、回滚
        └── Verification         命令记录、退出状态、哈希
```

## 跨平台运行模型

转换层使用同一套 Python 代码，运行时层按宿主系统和架构解析外部工具：

```text
VM2Q_RUNTIME_DIR
  → PyInstaller one-file 的 sys._MEIPASS/runtime/<os>-<arch>/
  → macOS .app/Contents/Frameworks/runtime/<os>-<arch>/
    （同时兼容 Contents/Resources/runtime/<os>-<arch>/）
  → 便携包 runtime/<os>-<arch>/bin/
  → 程序目录附近的 runtime/
  → 系统 PATH
```

当前宿主身份统一为：

```text
host_os: macos | linux | windows | other
host_architecture: amd64 | arm64 | x86 | other
```

报告会记录 `qemu-img`、`virt-v2v`、WinPE 用 QEMU system emulator 的实际路径和来源。转换磁盘格式时不要求
宿主 CPU 与客体 CPU 相同；客体启动阶段仍由目标 QEMU/UTM 的模拟或虚拟化
配置负责。

当解析到嵌入式运行时，`RuntimeResolver` 会为子进程补齐平台库搜索路径：
Windows 使用 `PATH` 中的 `runtime/.../bin` 和 `runtime/.../lib`，Linux 使用
`LD_LIBRARY_PATH`，macOS 使用 `DYLD_LIBRARY_PATH`；若存在
`runtime/.../share/qemu`，同时设置 `VM2Q_QEMU_DATA_DIR`。这样 EXE/DMG
中的 WinPE UEFI 引导也能找到 QEMU firmware 文件。

## 关键状态

```text
DISCOVERED
  → LOCK_CHECKED
  → DRIVER_PLAN_READY
  → CONVERTED
  → VERIFIED
  → PACKAGED
```

任何阶段失败都保留已生成的日志和命令记录，输出目录中不覆盖用户已有文件，除非显式传入 `--overwrite`。

## 驱动后端

### 内置版本目录

程序内置以下两个 bundle 的元数据和自动选择规则：

```text
Windows 7/Vista/legacy → virtio-win-0.1.173-9
Windows 8/10/11/未知 Windows → virtio-win-0.1.285-1
Linux                  → 不使用 virtio-win
```

源代码安装时，ISO 文件作为可选资产放在：

```text
vmware2qcow2/drivers/
~/.cache/vmware2qcow2/
```

也可通过 `--virtio-win-old`、`--virtio-win-new` 或 `--driver-iso` 指定。
离线便携包构建则要求两个版本的 ISO 都进入 `assets/drivers/`。

### Windows 后端：`plan` / `virt-v2v` / `winpe-dism`

检查 Windows 客体、选择 bundle，并生成：

```text
install-virtio-win7.cmd
install-virtio-win-modern.cmd
```

脚本会查找并预置：

```text
viostor\<w7|w8|w10|w11>\<amd64|x86>
NetKVM\<w7|w8|w10|w11>\<amd64|x86>
```

当前状态会明确写入报告；生成脚本不等于驱动已经进入 Windows Driver Store。

在 Linux 转换主机上选择 `--windows-backend virt-v2v` 时，工具会将选中的
ISO 通过 `VIRTIO_WIN` 传给 `virt-v2v`，由其执行 Windows 驱动注入流程；
命令成功只表示转换/注入命令完成，仍需首次启动验证。

使用 `--require-virtio` 时，Windows 流程不允许回退到 `qemu-img`：

1. 必须检测到匹配的 `virtio-win` ISO；
2. 必须检测到可运行的 `virt-v2v`，或准备好的同架构 WinPE ISO 和
   `qemu-system-x86_64`；
3. `virt-v2v` 路径显式使用 `--block-driver virtio-blk`；WinPE 路径把
   目标磁盘以 IDE 方式挂载给辅助机；
4. 注入 `viostor` 与 `NetKVM`，并传入或写入
   `configure-windows-virtio-net.cmd`；
5. 只有命令退出且结果盘写入 `status=success` 后，报告才将
   `virtio_ready` 设为 `true`，导入说明直接使用
   `if=virtio` 与 `virtio-net-pci`。

首启脚本会等待启用的物理网卡出现，再通过 `netsh` 和 `ipconfig /renew`
配置 DHCP。它记录首启结果，但转换阶段不把未启动的客体标记为“已启动验证”。

### QEMU Guest Agent 必选流程

Windows 转换默认启用 `require_qga=true`。该流程不把 QGA 当作网卡驱动，
而是将以下组件作为同一份镜像交付的一部分：

```text
vioserial\<w7|w8|w10|w11>\<amd64|x86>\vioser.inf
guest-agent\qemu-ga-x86_64.msi 或 qemu-ga-i386.msi
install-qemu-guest-agent.cmd
```

`qemu-img`-only 和 `virt-v2v`-only 后端不满足该必选项；Windows 默认后端
会选择 WinPE/DISM。默认 `qga_install_mode=bake` 时，流程执行以下动作：

1. 通过 DISM 注入 `vioserial`；
2. 从 VirtIO ISO 复制匹配架构的 QGA MSI 到
   `ProgramData\VM2Q-Forge`；
3. 复制 `install-qemu-guest-agent.cmd`；
4. 在离线 SOFTWARE hive 的 `RunOnce` 中注册临时安装任务，并写入临时自动登录
   注册表；
5. 用 QEMU 启动一次目标 Windows，提供 `virtio-serial` 通道；
6. Windows 自动登录后执行 MSI，确认 `QEMU-GA` 服务进入运行状态；
7. 执行 `sc config QEMU-GA start= auto`，并校验
   `HKLM\SYSTEM\CurrentControlSet\Services\QEMU-GA\Start` 为 `0x2`；
8. 删除临时自动登录值和 MSI 缓存；
9. 只有 `VM2QRES.TXT` 中的 `qga_installed=1` 且
   `qga_service_start=auto` 才发布镜像。

转换报告将状态区分为
`required-bake-before-publish`、`qga-installed-in-image` 和
`dry-run-qga-bake`。目标 Proxmox/QEMU 仍需启用 QEMU Guest Agent 和
virtio-serial 通道。需要延迟到首次登录的旧行为时显式传入
`--qga-install-mode firstboot`；磁盘-only 行为使用 `--no-require-qga`。

### Windows 通用 DHCP 模式

当 `--network-model auto` 且未启用 `--require-virtio` 时，Windows 客体选择
`e1000`。Windows 7 自带该驱动；若同时启用默认的 QGA 必选流程，存储转换
仍通过 WinPE/DISM 完成，`qemu-img`-only 仅在显式 `--no-require-qga` 时成立：

```text
VMDK → qemu-img → qcow2
目标磁盘：IDE
目标网卡：e1000
DHCP：目标环境启动后直接提供
```

该模式将报告中的 `dhcp_ready` 设为 `true`，不把未注入的 VirtIO 驱动
误报为就绪。`--require-virtio` 仍保留为需要首启直接使用 VirtIO 的专用模式。

### WinPE/DISM 后端：`winpe-dism`

WinPE/DISM 是无 `virt-v2v/libguestfs` 主机上的实际注入路径。驱动离线注入
阶段不启动源 Windows 桌面；默认 QGA bake 阶段会在注入完成后无界面启动一次
已服务的 Windows 磁盘：

1. 用 `qemu-img` 将源 VMDK 转换为临时 qcow2；
2. 创建 FAT16 结果盘，放入请求标记、QGA 安装脚本、临时自动登录注册表和网络脚本；
3. 用 `qemu-system-x86_64` 启动准备好的 amd64 WinPE；
4. 把临时 qcow2 作为 IDE 磁盘、VirtIO ISO 作为 CD-ROM 交给 WinPE；
5. `startnet.cmd` 扫描 Windows 分区和 `w7\amd64` 驱动目录；
6. 执行 DISM 注入 `viostor`、可选 `vioscsi`、`NetKVM` 和 `vioserial`；
7. 复制 QGA MSI 和安装脚本，并写入离线 SOFTWARE 的 RunOnce；
8. `qemu-system-x86_64` 启动一次目标 Windows，安装 QGA 并验证服务状态；
9. 将首启 DHCP 脚本复制到 Windows 并写入离线 SOFTWARE 的 RunOnce；
10. 读取 FAT 结果盘的 `VM2QRES.TXT`，确认 QGA 已安装后才发布最终 qcow2。

如果源 VMware VM 使用 LSISAS/LSILogic 这类 SCSI 控制器，QGA bake 阶段会
自动映射到兼容的 QEMU SCSI 控制器再启动一次 Windows；这避免了仅用 IDE
启动时的控制器不匹配问题，同时不改变最终 qcow2 的客体磁盘内容。

WinPE ISO 在构建阶段由 `packaging/build_winpe_iso.py` 把
`assets/winpe/startnet.cmd` 写入 `boot.wim`。运行阶段只需准备好的 ISO、
QEMU system emulator、VirtIO ISO 和 Python 运行时；不要求宿主 macOS
安装 DISM、Windows GUI 或 UTM。

## Linux 后端

Linux 不加载 `virtio-win`。支持两种存储转换路径：

```text
--linux-backend qemu-img
  VMDK → qemu-img → qcow2
  生成 virtio-net DHCP 配置脚本

--linux-backend virt-v2v
  VMware VMX/VMDK → virt-v2v → qcow2
  将 virtio-net DHCP 脚本作为 first-boot 脚本传入

--linux-backend auto
  检测到 virt-v2v 时使用 virt-v2v，否则使用 qemu-img
```

Linux 网卡分为两层：

1. QEMU/UTM 设备：`virtio-net-pci`；
2. 客体配置：NetworkManager、Netplan、systemd-networkd 或
   `/etc/network/interfaces`。

转换前会生成 `LinuxDependencyPlan`。它根据 VMX 的 Linux 发行版和版本提示
记录：

```text
virtio_pci / virtio_blk / virtio_net / virtio_scsi
update-initramfs / dracut / mkinitcpio / mkinitrd
NetworkManager / Netplan / wicked / systemd-networkd / ifupdown
dhclient / dhcpcd / udhcpc
```

`configure-linux-virtio-net.sh` 现在是一个兼容性首启脚本：除了配置 DHCP，
还会尝试加载并持久化 VirtIO 模块、写入 dracut 模块清单、刷新可用的
initramfs，并记录 `/var/log/vm2q-forge/linux-virtio-firstboot.log`。
`qemu-img` 模式只生成脚本和报告，不在宿主机上直接修改 Linux 客体；
`virt-v2v` 模式把脚本作为 firstboot 入口传入。

老版本或版本未知的 Linux 会标记为
`legacy-or-unknown-firstboot-check`，报告中同时列出发行版包提示。包提示不
代表工具已经联网安装软件；缺少内核模块时应在 VMware 中补齐内核/initramfs，
或先用 E1000/兼容磁盘启动后执行脚本。

qcow2 文件自身不包含虚拟网卡设备，所以报告会同时给出 QEMU 参数、客体脚本
和依赖画像。

## 便携包

`packaging/build_portable.py` 使用 PyInstaller 生成宿主专用运行程序，并按
`edition=online|offline`、`flavor=cli|gui` 组合输出不同便携包：

```text
app/                       vm2qforge-cli 或 vm2qforge-gui
runtime/<host_os>-<host_architecture>/bin/
```

构建流程还会将 `qemu-img` 的平台动态库放入：

```text
runtime/<host_os>-<host_architecture>/lib/
```

PyInstaller 的 `app/` 目录包含 Python 解释器和标准库。离线构建时，两个
VirtIO ISO 由 `packaging/fetch_driver_assets.py` 下载到构建目录，再复制到：

```text
assets/drivers/virtio-win-0.1.173-9.iso
assets/drivers/virtio-win-0.1.285-1.iso
assets/winpe/winpe-amd64.iso
```

`online` 包只携带程序和宿主运行时；`offline` 包还会携带 VirtIO ISO 和
WinPE ISO。`portable-manifest.json` 记录 Python 运行时、QEMU 动态库、
WinPE/驱动 ISO 文件大小和 SHA-256。启动器通过 `VM2Q_RUNTIME_DIR`、
`VM2Q_DRIVER_DIR`、`VM2Q_WINPE_DIR` 以及平台库搜索路径让程序只使用包内
资产。发布流水线在 Linux/macOS/Windows 的 amd64 与 arm64 runner 上分别
构建，不把不同系统和架构强行合并为一个二进制。

离线包的运行阶段不下载 Python、QEMU、WinPE 或 VirtIO 资产。构建阶段需要
准备 WinPE 和驱动 ISO；也支持使用本地 `--virtio-win-old`、
`--virtio-win-new`、`--winpe-iso` 路径构建。
`virt-v2v` 需要完整的 libguestfs 运行时，普通离线包默认使用 `qemu-img`
路径。

## 原生 GUI 发行物

`packaging/build_gui_release.py` 构建不依赖外部启动脚本的 GUI 发行物：

```text
Windows AMD64
  PyInstaller --onefile --windowed
  └── VM2Q-Forge-windows-amd64-gui-{online|offline}.exe

macOS ARM64
  PyInstaller --onedir --windowed → .app
  hdiutil UDZO
  └── VM2Q-Forge-macos-arm64-gui-{online|offline}.dmg
```

构建不会跨编译 Python GUI：Windows EXE 在 `windows-latest` AMD64 runner
原生生成，macOS ARM64 DMG 在 `macos-14` runner 原生生成。两个文件均携带
匹配宿主的 QEMU runtime；`offline` 额外携带离线配置文件要求的 VirtIO/WinPE
资产。每次构建输出发行物、资产清单、SHA-256 和验证记录。

GUI 的显示文本位于 `vmware2qcow2/i18n.py`。内部选项值继续使用稳定的
`auto`、`winpe-dism`、`virtio-net-pci` 等标识；界面中显示的本地化文本与
内部值映射分离，因此语言切换不会改变已经选择的转换行为。转换引擎日志保留
原始技术文本，便于定位外部工具问题。

## 识别信息

`VMX guestOS` 和 `VMX guestOS.detailedData` 用于识别：

- `guest_os_family`: `windows`、`linux` 或 `unknown`；
- `architecture`: `amd64`、`x86`、`arm64` 或 `unknown`；
- Windows legacy/modern 代际；
- VirtIO bundle 和网络模型选择原因。

## 输入识别规则

优先级如下：

1. VMX 中 `scsi0:0.fileName`、`sata0:0.fileName`、`ide0:0.fileName`；
2. VMX 中第一个以 `.vmdk` 结尾的磁盘字段；
3. 工作目录中唯一的 VMDK 描述文件。

分片文件，例如 `-s001.vmdk`，只作为描述文件的 extent，不作为独立输入。

## Windows 7 x64 默认策略

```text
固件：沿用 VMX；当前样例为 Legacy BIOS
默认 winpe-dism：目标磁盘 IDE、网卡 E1000，转换阶段安装 QGA 并直接 DHCP
默认 winpe-dism：严格 VirtIO 时注入 viostor、NetKVM、vioserial 和 QGA
`--no-require-qga`：允许 qemu-img-only 磁盘转换，不报告 QGA 就绪
```

严格策略的命令示例：

```bash
vmware2qcow2 VMWARE_DIR \
  --output WIN7_QCOW2 \
  --driver-iso VIRTIO_WIN_OLD_ISO \
  --winpe-iso WINPE_AMD64_ISO \
  --windows-backend winpe-dism \
  --require-virtio \
  --network-model virtio-net-pci
```

## 安全边界

- ZIP 解包拒绝绝对路径和 `..` 路径；
- 检测 `.lck` 后停止；
- 源目录只读处理，输出写到独立路径；
- 凭证不进入配置文件、命令参数或日志；
- 外部命令使用参数数组调用，避免 shell 拼接；
- 每个输出附带校验和和可回滚清单。
