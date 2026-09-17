"""Phase 3 simulation: the whole control path, soft core included.

Amaranth's own simulator cannot execute the Verilog PicoRV32, so this harness
emits the design and drives it from a plain Verilog testbench. The program is
preloaded into the SoC's memory (``init=``), and everything after that goes over
the wire: a RUN frame starts the core, and COMMANDS frames travel UART -> frame
decoder -> mailbox -> firmware -> co-processor -> command FIFO -> scheduler, so
one passing run exercises every link in the phase 3 chain.

The testbench drives the UART by hand rather than instantiating one, because it
is the *host* end of the link and should not share logic with the device under
test.
"""

from __future__ import annotations

from dataclasses import dataclass

from amaranth import Elaboratable, Module

from xsynth.hdl.phase2 import Phase2Core
from xsynth.hdl.soc import PICORV32_SOURCE, SoC
from xsynth.sim.verilog import run as run_verilog

# Simulation frequencies: only the ratios matter, and a 100 MHz control clock
# makes the UART divisor a round 100 cycles.
CONTROL_HZ = 100_000_000
PIXEL_HZ = 76_923_077
BAUD = 1_000_000
CLOCK_HALF_NS = 5
# The engine walks its voices two pixel clocks each, so a sample boundary has
# to be more than sixteen clocks after the last one (the board gives it 525).
STROBE_PERIOD = 32

DEFAULT_TIMEOUT = 20_000
# The same memory the board has. The firmware's schedule alone is three
# kilobytes of it, so a smaller sim is a sim the firmware cannot fit in.
DEFAULT_MEM_WORDS = 2048


class Phase3Harness(Elaboratable):
    """A phase 3 core with the ports a Verilog testbench needs."""

    def __init__(self, *, program=(), baud: int = BAUD,
                 clock_hz: int = CONTROL_HZ, fifo_depth: int = 16,
                 mem_words: int = DEFAULT_MEM_WORDS):
        self.soc = SoC(mem_words=mem_words, init=list(program))
        self.core = Phase2Core(baud=baud, fifo_depth=fifo_depth,
                               control_clock_hz=clock_hz, soc=self.soc)

    def elaborate(self, platform):
        m = Module()
        m.submodules.core = self.core
        return m


def ports(harness):
    """Every signal the testbench reads or drives."""
    core, soc = harness.core, harness.soc
    return [
        core.rx, core.tx, core.locked, core.audio_strobe,
        core.sample, core.amp, core.fifo_level, core.error_flags,
        soc.run, soc.halted, soc.trap, soc.status, soc.counter,
        soc.commands, soc.mailbox_empty, soc.mailbox_overflow,
        soc.mailbox_frames, soc.mailbox_pushed, soc.mailbox_popped,
    ]


def testbench(frames, *, condition: str, baud: int = BAUD,
              clock_hz: int = CONTROL_HZ,
              timeout: int = DEFAULT_TIMEOUT) -> str:
    """Send ``frames`` over the UART, then wait for ``condition``.

    The condition is a Verilog expression over the harness ports; the run passes
    as soon as it holds and fails if it does not within ``timeout`` cycles.
    """
    divisor = round(clock_hz / baud)
    bit_ns = divisor * 2 * CLOCK_HALF_NS
    strobe_bits = (STROBE_PERIOD - 1).bit_length()
    sends = "\n".join(
        f"        send_byte(8'h{byte:02X});" for byte in b"".join(frames)
    )

    return f"""\
`timescale 1ns/1ps
module testbench;
    reg clk = 0;
    reg rst = 1;
    reg pixel_clk = 0;
    reg pixel_rst = 1;
    reg rx = 1;
    reg locked = 0;
    reg audio_strobe = 0;

    wire tx;
    wire [15:0] sample;
    wire [15:0] amp;
    wire [7:0] fifo_level;
    wire [7:0] error_flags;
    wire halted;
    wire trap;
    wire [31:0] status;
    wire [31:0] counter;
    wire [31:0] commands;
    wire mailbox_empty;
    wire mailbox_overflow;
    wire [15:0] mailbox_frames;
    wire [15:0] mailbox_pushed;
    wire [15:0] mailbox_popped;

    phase3 dut(
        .clk(clk), .rst(rst), .pixel_clk(pixel_clk), .pixel_rst(pixel_rst),
        .rx(rx), .tx(tx), .locked(locked), .audio_strobe(audio_strobe),
        .sample(sample), .amp(amp), .fifo_level(fifo_level),
        .error_flags(error_flags), .halted(halted), .trap(trap),
        .status(status), .counter(counter), .commands(commands),
        .mailbox_empty(mailbox_empty), .mailbox_overflow(mailbox_overflow),
        .mailbox_frames(mailbox_frames), .mailbox_pushed(mailbox_pushed),
        .mailbox_popped(mailbox_popped)
    );

    always #5 clk = ~clk;
    always #6.5 pixel_clk = ~pixel_clk;

    // One audio strobe every {STROBE_PERIOD} pixel cycles, as the pixel-clock
    // divider produces on the board.
    reg [{strobe_bits - 1}:0] strobe_div = 0;
    always @(posedge pixel_clk) begin
        strobe_div <= strobe_div + 1;
        audio_strobe <= (strobe_div == 0);
    end

    task send_byte(input [7:0] value);
        integer k;
        begin
            @(negedge clk);
            rx = 0;
            #{bit_ns};
            for (k = 0; k < 8; k = k + 1) begin
                rx = value[k];
                #{bit_ns};
            end
            rx = 1;
            #{bit_ns};
        end
    endtask

    integer i;
    initial begin
        repeat (8) @(negedge clk);
        rst = 0;
        pixel_rst = 0;
        locked = 1;
        repeat (8) @(negedge clk);

{sends}

        for (i = 0; i < {timeout}; i = i + 1) begin
            @(negedge clk);
            if (trap) begin
                $fatal(1, "FAIL: the cpu trapped; status=%h counter=%0d commands=%0d",
                       status, counter, commands);
            end
            if ({condition}) begin
                $display("PASS after %0d cycles: status=%h amp=%h fifo=%0d commands=%0d frames=%0d",
                         i, status, amp, fifo_level, commands, mailbox_frames);
                $finish;
            end
        end
        $fatal(1, "FAIL: status=%h amp=%h fifo=%0d commands=%0d frames=%0d pushed=%0d popped=%0d overflow=%b",
               status, amp, fifo_level, commands, mailbox_frames,
               mailbox_pushed, mailbox_popped, mailbox_overflow);
    end
endmodule
"""


