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

#### 1.6.6 首次自建内核上机：跳到 XNU 后静默卡死（`0097`）

现象：shim 打印到 `XNU jumping (fbcon clears screen)` 就停住，屏幕定格在 shim 的蓝底白字，
没有 PANIC，也不会自己重启。

**先看照片里的一个细节**：底部左侧有一块淡色小方块。那是 `start.s` 里 `start_first_cpu` 的
stage 0 白带——它不建栈、不建页表、**在 MMU 关闭状态下直接用 `boot_args` 里的 `Video.v_baseAddr`
往物理地址写 32 行 × 64 像素**（`ldr x0,[x20,#40]`、`madd x0,x2,x4,x0`）。
所以：

* 白带存在 ⇒ 内核**确实开始执行了**（`_start` → `start_first_cpu`），不是跳错地址；
* 但 `vstart()` 的第一句 `sweet_early_mark(1, "arm_init entered (MMU on)")` 什么都没留下
  ⇒ 死在 stage 0 与 `arm_init` 之间。

这一段只有几步：设 `VBAR_EL1 = LowExceptionVectorBase`、读 boot_args、建 bootstrap 页表、开 MMU、
`b common_start`。而 `LowExceptionVectorBase` 的向量全是 `b .`——**一旦开 MMU 之后发生异常，
就是原地死循环，屏幕不动、没有 panic**，和观察完全吻合。

根因在 bootstrap 页表里这一行（共享补丁 `0083` 加的）：

```asm
#if defined(SWEET)
	MOV64	x10, 0x9c000000      /* sweet 的开屏缓冲 */
	...
	str		x11, [x13, x12, lsl #TTE_SHIFT]
#endif
```

它只给 `0x9c000000`（sweet）建了 Normal-NC 块映射。mido 的开屏缓冲在 **`0x90001000`**，没有映射，
于是 MMU 一开，`sweet_early_mark(1)` 写这个 VA 就翻译失败 → 异常 → `b .` 死循环。

`0097-mido-early-console.patch` 修两处：

| 文件 | 改动 | 作用 |
| --- | --- | --- |
| `osfmk/arm64/start.s` | `MOV64 x10, 0x9c000000` → `0x90000000` | 给 mido 的开屏建 32MB 块映射（覆盖 `0x90001000`+`0x5EEC00`）|
| `osfmk/arm64/sweet_early.c` | `early_fb` 改 `uint8_t *`；新增 `sweet_early_put()` 3/4 字节分支；`early_bpp` 从 `Video.v_depth/8` 取；几何门槛 `v_rowBytes >= v_width * 4` → `* early_bpp` | 早期控制台按 24bpp 逐字节画，几何真正跟随 boot_args |

`0x90000000` 的块号是 `0x48`，而 V=P 的 RAM 从 `physBase=0x99004000`（块号 `0x4C`）起，两者不冲突。

验证方式（这套流程以后每次都该走，比上机快）：

```sh
# 1) 从产物里抽出打过补丁的源码，确认真进去了
tar -xzOf xnu/out-mido/buildtree.tgz xnu/osfmk/arm64/start.s | grep -n "MOV64.*0x90000000"
# 2) 合成树里回放补丁，逐字节比对
git apply --check xnu/patches-mido/0097-mido-early-console.patch
# 3) C 部分用 stub 头单独编一遍
zig cc -target aarch64-freestanding -c sweet_early.c
```

修好后的预期：屏幕会按 24bpp 正常显示 `SWEET EARLY 01: arm_init entered (MMU on)` 到
`SWEET EARLY 20: kprintf startup complete` 这条链，卡在哪一步一眼就能看出来。

#### 1.6.7 带 `0097` 的内核与镜像（第二次 CI）

`kernel.development.sweet` 17,273,280 B，
`32d853bd507714f46afd7083baf239d664fb51033937e8a837dc084c70792df0`（比上一版大 16,408 B）。

从 `buildtree.tgz` 直接验到 `0097` 落地：`start.s:701` = `MOV64 x10, 0x90000000`；
`sweet_early.c` 里 `early_bpp` / `sweet_early_put` / `early_stride = 3240` 全在，且
`0096` 的 `fbcon_bpp` / `fbcon_put` / `"depth"` 也仍在（两条补丁互不干扰）。

