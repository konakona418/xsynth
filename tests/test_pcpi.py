"""The command co-processor: pushing, and stalling when there is no room.

The acceptance gate (see PLAN.md) is that a full command FIFO stalls the CPU
rather than losing a command. That is a property of the co-processor's
handshake, so it is tested here at the signal level, where the FIFO depth can
be made small enough to fill deliberately.

The handshake is combinational: an instruction presented in cycle N is
acknowledged in cycle N, and the FIFO write lands on that same edge. The tests
therefore sample ``ready``/``wait``/``w_inc`` in the cycle the instruction is
presented, before the clock edge that acts on it.
"""

from amaranth import Elaboratable, Module
from amaranth.sim import Simulator

from xsynth.hdl.fifo import AsyncFifo
from xsynth.hdl.pcpi import CommandCoProcessor, level_instruction, push_instruction

DEPTH = 4


class Harness(Elaboratable):
    """A co-processor on a real FIFO, with the PCPI lines under test control."""

    def __init__(self):
        self.fifo = AsyncFifo(width=64, depth=DEPTH,
                              w_domain="sync", r_domain="sync")
        self.coproc = CommandCoProcessor(self.fifo)

        self.valid = self.coproc.valid
        self.insn = self.coproc.insn
        self.rs1 = self.coproc.rs1
        self.rs2 = self.coproc.rs2
        self.wait = self.coproc.wait
        self.ready = self.coproc.ready
        self.wr = self.coproc.wr
        self.rd = self.coproc.rd

    def elaborate(self, platform):
        m = Module()
        m.submodules.fifo = self.fifo
        m.submodules.coproc = self.coproc
        m.d.comb += [self.fifo.w_rst.eq(0), self.fifo.r_rst.eq(0)]
        return m


def present(ctx, harness, *, insn, rs1=0, rs2=0):
    """Drive one PCPI instruction and read its handshake in the same cycle."""
    ctx.set(harness.insn, insn)
    ctx.set(harness.rs1, rs1)
    ctx.set(harness.rs2, rs2)
    ctx.set(harness.valid, 1)
    return {
        "ready": ctx.get(harness.ready),
        "wait": ctx.get(harness.wait),
        "w_inc": ctx.get(harness.fifo.w_inc),
        "level": ctx.get(harness.fifo.w_level),
    }


def test_a_push_goes_into_the_fifo_and_never_blocks_when_there_is_room():
    harness = Harness()
    taken = []

    async def bench(ctx):
        for value in range(DEPTH):
            seen = present(ctx, harness,
                           insn=push_instruction(0, 0),
                           rs1=value, rs2=0xA000 + value)
            await ctx.tick()
            taken.append(seen)
        ctx.set(harness.valid, 0)
        await ctx.tick()
        assert ctx.get(harness.fifo.w_level) == DEPTH

    sim = Simulator(harness)
    sim.add_clock(1e-6)
    sim.add_testbench(bench)
    sim.run()

    assert all(seen["ready"] and not seen["wait"] for seen in taken), taken
    assert all(seen["w_inc"] for seen in taken), taken


def test_a_push_into_a_full_fifo_stalls_and_keeps_the_command():
    harness = Harness()
    observed = {}

    async def bench(ctx):
        # Fill the FIFO.
        for value in range(DEPTH):
            present(ctx, harness, insn=push_instruction(0, 0), rs1=value)
            await ctx.tick()

        # One more push, into a full FIFO: it must stall, not drop.
        for _ in range(4):
            seen = present(ctx, harness, insn=push_instruction(0, 0),
                           rs1=0xCAFE, rs2=0xBEEF)
            await ctx.tick()
        observed["stalled"] = seen

        # Make room. The held instruction is still presented, and must complete
        # by itself on the first cycle the FIFO can take it -- which is two
        # cycles after the pop, once the read pointer has crossed back.
        ctx.set(harness.fifo.r_inc, 1)
        await ctx.tick()
        ctx.set(harness.fifo.r_inc, 0)
        for _ in range(8):
            seen = present(ctx, harness, insn=push_instruction(0, 0),
                           rs1=0xCAFE, rs2=0xBEEF)
            if seen["ready"]:
                break
            await ctx.tick()
        observed["after_room"] = seen
        await ctx.tick()
        ctx.set(harness.valid, 0)
        await ctx.tick()

        words = []
        while not ctx.get(harness.fifo.r_empty):
            words.append(ctx.get(harness.fifo.r_data))
            ctx.set(harness.fifo.r_inc, 1)
            await ctx.tick()
            ctx.set(harness.fifo.r_inc, 0)
            await ctx.tick()
        observed["words"] = words

    sim = Simulator(harness)
    sim.add_clock(1e-6)
    sim.add_testbench(bench)
    sim.run()

    stalled = observed["stalled"]
    assert stalled["wait"], "a full fifo must stall the cpu"
    assert not stalled["ready"], "a stalled push must not complete"
    assert not stalled["w_inc"], "a stalled push must not write"
    assert stalled["level"] == DEPTH

    assert observed["after_room"]["ready"], "the push must complete once there is room"
    assert (0xBEEF << 32) | 0xCAFE in observed["words"], observed["words"]


def test_the_level_instruction_reads_the_fifo_without_consuming_it():
    harness = Harness()
    observed = {}

    async def bench(ctx):
        for value in range(2):
            present(ctx, harness, insn=push_instruction(0, 0), rs1=value)
            await ctx.tick()
        ctx.set(harness.valid, 0)
        await ctx.tick()

        ctx.set(harness.insn, level_instruction(0))
        ctx.set(harness.valid, 1)
        observed["wr"] = ctx.get(harness.wr)
        observed["rd"] = ctx.get(harness.rd)
        observed["ready"] = ctx.get(harness.ready)
        await ctx.tick()
        ctx.set(harness.valid, 0)
        await ctx.tick()
        observed["level"] = ctx.get(harness.fifo.w_level)

    sim = Simulator(harness)
    sim.add_clock(1e-6)
    sim.add_testbench(bench)
    sim.run()

    assert observed["wr"], "reading the level should write back"
    assert observed["rd"] == 2, observed
    assert observed["ready"] and observed["level"] == 2, "the level must not pop"


def test_a_foreign_custom_instruction_is_left_alone():
    """Anything we do not claim must stay unacknowledged, so the CPU traps."""
    harness = Harness()
    observed = {}

    async def bench(ctx):
        seen = present(ctx, harness, insn=push_instruction(0, 0) ^ 0x7F)
        observed.update(seen)
        await ctx.tick()
        ctx.set(harness.valid, 0)
        await ctx.tick()
        observed["level"] = ctx.get(harness.fifo.w_level)

    sim = Simulator(harness)
    sim.add_clock(1e-6)
    sim.add_testbench(bench)
    sim.run()

    assert not observed["ready"], "a foreign instruction must not complete"
    assert not observed["wait"], "a foreign instruction must not be stalled"
    assert not observed["w_inc"], "a foreign instruction must not write"
    assert observed["level"] == 0, "a foreign instruction must not write"
