"""The synth engine: several wavetable voices, an ADSR each, and a mix.

Phase 1's :class:`~xsynth.hdl.audio.SineDDS` stays as it is (it is verified on
hardware and is the reference for the 32-bit accumulator). This module is the
parameterised engine PLAN.md calls for at Phase 4.

**One table read port, many voices.** A voice needs a wavetable lookup per
sample, and the tables live in one BSRAM. BSRAM ports are far too precious to
replicate the bank once per voice, so :class:`VoiceBank` walks the voices one at
a time inside the 525 cycles a 48 kHz sample leaves in the 25.2 MHz pixel
domain, and every voice shares one read port, one multiplier and one
accumulator. The walk costs two cycles per voice, sixteen for eight of them:
3% of the budget, which leaves the rest for the filter Phase 4b adds.

**The envelope settings are global, the envelope state is per voice.** That is
what a synth normally does -- one set of rates, one level and one stage per
voice -- and it means a note keeps its own shape while the panel is retuned.

**Velocity is the envelope's peak, not a gain on top of it.** ``OP_SET_AMP``
scales how far the attack travels, so a quiet note is quiet all the way through
its decay. The decay's floor is the global sustain level, capped by the note's
own peak so a quiet note cannot swell.

The four tables are naive (not band-limited), so saw and square alias. That is
deliberate: PLAN.md puts the real oscillator work later, and a naive table is
the honest reference to measure that work against.
"""

from __future__ import annotations

import math

from amaranth import Array, Elaboratable, Mux, Module, Signal, signed, unsigned
from amaranth.lib.memory import Memory

from xsynth.hdl.audio import PEAK, PHASE_BITS, SAMPLE_BITS
from xsynth.protocol import (
    DELAY_BITS,
    OP_NOTE_OFF,
    OP_NOTE_ON,
    OP_RESET,
    OP_SET_AMP,
    OP_SET_ATTACK,
    OP_SET_DECAY,
    OP_SET_FREQ,
    OP_SET_MASTER,
    OP_SET_RELEASE,
    OP_SET_SUSTAIN,
    OP_SET_WAVE,
)

WAVE_BITS = 2
WAVES = ("sine", "saw", "square", "triangle")
TABLE_BITS = 11
TABLE_SIZE = 1 << TABLE_BITS

VOICES = 8

SAMPLE_MASK = (1 << SAMPLE_BITS) - 1

# The envelope runs at a finer resolution than the sample it produces: the
# level is the top SAMPLE_BITS of the accumulator. That extra ENV_SHIFT of
# fraction is what turns a 16-bit rate into a useful range of times -- with the
# accumulator and the rate both 24-bit, the slowest attack is 2**23 samples
# (about three minutes) and the fastest is instant.
ENV_BITS = 24
ENV_SHIFT = ENV_BITS - SAMPLE_BITS

STAGE_BITS = 3
STAGE_IDLE = 0
STAGE_ATTACK = 1
STAGE_DECAY = 2
STAGE_SUSTAIN = 3
STAGE_RELEASE = 4