顺带确认了一个**不是**问题的地方：`arm_vm_init.c` 里 `uint64_t fb = 0x9c000000ULL;` 只是缺省值，
紧接着会被 `args->Video.v_baseAddr` 覆盖，所以标记 9 以后的重定位（`sweet_early_relocate`）
在 mido 上同样落在真屏，不需要再改。

成品镜像 `out/shim-mido-xnu-gz.img` 16,910,336 B，
`728788bb1e4dc70cea8a6d54c755a9eec9c3ae4a2dcf1df0aba4ea1d3e95bb1f`：

| 项 | 值 |
| --- | --- |
| kernel blob | gzip 15,157 B + stock DTB 247,356 B（FDT `0x3335`，与 stock 逐字节相同） |
| ramdisk | payload 16,646,080 B，`phys_base 0x98000000` / `mem_size 0x28000000` |
| 内嵌 ADT | 10,764 B，与 `out/mido.adt` 相同（`adt_off 0xfdd5b0`） |
| 段 0 | `__TEXT 0xfffffe0007004000`，`file_size 0x164000`，与 CI 内核前 1,458,176 B 逐字节相同 |

上一版留在 `out/shim-mido-xnu-gz.prev.img`。

#### 1.6.8 第三次上机：还是只有 stage 0 —— 改实验设计（`0098`）

带 `0097` 的内核上机后，屏幕依旧停在 shim 那 14 行，左下角只有 stage 0 的白色小块，
一条 `SWEET EARLY` 都没有。

用 capstone 把 `kernel.development.sweet` 反汇编了一遍，四条静态结论说明**这段代码按理应该能跑**：

* stage 0 读的是 `[x20,#40]`（`v_baseAddr`）、`#56`（`v_rowBytes`）、`#72`（`v_height`），
  与 shim 写入一致；白带位置也对，所以它确实执行了；
* `adrp` 是 PC 相对的，运行时得到的是**物理地址**（被 `load_pa - link_base` 偏移过），
  所以 `add x22 / sub x23` 那套“转 KVA”的算法是自洽的，栈落在 `0xfffffe00080c4000`（合法 KVA）；
* 页表布局：TTBR0 根在 `bootstrap_pagetables+0`、TTBR1 根在 `+0x4000`、V=P 的 L2 在 `+0x8000`
  （`add x13, x25, #8, lsl #12` 证实 `PGBYTES=16K`），根索引用 VA[46:36]、L2 索引用 VA[35:25]，
  因此 splash 块写进 `l2[0x48]` 本身是**正确**的；
* 内核模板 TCR = `T0SZ=T1SZ=17`（47 位 VA）、`IPS=2`（40 位 PA）；`0x90001000` 走 TTBR0、
  `0xfffffe…` 走 TTBR1，与建表方式一致。

既然静态分析解释不了，就**换实验设计而不是继续猜**。`0098-mido-early-console.patch`：

1. `sweet_early.c` 在 `arm_vm_init()` 调用 `sweet_early_relocate()` **之前完全不碰 framebuffer**
   （新增 `early_fb_live`，`put` / `putc` / `mark` / `panic` 全部提前返回）。标记 1–7 因此不可能再触发缺页。
2. `start.s` 删掉已无用的 bootstrap splash 映射，改为在 **MMU 仍关闭**时画两个进度块（物理写，不会失败）：

| 块 | 位置（底部一行，单位 dword） | 含义 |
| --- | --- | --- |
| 白 | 0 | stage 0（原有）|
| 绿 | 96 | 页表与 V=P / KVA 两套映射都建完 |
| 黄 | 192 | 即将写 `SCTLR_EL1`（开 MMU）|

判读方式：

* **只有白** → 死在建页表之前或之中（`create_bootstrap_mapping` 一带）；
* **白 + 绿** → 死在开 MMU 或 `ret` 到 `arm_init` 之间；
* **白 + 绿 + 黄** 且屏幕静止 → 死在 MMU 打开之后，或没走到 `sweet_early_map_final`；
* **白 + 绿 + 黄** 之后出现 `SWEET EARLY 08/09…` 与 XNU 日志 → 已经越过，接着看 fbcon。

