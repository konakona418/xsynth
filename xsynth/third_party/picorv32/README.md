# YosysHQ/picorv32 (vendored)

Upstream: https://github.com/YosysHQ/picorv32
Commit:   `ef203c2b0a3fb793280f5114941416c425c5b461`
License:  ISC (see `COPYING`)

`picorv32.v` is copied verbatim from upstream. It is read by Yosys's plain
`read_verilog` (not `-sv`) via Amaranth's normal `.v` loop, so it is added with
`platform.add_file("picorv32.v", ...)` and needs no patch machinery. If a future
re-vendoring needs changes, add them as explicit patches in
`xsynth/platform/`, not by editing this file.

`UPSTREAM.md` is upstream's README; it documents the parameters we rely on
(`ENABLE_PCPI`, `ENABLE_IRQ`, `BARREL_SHIFTER`, `COMPRESSED_ISA`, ...).

## Gowin notes

PicoRV32 is technology-neutral Verilog and needs no Gowin-specific handling.
The one thing to watch is the memory interface: `mem_valid`/`mem_ready` is a
single-cycle handshake, so every slave must assert `mem_ready` combinationally
(or the CPU will stall, which is correct but slow). Our BRAM and MMIO slaves
answer in the same cycle.
