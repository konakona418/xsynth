"""Emulated-LVDS I/O buffers for the Tang Nano 9K's HDMI pins.

Amaranth's Gowin platform drives differential outputs with the true-LVDS
``TLVDS_TBUF`` primitive. The Tang Nano 9K's HDMI pins are *emulated* LVDS
pins, so gowin_pack rejects that ("it is an emulated lvds pin").

Hand-instantiating ``ELVDS_OBUF`` instead works through packing, but it has no
output-enable input, and nextpnr-gowin then packs it into an ``ELVDS_IOBUF``
whose output enable is left unconnected, so the pair never actually drives.

The fix is to keep Amaranth's normal ``platform.request(..., dir="o")`` flow,
which drives the output enable correctly, and only swap the primitive it emits
for the emulated-LVDS one. This module does that by overriding
``GowinPlatform.get_io_buffer``.
"""

from __future__ import annotations

from amaranth import Instance, Module
from amaranth.lib import io, wiring


class ELVDSInnerBuffer(wiring.Component):
    """Like Amaranth's Gowin ``InnerBuffer``, but emulated LVDS."""

    def __init__(self, direction, port):
        self.direction = direction
        self.port = port
        members = {}
        if direction is not io.Direction.Output:
            members["i"] = wiring.In(len(port))
        if direction is not io.Direction.Input:
            members["o"] = wiring.Out(len(port))
            members["t"] = wiring.Out(len(port))
        super().__init__(wiring.Signature(members).flip())

    def elaborate(self, platform):
        m = Module()

        for bit in range(len(self.port)):
            name = f"buf{bit}"
            if isinstance(self.port, io.SingleEndedPort):
                if self.direction is io.Direction.Input:
                    m.submodules[name] = Instance(
                        "IBUF", i_I=self.port.io[bit], o_O=self.i[bit],
                    )
                elif self.direction is io.Direction.Output:
                    m.submodules[name] = Instance(
                        "TBUF", i_OEN=self.t[bit], i_I=self.o[bit],
                        o_O=self.port.io[bit],
                    )
                else:
                    m.submodules[name] = Instance(
                        "IOBUF", i_OEN=self.t[bit], i_I=self.o[bit],
                        o_O=self.i[bit], io_IO=self.port.io[bit],
                    )
            elif isinstance(self.port, io.DifferentialPort):
                if self.direction is io.Direction.Input:
                    m.submodules[name] = Instance(
                        "ELVDS_IBUF",
                        i_I=self.port.p[bit], i_IB=self.port.n[bit],
                        o_O=self.i[bit],
                    )
                elif self.direction is io.Direction.Output:
                    m.submodules[name] = Instance(
                        "ELVDS_TBUF",
                        i_OEN=self.t[bit], i_I=self.o[bit],
                        o_O=self.port.p[bit], o_OB=self.port.n[bit],
                    )
                else:
                    m.submodules[name] = Instance(
                        "ELVDS_IOBUF",
                        i_OEN=self.t[bit], i_I=self.o[bit], o_O=self.i[bit],
                        io_IO=self.port.p[bit], io_IOB=self.port.n[bit],
                    )
            else:
                raise TypeError(f"Unknown port type {self.port!r}")

        return m


class ELVDSIOBuffer(io.Buffer):
    """Like Amaranth's Gowin ``IOBuffer``, but built on :class:`ELVDSInnerBuffer`."""

    def elaborate(self, platform):
        m = Module()

        m.submodules.buf = buf = ELVDSInnerBuffer(self.direction, self.port)
        inv_mask = sum(inv << bit for bit, inv in enumerate(self.port.invert))

        if self.direction is not io.Direction.Output:
            m.d.comb += self.i.eq(buf.i ^ inv_mask)

        if self.direction is not io.Direction.Input:
            m.d.comb += buf.o.eq(self.o ^ inv_mask)
            m.d.comb += buf.t.eq(~self.oe.replicate(len(self.port)))

        return m


def install_elvds_buffers() -> None:
    """Make the Gowin platform use emulated-LVDS buffers for differential ports."""
    from amaranth.vendor import _gowin

    original = _gowin.GowinPlatform.get_io_buffer

    def get_io_buffer(self, buffer):
        if not (isinstance(buffer, io.Buffer)
                and isinstance(buffer.port, io.DifferentialPort)):
            return original(self, buffer)

        # Mirrors GowinPlatform.get_io_buffer, but with ELVDSIOBuffer. The
        # port connections below are essential: without them the output data
        # and enable are left undriven and the pair is tied to ground.
        result = ELVDSIOBuffer(buffer.direction, buffer.port)
        if buffer.direction is not io.Direction.Output:
            result.i = buffer.i
        if buffer.direction is not io.Direction.Input:
            result.o = buffer.o
            result.oe = buffer.oe
        return result

    _gowin.GowinPlatform.get_io_buffer = get_io_buffer
