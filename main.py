from amaranth import Elaboratable, Module, Signal
from amaranth_boards.tang_nano_9k import TangNano9kPlatform

import argparse

from patcher import patch_all

class Blinky(Elaboratable):
    def elaborate(self, platform):
        m = Module()

        led = platform.request("led")

        half_freq = int(platform.default_clk_frequency // 2)
        timer = Signal(range(half_freq + 1), init=half_freq)

        with m.If(timer == 0):
            m.d.sync += led.o.eq(~led.o)
            m.d.sync += timer.eq(timer.init)
        with m.Else():
            m.d.sync += timer.eq(timer - 1)

        return m
    
from amaranth.sim import Simulator

cnt = Blinky()
async def bench(ctx):
    pass
    
def program():
    platform = TangNano9kPlatform()
    platform.build(cnt, do_program=True, toolchain="Apicula")
    
def sim():
    sim = Simulator(cnt)
    sim.add_clock(1e-6)
    sim.add_testbench(bench)
    with sim.write_vcd("output.vcd"):
        sim.run()
        
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--no-flash", action="store_true", help="Program to RAM instead of flash. The program is non-persistent.")
    args = parser.parse_args()

    patch_all(program_to_flash=not args.no_flash)
    
    if args.build:
        program()
    else:
        sim()

if __name__ == "__main__":
    main()