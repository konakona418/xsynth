"""Phase 3: the PicoRV32 soft core, its program memory and its peripherals.

The CPU sees one flat 32-bit address space::

    0x0000_0000  program and data memory (BSRAM, ``mem_words`` words)
    0x1000_0000  memory-mapped registers, at the ``REG_*`` offsets below

Program memory has a single write port shared by the CPU and the host loader.
The loader wins and stalls the CPU. The host only ever loads while the CPU is
held in reset, but making the loader win anyway means a mistimed load can never
silently corrupt a running program.

``run`` is the host's "let the CPU out of reset" control. The firmware can halt
itself by writing :data:`REG_CONTROL`, which puts the CPU back into reset; the
host clears that by dropping ``run``, so a fresh run always starts from the
reset vector rather than immediately re-halting.
"""

from __future__ import annotations

from pathlib import Path

from amaranth import (
    Cat,
    ClockSignal,
    Elaboratable,
    Instance,
    Module,
    Mux,
    Signal,
    unsigned,
)
from amaranth.lib.memory import Memory

# Addresses, as the CPU sees them.
BRAM_BASE = 0x0000_0000
MMIO_BASE = 0x1000_0000

# Offsets inside the MMIO page.
REG_STATUS = 0x00    # scratch: the firmware writes, the host reads
REG_COUNTER = 0x04   # free running, so the host can see the CPU is alive
REG_CONTROL = 0x08   # write bit 0 to halt; reads back {run, halted}
REG_SAMPLES = 0x0C   # the 48 kHz sample counter, from the audio domain

DEFAULT_MEM_WORDS = 2048

PICORV32_SOURCE = (
    Path(__file__).resolve().parent.parent
    / "third_party"
    / "picorv32"
    / "picorv32.v"
)


def install_cpu_sources(platform) -> None:
    """Attach the vendored PicoRV32 Verilog to the build.

    It is plain Verilog, so Amaranth's ordinary ``read_verilog`` loop picks it
    up and no patch machinery is needed.
    """
    platform.add_file("picorv32.v", PICORV32_SOURCE.read_text())


class PicoRV32(Elaboratable):
    """The vendored PicoRV32 core as an Amaranth black box.

    Only the native memory interface is wired up. PCPI, interrupts and the
    look-ahead interface exist as ports because the module always has them, but
    nothing drives them yet; the core is built without the multiply, divide and
    compressed-ISA options so that it stays small.
    """

    def __init__(self, *, mem_words: int = DEFAULT_MEM_WORDS):
        self.mem_words = mem_words

        self.resetn = Signal(init=0)
        self.trap = Signal()

        self.mem_valid = Signal()
        self.mem_instr = Signal()
        self.mem_ready = Signal()
        self.mem_addr = Signal(32)
        self.mem_wdata = Signal(32)
        self.mem_wstrb = Signal(4)
        self.mem_rdata = Signal(32)

        self.pcpi_valid = Signal()
        self.pcpi_insn = Signal(32)
        self.pcpi_rs1 = Signal(32)
        self.pcpi_rs2 = Signal(32)
        self.pcpi_wr = Signal()
        self.pcpi_rd = Signal(32)
        self.pcpi_wait = Signal()
        self.pcpi_ready = Signal()

        self.irq = Signal(32)
        self.eoi = Signal(32)

    def elaborate(self, platform):
        m = Module()

        # The look-ahead and trace interfaces are unused; they need real
        # signals rather than open ports because Yosys objects to unconnected
        # outputs.
        look_ahead_read = Signal()
        look_ahead_write = Signal()
        look_ahead_addr = Signal(32)
        look_ahead_wdata = Signal(32)
        look_ahead_wstrb = Signal(4)
        trace_valid = Signal()
        trace_data = Signal(36)

        m.submodules.cpu = Instance(
            "picorv32",
            p_ENABLE_COUNTERS=1,
            p_ENABLE_COUNTERS64=1,
            p_ENABLE_REGS_16_31=1,
            p_ENABLE_REGS_DUALPORT=1,
            p_LATCHED_MEM_RDATA=0,
            p_TWO_STAGE_SHIFT=1,
            p_BARREL_SHIFTER=0,
            p_TWO_CYCLE_COMPARE=0,
            p_TWO_CYCLE_ALU=0,
            p_COMPRESSED_ISA=0,
            p_CATCH_MISALIGN=1,
            p_CATCH_ILLINSN=1,
            p_ENABLE_PCPI=0,
            p_ENABLE_MUL=0,
            p_ENABLE_FAST_MUL=0,
            p_ENABLE_DIV=0,
            p_ENABLE_IRQ=0,
            p_ENABLE_IRQ_QREGS=1,
            p_ENABLE_IRQ_TIMER=1,
            p_ENABLE_TRACE=0,
            p_REGS_INIT_ZERO=0,
            p_MASKED_IRQ=0,
            p_LATCHED_IRQ=0xFFFF_FFFF,
            p_PROGADDR_RESET=BRAM_BASE,
            p_PROGADDR_IRQ=0x0000_0010,
            p_STACKADDR=self.mem_words * 4,
            i_clk=ClockSignal("sync"),
            i_resetn=self.resetn,
            o_trap=self.trap,
            o_mem_valid=self.mem_valid,
            o_mem_instr=self.mem_instr,
            i_mem_ready=self.mem_ready,
            o_mem_addr=self.mem_addr,
            o_mem_wdata=self.mem_wdata,
            o_mem_wstrb=self.mem_wstrb,
            i_mem_rdata=self.mem_rdata,
            o_mem_la_read=look_ahead_read,
            o_mem_la_write=look_ahead_write,
            o_mem_la_addr=look_ahead_addr,
            o_mem_la_wdata=look_ahead_wdata,
            o_mem_la_wstrb=look_ahead_wstrb,
            o_pcpi_valid=self.pcpi_valid,
            o_pcpi_insn=self.pcpi_insn,
            o_pcpi_rs1=self.pcpi_rs1,
            o_pcpi_rs2=self.pcpi_rs2,
            i_pcpi_wr=self.pcpi_wr,
            i_pcpi_rd=self.pcpi_rd,
            i_pcpi_wait=self.pcpi_wait,
            i_pcpi_ready=self.pcpi_ready,
            i_irq=self.irq,
            o_eoi=self.eoi,
            o_trace_valid=trace_valid,
            o_trace_data=trace_data,
        )

        return m