验证三条（都在仓库根目录跑）：

```sh
tools/mkpatch_mido_early.py   # 合成树导出 diff + 「apply 后逐字节比对」回放
tools/check_early_asm.py      # 两段内联汇编单独用 zig cc -target aarch64-freestanding 汇编
python -m ziglang cc -target aarch64-freestanding -DSWEET -c \
    out/p98/osfmk/arm64/sweet_early.c    # C 部分（stub 头见 out/p97/stub）
```

#### 1.6.9 第四次上机：白 + 绿 + 黄 —— 把观测点搬过 MMU 边界（`0099`）

真机回报底部是**白 + 绿 + 黄**三种颜色。这同时证明两件事：`0098` 的两段进度块确实编进了
内核，而且内核一路健康地走到了 `MOV64 x0, SCTLR_EL1_DEFAULT` 的前一条指令。

**一个必须记住的事实**：`sweet_early_mark()` 的调用点**全部**在 `arm_vm_init.c` 里，而且
都在 `sweet_early_relocate()` 之后（`grep -rn sweet_early_ arm_vm_init.c` 可见 10–14 号）。
也就是说 `0097` 的“一条日志都没有”和 `0098` 的“只有三块颜色”**并不能证明 MMU 打开后立刻
出事**——在到达 `arm_vm_init()` 之前，内核本来就没有任何输出能力。观测点必须自己搬到
MMU 的另一侧。

顺带排除了两个候选根因：

* **TCR 的编码没错，但 granule 是错的。** `0x226511a511` 解出来是 `T0SZ=T1SZ=17`、
  `TG0=0b10`、`TG1=0b01`，确实是 16K granule 的正确写法（`TG1` 的编码与 `TG0` 不同，
  见 `proc_reg.h:858`）。**但 Cortex-A53 根本不实现 16K granule**——见 1.6.12。
  「编码正确」和「硬件支持」是两件事，这一段当时把两者混为一谈了。
* **`SCTLR_EL1_DEFAULT` 不含 `WXN`，`ARM_TTE_BOOT_BLOCK` 不含 `PXN`**，所以 MMU 打开后
  继续执行 V=P 映射里的代码不会取指异常。

**又一条反证：MMU 前后那段指令在两个内核里几乎逐条相同。** 反汇编比对
`out/santoni/xnu_flat.bin`（能跑到 `vm_page_bootstrap` 的参考内核）和自建 mido 内核的
`msr mair_el1` → `msr sctlr_el1` 窗口：

* 写入的 `SCTLR_EL1` 值**完全相同**，都是 `0x3454593D`；
* `MAIR_EL1` 的构造序列、`tlbi vmalle1` / `ic iallu` / `dsb sy` / `isb` 冲刷序列完全相同；
* `VBAR_EL1 = adrp(vectors) + x22 - x23`（物理地址）的写法相同；
* 唯一的结构差异是 **granule**：santoni 用 4K（`TTBR1 = x25 + 0x1000`，块 2MB，L2 索引
  右移 21），mido 用 16K（`TTBR1 = x25 + 0x4000`，块 32MB，索引右移 25）。

因此 SCTLR 的 RES0 位、MAIR 属性这些“可疑点”都不是差异来源——两个内核写的是同一个
常量，跑在同一种核（Cortex-A53）上。**剩下的那个结构差异（granule）就是根因，见 1.6.12。**

`0099-mido-mmu-trace.patch` 因此改四处：

1. **把 splash 的 V=P 映射加回来**（一个 32MB Normal-NC block，写进已有的 V=P L2 表，
   `add x13, x25, #(2 * PGBYTES)`）。这是下面几项成立的前提。
2. **第 4 个进度块（洋红）紧跟在 `MSR SCTLR_EL1` + `isb` 之后**。它仍然是物理写：只有
   MMU 真的开了、V=P 映射真的有效，它才会出现在屏幕上。
3. **第 5 个进度块（青）在 `ret` 之前**（`cbnz x21, Ltrampoline` 之后的返回序列里），
   把 `CPACR_EL1`、`TPIDR_EL1`、`MIDR_EL1` 三条也覆盖进来——如果 `CPACR_EL1` 写到
   EL2/EL3 被拦，青块就不会出现。
