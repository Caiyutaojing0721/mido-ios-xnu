# 红米 Note 4X（`mido` / MSM8953）运行 iOS 16 —— 实验性适配脚手架

[项目主页](<../README.md>) · [Polaris 指南](<POLARIS-MIX2S.md>) · [工作记录](<../NOTES.md>)

> **实验性、未启动、非日用。** 本页描述的是 2026-10-24 新增的 **mido 板级脚手架**：
> 已完成 GitHub Actions 内核构建管线与板级工具/补丁骨架；**尚未在任何 mido 真机
> 上启动过**，且 mido 用 **eMMC 5.1（不是 UFS）**，根文件系统的 eMMC 后端尚未实现。
> 任何"编译通过"都不等于"真机可运行"。
>
> **2026-10-24 更新**：板级 MMIO 常量已不再是"公开值猜测"——它们是从用户提供的
> crDroid 15（mido）刷机包的 `boot.img` 内置 DTB 里**实测**出来的（见 1.1 节），
> 打包几何也与原厂 `boot.img` 头部逐字段对齐。
>
> **2026-10-24 二次更新（重要）**：用户另提供了一份**在红米 4X（`santoni` /
> MSM8940）上确实能引导 iOS** 的镜像。拆解它拿到了三样之前只能靠猜的东西：能工作的
> ADT 结构、能工作的启动参数、以及 eMMC 根驱动的完整行为清单（见 1.3 节）。据此修掉了
> **3 个会让内核悄悄不工作的匹配串错误**，并生成了一个可直接 `fastboot boot` 的
> mido 实验镜像（见 1.4 节）。**它仍未在 mido 上验证过。**

## 0. 先说实话

| 你现在能拿到 | 说明 |
| --- | --- |
| GitHub Actions 内核构建（`xnu-mido.yml`） | 真正能编译/链接 XNU 8796 内核，产物 `xnu-kernel-mido` |
| 板级 ADT 生成器（`tools/mkadt_mido.py`） | 生成 8×A53、GIC-400、eMMC 节点、1080×1920 的 ADT（地址已实测，见 1.1） |
| 板级内核补丁骨架（`xnu/patches-mido/`） | 显示缩放 + eMMC/GPU 门禁 + 板型判定，可 apply/编译 |
| shim 的 mido 分支（`shim/emmc.c` 等） | 只读 eMMC/SDHCI 寄存器探针 + 1080×1920 控制台 |
| 打包工具（`mkpayload.py --mido`） | mido 默认启动参数 + `--check-mido-cpus` 拓扑校验 |
| DTB 提取/解析工具（`tools/mido_dtb.py`） | 从任意 mido `boot.img` 抽 DTB 并打印板级常量，用于核对 |

| 你**不能**拿到 | 原因 |
| --- | --- |
| 可启动的 iOS 镜像 | eMMC 根设备后端缺失（最大阻塞） |
| 触摸 / 按键 | 未移植（S3330 与 mido 无关；mido 是一般 Synaptics/其它） |
| GPU（Adreno 506） | 无驱动；`mido_gpu` 是默认关闭的门禁 |
| Wi‑Fi / 蓝牙 / 基带 / 音频 / 相机 / Face ID | 未实现 |
| 真机验证 | 本仓库没有 mido 设备；所有地址均为公开 DT 值，带 TODO |

## 1. 为什么 mido 比 polaris 更难：eMMC ≠ UFS

`sweet`（SM7150）和 `polaris`（SDM845）都用 **UFS**，内核里有 `0028/0030/0034`
三组 UFS 根设备补丁。**mido 是 MSM8953，用 eMMC 5.1（SDHCI/SDCC）**，这两条路
完全不通用：

| 维度 | sweet / polaris | mido |
| --- | --- | --- |
| 存储控制器 | UFS（`ufshc@…`） | eMMC（`/soc/sdhci@7824900`，SDHCI） |
| 根设备 | `rd=ufsroot0` | 需新的 SDHCI 根后端（**未实现**） |
| 中断控制器 | GIC‑v3（GICD+GICR） | GIC‑400（两簇各四核，GICD+GICC） |
| GPU | Adreno 630v2 | Adreno 506 |
| 屏幕 | 1080×2160 | 1080×1920 |
| 内核上限核心 | 8（A55+A75） | 8（全 A53，同构） |

因此 mido 的 `mido_emmc.c` 目前只是一个**默认关闭的拒绝式门禁**（类比 polaris 的
`0093`）：它不会给 eMMC 供电、不会映射寄存器、不会挂载任何分区。真正能用需要先写一个
可回退、有归属的 SDHCI 后端。

### 1.1 实测硬件常量（来自设备自己的 `boot.img` DTB）

mido 没有 `dtb`/`dtbo` 分区，也**没有 QCDT 表**：DTB 直接追加在 kernel blob 尾部
（gzip Image + DTB，`kernel_size` 覆盖两者）。所以原厂 `boot.img` 就是这台机器的
权威硬件描述。提取方式（只读，不碰设备）：

```sh
# 从刷机包取出 boot.img（任意方式，例如 unzip / python zipfile）
python3 tools/mido_dtb.py boot.img --extract dtb/mido_stock/mido.dtb   # 写 DTB
python3 tools/mido_dtb.py boot.img                                    # 打印板级常量报告
python3 tools/mido_dtb.py --dtb dtb/mido_stock/mido.dtb --dump        # 整棵树
python3 tools/mido_dtb.py --dtb dtb/mido_stock/mido.dtb --prop /chosen:bootargs
```

实测结果（`crDroidAndroid-15.0-20250207-mido-v11.1.zip` → `boot.img`）：