class SoC(Elaboratable):
    """PicoRV32, its program memory and the memory-mapped registers."""

    def __init__(self, *, mem_words: int = DEFAULT_MEM_WORDS,
                 domain: str = "sync", init=None):
        if mem_words < 16:
            raise ValueError("the program memory needs at least 16 words")
        self.mem_words = mem_words
        self.domain = domain
        self.init = list(init) if init is not None else []

        # The loader port: the host writes the program through this.
        self.load_stb = Signal()
        self.load_addr = Signal(range(mem_words))
        self.load_data = Signal(32)

        # Control and observation.
        self.run = Signal()
        self.halted = Signal()
        self.trap = Signal()
        self.status = Signal(32)
        self.counter = Signal(32)
        self.samples = Signal(32)

    def elaborate(self, platform):
        m = Module()
        d = m.d[self.domain]

        cpu = PicoRV32(mem_words=self.mem_words)
        m.submodules.cpu = cpu

        addr_bits = (self.mem_words - 1).bit_length()

        memory = Memory(shape=unsigned(32), depth=self.mem_words, init=self.init)
        m.submodules.memory = memory
        read_port = memory.read_port(domain=self.domain)
        write_port = memory.write_port(domain=self.domain)

        ready = Signal()
        in_memory = Signal()
        address_phase = Signal()

        # Every access takes exactly one cycle: the address is presented on the
        # cycle mem_valid rises, and the data is ready on the next. A load
        # strobe postpones the CPU for that cycle.
        m.d.comb += [
            in_memory.eq(cpu.mem_addr < MMIO_BASE),
            address_phase.eq(cpu.mem_valid & ~ready & ~self.load_stb),
            cpu.mem_ready.eq(ready),
        ]
        d += ready.eq(address_phase)

        m.d.comb += [
            read_port.en.eq(cpu.mem_valid),
            read_port.addr.eq(cpu.mem_addr[2:2 + addr_bits]),
            write_port.addr.eq(
                Mux(self.load_stb, self.load_addr,
                    cpu.mem_addr[2:2 + addr_bits])
            ),
            write_port.data.eq(
                Mux(self.load_stb, self.load_data, cpu.mem_wdata)
            ),
            write_port.en.eq(
                Mux(self.load_stb, 1,
                    address_phase & (cpu.mem_wstrb != 0) & in_memory)
            ),
        ]

        offset = cpu.mem_addr[:8]
        halted = Signal()
        mmio_write = Signal()
        m.d.comb += [
            mmio_write.eq(address_phase & (cpu.mem_wstrb != 0) & ~in_memory)
        ]

        with m.If(mmio_write):
            with m.Switch(offset):
                with m.Case(REG_STATUS):
                    d += self.status.eq(cpu.mem_wdata)
                with m.Case(REG_CONTROL):
                    with m.If(cpu.mem_wdata[0]):
                        d += halted.eq(1)

        # Dropping run clears the halt, so the next run starts from the reset
        # vector rather than re-halting on its first store.
        with m.If(~self.run):
            d += halted.eq(0)

        m.d.comb += [
            self.halted.eq(halted),
            cpu.resetn.eq(self.run & ~halted),
            self.trap.eq(cpu.trap),
        ]

        d += self.counter.eq(self.counter + 1)

        mmio_read = Signal(32)
        with m.Switch(offset):
            with m.Case(REG_STATUS):
                m.d.comb += mmio_read.eq(self.status)
            with m.Case(REG_COUNTER):
                m.d.comb += mmio_read.eq(self.counter)
            with m.Case(REG_CONTROL):
                m.d.comb += mmio_read.eq(Cat(halted, self.run))
            with m.Case(REG_SAMPLES):
                m.d.comb += mmio_read.eq(self.samples)
            with m.Default():
                m.d.comb += mmio_read.eq(0)

        # The CPU holds mem_addr stable until it sees mem_ready, so a
        # combinational read mux still matches the address that was sampled.
        m.d.comb += cpu.mem_rdata.eq(
            Mux(in_memory, read_port.data, mmio_read)
        )

        return m
