"""Run an Amaranth design that contains Verilog black boxes under iverilog.

Amaranth's own simulator cannot execute a Verilog ``Instance``, so any design
that instantiates one -- PicoRV32, the HDMI core -- has to be simulated by
emitting Verilog and handing it to an external simulator. iverilog is small and
fast enough for our designs, and keeping the testbench in plain Verilog means
nothing has to be translated.

The testbench decides the verdict: ``$fatal`` fails the run and ``$finish``
passes it, which is how the exit status is derived.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from amaranth.back import verilog


def to_verilog(design, *, name: str, ports) -> str:
    """Elaborate an Amaranth design to Verilog with the given top-level ports."""
    return verilog.convert(design, name=name, ports=list(ports))


def run(design, *, name: str, ports, testbench: str, sources=(),
        keep: str | None = None) -> str:
    """Compile and run ``design`` against ``testbench``, returning its output.

    ``sources`` are extra Verilog files the design instantiates (the vendored
    PicoRV32, for instance). ``keep`` writes the generated files to a directory
    so a failure can be looked at afterwards.

    Raises :class:`RuntimeError` if iverilog cannot compile the design and
    :class:`AssertionError`, carrying the simulator output, if the testbench
    calls ``$fatal``.
    """
    directory = Path(keep) if keep is not None else None
    with tempfile.TemporaryDirectory() as tmp:
        root = directory if directory is not None else Path(tmp)
        root.mkdir(parents=True, exist_ok=True)

        top = root / f"{name}.v"
        top.write_text(to_verilog(design, name=name, ports=ports))
        bench = root / "testbench.v"
        bench.write_text(testbench)

        binary = root / "simulation"
        files = [top, bench, *(Path(source) for source in sources)]
        compiled = subprocess.run(
            ["iverilog", "-g2012", "-s", "testbench", "-o", str(binary),
             *[str(file) for file in files]],
            capture_output=True, text=True,
        )
        if compiled.returncode:
            raise RuntimeError(
                "iverilog could not compile the design:\n"
                + compiled.stdout + compiled.stderr
            )

        executed = subprocess.run(
            ["vvp", str(binary)], capture_output=True, text=True,
        )

    output = executed.stdout + executed.stderr
    if executed.returncode:
        raise AssertionError(output)
    return output