| 项目 | 实测值 | 来源节点 / 证据 |
| --- | --- | --- |
| eMMC 控制器（SDHCI 标准寄存器） | **0x07824900**，窗口 0x500 | `/soc/sdhci@7824900`；cmdline `androidboot.bootdevice=7824900.sdhci` |
| eMMC 厂商 HC 块 | **0x07824000**，窗口 0x800 | 同上 `reg` 第 2 项（只读诊断用，永不写） |
| eMMC ICE 块 | **0x07824e00**，窗口 0x200 | 同上 `reg` 第 3 项 |
| GIC‑400 分发器 / CPU 接口 | **0x0B000000**+0x1000 / **0x0B002000**+0x2000 | `/soc/interrupt-controller@b000000`（`qcom,msm-qgic2`） |
| PSCI | `method = smc` | `/psci` |
| CPU | 8×`arm,cortex-a53`，MPIDR **0,1,2,3,0x100,0x101,0x102,0x103**（两簇各 4 核） | `/cpus/cpu@*` |
| 串口（earlycon） | **0x078AF000** | cmdline `earlycon=msm_hsl_uart,0x78af000`；`/soc/serial@78af000` |
| 架构定时器 | **0x0B120000** | `/soc/timer@b120000` |
| 定时器 PPI | INTID **17/18/19/20** | `/soc/timer`：`<1 1>` `<1 2>` `<1 3>` `<1 4>` → 16+n。**ADT 的 `virtual-timer-intid` 用 20**，这是照搬能启动的 santoni ADT（同一套 timer 块）；shim 自身的 `VTIMER=19` 只用于它自己的探针延时，XNU 会在启动时重新初始化 GIC/定时器 |
| 连续开屏 framebuffer | **0x90001000**，区大小 0x13FF000（20.9 MiB） | `/reserved-memory/splash_region@0x90001000`（label `cont_splash_mem`） |
| RAM 预留：modem | 0x86C00000–0x8D600000 | `/reserved-memory/modem_region@0` |
| RAM 预留：adsp / wcnss | 0x8D600000–0x8E700000 / 0x8E700000–0x8EE00000 | 同名节点 |
| RAM 预留：pstore | 0x9FF00000–0xA0000000 | `/reserved-memory/pstore_reserve_mem_region@0` |
| `/memory` 节点 | **全 0**（引导程序运行时才填） | 因此 **DTB 不能证明实装 RAM**；`--ram 3/4` 仍需实机确认 |
| boot.img 几何 | kernel 0x80008000 / ramdisk 0x81000000 / tags 0x80000100 / page 2048 / header v0 / os_version 0x1e000191 | boot header |
| 启动参数（原厂） | `quiet core_ctl_disable_cpumask=0-7 kpti=0 noirqdebug nodebugmon` | `/chosen/bootargs` |

这些值已写入 `tools/mkadt_mido.py`（含来源注释）、`shim/console.h`（`FB_BASE`）、
`shim/emmc.c`（`MIDO_SDCC1`）与 `shim/build.sh`（打包几何）。

### 1.2 刷机包给出的分区与引导事实

同一份 crDroid 包的 `updater-script` / `transfer.list` 还给了这些 mido 专属事实
（对将来的 eMMC 根后端有用）：

| 事实 | 值 | 影响 |
| --- | --- | --- |
| 分区名 | `system`、**`cust`**、`boot`（**没有 `vendor`**） | mido 把 vendor 内容放在 `cust`；做分区表/镜像时不要照抄新机型的 `vendor` |
| 路径形态 | `/dev/block/by-name/<name>`、`/dev/block/bootdevice/by-name/boot` | 有 `by-name` 符号链接；未来 eMMC 后端可直接按名字找分区 |
| system 镜像 | 566,642 × 4096 B ≈ **2.16 GiB** | eMMC 上确实装得下这一量级的读写，根分区不是"卡太小"的问题 |
| cust 镜像 | 92,420 × 4096 B ≈ **361 MiB** | 同上 |
| 基线 | `xiaomi/mido/mido:7.0/V11.0.2.0.NCFMIXM`（MIUI 11 / Android 7 base） | 解释了 `boot.img` 是 **header v0 + 2048 页**这种较老格式，而不是 v2 |

### 1.3 参考实现：一份**能引导 iOS** 的 `santoni` 镜像（本轮新增）

用户提供的 `shim-boot_unlock.img` 是给 **红米 4X（`santoni` / MSM8940 / 骁龙 435 /
eMMC）** 的、报告"可以引导 iOS"的镜像。拆解结果（全部为实测，不是推断）：

| 项 | 实测值 |
| --- | --- |
| 外层 | Android boot header **v0**，page 2048，kernel 0x80008000 / ramdisk 0x81000000 / tags 0x80000100，`second_addr=0` |
| kernel 槽（208,528 B） | 未压缩 arm64 `Image`（`ARMd` @0x38），字符串自述 `santoni bare-metal shim` / `santoni phase 2`，并内嵌 lk2nd DTB（面板 `hx8394f-boe` / `ili9881c-ebbg`…） |
| ramdisk 槽（16,758,816 B） | 标准 **SWEETXNU v1** 容器，11 段，`virt_base=0xfffffff006000000`，**`phys_base=0x98000000`、`mem_size=0x28000000`**，entry `0xfffffff007439488`，ADT 10,588 B |
| ADT 根 | `compatible = D22AP|iPhone10,3|xiaomi,santoni|xiaomi,sweet|qcom,msm8937|AppleARM` |
| `/chosen` | `dram-base 0x98000000` / `dram-size 0x28000000`（二者必须与 payload 头一致）、`display-scale 2`、`firmware-version santoni-shim`、`memory-map/RAMDisk` |
| `/cpus/cpu0..7` | `compatible = arm,cortex-a53|qcom,kryo470|ARM,v8`，`reg` = MPIDR（两簇），时钟 1.4 / 1.09 GHz |
| `/arm-io` | `compatible = sweet-io|arm-io`，`ranges = <0 0x1000000 0xff000000>`（子节点地址相对 0x1000000） |
| `/arm-io/gic` | `compatible = qcom,msm-qgic2|arm,gic-400`，`reg = <0xa000000 0x1000 0xa002000 0x1000>`（= 绝对 0xb000000 / 0xb002000），`virtual-timer-intid = 0x14 (20)`，`#interrupt-cells 3` |
| `/arm-io/disp0` | `compatible = sweet,display`，canvas 720×1280 |
| `/sweet-fbcon` | `reg = <0x90001000 0x2a3000>`，720×1280，stride 2160，depth 24 |
| `/santoni-sdhci` | `compatible = qcom,sdhci-msm-v4|generic-sdhci`，`device_type = sdhci`，`reg = <0x7824000 0x1000>`（厂商 core），`hc-reg = <0x7824900 0x500>`（SDHCI HC），`bus-width 8`，`non-removable` |
| `/defaults` | `no-effaceable-storage = 1` |

**它为什么对 mido 有参考价值**：MSM8940 是 MSM8953 的衍生型号，两者的存储/中断/串口/
splash 地址完全重合——eMMC core `0x7824000` + HC `0x7824900`、GIC `0xb000000`、
UART `0x78af000`、splash `0x90001000`。也就是说 santoni 的 eMMC 驱动所面对的寄存器
布局与 mido 相同。

