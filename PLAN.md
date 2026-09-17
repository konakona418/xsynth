# Xsynth FPGA 实施计划

## 1. 项目目标

Xsynth 是面向 Tang Nano 9K、后续可移植到 Tang Nano 20K 的软硬件协同合成器平台。

- 主机通过 USB-UART 上传程序、发送实时事件并读取状态。
- PicoRV32 负责协议解析、voice allocation、参数管理和 sequencing。
- 独立 synth engine 负责 oscillator、wavetable、ADSR、mixer、filter 和后续 effects。
- CPU 不参与逐 sample DSP；所有实时音频计算由 FPGA 数据平面完成。
- 初期输出为 48 kHz、16-bit stereo HDMI audio，后续可增加 I2S 等 transport。
- 最终提供 `C/C++ intrinsic -> LLVM IR -> Xsynth custom instruction -> PicoRV32 PCPI -> synth command` 的完整纵向链路。

## 2. 已确认的架构决策

| 主题 | 决策 |
| --- | --- |
| 初期目标板 | Tang Nano 9K；板级逻辑保持可移植，20K 后续支持 |
| HDMI 实现 | Vendor `hdl-util/hdmi` SystemVerilog 源码，以 Amaranth `Instance` 封装 |
| 视频模式 | 640x480@60.00 Hz |
| HDMI 时钟 | 单个 Gowin rPLL：CLKOUT=126 MHz（TMDS x5）、CLKOUTD=126/5=25.2 MHz（pixel）；PFD=9 MHz、VCO=1008 MHz；第二个 PLL 保留 |
| 音频时钟 | 25.2 MHz / 525 = 48 kHz，和 pixel clock 精确锁定 |
| ACR 参数 | 48 kHz、25.2 MHz 下 N=6144、CTS=25200 |
| HDMI 核心前端 | 用 Yosys `read_slang`（slang）编译 hdl-util 的 SystemVerilog；`read_verilog -sv` 无法解析其 unpacked array 端口，且 `read_slang` 必须用 `-j 1`（WebAssembly 下多线程解析崩溃） |
| HDMI 引脚电气标准 | Tang Nano 9K 的 HDMI 引脚是 emulated LVDS；自行实例化 `ELVDS_OBUF`，板文件 `IO_TYPE` 改为 `LVCMOS33D`（Amaranth 默认的 `TLVDS_TBUF` 会被 gowin_pack 拒绝） |
| 时钟约束 | Amaranth 的 Apicula 流程不向 nextpnr 传递时钟约束；额外生成 `xsynth_timing.sdc` 并通过 `--sdc` 传入，否则 nextpnr 回退到 12 MHz 默认值 |
| DSP 执行模型 | synth engine 运行在 25.2 MHz 域，以 48 kHz sample strobe 驱动，每 sample 有 525 个处理周期 |
| 控制域 | UART、PicoRV32 运行在板载 27 MHz 时钟域 |
| 控制/数据接口 | 27 MHz 控制域到 25.2 MHz音频域的异步 command FIFO |
| UART 成帧分工 | 成帧与 CRC 留在硬件（Phase 2 已验证）；固件只负责 payload 语义（loader、voice allocation、sequencing），经 PCPI 写 command FIFO |
| RISC-V 核心 | PicoRV32，未来通过 PCPI 接入 Xsynth custom instruction |
| 程序执行 | 主机上传 RV32 机器码到 BRAM，由 PicoRV32 直接执行，不增加解释器 |
| 事件时序 | 命令携带样本时间戳，synth engine 按 sample counter 精确触发 |
| 内部数值格式 | 16-bit 样本、24-bit 数据通路、32-bit 累加器，最终饱和/舍入到 16-bit stereo |
| DDS | 32-bit phase accumulator；Phase 1 使用 4096x16 sine wavetable |
| 9K 默认配置 | 参数化设计；默认 8 voice、4 种波形、每张 2048x16 wavetable |
| Filter | **不做**。改为将来用 band-limited wavetable（mipmap）从源头消除混叠，见 Phase 4b |
| 复位策略 | PLL lock 后释放系统复位；HDMI 视频持续运行；synth/FIFO 支持独立软复位 |
| Host 工具 | Python CLI，使用和 Amaranth 相同的 `uv` 环境 |
| 验证策略 | Amaranth Python Simulator 为主，复用 `hdl-util/hdmi` 自带 Verilator 测试，上板做阶段验收 |
| 仓库名称 | Python 项目名由 `rv32i-soft` 改为 `xsynth` |
| Compiler 路线 | 维护 Xsynth LLVM fork，在 fork 中实现 intrinsic、IR lowering 和 RISC-V custom instruction backend 支持 |

