"""The phase 3 SoC: a real PicoRV32 running what the loader wrote."""

from xsynth.firmware import build_firmware
from xsynth.sim.soc import HALT_PROGRAM, MAGIC, simulate

# Reads a word that the loader put at index 16 and copies it to the status
# register, which proves the loader can reach past the program.
#
#     lui  x1, 0x10000     x1 = 0x1000_0000
#     lw   x2, 64(x0)      x2 = mem[16]
#     sw   x2, 0(x1)       status = x2
#     j    .
READ_PROGRAM = {
    0: 0x100000B7,
    1: 0x04002103,
    2: 0x0020A023,
    3: 0x0000006F,
    16: 0xDEADBEEF,
}


def test_the_cpu_executes_a_program_from_the_loader():
    result = simulate()
    assert "PASS" in result.output


def test_the_cpu_can_halt_itself():
    result = simulate(HALT_PROGRAM, condition="halted")
    assert "PASS" in result.output


def test_the_loader_can_write_past_the_program():
    result = simulate(READ_PROGRAM, condition="status === 32'hDEADBEEF")
    assert "PASS" in result.output


def test_an_empty_memory_does_not_run_away():
    # Nothing loaded: the CPU fetches zeros, which decode as an illegal
    # instruction. It must trap rather than appear to succeed.
    try:
        simulate((0,), condition="status === 32'h12345678", timeout=50)
    except AssertionError as error:
        assert "trapped" in str(error)
    else:
        raise AssertionError("an empty program should not have run")


def test_the_reported_cycle_count_is_plausible():
    # The boot program is four instructions plus a spin, so it cannot finish
    # instantly; this also guards against the loader and the CPU racing.
    result = simulate()
    cycles = int(result.output.split("after")[1].split("cycles")[0])
    assert 5 < cycles < 100


def test_the_real_firmware_runs():
    """End to end: clang output, through the loader, executed by the CPU."""
    image = build_firmware()
    program = [
        int.from_bytes(image[start:start + 4].ljust(4, b"\0"), "little")
        for start in range(0, len(image), 4)
    ]
    result = simulate(program, condition=f"status === 32'h{MAGIC:08X}")
    assert "PASS" in result.output