#### 1.3.1 内核里的 eMMC 根驱动：行为清单（移植规格）

从该内核的符号与字符串可以完整还原它的行为（符号名：`_santoni_sdhci_probe`、
`_sdhci_cmd`、`_sdhci_gcc_set_apps`、`_sdhci_switch_ext_csd`、`_santoni_sdhci_strategy`
等）。这是 mido 要写的驱动的最小功能集：

1. **节点**：`SecureDTFindEntry` 找 `<name>-sdhci`，读 `reg`（厂商 core）、`hc-reg`
   （SDHCI）、`bus-width`、`non-removable`；`reg window too small` 会直接拒绝。
2. **接管 host**：`HC_MODE_EN` latch 检查；`FF_CLK_SW_RST_DIS` 必须写住（否则 SDCLK
   反馈停止时 core 自复位）；`POWER_CONTROL` latch + `PWRCTL` 握手（`SDHCI_PWRCTL`）。
3. **时钟**：GCC `sdcc1_apps` 的 `CMD/CFG/M/N/D/CBCR` 直接改分频（400 kHz 枚举 →
   25 MHz 传输），并等 CBCR 更新完成；SDHCI 自己的 divider 被忽略。
4. **DLL**：`CORE_DLL_CONFIG` / `DLL_STATUS`（RST|PDN、CK_OUT_EN、LOCK）。
5. **枚举**：CMD0（`0xf0f0f0f0` GO_PRE_IDLE 重试）→ CMD1（等 OCR bit31）→ CMD2（R2
   CID）→ CMD3（RCA）→ CMD9（CSD）→ CMD7 → CMD8（EXT_CSD 512 B，校验 SEC_COUNT）
   → CMD6（HS_TIMING、BUS_WIDTH）→ CMD13 确认。8-bit HS 失败会自动回滚到 25 MHz
   legacy edge / 4-bit，而不是卡死。
6. **分区**：GPT 头 + 表项 CRC32 校验，按名字找 `system`；所有读写先做越界检查
   （`refusing read/write of %u blocks`）。
7. **挂根**：注册 `bdevsw`/`cdevsw`（`/dev/emmcroot0`、`/dev/remmcroot0`），
   `IOFindBSDRoot` 把 `rd=emmcroot0` 解析成根设备；失败信息形如
   `IOFindBSDRoot: ... eMMC root %s unavailable`。
8. 额外：把控制台日志刷进 `system` 分区（`SANTONILOG1` 魔数）+ 读写权限开关
   `emmcrw=1` / `rootrw=1`。

**不能照搬的部分**：`santoni_varfix`（VFS 属主修补）、`santoni_keys`（TLMM/PMIC 按键）、
`santoni_touch`（FT5435）、`santoni_pmic` 都是 santoni 专属硬件，mido 的触摸 IC、
PMIC 与按键 GPIO 都不同。

### 1.4 据此修掉的 3 个"静默失效"bug

这些错误不会报错，只会让内核**永远不实例化对应驱动**——之前的 mido ADT 三条全中：

| 位置 | 之前（错） | 现在 | 依据 |
| --- | --- | --- | --- |
| `/cpus/cpuN compatible` | `qcom,cortex-a53` | `arm,cortex-a53` + **`qcom,kryo470`** + `ARM,v8` | 0018 SweetCPU `IONameMatch = 'qcom,kryo470'` |
| `/arm-io/disp0 compatible` | `mido,display` | **`sweet,display`** | 0046/0058 `IONameMatch = 'sweet,display'` |
| framebuffer 节点名 | `mido-fbcon` | **`sweet-fbcon`** | 0003 `SecureDTFindEntry("name","sweet-fbcon")`，找不到就完全没有屏幕输出 |

同时把 eMMC 节点从 `mido-emmc` 改为 `<name>-sdhci`（`device_type = sdhci` + `hc-reg`
+ `bus-width` + `non-removable`），因为根驱动按**节点名**查找。`mido_cpu_topology.py`
也同步要求 `qcom,kryo470`，防止以后再退回去。

### 1.5 mido 实验镜像（`fastboot boot`，不写分区）

由于内核段已经是拍平且重定位好的，**换 ADT 不需要重编 XNU**。`tools/payload_repack.py`
可以把任何 `SWEETXNU` payload 的 ADT/命令行换掉：

```sh
# 1) mido 的 ADT：节点名用参考内核要找的名字，画布用面板尺寸
python3 tools/mkadt_mido.py out/mido_santoni.adt --canvas panel --sdhci-node santoni-sdhci --fb-depth 32

# 2) 把参考内核的 payload 换成 mido 的 ADT
python3 tools/payload_repack.py out/santoni/ramdisk.bin out/mido-payload-santoni.bin --adt out/mido_santoni.adt --cmdline "debug=0x14e serial=3 sweetfb_native=1 sweetfb_noconsole=1 sweetfb_stayon=1 sweethidtest=5 santoni_varfix=1 cpus=4 rd=emmcroot0 emmcrw=1 rootrw=1 wdt=-1 mido"

# 3) 套上参考 shim + **mido 自己的 DTB**，打成原厂几何的 boot.img
python3 tools/mk_mido_boot.py --shim out/santoni/kernel.bin --dtb dtb/mido_stock/mido.dtb --payload out/mido-payload-santoni.bin --out out/shim-mido-boot-v2.img --stock out/mido-rom/boot.img

fastboot boot out/shim-mido-boot-v2.img     # 只启动，不写任何分区
```

**期望（成功的话）**：屏幕出现 `SANTONI SDHCI: ... takeover complete`
→ `enumeration complete: 25 MHz, 8-bit, high-speed` → `"system" first block ...`
→ `IOFindBSDRoot` 找到根 → 用户态启动。

#### 1.5.1 `dtb not found` —— 已定位并修复

第一次实验用的是 `mkbootimg.py` 直接套 shim（镜像
`out/shim-mido-santoni-boot.img`），真机返回：

```
fastboot: Sending 'boot.img' (16574 KB)  OKAY
fastboot: Booting   FAILED (remote: 'dtb not found')
```

**根因**：santoni 镜像的内核段是

```
[ arm64 Image，头里 image_size = 0x2b000 ][ 29 个 santoni 的 DTB，紧接其后 ]
```