## 3. 时钟域与数据流

```text
                           27 MHz control domain
USB-UART -> parser/loader -> PicoRV32/firmware/PCPI
                                  |
                                  v
                         async command FIFO
                                  |
                                  v
                    25.2 MHz audio/pixel domain
                    timestamp scheduler
                         synth engine
                             |
                    48 kHz stereo samples
                             |
                  hdl-util HDMI packet engine
                             |
               OSER10 + differential output buffers
                             |
                            HDMI
```

### 3.1 PLL 规划

实际实现使用**单个** rPLL，第二个 PLL 保留给后续需求：

- `CLKOUT` = 27 MHz × 14 / 3 = 126 MHz → `clk_pixel_x5`
- `CLKOUTD` = 126 MHz / 5 = 25.2 MHz → `clk_pixel`
- VCO = 126 × 8 = 1008 MHz（400–1200 MHz）
- PFD = 27 / 3 = 9 MHz（3–400 MHz）

25.2 MHz 不能由 27 MHz 直接整数生成（化简比为 14/15，会要求 PFD=1.8 MHz），
但用 `CLKOUTD` 分频 126 MHz 可以精确得到，且两个时钟同源、相位相关。
参数与约束记录在 `xsynth/hdl/clock.py`，并由 `tests/test_clock.py` 校验。

`clk_audio` 是 `clk_pixel` 的 525 分频（262 高 / 263 低）；HDL 只用上升沿，
因此占空比不影响，频率精确为 48 kHz。

### 3.2 Clock-domain crossing

- 27 MHz 与 25.2 MHz 无固定相位关系，控制命令必须经过真正的异步 FIFO。
- FIFO 使用 Gray-code pointer 同步，不允许直接同步多位 payload。
- 48 kHz sample clock/enable 由 25.2 MHz 整数分频产生，因此音频 sample timing 与 HDMI pixel timing 同源。
- HDMI 视频不得因 synth reset、FIFO reset 或程序重新加载而中断。

## 4. 命令、程序和时序模型

UART 是统一的二进制控制通道，支持三类操作：

1. Loader：写入 BRAM、设置入口地址、运行、停止和 CPU reset。
2. 控制与状态：读取 FIFO 水位、错误标志、版本及运行状态。
3. 实时事件：`NOTE_ON`、`NOTE_OFF`、`SET_FREQ`、`SET_WAVE`、`SET_ENV`、`SET_FILTER` 等。

Phase 2 只实现实时事件到 command FIFO 的路径。Phase 3 增加 loader 和 PicoRV32。实时演奏可以直接发送事件；离线 sequence 或复杂自动化可以上传程序执行。

内部命令编码由 UART、command FIFO、PCPI custom instruction 共用，Phase 2 已冻结为
**64-bit 命令字**（`xsynth/protocol.py`）：

```
bits 63..56  opcode
bits 55..48  voice
bits 47..16  value
bits 15..0   delay，单位 48 kHz sample，相对上一条命令生效时刻
```

选择相对 delay 而不是绝对 timestamp：队列按序消费，引擎不需要时间基准，
host 可以一次性下发整段 sequence，每条命令在上一条生效后经过 `delay` 个 sample
生效（0 和 1 都表示下一个 sample）。绝对时间戳若要引入，应由 Phase 3 firmware
把绝对时间换算成相对 delay 后下发，而不是改动命令字。

UART 上的帧格式为 `AA 55 | LEN | PAYLOAD | CRC16-LE`（CRC-16/CCITT-FALSE 覆盖
LEN 与 payload），payload 首字节为 packet type。

## 5. 仓库结构

计划采用以下布局：

```text
xsynth/
  cli.py           # 命令行入口
  design.py        # 阶段组合、build/simulate 驱动
  hdl/             # Amaranth RTL: clocks, DDS, synth, FIFO, UART, SoC glue
  platform/        # Tang Nano 平台、Gowin primitive wrapper、toolchain patch
  sim/             # Amaranth 仿真辅助代码
  sv/              # Xsynth 自有的 SystemVerilog 胶水代码
  host/            # UART loader、实时控制和调试 CLI
  sw/              # PicoRV32 firmware、linker script、runtime
  third_party/     # hdl-util/hdmi、PicoRV32 及对应 LICENSE
tests/             # Python 单元/集成测试
PLAN.md
README.md
```

顶层 CLI 目标形式：

```bash
uv run xsynth sim --phase 1
uv run xsynth build --board tang-nano-9k --phase 1
uv run xsynth program --board tang-nano-9k
uv run xsynth host upload program.bin
```

不同阶段使用不同 top-level composition，但 PLL、HDMI wrapper、FIFO、DDS 等模块必须复用，不为每个阶段复制实现。