4. **16 个 low vector 槽位带着自己的编号跳进 painter**（`mov x9, #n`）。painter 不用栈、
   不读 `boot_args`，几何全部写死，所以 MMU 开或关都有效。它画两条带：
   * `0x905BD200`（第 1856–1887 行）：第 `n` 块位置画红，**哪一块亮就是哪个向量触发**
     （0–3 = EL1/SP0，4–7 = EL1/SP1，8–11 = EL0 aarch64，12–15 = EL0 aarch32；
     每 4 个内部依次是 sync / irq / fiq / serror）。
   * `0x905A3D00`（第 1824–1855 行）：`ESR_EL1.EC`，**列 = EC & 0xF，颜色 = EC >> 4**
     （红 0 / 绿 1 / 蓝 2）。EC=0x21 取指异常、0x25 数据异常、0x26 SP 对齐、0x0E 非法执行态。

底部一行从左到右：白、绿（+384B）、黄（+768B）、洋红（+1152B）、青（+1536B）。

| 看到的颜色 | 含义 |
| --- | --- |
| 白 | 建页表（或它之前）出错 |
| 白 + 绿 | 开 MMU / 跳 `arm_init` 之间出错 |
| 白 + 绿 + 黄 | `MSR SCTLR_EL1` 或紧随的 `isb` 就出错 |
| 白 + 绿 + 黄 + 洋红 | **MMU 已开且 V=P 有效**，问题在其后 |
| 再加青 | 一路活到返回 C 之前，问题在 `ret` 之后的 KVA 上 |
| 上面出现红块 | 发生了异常，块的位置 = 向量号；再上一带 = `ESR_EL1.EC` |

验证（都在仓库根目录跑）：

```sh
tools/mkpatch_mido_mmu_trace.py   # 合成树导出 diff + 逐字节回放
tools/check_mmu_trace_asm.py      # 单独汇编向量表与 painter，并检查 16 个槽位都是合法 b
```

#### 1.6.10 第五次上机：还是白 + 绿 + 黄 —— 观测点自毁（`0100`）

`0099` 打在 0099 v2 内核上，真机底部仍然只有**白、绿、黄**，洋红缺席。但这次不能再得出
「`MSR SCTLR_EL1` 炸了」的结论，因为复核代码时发现 `0099` 的两个后置进度块开头是：

```asm
ldr     x9, [x20, #40]          /* Video.v_baseAddr */
```

`x20` 是 `boot_args` 的**物理**指针，MMU 关闭时可以直接解引用；MMU 打开后这条 `ldr` 要走
页表。于是「MMU 起来了吗」这个探针的**第一条指令**自己就可能 data abort——而低向量表的 VA
若同时不可取指，异常就会二次失败，屏幕上什么都不留。

也就是说「白绿黄」至少有三种解释，而 0099 无法区分：

1. `msr sctlr_el1` / `isb` 本身出错；
2. MMU 开了，但 `ldr [x20, #40]` 走页表失败；
3. 前两块写进了写缓冲/cache，没有到达 DRAM（洋红块之后没有 `dsb`）。

这就是把观测代码写得依赖被观测对象本身的下场。`0100-mido-mmu-const-crumbs.patch` 因此：

1. **两个后置块全部改成编译期常量**，与 painter 同一套路：`0x905D6700`（第 1888 行）
   `+ 288*4`（洋红，x=384px）/ `+ 384*4`（青，x=512px），**在第一条 `str` 之前一条 `ldr`
   都没有**，不碰 `x20`、不读 `Video`。
2. **每块末尾加 `dsb sy`**，把 Normal-NC 的写缓冲排空。
3. ~~修掉青色写错~~ —— **这一步改错了方向，见 1.6.11**。`0100` 把第五块从 `0x00FFFF00`
   改成了 `0x0000FFFF`，而后者在这块屏上就是**黄**，等于又画了一遍第三块。

现在的判读是干净的：