这是标准的 *appended DTB* 布局：ABL 从 `image_size` 往后逐个扫 FDT，再用根节点的
`qcom,msm-id` 匹配本机。santoni 那 29 条全是别的 SoC：

| 镜像 | `qcom,msm-id` | `qcom,board-id` | 结果 |
| --- | --- | --- | --- |
| mido 原厂 DTB | **0x125**（MSM8953） | 0xb | 本机要的就是它 |
| santoni 表内 29 条 | 0x12f / 0x108 / 0x10a / 0x116 / 0x161 / 0x162 / 0x1b5 / 0x182 | — | **没有 0x125 → 匹配失败** |

所以 ABL 报 `dtb not found` —— 它并没有"没收到 DTB"，而是"收到的一堆 DTB 里没有
一块是本机的"。这也顺带解释了为什么 mido 原厂 `boot.img` 的 `dt_size` 字段是 0：
DTB 是追加在 kernel blob 里、由 `image_size` 定位的，不靠头部字段。

**修法**（`tools/mk_mido_boot.py`）：保留 shim 的 Image 本体
（`shim[:image_size]`），把它后面那张 santoni DTB 表**整表替换成 mido 自己的 DTB**，
重新按原厂几何打包。工具会自校验：7 个头部字段对原厂、`image_size` 之后存在
`msm-id=0x125` 的 FDT、`SWEETXNU` payload 仍可解析。

生成结果：

| 文件 | 大小 | sha256 | 说明 |
| --- | --- | --- | --- |
| `out/shim-mido-boot-v2.img` | 17,186,816 B | `c7efb7f3e2cc559ebb19025af3a8731531ad54b976ea446931ddfbe83ffae9a6` | **首选**：`shim[:0x2b000]` + mido DTB（丢弃 29 条外来 DTB） |
| `out/shim-mido-boot-v2b.img` | 17,219,584 B | `659950c61ba0e87fcfc3c0e005de25e6927d5184aae8be0d3956a69a6db3d0f4` | 备选：保留 santoni 表，mido DTB 追加在 0x32e90 |

先试 v2；若 v2 出现"过了 DTB 但行为异常"（例如 shim 疑似按自带表选面板），再试
`--keep-shim-dtbs` 的 v2b。

**风险，必须先知道**：

* 这是**另一个机器的 shim + 内核**（santoni 的屏幕时序、触摸、按键、PMIC 全都不对）。
  即使 eMMC 通了，屏幕也可能错位/花屏，触摸和按键一定不可用。
* 它可能停在任意一步（死机黑屏），需要长按电源键重启。
* 只用 `fastboot boot`；**不要** `flash`。若要走这条路，先把 mido 的 `boot` 分区
  备份出来。
* 这是**实验**，不是"mido 已经能启动 iOS"。真要支持 mido，仍需在
  `xnu/patches-mido/` 里实现 1.3.1 那份 eMMC 驱动。

### 1.6 首次真机运行（2026-10-24）：镜像确实在跑，但屏不对

`fastboot boot out/shim-mido-santoni-boot.img` 的实测结果是：屏幕被大量文字覆盖，
下方仍留着 ABL 的 `FASTBOOT` logo。可辨认的字符串包括
`... shim: santoni bare-metal shim ...`、`FFFFFFFF0006000000`（payload 的
`virt_base`）、`0x98000000`（`phys_base`）、以及
`... table max depth = ... each obj count = ...` 这类 DTB/ADT 遍历输出。

**结论**：镜像已经在 mido 上执行（不是被 ABL 拒绝，也不是死在 DTB 匹配上），
但**日志被撕裂到不可读**。

**为什么撕裂**：Redmi 4X（santoni）是 **720×1280**，Redmi Note 4X（mido）是
**1080×1920**。santoni 的 shim 按 720×4 = 2880 字节行宽写 framebuffer，而面板实际
行宽是 1080×4 = 4320，于是每写一行就左移 360 像素并回卷 —— 一行里叠着几行文字，
斜向拖尾；满屏红色是该 shim 的 `con_clear(RED)` 被同样撕裂后的结果。
**这是二进制里写死的几何，改不了**，所以这条路无法用来读日志。

#### 1.6.1 改用自建 mido shim（1080×1920 控制台 + 自己的诊断）

本仓库的 `shim/` 本来就是按平台常量编译的（`shim/console.h` 里 mido 已是
`FB_BASE 0x90001000`、`FB_W 1080`、`FB_H 1920`、`FB_STRIDE 4320`），所以直接为 mido
编一份自己的 shim，屏幕就正常了。它同时带来：

* `BOARD` / `RAM` 来自 **ABL 填充后的** DTB（`/memory` 在 DTB 里是 0，运行时才填）；
* GIC-400 只读识别（mido 分支不碰 GICv3 的 SRE/GICR）；
* `shim/emmc.c` 的 **只读** SDHCI 探针（不写任何寄存器）。

交叉编译器：不需要系统装 aarch64 gcc，用 Zig 即可（本机实测 Zig 0.16）：

```sh
pip install ziglang
# build.sh 用 "$ZIG cc" 调用，包一层脚本即可
cat > ../tools/zig-0.16/zig <<'EOF'
#!/bin/sh
exec python -m ziglang "$@"
EOF
chmod +x ../tools/zig-0.16/zig
```

构建（`PLATFORM=mido` 现在产出 `raw Image + stock DTB`，**不再 gzip**：shim 不是
Linux 内核，没有自解压器，压缩过的裸机代码 ABL 跳进去就是乱码）：

```sh
PLATFORM=mido shim/build.sh                      # -> out/shim-mido-boot.img   （只读探针）
PLATFORM=mido shim/build.sh out/mido-payload-santoni.bin   # -> out/shim-mido-xnu.img
```

| 文件 | 大小 | sha256 | 用途 |
| --- | --- | --- | --- |
| `out/shim-mido-boot.img` | 477,184 B | `d4aefe3c57a092456288dc4e0ccb0083b133117b48d35d28c7935789aa300a1f` | **只读探针**：屏幕可读，打印 BOARD/RAM/GIC/EMMC |
| `out/shim-mido-xnu.img` | 17,235,968 B | `f918a08d5df9e77881d519212369ebc0aed2c6d752e1e563c70c59958d7c06ad` | 自建 shim + santoni 的 XNU payload（mido ADT） |

两张镜像的 kernel blob 都是 `arm64 Image(225,280 B) + mido DTB(247,356 B)`，
`msm-id=0x125` 正好落在 `image_size` 处，头部 7 字段与原厂逐位一致。