## 6. 分阶段实施

### Phase 0：HDMI/工具链去风险

目标是尽早验证项目最大的未知项，不实现 synth 功能。

工作项（已完成，见下方“Phase 0 实际结果”）：

- 安装并锁定 `uv`/YoWASP 工具链。
- Vendor `hdl-util/hdmi` 及其许可证。
- 编写 Gowin PLL、OSER10 和差分输出薄封装。
- 先以 `DVI_OUTPUT=1` 实例化 HDMI core，输出固定彩条或渐变。
- 验证 Yosys 对 hdl-util SystemVerilog 的兼容性，重点检查 `parameter real VIDEO_RATE`、unpacked array 和 SystemVerilog cast。
- 验证 PLL 锁定、OSER10 放置、差分输出和 nextpnr 时序收敛。

验收门：

- Tang Nano 9K 上稳定显示 640x480@60 图像。**（已通过：接显示器有画面）**
- PLL lock、nextpnr timing 和资源报告可检查。**（已满足）**
- HDMI 拔插不要求自动恢复；重新上电后必须稳定工作。**（待复测）**

**根因（已定位并修复）**：HDL 与 bitstream 一直是对的；问题出在 HDMI 引脚的
`IO_TYPE`。`gowin_pack` 对 `ELVDS_*` buffer 的默认 I/O 标准是 `LVCMOS33D`
（`_default_iostd['ELVDS_TBUF']`），但 CST 里任何 `IO_TYPE` 都会覆盖它。
板文件带 `IO_TYPE=LVCMOS33`，于是 IOB 被配成普通单端输出、差分输出模式未使能。
在 `patch_tang_nano_9k_hdmi_resource` 中**删除**该属性（不覆盖 `IO_TYPE`）后，
显示器立即出图。这一条也是排查过程中最反直觉的地方：netlist、OEN、时钟连接、
`ELVDS_IOBUF` 放置全部正确，唯独 I/O 标准被悄悄改掉。

排查过程中排除的项（供以后参考）：

- 时钟方案（`CLKOUTD` vs `CLKDIV`）、`DRIVE=8 PULL_MODE=NONE`、
  TMDS 时钟直连 `clk_pixel` 或经 OSER10 对齐、DVI 与 HDMI 模式、
  640x480 与 1280x720、`ELVDS_OBUF` 与 `ELVDS_TBUF`、CST 单端口双引脚形式
  （nextpnr 不接受，会报 Unconstrained IO）——均非根因。
- 采集卡与第三方预构建 bitstream 的对照实验一度误导了方向；最终以显示器直连
  作为判定依据。

Phase 0 实际结果：

- `read_verilog -sv` 无法解析 hdl-util 的 unpacked array 端口（例如
  `logic [55:0] sub [3:0]`）。改用 Yosys 的 **slang 前端**（`read_slang`）编译这些
  SystemVerilog 源码，并预加载 `+/gowin/cells_sim.v` 以解析 `OSER10`。
  `read_slang` 必须加 `-j 1`，否则在 WebAssembly 下多线程解析崩溃。
- slang 拒绝 hdl-util 中两处阻塞/非阻塞赋值混用；通过
  `xsynth/platform/hdmi_patch.py` 在构建期打补丁，vendored 源保持原样。
- slang 支持 `parameter real VIDEO_RATE`，因此**不需要**整数化 N/CTS。
- HDMI 引脚是 **emulated LVDS**：Amaranth 默认的 `TLVDS_TBUF` 被 gowin_pack 拒绝，
  改为自行实例化 `ELVDS_OBUF` 并把板文件 `IO_TYPE` 改为 `LVCMOS33D`。
- 单 PLL 同时产生 126 MHz 与 25.2 MHz（`CLKOUTD`），第二个 PLL 保留。
- Amaranth 的 Apicula 流程不向 nextpnr 传时钟约束；补充 `xsynth_timing.sdc`
  并通过 `--sdc` 传入，否则 nextpnr 回退到 12 MHz 默认值并给出无意义的 PASS。
- DVI 模式下 HDMI core 不含 data-island/audio 逻辑，资源占用约
  **334 LUT4、258 ALU、0 BSRAM、3 OSER10、1 CLKDIV**，`clk_pixel` 时序收敛。
- 已验证 bitstream 的 netlist 正确：`OSER10.Q → ELVDS_IOBUF.I` 连通、
  `OEN = 1'b0`（输出使能）、TMDS 时钟 buffer 由 `clk_pixel` 驱动。

参考实现（都已找到并比对）：