| 底部一行 | 含义 |
| --- | --- |
| 白 + 绿 + 黄 | `MSR SCTLR_EL1` 或 `isb` 本身出错（唯一可能） |
| … + 洋红 | MMU 已开，V=P 映射真实有效 |
| … + 第 5 块 | `CPACR_EL1` / `TPIDR_EL1` / `MIDR_EL1` 都过了，`ret` 进 C 之前 |
| 上方出现红块 | 取了异常；块位置 = 向量号，再上一带 = `ESR_EL1.EC` |

验证：

```sh
tools/mkpatch_mido_mmu_const.py   # 生成 0100：从 out/p99 树导出 diff + 逐字节回放
tools/check_const_crumbs.py       # 抽出两个块单独汇编（含 MOV64 宏），防止手写错
```

#### 1.6.11 颜色字节序搞反了，第五块实际是黄（`0101`）

`0100` 编出的内核已经打过一次镜像（`out/shim-mido-xnu-gz.img`，16,910,336 字节，
sha256 `c76e691f…2848d9cf`），自检九项全过，唯独第五块的颜色仍不对。

原因在**两种写屏方式的字节序相反**：

- shim 控制台（`shim/console.c` 的 `put_px`）按 `p[0]=c>>16, p[1]=c>>8, p[2]=c` 逐字节写，
  内存顺序是 **R,G,B**，所以 `RED=0x00ff0000` 出来是红。
- 进度块用 `str w1, [x5], #4` 整字写，小端序把**低字节**先落地，字节序正好倒过来。

于是同一个字在两套代码里含义相反：

| 字 | `str` 落地的字节 | 屏幕颜色 |
| --- | --- | --- |
| `0x0000FF00` | `00 FF 00` | 绿 |
| `0x0000FFFF` | `FF FF 00` | **黄** |
| `0x00FF00FF` | `FF 00 FF` | 洋红 |
| `0x00FFFF00` | `00 FF FF` | **青** |

真机早就给过答案：0098 的第三块是 `0x0000FFFF`，用户看到的就是黄。绿和洋红在字节交换下
是对称的，所以只有第五块受影响。`0101-mido-crumb-color.patch` 把第五块改回 `0x00FFFF00`。

**这一轮不必为它再跑一次 CI**：第五块是黄也不影响判读，按「数几块」读即可——
底部一行 `白 绿 黄 洋红 黄` 就是五块全到，说明 `ret` 之前无事。

#### 1.6.12 根因：Cortex-A53 没有 16KB 页粒度（`0102`）

第六次上机（`0100` 内核，常量进度块）：底部**白 + 绿 + 黄**，洋红缺席。这次探针里
一条 `ldr` 都没有，所以只剩下一个解释：`MSR SCTLR_EL1` / `isb` 本身就过不去。

**硬件事实**：Cortex-A53（SDM8953 的八个核）**不实现 16KB translation granule**。
`ID_AA64MMFR0_EL1.TGran16` 在这一核上恒为 0，是设计如此（ARM DDI 0500 §4.2.1），
SoC 集成商也改不了。把 `TCR_EL1.TG0/TG1` 写成 `0b10` 之后所有翻译都会失败，于是
开 MMU 之后的第一条 `str` 立刻 data abort；而此时 `VBAR_EL1` 指向的内核 VA 同样翻译
失败，取指再 abort 一次，屏幕上一个字节都留不下。这与实测现象完全吻合。

旁证：Aleph Security 的 iOS-on-QEMU/KVM 工作在同一块 Rock64（A53）上撞过同一堵墙，
最后只能换支持 16K 的核（Cavium/Ampere 服务器）继续。而 santoni 那个**能**跑起 XNU
的 payload 是 **4KB** 构建（`TTBR1 = x25 + 0x1000`、2MB 块、L2 索引右移 21）。
我们自己的内核来自 sweet（Kryo 470 = A76/A55，两者都支持 16K），机器配置里写着
`#define __ARM_16K_PG__ 1`。