**探针图应显示**（1080×1920，满屏红底白字）：

```
SHIM    mido milestone 4: xnu loader      （无 payload 时会继续走探针分支）
BOARD   Qualcomm Technologies, Inc. MSM8953 + PMI8950 QRD SKU3
RAM     hi=00000000 lo=80000000 / MiB=3584 ...
GICD    ctlr=... typer=...
GICv2   pidr2=... iidr=...
GIC     GIC-400 read-only probe (no SRE/GICR on this SoC)
EMMC    base=07824900 ...
EMMC    read-only register probe only
EMMC    probe done, reset in ~3 min
```

3 分钟后自动重启。**这一步同样只 `fastboot boot`，不写任何分区。**

#### 1.6.2 `raw Image + DTB` 被 mido ABL 拒绝 → 改回原厂 gzip 布局

自建 shim 的第一版镜像是 `raw Image(225,280) + mido DTB`，结构上与能启动的 v2
（`santoni shim(0x2b000) + 同一份 mido DTB`）完全同型：arm64 头 `code0/text_offset/
flags` 全一致，DTB 都在 `image_size` 处、都结束于 kernel blob 末尾。但真机返回：

```
fastboot: Sending 'boot.img' (466 KB)  OKAY
fastboot: Booting   FAILED (remote: 'dtb not found')
```

唯一量级上的差别是 `image_size`：v2 是 `0x2b000`，自建 shim 是 `0x37000`
（`_end` = 代码 + bss/栈）。这说明 mido 的 ABL 对"内核段"的识别比"扫 FDT 魔数"更挑剔。

**对策**：不再猜规则，直接复刻**原厂 mido `boot.img` 的内核布局**——gzip 流 + 追加 DTB。
这是出厂镜像自己的形状，ABL 必然接受（且 ABL 本来就带 gunzip，否则跑不了原厂内核）：

```sh
MIDO_KERNEL_GZIP=1 PLATFORM=mido shim/build.sh                       # -> out/shim-mido-boot-gz.img
MIDO_KERNEL_GZIP=1 PLATFORM=mido shim/build.sh out/mido-payload-santoni.bin  # -> out/shim-mido-xnu-gz.img
```

| 文件 | 大小 | sha256 | kernel blob |
| --- | --- | --- | --- |
| `out/shim-mido-boot-gz.img` | 266,240 B | `bbde4d0c60246d69a87cedabaf07f7a4410f6b6f0de4e8b46856025c74aac6d4` | gzip(13,108 B) + DTB，FDT 在 `0x3334`，结束于 blob 末 |
| `out/shim-mido-xnu-gz.img` | 17,025,024 B | `36f26a8d13382f3fc749d13fcac6c90644e887e1d7e414182d762fc7b2decf2f` | 同上 + 16.7 MB payload |

`MIDO_KERNEL_GZIP` 只影响 mido 分支；polaris 一直是 gzip，sweet 保持裸 Image。

#### 1.6.3 XNU 在 mido 上跑起来了；屏幕是 **24bpp（RGB888）**，不是 32bpp

`MIDO_KERNEL_GZIP=1` 的两张镜像真机都能被 ABL 接受（`dtb not found` 消失）。屏幕上是
**XNU 自己的输出**，可辨认：

```
vm_page_bootstrap: ... free pages ...
kdp_core zlib memory 0x7000
SWEETCPUCPD: no cpufreq on this board
SWEET HID: key 0x10 down
SWEET SURF: IOConsole ... registered
...
```

也就是说 santoni 的 XNU 内核 + mido 的 ADT 已经跑到内核子系统初始化。但画面是
"重影 + 斜移"，每行偏移约 **360 px**。360 = (4320 − 3240) / 3，正是**按 32bpp 写、
面板按 24bpp 扫**的结果。

**直接证据**（来自 1.3 节那份能显示的参考 ADT）：

| 项 | 参考 ADT（santoni，能显示） | 我们原来给的（mido） | 现在改成 |
| --- | --- | --- | --- |
| `reg` size | `0x2A3000` = 720×1280×**3** | `0x7E9000` = 1080×1920×**4** | `0x5EEC00` = 1080×1920×3 |
| `stride` | `0x870` = 720×**3** | `0x10E0` = 1080×**4** | `0xCA8` = 1080×3 |
| `depth` | **24** | 32 | 24 |

三处同步修改：

* `tools/mkadt_mido.py`：`MIDO_FB_DEPTH = 24`，`reg` 尺寸改为按 depth 计算；
* `shim/console.h`：新增 `FB_BPP`（polaris sweet = 4，**mido = 3**），`FB_STRIDE = FB_W * FB_BPP`；
  `shim/console.c` 的 `con_bpp` 默认取 `FB_BPP`；`shim/loader.c` 的
  `boot_args.Video.v_depth` 由 `FB_BPP * 8` 得出（原来是写死 32）。

**当时的遗留 TODO（已由 1.6.4 解决）**：本仓库 `xnu/patches/0003` 的控制台是**写死 32bpp**
的（`fbcon_stride / 4`、逐像素写 `uint32_t`、且 `fbcon_stride < width * 4` 就放弃初始化）。
另外 santoni 那份参考内核的输出只占屏幕约 44%（`720×1280×3 / 1080×1920×3`），说明它的
fbcon 几何是编进二进制里的、并不跟着 ADT 走 —— 所以必须能编出**自己的**内核。

新镜像：

| 文件 | 大小 | sha256 |
| --- | --- | --- |
| `out/shim-mido-boot-gz.img`（**先跑这个**，只读探针，24bpp 控制台） | 266,240 B | `75455a44ad8c176b9cc773a0ba657da4f64a23dea61cf71c2b03f9ce881a820c` |
| `out/shim-mido-xnu-gz.img`（24bpp ADT + santoni XNU payload） | 17,025,024 B | `d3dd975259e8639f1ee42e3d68384c9388c9e54133ada867c957c9d1be5325ec` |

#### 1.6.4 给自建内核补 24bpp：`xnu/patches-mido/0096-mido-fbcon-24bpp.patch`

`0003` 是**共享补丁**（sweet / polaris / mido 共用），不能就地改：`0012` 的
`pexpert/arm/pe_serial.c` hunk 直接拿 `fbcon_probe()` 的尾部当上下文行
（`fbcon_stride < fbcon_width * 4`、`fbcon_base = (volatile uint32_t *)ml_io_map_wcomb(...)`），
一旦 `0003` 的文本变了，`0012` 就 `git apply` 失败。按本仓库约定，mido 的差异一律放
`xnu/patches-mido/`，所以新增 **0096**，在 `0003` → `0012` **之后**再改这块：