- `muzhiyun/Tang_9K_HDMI`：**同样 vendor 了 hdl-util/hdmi 并跑在 Tang Nano 9K 上**，
  使用 `Gowin_rPLL` + `Gowin_CLKDIV /5`、`OSER10` ×3、`ELVDS_OBUF` ×4，
  `assign tmds_clock = clk_pixel`，CST 为 `IO_LOC "tmds_d_p[0]" 71,70;`
  （单端口双引脚、`PULL_MODE=NONE DRIVE=8`、不设 `IO_TYPE`）。我们的设计与之等价。
- `zf3/some-tang-nano-9k-examples` 的 `01.hdmi`：SVO core，1280x720，同样的
  PLL+CLKDIV+OSER10+ELVDS_OBUF 结构。
- `joachimdraeger/vic64-t9k`：C64 VIC-II，9K HDMI，CST 额外显式设置
  `IO_TYPE=LVCMOS33D ... BANK_VCCIO=3.3`。
- 以上参考**全部使用 Gowin IDE**（`gw_sh`/`.gprj`），而非 Apicula/nextpnr。

**采集卡对照实验（记录，非结论）**：排查期间，采集卡（MACROSILICON C1-1 USB3，
V4L2）对 FPGA 输出只给出平坦的 limited-range 黑。同一张卡接笔记本正常出图，
第三方预构建 bitstream（`Khadija411/HDMI` 的 `HDMI.fs` / `HDMI_take2.fs`）同样无信号。
这些现象一度指向物理链路（HDMI +5V/HPD），但最终证明是 `IO_TYPE` 配置问题
（见上方根因）；显示器直连才是可靠的判定手段。采集卡仍可用于自动化截图验证，
但不应作为唯一判据。

回退方案：

- 若 sink 兼容性无法解决，评估 720x480@59.94、27/135 MHz 路线。
- 只有 hdl-util serializer 无法适配时，才单独替换 serializer；不重写 data-island/audio packet engine。

### Phase 1：固定测试音 HDMI 输出

工作项：

- 生成 48 kHz sample strobe/clock。
- 实现可复用的 32-bit DDS phase accumulator。
- 实现 4096x16 sine wavetable BRAM。
- 生成固定 440 Hz stereo sample，并接入 hdl-util HDMI audio 输入。
- 保留视频测试图案，并增加可见移动元素确认链路仍在运行。
- 为 phase increment、wavetable addressing、sample rate divider 和饱和逻辑编写 Python 仿真。

验收门：

- 显示器稳定出图。
- HDMI sink 播放稳定的 440 Hz 音频。
- 输出格式为 48 kHz、16-bit stereo。
- DDS 不是一次性测试模块，后续阶段继续复用。

Phase 1 实际结果：

- `clk_audio` 是 **fabric 分频器**（pixel 域寄存器，`pixel/525`），不是专用时钟网络。
  这与已知可用的 9K 参考实现做法一致，nextpnr 能正常把它当时钟布线
  （报告 Fmax 322 MHz）。已在 SDC 中补 `create_clock`，否则它的 PASS 也是
  12 MHz 默认值下的无意义结果。
- DDS 在 `pixel` 域由 `audio_strobe`（counter == 524 的组合脉冲）推进，全设计
  **只有一个时钟**。相位累加器 32-bit，取高 12 位查 4096x16 表。
- 正弦表落在 **4 个 BSRAM（`SP`）**；`synth_gowin` 能正确导入 `$meminit`。
  总资源 502 LUT4、388 ALU、4 BSRAM、3 OSER10、1 CLKDIV、1 rPLL；
  `clk_pixel` 布线后 Fmax 128.5 MHz（目标 25.2 MHz）。
- **关键坑：HDMI L-PCM 是带符号的。** hdl-util 把 16-bit 输入**零扩展**到 24-bit
  再左对齐，所以输入必须是**二进制补码位型**。若按 offset-binary（无符号、
  以 `0x8000` 为直流）给样本，sink 读到的是一个巨大直流偏置 + 类似方波的波形：
  基频仍然正确（440 Hz），但波形畸变、充满奇次谐波（1/n），极易误判为
  “采样率错 3 倍”。修复后实测峰值 32767、RMS 23051（≈32767/√2）、单峰 440 Hz。
- 采集卡（MACROSILICON C1-1）的 USB 音频输入默认在 PipeWire 里是 **muted**，
  抓到的会是**全零**而非噪声；录音前先 `pactl set-source-mute ... 0`。

### Phase 2：UART 到 DDS 最小控制链路

工作项：

- 实现 27 MHz UART RX/TX 和二进制 framing。
- 实现带 CRC 的帧解析、错误恢复和状态回报。
- 实现异步 command FIFO。
- 实现最小命令集合：`NOTE_ON`、`NOTE_OFF`、`SET_FREQ`、`SET_WAVE`。
- 在 25.2 MHz 域按 sample 边界应用命令。
- 实现 Python host CLI 发送命令和查询状态。

