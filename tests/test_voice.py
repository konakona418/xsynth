# amaranth: UnusedElaboratable=disable
"""Voice: wavetable contents, the engine's mixing, and the scheduler's timing."""

import math

import pytest
from amaranth import Array, Elaboratable, Module, Signal
from amaranth.sim import Simulator

from xsynth.hdl.audio import PEAK, SAMPLE_MASK, to_signed
from xsynth.hdl.fifo import AsyncFifo
from xsynth.hdl.voice import (
    ENV_SHIFT,
    STAGE_IDLE,
    STAGE_SUSTAIN,
    TABLE_BITS,
    TABLE_SIZE,
    VOICES,
    WAVES,
    CommandScheduler,
    VoiceBank,
    waveform,
    wavetable_init,
)
from xsynth.protocol import (
    OP_NOTE_OFF,
    OP_NOTE_ON,
    OP_SET_AMP,
    OP_SET_DECAY,
    OP_SET_FREQ,
    OP_SET_MASTER,
    OP_SET_RELEASE,
    OP_SET_SUSTAIN,
    Command,
)

PIXEL_HZ = 25_200_000
CONTROL_HZ = 27_000_000
ADDRESS_STEP = 1 << (32 - TABLE_BITS)

# The walk is two clocks per voice; a sample boundary has to leave room for all
# of them plus the cycle that starts it, so eighteen is the floor.
SAMPLES_PER_STROBE = 2 * VOICES + 2

TOP = PEAK << ENV_SHIFT


def test_every_waveform_has_one_full_period_in_the_signed_range():
    for name in WAVES:
        table = waveform(name)
        assert len(table) == TABLE_SIZE
        assert all(0 <= value <= SAMPLE_MASK for value in table)
        signed = [to_signed(value) for value in table]
        assert min(signed) == -PEAK, name
        # A sawtooth ramps to just below the peak rather than reaching it.
        assert max(signed) >= PEAK - PEAK // 100, name


def test_the_sine_waveform_matches_the_ideal():
    table = waveform("sine")
    for index in range(TABLE_SIZE):
        ideal = round(PEAK * math.sin(2 * math.pi * index / TABLE_SIZE))
        assert to_signed(table[index]) == ideal


def test_the_square_waveform_is_two_levels():
    assert set(waveform("square")) == {PEAK, -PEAK & SAMPLE_MASK}


