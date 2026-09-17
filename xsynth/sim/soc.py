"""Simulate the phase 3 SoC: load a program, run it, watch the CPU.

The point of this testbench is the glue, not the CPU: it drives the loader port
the way the host will, then checks that a real PicoRV32 fetches and executes
what was loaded. Amaranth cannot simulate the Verilog core, so this goes through
iverilog (see :mod:`xsynth.sim.verilog`).
"""

from __future__ import annotations

from dataclasses import dataclass

from xsynth.hdl.soc import DEFAULT_MEM_WORDS, PICORV32_SOURCE, SoC
from xsynth.sim.verilog import run

# The tracer bullet, hand assembled: write 0x12345678 to the status register,
# then spin forever.
#
#     lui  x1, 0x10000     x1 = 0x1000_0000, the MMIO page
#     lui  x2, 0x12345     x2 = 0x1234_5000
#     addi x2, x2, 0x678   x2 = 0x1234_5678
#     sw   x2, 0(x1)       status = 0x1234_5678
#     j    .               spin
BOOT_PROGRAM = (
    0x100000B7,
    0x12345137,
    0x67810113,
    0x0020A023,
    0x0000006F,
)

MAGIC = 0x1234_5678

# Halts itself by writing bit 0 of REG_CONTROL, then spins.
#
#     lui  x1, 0x10000     x1 = 0x1000_0000
#     addi x2, x0, 1       x2 = 1
#     sw   x2, 8(x1)       control = 1, so the CPU drops back into reset
#     j    .               never reached
HALT_PROGRAM = (
    0x100000B7,
    0x00100113,
    0x0020A423,
    0x0000006F,
)

# The same memory the board has, so that a program which outgrows the sim has
# outgrown the hardware too rather than only the testbench. `load_addr` is as
# wide as the memory needs, which is what makes an oversized image wrap and
# overwrite its own start instead of failing loudly.
DEFAULT_TIMEOUT = 500


def testbench(program, *, condition: str, mem_words: int = DEFAULT_MEM_WORDS,
              timeout: int = DEFAULT_TIMEOUT) -> str:
    """A testbench that loads ``program`` and waits for ``condition``.

    ``condition`` is a Verilog expression over the design's outputs; the run
    passes as soon as it becomes true, and fails if it has not within
    ``timeout`` cycles.
    """
    address_bits = (mem_words - 1).bit_length()
    words = (sorted(program.items()) if isinstance(program, dict)
             else list(enumerate(program)))
    loads = "\n".join(
        f"        load({index}, 32'h{word:08X});" for index, word in words
    )

    return f"""\
module testbench;
    reg clk = 0;
    reg rst = 1;
    reg run = 0;
    reg load_stb = 0;
    reg [{address_bits - 1}:0] load_addr = 0;
    reg [31:0] load_data = 0;
    reg [31:0] samples = 0;

    wire halted;
    wire trap;
    wire [31:0] status;
    wire [31:0] counter;

    soc dut(
        .clk(clk), .rst(rst), .run(run), .samples(samples),
        .load_stb(load_stb), .load_addr(load_addr), .load_data(load_data),
        .halted(halted), .trap(trap), .status(status), .counter(counter)
    );

    always #5 clk = ~clk;

    task load(input [{address_bits - 1}:0] address, input [31:0] data);
        begin
            @(negedge clk);
            load_addr = address;
            load_data = data;
            load_stb = 1;
            @(negedge clk);
            load_stb = 0;
        end
    endtask

    integer i;
    initial begin
        repeat (4) @(negedge clk);
        rst = 0;
{loads}
        @(negedge clk);
        run = 1;

        for (i = 0; i < {timeout}; i = i + 1) begin
            @(negedge clk);
            if (trap) begin
                $fatal(1, "FAIL: the cpu trapped; status=%h counter=%0d", status, counter);
            end
            if ({condition}) begin
                $display("PASS after %0d cycles: status=%h halted=%b counter=%0d",
                         i, status, halted, counter);
                $finish;
            end
        end
        $fatal(1, "FAIL: condition never held; status=%h halted=%b trap=%b",
               status, halted, trap);
    end
endmodule
"""


@dataclass(frozen=True)
class Result:
    """What the simulation printed."""

    output: str

    def __str__(self) -> str:
        return self.output


def simulate(program=BOOT_PROGRAM, *, condition: str | None = None,
             mem_words: int = DEFAULT_MEM_WORDS,
             timeout: int = DEFAULT_TIMEOUT, keep: str | None = None) -> Result:
    """Load ``program`` into a fresh SoC and run it until ``condition`` holds."""
    if condition is None:
        condition = f"status === 32'h{MAGIC:08X}"

    soc = SoC(mem_words=mem_words)
    output = run(
        soc,
        name="soc",
        ports=[
            soc.load_stb, soc.load_addr, soc.load_data, soc.run, soc.samples,
            soc.halted, soc.trap, soc.status, soc.counter,
        ],
        testbench=testbench(program, condition=condition, mem_words=mem_words,
                            timeout=timeout),
        sources=[PICORV32_SOURCE],
        keep=keep,
    )
    return Result(output)


def main() -> None:
    print(simulate())


if __name__ == "__main__":
    main()