验收门：

- 主机可实时开关音符、改变频率和波形。
- FIFO 在随机相位、burst 和满/空边界下通过仿真。
- malformed frame 不会破坏后续合法帧解析。

Phase 2 实际结果：

- 帧格式 `AA 55 | LEN | PAYLOAD | CRC16-LE`，payload 首字节为 packet type
  （`0x01` 命令、`0x02` ping、`0x03` status）；响应为 `0x81` pong、`0x82` status、
  `0x83` error。解码器先缓冲 payload、校验 CRC 通过后才释放，所以坏帧永远进不了
  command FIFO；重同步靠下一个 `AA 55`。
- 命令字即上文冻结的 64-bit 编码。`delay` 的语义是「距上一条命令生效的 sample
  数」，因此不需要绝对时间基准。
- async FIFO：Gray 指针、64-bit × 16、写侧 27 MHz、读侧 25.2 MHz。读口是
  **组合读**，所以是 first-word-fall-through，scheduler 可以在命令到期的那一个
  `audio_strobe` 边沿直接取用。占用度在写侧由同步过来的读指针算出，status 不需要
  额外的跨域握手。
- voice：4 张 2048 点波表（sine/saw/square/triangle）拼成一个 8192x16 存储，
  地址为 `wave * 2048 + phase[31:21]`；16-bit 幅度门、32-bit 累加器。波表是朴素
  实现（saw/square 有混叠），真正的 oscillator 留给 Phase 4。
- 错误位在 status flags 中保持（bit0 CRC、bit1 length、bit2 FIFO overflow、
  bit3 bad command、bit4 unknown packet、bit5 PLL lock），只有 `OP_RESET` 清除。
- `Phase2Core` 抽出了除时钟/引脚/HDMI 之外的全部逻辑，因此整条链路可以直接仿真
  （`xsynth sim --phase 2`）。
- 资源：**2248 LUT4 (26%)**、616 ALU、1387 DFF (21%)、**8 BSRAM (30%)**、
  18 RAM16SDP4（FIFO 与帧缓冲）、1 MULT18X18（幅度）、1 rPLL、1 CLKDIV。
  布线后 `clk`(27 MHz) 86.96 MHz、`clk_pixel` 87.73 MHz、`clk_pixel_x5` 与
  `clk_audio` 均 PASS。27 MHz 控制时钟此前没有 `create_clock`，nextpnr 是按
  12 MHz 默认值报告的；已补上。
- **已上板验证**：控制 UART 是板载调试器（`0403:6010` "Sipeed JTAG Debugger"，
  FT2232 克隆）的 interface 1，即 **`/dev/ttyUSB1`**（interface 0 是 JTAG）。
  `ping` → pong，`status` → version 2 / locked / errors none / sample counter 递增；
  `note-on`、`freq`、`wave`、`amp`、`note-off` 均实时生效，画面全程不中断。
  采集卡实测：440 Hz 正弦为单峰、各次谐波 0.000、稳态 RMS 23167（满幅理想值）；
  300 Hz 方波在 900/1500/2100 Hz 为 0.349/0.183/0.151（即 1/3、1/5、1/7，偶次为 0）。

### Phase 3：PicoRV32 SoC 与程序上传

拆成 3a（SoC 跑起来）和 3b（CPU 进入命令路径）。

工作项：

- Vendor PicoRV32 及许可证。
- 集成 program/data BRAM、UART、timer、command FIFO MMIO 和基本状态寄存器。
- 定义 SoC memory map、linker script 和启动代码。
- 扩展 UART 协议以支持 BRAM 写入、入口设置、RUN/STOP/RESET。
- 由 firmware 负责 UART 协议解析、voice allocation 和参数管理。
- 接入 PCPI，并先实现最小 custom opcode 到内部命令编码的转换。
- Host 上传含 custom opcode 的 RV32 程序，PicoRV32 直接执行。

验收门：

- 主机可上传、启动和停止程序。
- PicoRV32 程序可通过 command FIFO 控制 DDS。
- PCPI backpressure 在 FIFO 满时行为明确，不静默丢失命令。

#### Phase 3a 实际结果（已上板验证）

- Memory map：`0x0000_0000` 起 8 KB program/data BSRAM；`0x1000_0000` 起为 MMIO
  —— `+0x00` status、`+0x04` 自由计数器、`+0x08` control（写 bit0 停机）、
  `+0x0C` 48 kHz 采样计数。
- 复位向量固定为 0（`PROGADDR_RESET=0`），所以不需要 SET_ENTRY：loader 总是把
  镜像写在 0，linker script 也把 `_start` 放在最前。