@dataclass(frozen=True)
class Result:
    """What the simulation printed."""

    output: str

    def __str__(self) -> str:
        return self.output


def simulate(frames, *, program=(), condition: str,
             timeout: int = DEFAULT_TIMEOUT, mem_words: int = DEFAULT_MEM_WORDS,
             keep: str | None = None) -> Result:
    """Run a phase 3 core with ``program`` loaded, sending ``frames``."""
    harness = Phase3Harness(program=program, mem_words=mem_words)
    output = run_verilog(
        harness,
        name="phase3",
        ports=ports(harness),
        testbench=testbench(frames, condition=condition, timeout=timeout),
        sources=[PICORV32_SOURCE],
        keep=keep,
    )
    return Result(output)


def program_words(image: bytes) -> list[int]:
    """A flat firmware image as the word list the SoC memory wants."""
    return [
        int.from_bytes(image[start:start + 4].ljust(4, b"\0"), "little")
        for start in range(0, len(image), 4)
    ]


def run(*, vcd: str | None = None) -> None:
    """The ``xsynth sim --phase 3`` entry point: a short, readable demo.

    The ``vcd`` argument is accepted for symmetry with the other phases; this
    bench runs under iverilog, which writes its own dump when asked.
    """
    from xsynth.firmware import FIRMWARE_MAGIC, build_firmware
    from xsynth.hdl.audio import PEAK, phase_step
    from xsynth.hdl.voice import WAVES
    from xsynth.protocol import (
        OP_NOTE_OFF,
        OP_NOTE_ON,
        OP_SET_WAVE,
        Command,
        encode_commands,
        encode_run,
    )

    program = program_words(build_firmware())

    booted = simulate(
        [encode_run(True)],
        program=program,
        condition=f"status === 32'h{FIRMWARE_MAGIC:08X}",
    )
    print(f"boot: {booted.output.splitlines()[0]}")

    tone = 440.0
    step = phase_step(tone, PIXEL_HZ // STROBE_PERIOD)
    played = simulate(
        [
            encode_run(True),
            encode_commands([
                Command(OP_SET_WAVE, value=WAVES.index("saw")),
                Command(OP_NOTE_ON, value=step),
            ]),
        ],
        program=program,
        condition=f"amp === 16'h{PEAK:04X}",
        timeout=60_000,
    )
    print(f"note_on: {played.output.splitlines()[0]}")

    stopped = simulate(
        [
            encode_run(True),
            encode_commands([Command(OP_NOTE_ON, value=step)]),
            encode_commands([Command(OP_NOTE_OFF, delay=8)]),
        ],
        program=program,
        condition="amp === 16'h0000",
        timeout=60_000,
    )
    print(f"note_off: {stopped.output.splitlines()[0]}")