**三处必须一起改**（`0102-mido-4k-pages.patch`）：

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `pexpert/pexpert/arm64/SWEET.h` | 删掉 `__ARM_16K_PG__` | `PGBYTES` 变 4096，`TCR` 的 `TG0/TG1` 都变 4KB、`T0SZ/T1SZ=25` |
| `makedefs/MakeInc.def` | 删掉 MacOSX 的 `-DARM_LARGE_MEMORY=1` 覆盖 | `ARM_LARGE_MEMORY` 把内核链到 `0xfffffe0007004000`（-2TB），需要 41 位内核 VA；4KB 配置只描述 39 位（`proc_reg.h` 的表写得很清楚），TTBR1 够不到 |
| `osfmk/arm64/start.s` | splash 的 V=P 映射从 1 个块改成 4 个连续块 | 块大小随 granule 变（`ARM_TT_L2_SIZE`：16K→32MB，4K→2MB）。面板缓冲是 `0x90001000 + 0x5EEC00` = 5.9MB，一个 2MB 块盖不住；四块覆盖 `0x90000000..0x907FFFFF`，顺带盖住 `0x905D6700` 的进度块色带 |

链基址随之变成 `0xfffffff007004000`（-64GB），**正是 santoni 的布局**；payload 的
`virt_base` 由 `mkpayload` 从 Mach-O `__TEXT` 算出来，变成 `0xfffffff006000000`，
与 santoni 的 payload 头逐字段同型。shim 和工具里没有任何内核 VA 硬编码，全部自适应。

验证：

```sh
tools/mkpatch_mido_4k.py     # 从 out/mido4k-src（CI 树）+ out/p101 导出 diff，改完逐字节回放
tools/check_mido_4k_asm.py   # 抽出四块映射单独汇编，核 4 条 str 偏移 0/8/16/24 + 2MB 步进
```

这一步之后预期：底部 `白 绿 黄 洋红`（常量 V=P 生效 = MMU 真的开了），随后 0098 的
早期控制台开始出字（因为 MMU 通了，内核终于能走到 `arm_vm_init`）。


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
tools/mkpatch_mido_early.py             # 生成 0098：合成树导出 diff + 逐字节回放
tools/check_early_asm.py                # 把 start.s 里两段进度块单独汇编
tools/mkpatch_mido_mmu_trace.py         # 生成 0099：splash V=P 映射 + 洋红/青块 + 带编号的异常 painter
tools/check_mmu_trace_asm.py            # 单独汇编向量表/painter，校验 16 个槽位可跳
tools/mkpatch_mido_mmu_const.py         # 生成 0100：两个后置进度块改常量几何 + dsb
tools/check_const_crumbs.py             # 抽出 0100 的两块单独汇编（自带 MOV64 宏）
tools/mkpatch_mido_crumb_color.py       # 生成 0101：第五块颜色改回青（字节序修正）
tools/mkpatch_mido_4k.py                # 生成 0102：4KB 页 + 经典 iOS 链基址 + splash 四块映射
tools/check_mido_4k_asm.py              # 单独汇编 0102 的四块映射，核偏移/步进编码
xnu/patches-mido/0094-mido-display-scaling.patch   # iPhone X 画布按高度缩放进 1080×1920
xnu/patches-mido/0095-mido_board.patch             # bsd/dev/mido_board.c（板型判定）
xnu/patches-mido/0095-mido_emmc.patch              # bsd/dev/mido_emmc.c（eMMC 门禁 stub）
xnu/patches-mido/0095-mido_gpu.patch               # bsd/dev/mido_gpu.c（Adreno 506 门禁 stub）
xnu/patches-mido/0096-mido-fbcon-24bpp.patch       # fbcon 读 ADT depth、支持 24bpp 绘制（自建内核用）
xnu/patches-mido/0097-mido-early-console.patch     # 引导期控制台：映射 mido 开屏、按 24bpp 画
xnu/patches-mido/0098-mido-early-console.patch     # 控制台延后到最终映射；start.s 加两段 MMU 前进度块
xnu/patches-mido/0099-mido-mmu-trace.patch         # splash 的 V=P 映射 + MMU 后洋红块 + 异常红块 painter
xnu/patches-mido/0100-mido-mmu-const-crumbs.patch  # 后置进度块改常量几何（不读 boot_args）+ dsb
xnu/patches-mido/0101-mido-crumb-color.patch       # 第五块颜色 0x0000FFFF→0x00FFFF00（整字写是小端）
xnu/patches-mido/0102-mido-4k-pages.patch          # A53 无 16K granule：改 4KB 页 + 非 large-memory 链基址
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