- Loader 是硬件（引导阶段没有 CPU 可用）。它只认 `PKT_LOAD` / `PKT_RUN`，按
  payload 的字节下标定位字段；CRC 已由硬件解码器验证过。越界的 load 被拒绝而
  不是回绕。
- 成帧与 CRC 仍在硬件，CPU 只做语义 —— 见 §2 决策表。
- 协议 `VERSION` 升到 3：status reply 17 字节，增加 CPU flags、固件 scratch
  寄存器和自由计数器。
- 固件用 clang（`--target=riscv32-unknown-elf -march=rv32i`）+ `ld.lld` +
  `llvm-objcopy` 构建；C 寄存器头由 `xsynth.hdl.soc` 生成，避免两边漂移。
- 验证：`xsynth/sim/soc.py` 通过 iverilog 跑**真实 PicoRV32 + 真实固件**；
  板上 `load` 后 `cpu_stat` 读到 `0x12345678`，`run --stop` / `run` 都生效。
- 资源：4504 LUT4 (52%)、2443 DFF (37%)、12/26 BSRAM；整次构建约 5m45s
  （CPU 让 LUT 翻倍，nextpnr 布局器对密度超线性）。

#### Phase 3b 实际结果（已上板验证）

- **命令路径改走 CPU**：UART → 硬件解码/CRC → `FrameMailbox` → 固件解析 →
  PCPI custom instruction → command FIFO → scheduler → voice。
- `xsynth/hdl/pcpi.py`：custom-0 指令 `xsynth.push {rs2, rs1}`（funct3=0）把
  64-bit 命令写入 FIFO；FIFO 满时断言 `pcpi_wait` 停住 CPU，否则 `pcpi_ready`
  完成。funct3=1 读回 FIFO level。
- `xsynth/hdl/mailbox.py`：只转发 `PKT_COMMANDS`；每帧用 header word 宣告
  （bit15 标志、bits 13:8 body 长度、bits 7:0 packet type），body 逐字节跟随。
  type 放进 header 而不是单独占一个字节，避免固件和硬件对「type 算不算长度」
  产生分歧。
- `PacketHandler(forward_commands=True)`：Phase 3 下 handler 只做长度校验，
  不再写 FIFO（只有一个驱动源）。
- 新 MMIO：`+0x10` RX_DATA（读弹出、写 flush）、`+0x14` RX_STATUS
  （`{frames, overflow, empty}`）、`+0x18` REG_COMMANDS（固件转发的命令数）。
  `REG_STATUS` 开机写入 `0x5853594e`（"XSYN"）作为固件身份。
- 验证：`xsynth/sim/phase3.py` + `tests/test_phase3.py` 在 iverilog 下跑完整链路；
  `tests/test_pcpi.py` 在 depth=4 的 FIFO 上直接验证「满则 stall、不丢命令」。
  板上 `load` 后 `cpu_stat=0x5853594e`，`note-on --hz 440 --wave saw` 采集到
  440.1 Hz、谐波 1/n（0.516/0.336/0.256/0.203…）的锯齿波。
- 测试总数 111。
- 资源：4833 LUT4 (55%)、2542 DFF (39%)、12/26 BSRAM；co-processor + mailbox
  约比 3a 多 330 LUT / 100 DFF，构建时间不变（约 6 分钟）。

#### Phase 3b 的两个真坑（已写入 HANDOFF gotchas）

- MMIO 读有两个周期：`address_phase` 是地址周期，CPU 在下一拍 `ready` 才采样
  `mem_rdata`。读时「产生副作用」的寄存器（弹出 FIFO）必须作用在 `ready` 上，
  否则每次读都返回相邻项 —— 症状像成帧 bug，其实不是。
- strobe 寄存器必须有显式默认值，否则会永久锁存为 1；默认赋值要写在条件块
  **之前**（Amaranth 同域内后写覆盖先写）。

### Phase 4：完整基础 synth engine

工作项：

- 参数化 voice 数、phase width、wavetable size 和 sample width。
- 9K 默认实现 8 voice、4x2048x16 wavetable。
- 在 525 cycles/sample 内时分复用 oscillator、envelope 和 mixer 资源。
- 实现 ADSR、wave selection、voice allocation 所需状态。
- 实现 24-bit mix path、32-bit accumulator 和 16-bit 饱和输出。
- ~~加入基础 filter，优先从单个全局 biquad 开始，再评估 per-voice filter。~~ 见下方决策。
- 记录 LUT、FF、BSRAM、DSP 和 timing 使用量。

验收门：

- 8 voice 复音、ADSR、多个 waveform ~~和 filter~~ 均可实时控制。
- 每个 sample deadline 内完成全部运算。
- 9K 资源与时序留有明确余量；无法满足时再缩减默认配置或启动 20K 移植。