def test_the_triangle_waveform_peaks_halfway_through():
    table = waveform("triangle")
    assert to_signed(table[0]) == -PEAK
    assert to_signed(table[TABLE_SIZE // 2]) == PEAK


def test_unknown_waveforms_are_rejected():
    with pytest.raises(ValueError):
        waveform("noise")


def test_wavetable_init_concatenates_the_tables():
    init = wavetable_init(WAVES)
    assert len(init) == len(WAVES) * TABLE_SIZE
    assert init[:TABLE_SIZE] == waveform("sine")
    assert init[TABLE_SIZE:2 * TABLE_SIZE] == waveform("saw")


def _play(bank, *, samples, setup=None, commands=(), probe=None):
    """Strobe ``bank`` and collect its output and every voice's envelope.

    ``setup`` runs once, before the first sample. ``commands`` maps a sample
    index to a :class:`Command`; it is applied on that boundary with the one
    cycle offset the scheduler uses, so the tests exercise the same handshake
    the hardware does rather than poking registers behind its back. ``probe``
    is called once, after the last sample, to read state back out of the
    simulator.

    ``envelopes[voice][k]`` is the raw accumulator, not the level: it is read
    before boundary ``k``, which is the value boundary ``k`` scales by, shifted
    down by ``ENV_SHIFT``. Keeping the raw value makes the arithmetic exact.
    """
    outputs: list[int] = []
    envelopes: list[list[int]] = [[] for _ in range(VOICES)]
    pending = dict(commands)

    async def bench(ctx):
        if setup is not None:
            setup(ctx)
        for index in range(samples):
            for voice in range(VOICES):
                envelopes[voice].append(ctx.get(bank.env[voice]))
            command = pending.get(index)
            ctx.set(bank.strobe, 1)
            await ctx.tick("pixel")
            ctx.set(bank.strobe, 0)
            if command is not None:
                ctx.set(bank.command, command.word)
                ctx.set(bank.apply, 1)
            await ctx.tick("pixel")
            if command is not None:
                ctx.set(bank.apply, 0)
            for _ in range(SAMPLES_PER_STROBE - 2):
                await ctx.tick("pixel")
            outputs.append(to_signed(ctx.get(bank.sample)))
        if probe is not None:
            probe(ctx)

    sim = Simulator(bank)
    sim.add_clock(1 / PIXEL_HZ, domain="pixel")
    sim.add_testbench(bench)
    sim.run()
    return outputs, envelopes


def _hold(bank, voice, *, level=PEAK, wave="square", env=None):
    """Park a voice at a fixed level with its phase still, so it emits a constant.

    A zero step plus a table entry that is not zero is the simplest way to get
    a known number out of the mix; it is also the shape a stuck voice would
    have, which is what the DC-offset test is about.
    """
    def setup(ctx):
        ctx.set(bank.step[voice], 0)
        ctx.set(bank.wave[voice], WAVES.index(wave))
        ctx.set(bank.level[voice], level)
        ctx.set(bank.env[voice], (level if env is None else env) << ENV_SHIFT)
        ctx.set(bank.stage[voice], STAGE_SUSTAIN)
    return setup


# Eight table entries per sample, so a full period fits in a test that still
# finishes quickly. Eight divides the table evenly, so the sweep lands exactly
# on the sine's peak at index 512.
SWEEP_STEP = 8 * ADDRESS_STEP
SWEEP_SAMPLES = TABLE_SIZE // 8 + 8


def _sweep(name: str, level: int) -> list[int]:
    """Step one voice through a whole table and collect what comes out."""
    bank = VoiceBank()

    def setup(ctx):
        _hold(bank, 0, level=level, wave=name)(ctx)
        ctx.set(bank.step[0], SWEEP_STEP)
        ctx.set(bank.sustain, PEAK)

    outputs, _ = _play(bank, samples=SWEEP_SAMPLES, setup=setup)
    return outputs


def test_the_voice_reads_the_selected_table():
    # The envelope and the master each scale by 32767/32768, so a full-scale
    # table entry comes out two LSBs low rather than one.
    levels = sorted(set(_sweep("square", PEAK)))
    assert len(levels) == 2, "a square wave has only two levels"
    assert levels[1] >= PEAK - 3
    assert levels[0] <= -PEAK + 3

    sine = set(_sweep("sine", PEAK))
    assert len(sine) > 100, "a sine should use many distinct values"


def test_the_note_level_scales_the_output():
    collected = _sweep("sine", PEAK // 2)
    assert max(collected) == pytest.approx(PEAK // 2, abs=3)
    assert min(collected) == pytest.approx(-(PEAK // 2), abs=3)


def test_an_idle_voice_is_silent_whatever_it_is_tuned_to():
    # A stopped voice must not hold whatever the table has at its parked phase:
    # a saw at phase 0 is -full scale, which is a DC offset, not silence.
    bank = VoiceBank()
    outputs, envelopes = _play(
        bank,
        samples=16,
        setup=lambda ctx: [
            ctx.set(bank.step[0], ADDRESS_STEP),
            ctx.set(bank.level[0], PEAK),
            ctx.set(bank.wave[0], WAVES.index("saw")),
            ctx.set(bank.env[0], 0),
            ctx.set(bank.stage[0], STAGE_IDLE),
        ],
    )
    assert set(outputs) == {0}
    assert set(envelopes[0]) == {0}


def test_a_command_reaches_the_voice_it_names():
    bank = VoiceBank()
    _, envelopes = _play(
        bank,
        samples=8,
        setup=lambda ctx: [
            ctx.set(bank.attack, TOP // 2),
            ctx.set(bank.wave[2], WAVES.index("square")),
            ctx.set(bank.step[2], 0),
        ],
        commands={0: Command(OP_NOTE_ON, voice=2, value=0)},
    )
    # Two rates' worth reaches the note's own peak, and it stops there.
    assert envelopes[2][-1] == TOP
    assert set(envelopes[0]) == {0}, "the note reached the wrong voice"


def test_note_on_starts_the_attack_and_note_off_the_release():
    bank = VoiceBank()
    rate = TOP // 100
    _, envelopes = _play(
        bank,
        samples=8,
        setup=lambda ctx: [
            ctx.set(bank.attack, rate),
            ctx.set(bank.release, rate),
            ctx.set(bank.wave[0], WAVES.index("square")),
            ctx.set(bank.step[0], 0),
        ],
        commands={
            0: Command(OP_NOTE_ON, value=0),
            4: Command(OP_NOTE_OFF, delay=0),
        },
    )
    # Nothing on the first boundary: the level this sample is scaled by was
    # latched before the command landed, so the attack starts from the next one.
    assert envelopes[0][0] == 0
    # The attack climbs by one rate per sample.
    assert envelopes[0][1:5] == [rate, 2 * rate, 3 * rate, 4 * rate]
    # Note off turns it around, still one rate per sample.
    assert envelopes[0][5:8] == [3 * rate, 2 * rate, rate]


def test_the_attack_stops_at_the_velocity_it_was_given():
    bank = VoiceBank()
    _, envelopes = _play(
        bank,
        samples=8,
        setup=lambda ctx: [
            ctx.set(bank.attack, TOP),
            ctx.set(bank.wave[0], WAVES.index("square")),
            ctx.set(bank.step[0], 0),
            ctx.set(bank.level[0], PEAK // 4),
        ],
        commands={0: Command(OP_NOTE_ON, value=0)},
    )
    assert envelopes[0][-1] == (PEAK // 4) << ENV_SHIFT


def test_the_decay_falls_to_the_sustain_level():
    bank = VoiceBank()
    step = TOP // 16
    _, envelopes = _play(
        bank,
        samples=14,
        setup=lambda ctx: [
            ctx.set(bank.attack, TOP),
            ctx.set(bank.decay, step),
            ctx.set(bank.sustain, PEAK // 2),
            ctx.set(bank.wave[0], WAVES.index("square")),
            ctx.set(bank.step[0], 0),
        ],
        commands={0: Command(OP_NOTE_ON, value=0)},
    )
    # The attack is instant, so the first boundary is already at the peak and
    # the decay starts from the one after it.
    assert envelopes[0][0] == 0
    assert envelopes[0][1] == TOP
    assert envelopes[0][2] == TOP - step
    # It settles on the sustain level instead of running down to silence.
    assert envelopes[0][-1] == (PEAK // 2) << ENV_SHIFT


def test_a_quiet_note_cannot_decay_up_to_the_sustain_level():
    # The decay floor is capped by the note's own peak, so a note played below
    # the sustain level stays where it was put instead of swelling to meet it.
    bank = VoiceBank()
    _, envelopes = _play(
        bank,
        samples=8,
        setup=lambda ctx: [
            ctx.set(bank.attack, TOP),
            ctx.set(bank.decay, TOP),
            ctx.set(bank.sustain, PEAK),
            ctx.set(bank.wave[0], WAVES.index("square")),
            ctx.set(bank.step[0], 0),
            ctx.set(bank.level[0], PEAK // 8),
        ],
        commands={0: Command(OP_NOTE_ON, value=0)},
    )
    assert envelopes[0][-1] == (PEAK // 8) << ENV_SHIFT


def test_the_envelope_settings_are_shared_and_the_levels_are_not():
    bank = VoiceBank()
    rate = TOP // 100
    _, envelopes = _play(
        bank,
        samples=60,
        setup=lambda ctx: [
            ctx.set(bank.attack, rate),
            ctx.set(bank.level[0], PEAK),
            ctx.set(bank.level[1], PEAK // 4),
            ctx.set(bank.wave[0], WAVES.index("square")),
            ctx.set(bank.wave[1], WAVES.index("square")),
            ctx.set(bank.step[0], 0),
            ctx.set(bank.step[1], 0),
        ],
        commands={
            0: Command(OP_NOTE_ON, voice=0, value=0),
            1: Command(OP_NOTE_ON, voice=1, value=0),
        },
    )
    # One set of rates: both voices climb by the same amount per sample, voice
    # 1 one boundary behind because its note started one boundary later.
    assert envelopes[0][1:6] == [rate, 2 * rate, 3 * rate, 4 * rate, 5 * rate]
    assert envelopes[1][2:7] == envelopes[0][1:6]
    # One level each: the quiet note stops at its own peak, the loud one is
    # still climbing, and the shared rate did not drag either of them.
    assert envelopes[1][-1] == (PEAK // 4) << ENV_SHIFT
    assert envelopes[0][-1] == 59 * rate


def test_two_voices_sum_into_the_mix():
    def two(bank, **extra):
        def setup(ctx):
            _hold(bank, 0)(ctx)
            _hold(bank, 1)(ctx)
            ctx.set(bank.sustain, PEAK)
            for name, value in extra.items():
                ctx.set(getattr(bank, name), value)
        return setup

    alone_bank = VoiceBank()
    alone, _ = _play(alone_bank, samples=4, setup=_hold(alone_bank, 0))
    assert max(alone) == pytest.approx(PEAK, abs=3)

    both_bank = VoiceBank()
    both, _ = _play(both_bank, samples=4, setup=two(both_bank))
    assert max(both) == PEAK, "the second voice should push the mix over"

    # Half the master and the same two voices fit again: the sum really is the
    # sum, and the clipping above was the master's fault rather than a bug.
    quiet_bank = VoiceBank()
    quiet, _ = _play(quiet_bank, samples=4,
                     setup=two(quiet_bank, master=PEAK // 2))
    assert max(quiet) == pytest.approx(PEAK, abs=4)


def test_the_mix_saturates_instead_of_wrapping():
    bank = VoiceBank()

    def setup(ctx):
        for voice in range(VOICES):
            _hold(bank, voice)(ctx)
        ctx.set(bank.sustain, PEAK)

    outputs, _ = _play(bank, samples=4, setup=setup)
    # Eight full-scale voices sum well past the sample range. Clipping holds
    # them at the rail; wrapping would flip the sign and be obvious.
    assert set(outputs) == {PEAK}


def test_the_master_scales_the_mix():
    bank = VoiceBank()

    def setup(ctx):
        _hold(bank, 0)(ctx)
        ctx.set(bank.master, PEAK // 2)

    outputs, _ = _play(bank, samples=4, setup=setup)
    assert max(outputs) == pytest.approx(PEAK // 2, abs=3)


def test_the_global_commands_take_effect_as_they_arrive():
    bank = VoiceBank()
    seen: dict[str, int] = {}
    _play(
        bank,
        samples=8,
        setup=lambda ctx: [
            ctx.set(bank.wave[0], WAVES.index("square")),
            ctx.set(bank.step[0], 0),
        ],
        commands={
            0: Command(OP_SET_DECAY, value=TOP // 32),
            1: Command(OP_SET_SUSTAIN, value=PEAK // 4),
            2: Command(OP_SET_RELEASE, value=TOP // 64),
            3: Command(OP_SET_MASTER, value=1234),
            4: Command(OP_SET_FREQ, value=4321),
            5: Command(OP_SET_AMP, value=PEAK // 2),
        },
        probe=lambda ctx: seen.update(
            decay=ctx.get(bank.decay),
            sustain=ctx.get(bank.sustain),
            release=ctx.get(bank.release),
            master=ctx.get(bank.master),
            step=ctx.get(bank.step[0]),
            level=ctx.get(bank.level[0]),
        ),
    )
    assert seen["decay"] == TOP // 32
    assert seen["sustain"] == PEAK // 4
    assert seen["release"] == TOP // 64
    assert seen["master"] == 1234
    assert seen["step"] == 4321
    assert seen["level"] == PEAK // 2


def test_the_envelope_settings_are_clamped_to_the_sample_range():
    bank = VoiceBank()
    seen: dict[str, int] = {}
    _play(
        bank,
        samples=4,
        setup=lambda ctx: ctx.set(bank.step[0], 0),
        commands={
            0: Command(OP_SET_SUSTAIN, value=0xFFFFFFFF),
            1: Command(OP_SET_MASTER, value=0xFFFFFFFF),
        },
        probe=lambda ctx: seen.update(
            sustain=ctx.get(bank.sustain), master=ctx.get(bank.master)),
    )
    assert seen["sustain"] == PEAK
    assert seen["master"] == PEAK


class _SchedulerHarness(Elaboratable):
    """Preloads the FIFO from the control side and strobes from the audio side."""

    def __init__(self, commands):
        self.words = [command.word for command in commands]
        self.fifo = AsyncFifo(width=64, depth=8)
        self.scheduler = CommandScheduler(self.fifo)

    def elaborate(self, platform):
        m = Module()
        m.submodules.fifo = self.fifo
        m.submodules.scheduler = self.scheduler
        index = Signal(range(len(self.words) + 1), init=0)
        m.d.comb += [
            self.fifo.w_data.eq(Array(self.words)[index]),
            self.fifo.w_inc.eq(index < len(self.words)),
        ]
        with m.If(index < len(self.words)):
            m.d.sync += index.eq(index + 1)
        return m


def test_the_scheduler_applies_commands_at_the_requested_sample():
    harness = _SchedulerHarness([
        Command(OP_NOTE_ON, value=100, delay=0),
        Command(OP_SET_FREQ, value=200, delay=5),
        Command(OP_SET_FREQ, value=300, delay=3),
    ])
    applied: list[tuple[int, int]] = []

    async def strobe(ctx):
        for index in range(48):
            ctx.set(harness.scheduler.strobe, 1)
            await ctx.tick("pixel")
            if ctx.get(harness.scheduler.apply):
                word = ctx.get(harness.scheduler.command)
                applied.append(
                    (index, Command.unpack(word.to_bytes(8, "little")).value)
                )
            ctx.set(harness.scheduler.strobe, 0)
            await ctx.tick("pixel")

    sim = Simulator(harness)
    sim.add_clock(1 / CONTROL_HZ, domain="sync")
    sim.add_clock(1 / PIXEL_HZ, domain="pixel")
    sim.add_testbench(strobe)
    sim.run()

    assert [value for _, value in applied] == [100, 200, 300]
    samples = [index for index, _ in applied]
    assert [b - a for a, b in zip(samples, samples[1:])] == [5, 3]


def test_the_scheduler_stays_idle_with_an_empty_fifo():
    harness = _SchedulerHarness([])
    applies = 0

    async def strobe(ctx):
        nonlocal applies
        for _ in range(32):
            ctx.set(harness.scheduler.strobe, 1)
            await ctx.tick("pixel")
            if ctx.get(harness.scheduler.apply):
                applies += 1
            ctx.set(harness.scheduler.strobe, 0)
            await ctx.tick("pixel")

    sim = Simulator(harness)
    sim.add_clock(1 / CONTROL_HZ, domain="sync")
    sim.add_clock(1 / PIXEL_HZ, domain="pixel")
    sim.add_testbench(strobe)
    sim.run()
    assert applies == 0