def waveform(name: str, *, table_bits: int = TABLE_BITS,
             sample_bits: int = SAMPLE_BITS) -> list[int]:
    """One period of ``name`` as two's-complement bit patterns."""
    size = 1 << table_bits
    peak = (1 << (sample_bits - 1)) - 1
    mask = (1 << sample_bits) - 1

    if name == "sine":
        values = [round(peak * math.sin(2 * math.pi * i / size))
                  for i in range(size)]
    elif name == "saw":
        values = [round(peak * (2 * i / size - 1)) for i in range(size)]
    elif name == "square":
        values = [peak if i < size // 2 else -peak for i in range(size)]
    elif name == "triangle":
        values = [
            round(peak * (4 * i / size - 1)) if i < size // 2
            else round(peak * (3 - 4 * i / size))
            for i in range(size)
        ]
    else:
        raise ValueError(f"unknown waveform {name!r}; known: {', '.join(WAVES)}")

    return [value & mask for value in values]


def wavetable_init(waves=WAVES, *, table_bits: int = TABLE_BITS,
                   sample_bits: int = SAMPLE_BITS) -> list[int]:
    """All tables concatenated, which is how they are addressed in BSRAM."""
    init: list[int] = []
    for name in waves:
        init.extend(waveform(name, table_bits=table_bits,
                             sample_bits=sample_bits))
    return init


class VoiceBank(Elaboratable):
    """Eight wavetable voices, an ADSR each, mixed and saturated.

    ``apply`` and ``command`` come from :class:`CommandScheduler`. A command is
    not applied the cycle it arrives: it is parked and applied when the walk
    reaches the voice it names, which is the only moment that voice's registers
    are not being written back. That keeps the two writers from racing without
    stalling either of them.

    ``strobe`` marks a sample boundary and starts a walk. The walk is sixteen
    pixel clocks, so the strobe has to be slower than that; at 48 kHz it is 525.
    """

    def __init__(self, *, voices: int = VOICES, waves=WAVES,
                 table_bits: int = TABLE_BITS, sample_bits: int = SAMPLE_BITS,
                 phase_bits: int = PHASE_BITS, env_bits: int = ENV_BITS,
                 domain: str = "pixel"):
        if not waves:
            raise ValueError("at least one wavetable is required")
        if table_bits > phase_bits:
            raise ValueError("table_bits cannot exceed phase_bits")
        if sample_bits > phase_bits:
            raise ValueError("sample_bits cannot exceed phase_bits")
        if env_bits < sample_bits:
            raise ValueError("env_bits cannot be smaller than sample_bits")
        if voices < 1:
            raise ValueError("at least one voice is required")

        self.voices = voices
        self.waves = tuple(waves)
        self.table_bits = table_bits
        self.sample_bits = sample_bits
        self.phase_bits = phase_bits
        self.env_bits = env_bits
        self.env_shift = env_bits - sample_bits
        self.wave_bits = max(1, (len(self.waves) - 1).bit_length())
        self.domain = domain

        self.strobe = Signal()
        self.apply = Signal()
        self.command = Signal(64)

        # Per voice. ``env`` is the envelope accumulator; its top sample_bits
        # are the level the oscillator is scaled by.
        self.phase = [Signal(phase_bits) for _ in range(voices)]
        self.step = [Signal(phase_bits) for _ in range(voices)]
        self.wave = [Signal(self.wave_bits) for _ in range(voices)]
        self.env = [Signal(env_bits) for _ in range(voices)]
        self.stage = [Signal(STAGE_BITS) for _ in range(voices)]
        self.level = [Signal(sample_bits, init=PEAK) for _ in range(voices)]

        # Global. The envelope settings are one set of knobs for every voice;
        # only the stage and the level are per voice.
        self.attack = Signal(env_bits, init=(1 << env_bits) - 1)
        self.decay = Signal(env_bits, init=(1 << env_bits) - 1)
        self.release = Signal(env_bits, init=(1 << env_bits) - 1)
        self.sustain = Signal(sample_bits, init=PEAK)
        self.master = Signal(sample_bits, init=PEAK)

        self.sample = Signal(sample_bits)
        self.busy = Signal()

    def elaborate(self, platform):
        m = Module()
        d = m.d[self.domain]
        voices = self.voices
        shift = self.env_shift

        table = Memory(
            shape=unsigned(self.sample_bits),
            depth=len(self.waves) << self.table_bits,
            init=wavetable_init(self.waves, table_bits=self.table_bits,
                                sample_bits=self.sample_bits),
        )
        m.submodules.table = table
        read_port = table.read_port(domain=self.domain)

        slot = Signal(range(voices))
        sub = Signal()
        busy = Signal()
        start = Signal()
        acc = Signal(signed(32))

        m.d.comb += self.busy.eq(busy)

        # The voice the walk is looking at. Reading through an Array is a mux;
        # writing back goes through the Switch below, one voice per branch.
        cur_phase = Signal(self.phase_bits)
        cur_step = Signal(self.phase_bits)
        cur_wave = Signal(self.wave_bits)
        cur_env = Signal(self.env_bits)
        cur_stage = Signal(STAGE_BITS)
        cur_level = Signal(self.sample_bits)
        m.d.comb += [
            cur_phase.eq(Array(self.phase)[slot]),
            cur_step.eq(Array(self.step)[slot]),
            cur_wave.eq(Array(self.wave)[slot]),
            cur_env.eq(Array(self.env)[slot]),
            cur_stage.eq(Array(self.stage)[slot]),
            cur_level.eq(Array(self.level)[slot]),
        ]

        # The envelope's two ends. The attack travels to the note's own peak;
        # the decay falls to the sustain level, but never above that peak, so a
        # quiet note cannot swell to the panel's level on the way down.
        top = Signal(self.env_bits)
        floor = Signal(self.env_bits)
        m.d.comb += [
            top.eq(cur_level << shift),
            floor.eq(Mux((self.sustain << shift) > top, top,
                         self.sustain << shift)),
        ]

        env_next = Signal(self.env_bits)
        stage_next = Signal(STAGE_BITS)
        attack_sum = Signal(signed(self.env_bits + 1))
        decay_diff = Signal(signed(self.env_bits + 1))
        release_diff = Signal(signed(self.env_bits + 1))
        m.d.comb += [
            attack_sum.eq(cur_env + self.attack),
            decay_diff.eq(cur_env - self.decay),
            release_diff.eq(cur_env - self.release),
        ]

        with m.Switch(cur_stage):
            with m.Case(STAGE_ATTACK):
                m.d.comb += [
                    env_next.eq(Mux(attack_sum >= top, top, attack_sum)),
                    stage_next.eq(Mux(attack_sum >= top, STAGE_DECAY,
                                      STAGE_ATTACK)),
                ]
            with m.Case(STAGE_DECAY):
                m.d.comb += [
                    env_next.eq(Mux(decay_diff <= floor, floor, decay_diff)),
                    stage_next.eq(Mux(decay_diff <= floor, STAGE_SUSTAIN,
                                      STAGE_DECAY)),
                ]
            with m.Case(STAGE_SUSTAIN):
                m.d.comb += [env_next.eq(floor), stage_next.eq(STAGE_SUSTAIN)]
            with m.Case(STAGE_RELEASE):
                m.d.comb += [
                    env_next.eq(Mux(release_diff <= 0, 0, release_diff)),
                    stage_next.eq(Mux(release_diff <= 0, STAGE_IDLE,
                                      STAGE_RELEASE)),
                ]
            with m.Default():
                m.d.comb += [env_next.eq(0), stage_next.eq(STAGE_IDLE)]

        # The shared multiplier. The address goes out combinationally, so the
        # registered BSRAM data arrives exactly one cycle later, in the slot's
        # second half; env_hold carries the level across that cycle.
        m.d.comb += read_port.addr.eq(
            (cur_wave << self.table_bits)
            | cur_phase[self.phase_bits - self.table_bits:]
        )

        env_hold = Signal(self.sample_bits)
        table_data = Signal(self.sample_bits)
        m.d.comb += table_data.eq(read_port.data)

        product = Signal(signed(2 * self.sample_bits))
        scaled = Signal(signed(self.sample_bits + 1))
        m.d.comb += [
            product.eq(table_data.as_signed() * env_hold.as_signed()),
            scaled.eq(product >> (self.sample_bits - 1)),
        ]

        # Master gain, then saturate. Eight voices at full level sum to eight
        # times full scale, so something has to give; saturating rather than
        # wrapping is what makes that a loud chord instead of a nasty one.
        total = Signal(signed(34))
        mixed = Signal(signed(34 + self.sample_bits))
        clipped = Signal(signed(self.sample_bits))
        m.d.comb += [
            total.eq(acc + scaled),
            mixed.eq((total * self.master.as_signed())
                     >> (self.sample_bits - 1)),
            clipped.eq(Mux(
                mixed > PEAK, PEAK,
                Mux(mixed < -(PEAK + 1), -(PEAK + 1), mixed),
            )),
        ]

        # The command that is waiting for the walk to reach its voice. It is
        # held, not defaulted to zero: a default would clear it on the cycle
        # after it arrived, before the walk ever got to the voice it names.
        pend_valid = Signal()
        pend_voice = Signal(8)
        pend_opcode = Signal(8)
        pend_value = Signal(32)
        with m.If(self.apply):
            d += [
                pend_voice.eq(self.command[48:56]),
                pend_opcode.eq(self.command[56:64]),
                pend_value.eq(self.command[16:48]),
                pend_valid.eq(1),
            ]

        # Global settings take effect as they arrive. Nothing in the walk writes
        # them, so there is nothing to race. Reset is global for the same
        # reason: it clears the voices the walk has not reached yet, and for the
        # ones it has, the write-back recomputes the envelope from the freshly
        # cleared stage and level, so it lands on idle either way.
        with m.If(self.apply):
            with m.Switch(self.command[56:64]):
                with m.Case(OP_SET_ATTACK):
                    d += self.attack.eq(self.command[16:48])
                with m.Case(OP_SET_DECAY):
                    d += self.decay.eq(self.command[16:48])
                with m.Case(OP_SET_RELEASE):
                    d += self.release.eq(self.command[16:48])
                with m.Case(OP_SET_SUSTAIN):
                    d += self.sustain.eq(
                        Mux(self.command[16:48][:self.sample_bits] > PEAK,
                            PEAK, self.command[16:48][:self.sample_bits]))
                with m.Case(OP_SET_MASTER):
                    d += self.master.eq(
                        Mux(self.command[16:48][:self.sample_bits] > PEAK,
                            PEAK, self.command[16:48][:self.sample_bits]))
                with m.Case(OP_RESET):
                    for voice in range(voices):
                        d += [
                            self.step[voice].eq(0),
                            self.env[voice].eq(0),
                            self.stage[voice].eq(STAGE_IDLE),
                            self.wave[voice].eq(0),
                        ]

        with m.If(self.strobe):
            d += start.eq(1)

        with m.If(~busy & start):
            d += [busy.eq(1), start.eq(0), slot.eq(0), sub.eq(0), acc.eq(0)]

        with m.If(busy):
            with m.If(sub == 0):
                d += sub.eq(1)
                d += env_hold.eq(cur_env[shift:])

                with m.If(pend_valid & (pend_voice == slot)):
                    d += pend_valid.eq(0)
                    with m.Switch(slot):
                        for voice in range(voices):
                            with m.Case(voice):
                                with m.Switch(pend_opcode):
                                    with m.Case(OP_NOTE_ON):
                                        d += [
                                            self.step[voice].eq(pend_value),
                                            self.env[voice].eq(0),
                                            self.stage[voice].eq(STAGE_ATTACK),
                                        ]
                                    with m.Case(OP_NOTE_OFF):
                                        d += self.stage[voice].eq(STAGE_RELEASE)
                                    with m.Case(OP_SET_FREQ):
                                        d += self.step[voice].eq(pend_value)
                                    with m.Case(OP_SET_WAVE):
                                        d += self.wave[voice].eq(
                                            pend_value[:self.wave_bits])
                                    with m.Case(OP_SET_AMP):
                                        d += self.level[voice].eq(Mux(
                                            pend_value[:self.sample_bits] > PEAK,
                                            PEAK,
                                            pend_value[:self.sample_bits]))
            with m.Else():
                d += sub.eq(0)
                d += acc.eq(acc + scaled)
                with m.Switch(slot):
                    for voice in range(voices):
                        with m.Case(voice):
                            d += [
                                self.phase[voice].eq(cur_phase + cur_step),
                                self.env[voice].eq(env_next),
                                self.stage[voice].eq(stage_next),
                            ]
                with m.If(slot == voices - 1):
                    # The walk is over. Anything still parked named a voice
                    # that does not exist; drop it rather than let it sit
                    # there and be mistaken for the next command.
                    d += [busy.eq(0), self.sample.eq(clipped), pend_valid.eq(0)]
                with m.Else():
                    d += slot.eq(slot + 1)

        return m


class CommandScheduler(Elaboratable):
    """Pop 64-bit commands and apply them one at a time, on sample boundaries.

    ``delay`` is the number of samples between this command taking effect and
    the previous one taking effect, so a host can express a whole sequence as
    relative gaps and the engine needs no absolute time base. ``delay`` of 0 and
    1 both mean "the very next sample"; larger values are honoured exactly.

    While the FIFO is empty the scheduler simply idles; it does not fabricate
    commands or report an error.
    """

    def __init__(self, fifo, *, domain: str = "pixel"):
        self.fifo = fifo
        self.domain = domain

        self.strobe = Signal()
        self.apply = Signal()
        self.command = Signal(64)
        self.valid = Signal()

    def elaborate(self, platform):
        m = Module()
        d = m.d[self.domain]
        fifo = self.fifo

        holding = Signal(64)
        delay = Signal(DELAY_BITS)
        valid = Signal()

        d += self.apply.eq(0)

        # delay 1 and delay 0 both mean "the next sample": the counter is loaded
        # one short, because the sample it is loaded on already counts.
        load_delay = Mux(
            fifo.r_data[:DELAY_BITS] == 0, 0, fifo.r_data[:DELAY_BITS] - 1
        )

        with m.If(self.strobe):
            with m.If(valid & (delay != 0)):
                d += delay.eq(delay - 1)
            with m.Else():
                with m.If(valid):
                    d += self.apply.eq(1)
                    d += self.command.eq(holding)
                    d += valid.eq(0)
                with m.If(~fifo.r_empty):
                    d += holding.eq(fifo.r_data)
                    d += delay.eq(load_delay)
                    d += valid.eq(1)

        # The combinational read port means the head is already on r_data, so
        # popping is just a one-cycle pulse alongside the fetch. A command is
        # consumed when it is fetched, or when it is applied and the next one is
        # fetched in the same sample.
        m.d.comb += fifo.r_inc.eq(
            self.strobe & ~fifo.r_empty & (~valid | (delay == 0))
        )
        m.d.comb += self.valid.eq(valid)

        return m