#### Phase 4a 实际结果（已构建、已仿真，尚未上板）

拆成 4a（多 voice 引擎 + ADSR + mix）和 4b（filter + 固件 voice allocation）
两步走。4a 完成。

`VoiceBank` 取代了单 voice 的 `WavetableVoice` + `VoiceControl`。8 个 voice
共用一条 BSRAM 读口、一个乘法器和一个累加器：**每个 voice 两个 pixel clock，
8 个共 16 个**，占 48 kHz 下 525 周期的 3%。voice 状态（phase/step/wave/env/
stage/level）各自是寄存器，读经 `Array` 多路选择，回写经 `Switch`。

命令不在到达当拍生效，而是**停在 pending 寄存器里，等轮转走到它指名的
voice 再应用**——那是该 voice 唯一不被回写的时刻，两个写者因此不用互相
stall。轮转走完仍未被消费的命令（voice 越界）在轮转结束时丢弃。

数值设计：

- envelope 设置（attack/decay/sustain/release rate）**全局**，envelope 状态
  （level、stage）**per voice**——这是合成器的常规做法，面板改包络时每个音
  仍保持自己的形状。
- velocity 是**包络的峰值**，不是包络之上的增益：轻音在整个衰减过程中都轻。
  decay 的下限是 sustain 电平，且被该音自己的峰值封顶，所以轻音不会涨上去。
- envelope 累加器 24-bit，输出取高 16 bit。多出的 8 bit 小数使 16-bit rate
  对应的时间范围从约三分钟到瞬时。
- mix 32-bit 累加，乘 master 后**饱和**到 16-bit。8 个满幅 voice 相加远超满
  幅，饱和而不是回绕；`master` 是 host 让和弦不越界的手段。

命令扩展（`xsynth/protocol.py`）：`OP_SET_ATTACK/DECAY/SUSTAIN/RELEASE`、
`OP_SET_MASTER`；`OP_SET_AMP` 语义改为「该音的 level」。host 负责把秒换成
rate（`XsynthClient.envelope_rate`）、把 0..1 换成电平（`level_for`）。

仿真覆盖：`tests/test_voice.py` 直接驱动 voice bank 并用精确算术核对每个
包络阶段；`tests/test_phase2.py` 通过整条 UART 路径发送和弦与按 voice 寻址的
note-off；`xsynth sim --phase 2` 打印三音和弦与包络。

实测（`build/top.tim`）：**6648 LUT4 (76%)、1426 ALU (22%)、3566 DFF (55%)、
12/26 BSRAM**。比 3b 多约 1815 LUT4、1024 DFF。`clk_pixel` 最高 59.09 MHz
（只需 25.2），`clk` 66.98 MHz（只需 27），余量充足。构建时间不变，约 6 分钟。

#### Phase 4b 决策：不做 filter

原计划是「优先从单个全局 biquad 开始」。做完 4a 后决定**不做**，理由：

1. **filter 是治标。** 我们的 saw/square 是 naive 表，混叠才是根本问题。filter
   只是把混叠压下去，而 **band-limited wavetable（mipmap）**——按音高选不同
   谐波数的表——是从源头消除，音质收益更大，而且不需要用户去拧截止频率。
2. **filter 是项目论点上最弱的一项。** 它不展示新的 co-design 想法，只是往
   已经证明过的时分复用槽里再塞 DSP。8 个 voice 塞进 525 周期的 3% 已经证明
   了资源可复用；filter 是同一论点的重复。而固件的 voice allocation 和
   sequencing 展示的是**软核在做决策**，那才是这个项目真正要说的事。
3. **成本不是问题，风险才是。** 全局 filter 只需约 400 LUT4（6648 → 约 7050，
   82%），装得下；但定点 SVF 的稳定性、系数量化和极限环是真实风险，而且难以
   靠听感验证。
4. PLAN 里那句「和 filter 均可实时控制」是**写计划时**写的，那时还不知道难点
   在哪、也不知道 naive 表会混叠。它是 checkbox，不是需求。

所以 Phase 4b 改为：**固件的 note-to-voice 分配器与 sample-accurate
sequencing**。band-limited wavetable 推迟到 Phase 6 之后——那时有硬件乘法器
可用（Xsynth ISA / LLVM fork），正好是它该在的位置。

固件目前只做一对一转发，不做任何决策。

### Phase 5：Sample-accurate sequencer

工作项：

- 实现单调递增 sample counter。
- 命令支持绝对 timestamp 或相对 delay。
- synth 端只在目标 sample 应用事件。
- firmware 实现 sequencer/MIDI-like event scheduling，并提前填充事件队列。
- 定义晚到事件、队列满、时间戳回绕和 synth reset 后时间基准的语义。