| 项 | 改之前（0003 + 0012） | 改之后（0096） |
| --- | --- | --- |
| 像素指针 | `volatile uint32_t *fbcon_base` | `volatile uint8_t *fbcon_pixels` |
| 像素宽度 | 隐含 4 字节 | `fbcon_bpp`，从 ADT `sweet-fbcon/depth` 读（`/8`） |
| 绘制 | `line[x] = px`，只有 32bpp 一条路 | `fbcon_put(x, y, argb)`，3 / 4 字节分支 |
| 合法性检查 | `fbcon_stride < fbcon_width * 4` | `fbcon_stride < fbcon_width * fbcon_bpp` |
| 缺省 | — | 没有 `depth`、或不是 24/32 → 回落 32bpp（sweet / polaris 行为不变）|

`fbcon_put()` 是唯一写像素的地方，`clear_row` 与 `transmit_data` 都改走它，所以 24bpp
下每行按 `stride` 推进、每像素 3 字节，不会再出现 360 px 漂移。

补丁不是手抄 diff 出来的，而是 `tools/mkpatch_mido_fbcon.py` 生成的（在仓库根目录跑）：
把 `0003` 与 `0012` 的 old-side 上下文摆进一棵合成树 → 真实应用这两个补丁 → 做文本手术 →
导出 `git diff`，最后「重置 → 0003 → 0012 → 0096」回放并与中间结果逐字节比对。校验两条：

```
tools/mkpatch_mido_fbcon.py   # 生成 + 回放验证（含 synthetic context 泄漏检查）
tools/check_fbcon_block.py    # 把改完的 fbcon 块用 zig cc -target aarch64-freestanding 编一遍
```

`.github/workflows/xnu-mido.yml` 已把 `0096-*.patch` 与 `0095-*` 一起拷进 `dxb/patches/`。

#### 1.6.5 第一份自建内核镜像（CI 产物 → boot.img）

`xnu-mido` 跑通后（`kernel.development.sweet`，17,256,872 B，
`30e542a7fcb00ee2c974b5bfc025c1b06ee07fa22771997fda95887e296e75e1`），
**0096 是否真的进了内核可以直接验**：`buildtree` 产物里有打过补丁的源码树，
把 `xnu/pexpert/arm/pe_serial.c` 抽出来就能看到 `fbcon_bpp` / `fbcon_pixels` / `fbcon_put`
与 `SecureDTGetProperty(entry, "depth", …)`。这比拿真机试要便宜得多。

打包（结果覆盖 `out/shim-mido-xnu-gz.img`，旧的 santoni 版先改名留档）：

```sh
python3 tools/mkadt_mido.py out/mido.adt --canvas panel --fb-depth 24
python3 tools/mkpayload.py xnu/out-mido/kernel.development.sweet out/mido.adt \
    out/mido-payload.bin --mido --check-mido-cpus
PLATFORM=mido MIDO_KERNEL_GZIP=1 shim/build.sh out/mido-payload.bin
```

成品核对过四项（`tools/mido_dtb.py` + 直接读 header）：

| 项 | 值 |
| --- | --- |
| boot header | v0 / page 2048 / kernel `0x80008000` / ramdisk `0x81000000` / tags `0x80000100` / os_version `0x1e000191` |
| kernel blob | gzip 流 15,157 B + stock DTB 247,356 B（FDT 落在 `0x3335`，`msm-id 0x125` / `board-id 0xb`，与 stock **逐字节相同**） |
| ramdisk | payload 16,646,064 B，`SWEETXNU` v1，`phys_base 0x98000000` / `mem_size 0x28000000`，内嵌 ADT 10,764 B（与 `out/mido.adt` 相同） |
| 全文件 | `out/shim-mido-xnu-gz.img` 16,910,336 B，`3b0cb47b6fb560bed9277b240e66233a38a93eec3e11fa2d0f1f857eceb19864` |

> 注意自建内核的链接基址是 `0xfffffe0006000000`（santoni 那份是 `0xfffffff006000000`）。
> shim 全程用 payload 头里的 `virt_base` / `entry` / `phys_base` 现算 `pa = va + (phys_base - virt_base)`，
> 没有写死任何基址，所以这个差异不影响启动。

## 2. 交付物一览

```
.github/workflows/xnu-mido.yml          # 独立的 mido 内核 CI（复用 SWEET machine config）
tools/mkadt_mido.py                     # mido ADT 生成器（--canvas/--sdhci-node/--sdhci-compat/--fb-depth）
tools/mido_cpu_topology.py              # 8×A53 连续 cpus=1..8 的失败即拒校验（含 kryo470 匹配串）
tools/mido_dtb.py                       # 从 boot.img 抽 DTB + 打印实测板级常量
tools/payload_repack.py                 # 换 ADT/命令行重打包 SWEETXNU payload（不重编内核）
tools/mk_mido_boot.py                   # 装 boot.img：shim Image + mido DTB + payload，带自校验
tools/mkpatch_mido_fbcon.py             # 生成 0096：合成树回放 0003→0012 后导出 diff，并自校验
tools/check_fbcon_block.py              # 把 fbcon 块单独用 zig cc -target aarch64-freestanding 编一遍
xnu/patches-mido/0094-mido-display-scaling.patch   # iPhone X 画布按高度缩放进 1080×1920
xnu/patches-mido/0095-mido_board.patch             # bsd/dev/mido_board.c（板型判定）
xnu/patches-mido/0095-mido_emmc.patch              # bsd/dev/mido_emmc.c（eMMC 门禁 stub）
xnu/patches-mido/0095-mido_gpu.patch               # bsd/dev/mido_gpu.c（Adreno 506 门禁 stub）
xnu/patches-mido/0096-mido-fbcon-24bpp.patch       # fbcon 读 ADT depth、支持 24bpp 绘制（自建内核用）
xnu/patches-mido/src/                  # 上述 .c 的参考源（patch 由它生成）
xnu/patches-mido/mido-hook.sh          # 把 mido 驱动接入 conf/files 与初始化的本地脚本
shim/emmc.c shim/emmc.h                # 只读 eMMC/SDHCI 探针
shim/console.h                         # 新增 MIDO 分支（"mido" / 1080×1920）
shim/shim.c shim/build.sh              # mido 平台的探针调度与打包分支
```

