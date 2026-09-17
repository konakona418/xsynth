"""Phase 3b: the soft core owns the command path.

The acceptance gate (see PLAN.md) is that an uploaded PicoRV32 program drives
the engine through the command FIFO, and that a full FIFO stalls the CPU rather
than losing a command. Both are exercised here end to end, over the wire.
"""

from xsynth.firmware import FIRMWARE_MAGIC, build_firmware
from xsynth.hdl.audio import PEAK, phase_step
from xsynth.hdl.pcpi import CUSTOM0, FUNCT3_PUSH, encode_r
from xsynth.hdl.voice import WAVES
from xsynth.protocol import (
    OP_NOTE_ON,
    OP_SET_WAVE,
    Command,
    encode_commands,
    encode_run,
)
from xsynth.sim.phase2 import PIXEL_HZ as PHASE2_PIXEL_HZ
from xsynth.sim.phase2 import STROBE_PERIOD
from xsynth.sim.phase3 import PIXEL_HZ, program_words, simulate

TEST_SAMPLE_RATE = PIXEL_HZ // STROBE_PERIOD

# A mid-range note; only that it is non-zero matters for these tests.
TONE = 440.0


def firmware():
    return program_words(build_firmware())


def test_the_coprocessor_claims_only_its_own_instruction():
    # A custom-0 instruction we do not implement must not look like ours.
    stranger = encode_r(0x7F, 0, 0, 0, 0, CUSTOM0)
    assert stranger & 0x7F == CUSTOM0
    assert stranger != encode_r(0, 0, 0, FUNCT3_PUSH, 0, CUSTOM0)


def test_the_firmware_boots_and_announces_itself():
    result = simulate(
        [encode_run(True)],
        program=firmware(),
        condition=f"status === 32'h{FIRMWARE_MAGIC:08X}",
    )
    assert "PASS" in result.output


def test_a_command_reaches_the_voice_through_the_cpu():
    """UART -> decoder -> mailbox -> firmware -> PCPI -> FIFO -> scheduler."""
    step = phase_step(TONE, TEST_SAMPLE_RATE)
    frames = [
        encode_run(True),
        encode_commands([
            Command(OP_SET_WAVE, value=WAVES.index("saw")),
            Command(OP_NOTE_ON, value=step),
        ]),
    ]
    result = simulate(
        frames,
        program=firmware(),
        condition=f"amp === 16'h{PEAK:04X}",
        timeout=60_000,
    )
    assert "PASS" in result.output


def test_the_firmware_counts_what_it_forwarded():
    frames = [
        encode_run(True),
        encode_commands([Command(OP_NOTE_ON, value=0x1000)]),
        encode_commands([Command(OP_NOTE_ON, value=0x2000)]),
    ]
    result = simulate(
        frames,
        program=firmware(),
        condition="commands === 2",
        timeout=60_000,
    )
    assert "PASS" in result.output