验收门：

- 事件在指定 sample 触发，不受 CPU 中断或 UART 抖动影响。
- 长时间 sequence 不出现可测量漂移。
- late event 和 queue overflow 行为可观察且可测试。

### Phase 6：Xsynth ISA 与 LLVM fork

在 FPGA SoC 和 synth engine 稳定后开始 compiler 工作，不允许该阶段反向阻塞前五阶段。

工作项：

- 冻结 Xsynth custom opcode、operand、返回值、FIFO 满时的 stall/error 行为和 memory ordering 语义。
- 在 PicoRV32 PCPI decoder 中实现正式 Xsynth 指令集。
- 创建并维护 Xsynth LLVM fork。
- 在 LLVM fork 中定义 intrinsic、TableGen instruction、feature/extension flag、instruction selection/lowering、assembler/disassembler 和 MC tests。
- 提供 C/C++ intrinsic header，生成 Xsynth custom instruction。
- 增加 compiler regression tests 和 FPGA 端端到端测试。

验收门：

```text
C/C++ intrinsic
  -> LLVM IR intrinsic
  -> RISC-V Xsynth instruction
  -> PicoRV32 PCPI decode
  -> command FIFO
  -> timestamp scheduler
  -> synth engine
```

正式路线只维护 LLVM fork，不把 `.insn`、独立 assembler 或 inline raw encoding 作为公开编程接口。开发早期允许临时使用原始指令编码做硬件冒烟测试，但必须在 Phase 6 完成后移除或限制在测试代码中。

## 7. 验证要求

### 7.1 单元测试

- PLL/divider 参数计算。
- DDS phase increment、回绕、频率精度和 wavetable addressing。
- fixed-point multiply、rounding、saturation 和 mixer overflow。
- UART framing、CRC、错误恢复。
- async FIFO 满/空、pointer wrap 和随机时钟比。
- timestamp compare、回绕、late event 和 reset。
- ADSR 各阶段转换和参数极值。

### 7.2 集成测试

- UART command -> FIFO -> DDS sample。
- PicoRV32 program -> MMIO/PCPI -> FIFO。
- 多 voice 在一个 sample budget 内完成。
- HDL 生成的音频样本与 Python reference model 对比。
- hdl-util 自带 Verilator 测试保持可运行。

### 7.3 板级验收

每个 Phase 必须有独立、可重复的 bitstream 和验收步骤。除听感外，还应通过 UART 状态、采样计数器、错误标志和综合报告提供可观察结果。

## 8. 资源预算原则

- Tang Nano 9K 的公开规格约为 8640 LUT4、6480 FF、26 个 18Kb BSRAM、10 个 18x18 DSP 和 2 个 PLL；以实际综合报告为最终依据。
- 2048x16 wavetable 约消耗 2 个 BSRAM；4 张表约消耗 8 个。
- PicoRV32、program/data BRAM、HDMI packet engine 和 FIFO 必须先保留预算。
- 默认配置不得以 100% 资源利用率为目标，需要给 routing 和后续状态/调试逻辑留余量。
- 复音数、wavetable 规模和 filter 复杂度通过参数调整；若 9K 无法满足完整默认档，优先降低配置，不改变模块边界。

## 9. 主要风险与处理顺序

1. `hdl-util/hdmi` 的 SystemVerilog/`real` 与 Yosys 兼容性：Phase 0 首先验证，必要时整数化常量。
2. Gowin `OSER10`、emulated-LVDS buffer 与开源 P&R：Phase 0 综合通过；**仍需上板验证**。
3. PLL 生成参数和时序：Phase 0 综合与时序报告通过；**仍需上板验证锁定**。
4. HDMI sink 兼容性：至少在两种不同显示设备上测试；不在早期实现 EDID。
5. 9K 资源不足：Phase 4 前持续记录综合结果，参数化降档，20K 作为后续目标。
6. Async FIFO/时间戳回绕错误：在上 CPU 前完成随机仿真与边界测试。
7. LLVM fork 维护成本：只在 ISA 语义和硬件接口稳定后启动，减少持续 rebase 的范围。

## 10. 非目标

在相应阶段到来前，以下内容不进入主线：

- HDMI hotplug/EDID；
- Tang Nano 20K 平台支持；
- I2S 输出；
- 高阶 effects；
- 完整 MIDI 物理接口；
- Xsynth LLVM backend；
- 自研 RISC-V CPU；
- CPU 逐 sample DSP。

任何新增功能不得破坏 command FIFO、timestamp scheduler、synth engine 和 output transport 之间的模块边界。