## 3. 在 GitHub Actions 构建内核

mido 复用下游 `ios-sweet` 的 **SWEET machine config**（板型由 ADT + 启动参数门控，
与 polaris 相同策略），所以内核构建不需要新的 machine config。

1. 把本仓库推到你的 fork（例如 `luuu20309/ios-sweet` 或本仓库 fork）。
2. GitHub → Actions → **xnu-mido** → **Run workflow**：
   - `macos_version`: `13.5`（= xnu‑8796，iOS 16 同代）
   - `kernel_config`: `DEVELOPMENT`
   - `machine_config`: `SWEET`（mido 复用）
3. 或命令行：

```sh
gh workflow run xnu-mido.yml -R <you>/<repo> --ref main \
  -f macos_version=13.5 -f kernel_config=DEVELOPMENT -f machine_config=SWEET
gh run download <RUN_ID> -R <you>/<repo> -n xnu-kernel-mido -D xnu/out-mido
```

**补丁应用策略（重要）**：`xnu-mido.yml` 把共享的 `xnu/patches/*` 与 mido 的
`xnu/patches-mido/0095-*.patch`、`0096-*.patch` 一起放进 `dxb/patches/`，由
`darwin-xnu-build` 的 `build.sh` 统一应用。`0095-*` 只新建 `bsd/dev/mido_*.c`，
必然可应用；`0096-*` 只改 `pexpert/arm/pe_serial.c` 的那一段 fbcon，且**必须排在
`0012` 之后**（`0012` 把同一段代码当上下文）。两者都已用回放验证过，因此内核构建
**始终能成功**。上下文型的 `0094`（显示缩放）**不会自动应用**——它需要在你的
`ios-sweet` XNU 检出里手动 `git apply --3way`（见第 5 节），避免上下文漂移拖垮 CI。

## 4. 生成 ADT / payload / shim

```sh
# 1) ADT（默认画布 = iPhone X 逻辑 1125×2436，由 0094 缩放进面板）
python3 tools/mkadt_mido.py out/mido.adt --dump
#   面板原生画布（给没有 0094 那样缩放的内核，例如 1.5 节的实验）：--canvas panel
#   eMMC 节点名/兼容串可覆盖：--sdhci-node mido-sdhci --sdhci-compat qcom,sdhci-msm,generic-sdhci
#   像素格式：--fb-depth 32（本树，stride 4320）| 24（santoni 参考，stride 3240）
#   /chosen 的 dram-base/dram-size 必须与 payload 头的 phys_base/mem_size 一致：
#   默认 0x98000000 + 0x28000000（3GB/4GB 都安全），可用 --dram-base/--dram-size 改
#   会警告缺少 research/ipsw-x/d22ap.adt 的 /product 节点；完整身份需自备 iPhone X 设备树
#   （与 polaris 相同：从 iOS 16.7.16 IPSW 提取 DeviceTree.d22ap.im4p -> d22ap.adt）

# 2) payload（内核 + ADT），带 8×A53 拓扑校验
python3 tools/mkpayload.py \
  xnu/out-mido/kernel.development.sweet out/mido.adt out/mido-payload.bin \
  --mido --check-mido-cpus \
  --max-payload-size 0xf000000

# 3) shim 探针镜像（临时启动用，不写分区）
PLATFORM=mido shim/build.sh              # -> out/shim-mido-boot.img
PLATFORM=mido shim/build.sh out/mido-payload.bin   # -> out/shim-mido-xnu.img
```

**mido 没有 polaris 那种"ramdisk 撞 DTB"的限制。** 原厂把 DTB 塞在 kernel blob
里，ramdisk 独立放在 0x81000000，可以一直长到开屏预留区 0x90001000 之前，因此上限是
`0x90001000 − 0x81000000 = 0xF000000`（约 240 MiB），比 polaris 的 ~14 MB 宽松得多。
`shim/build.sh` 的 `PLATFORM=mido` 分支已经按这个值做越界告警。

`shim/build.sh` 的 mido 分支还会把 `dtb/mido_stock/mido.dtb` 追加到 gzip 后的
`Image` 后面（复刻原厂布局）。没有这个 DTB 时它只打印警告——但**原厂 ABL 很可能
因为没有 DTB 而拒绝启动**，所以请先用 1.1 节的命令把 DTB 抽出来。

## 5. 把 mido 驱动接入内核树（本地）

`0095-*` 补丁只**新建** `bsd/dev/mido_*.c`，默认不会被编译（不在 `bsd/conf/files`）。
若要让它们真正参与编译/初始化，在你的 `ios-sweet` XNU 检出里运行：

```sh
sh xnu/patches-mido/mido-hook.sh /path/to/ios-sweet/xnu
```

它会：把 `src/mido_*.c` 拷进 `bsd/dev/`、在 `bsd/conf/files` 注册、并在
`bsd/dev/sweet_ufs.c` 的初始化里加入 `mido_board_init()/mido_emmc_init()/mido_gpu_init()`。
运行时通过启动参数选择：`mido=1`（板型）、`midogpu=1`（GPU 门禁，默认关）、
`midoemmc=1`（eMMC 门禁，默认关）。

## 6. 真机临时启动（安全，不写分区）

```sh
fastboot devices
fastboot boot out/shim-mido-boot-gz.img   # 只读探针；raw 版会被 ABL 拒，见 1.6.2
```

预期屏幕（1080×1920，应**完全清晰、每行不漂移**）：`SHIM mido milestone 4: xnu loader`、
`BOARD Qualcomm Technologies, Inc. MSM8953`、`RAM hi=… lo=…`、
`EMMC base=07824900 prs=…`、`ctr1=… pwr=…`、`CAP 0=… 1=…`、`VER spec=… clk_mhz=…`、
`EMMC read-only register probe only`。约 3 分钟后自动重启。**这一步不写任何分区。**

如果文字清晰但**整屏只有一部分有内容**，那是内核用自己编进去的几何（santoni 参考镜像就是
720×1280）；如果**每行向右/左斜移**，那是 bpp 与面板扫描不一致（24bpp 面板按 32bpp 写）。
两种情况都必须换 6.1 的自建内核。

### 6.1 自建内核 → 可启动镜像（拿到 CI 产物之后）

```sh
# 1) CI 产物（第 3 节下载的 xnu-kernel-mido）解到 xnu/out-mido/
ls xnu/out-mido/kernel.development.*        # 取未 strip 的那个

# 2) ADT：面板原生画布 + 24bpp（0096 会读 depth）
python3 tools/mkadt_mido.py out/mido.adt --canvas panel --fb-depth 24

# 3) payload：内核 + ADT + mido 命令行，顺带校验 8×A53 拓扑
python3 tools/mkpayload.py xnu/out-mido/kernel.development.* out/mido.adt \
    out/mido-payload.bin --mido --check-mido-cpus

# 4) 镜像：**必须 gzip**（raw Image + DTB 会被 ABL 判 dtb not found），DTB 自动追加
PLATFORM=mido MIDO_KERNEL_GZIP=1 shim/build.sh out/mido-payload.bin
#    -> out/shim-mido-xnu-gz.img

# 5) 上机（临时启动，不写分区）
fastboot boot out/shim-mido-xnu-gz.img
```

第 4 步要求 `dtb/mido_stock/mido.dtb` 存在（从原厂 `boot.img` 提，见 8.1 节）；缺了脚本会
打印提取命令。`MIDO_KERNEL_GZIP=1` 只在 mido 分支生效，polaris/sweet 不受影响。

真机完整启动（eMMC 根）在 `mido_emmc` 后端实现前**无法进行**；不要期待当前 payload
能进 iOS 锁屏。

## 7. 未完成 / 阻塞项（按优先级）

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| eMMC/SDHCI 根后端 | **未实现（最大阻塞）** | 已有可照抄的**行为规格**（1.3.1 节，来自能启动的 santoni 内核），但本仓库没有对应源码；仍需自己实现控制器接管/时钟/枚举/GPT/挂根 |
| GIC‑400 vs XNU GIC | 地址已实测，**语义未验证** | GICD 0x0B000000 / GICC 0x0B002000；XNU 中断专家需接受 `arm,gic-400` |
| 连续开屏 framebuffer | **已实测**：0x90001000，1080×1920，**24bpp**，stride 3240 | 探针镜像屏幕清晰、santoni ADT `depth=24` 互相印证（1.6.3）；shim 与 0096 都已按 3 字节/像素 |
| 自建内核的 mido 控制台 | 补丁完成，**待 CI 产物上机** | `0096` 让 fbcon 读 ADT `depth` 并支持 24bpp；已用回放 + 交叉编译验证（1.6.4） |
| DTB 装载方式 | **已实测**：内嵌 kernel blob（Image.gz + DTB） | 不再需要猜 `0x1f00000`；但没有 DTB 尾部 ABL 可能拒绝启动 |
| 实装 RAM 大小 | **DTB 无法证明**（`/memory` 全 0） | 引导程序运行时才填；`--ram 3/4` 需实机或只读探针确认 |
| PSCI CPU_ON（8×A53） | 未验证 | MPIDR 两簇各四核已实测；先 `cpus=4`，再 6→8 |
| 触摸 | 未移植 | 需识别 mido 实际触摸 IC 并写 I²C 驱动；`sweettouch=0` 必须保持 |
| 按键 | 未移植 | PM8953/PMI8953 PON/GPIO，与 sweet 的 PM6150 不同 |
| GPU（Adreno 506） | 无 | `mido_gpu` 默认关闭门禁；无 GMU/HFI/SMMU/渲染/Metal |
| Wi‑Fi/蓝牙/基带/音频/相机/传感器/Face ID | 无 | 锁屏不是这些模块可用的证据 |

## 8. 下一步（建议顺序）

0. ~~跑参考内核镜像拿信息~~ **已完成**：`shim-mido-santoni-boot.img` 能引导，屏幕撕裂是
   720×1280 几何所致；换成自建 shim 后探针输出完全清晰（1.6.1 / 1.6.2）。
1. ~~只读探针~~ **已完成**：`EMMC base=07824900 prs=01F80000`、`CAP 0=4D020000 1=07E7642D`、
   `ADDR bit64=00000001`、`WIN 0000000000000500`，且 `VER clk_mhz=00000000`（时钟未开）。
   `RAM` 仍显示为 `/memory` 的两段（`lo=0x10000000/0x80000000`、`MiB=0x700/0x800`），
   还不是实装容量——`--ram 3/4` 依然待定。
2. **跑自建内核**（现在最该做的一步）：在 fork 上 `Run workflow` → `xnu-mido`，拿到
   `xnu-kernel-mido` 后用 `shim/build.sh` + `tools/mk_mido_boot.py` 打成镜像上机。
   预期是完整 1080×1920 且**每行不再漂移**的 XNU 日志（1.6.4）。
3. **eMMC 根后端**：这是从"能点屏"到"能启动 iOS"的关键。按 1.3.1 的清单实现
   （节点 → 接管 host → GCC 时钟 → 枚举 → GPT → bdevsw → IOFindBSDRoot）。
4. **语义验证**：GIC‑400 在 XNU 中断专家里是否被接受——地址已实测，剩下的是"XNU 怎么用"。
5. **PSCI**：`cpus=4 → 6 → 8`，每次临时 `fastboot boot` 前单独确认。
6. 触摸/按键 → 才能"能用"；GPU 与其它外设最后。

## 8.1 用刷机包交叉验证你的镜像

每次改完打包参数，用同一套工具回读自己的产物并与原厂对比（不需要手机）：

```sh
python3 tools/mido_dtb.py out/shim-mido-boot.img        # 头部 + DTB 报告
# 7 个头部字段应与原厂一致：
#   kernel_addr 0x80008000  ramdisk_addr 0x81000000  second_addr 0
#   tags 0x80000100         page_size 2048           hdr@0x28 0  hdr@0x2c 0x1e000191
```

## 9. 安全与合规

- 只做临时 `fastboot boot`；**任何分区写入**（尤其 `flash userdata`）会清空数据，必须单独确认。
- 保留 bootloader / recovery / modem / persist 与备份；**不要**刷 bootloader/modem/persist。
- 仓库不提交 Apple 镜像、Qualcomm 专有固件、设备转储、序列号或分区备份。
  （`dtb/mido_stock/mido.dtb` 是被 `.gitignore` 排除的本地文件，只用于本地打包。）
- 本页地址来自设备自己的 `boot.img` DTB（**实测**，不是猜测），但**仍未在真机验证**；
  不要把"CI 编译通过"或"头部对齐"当作"真机运行成功"。
