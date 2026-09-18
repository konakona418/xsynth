# hdl-util/hdmi (vendored)

Upstream: https://github.com/hdl-util/hdmi
Commit:   `83b1c9543a91b776671a44e68e130f81cae437b7`
License:  MIT / Apache-2.0 (see `LICENSE-MIT`, `LICENSE-APACHE`)

`src/*.sv` are copied verbatim from upstream. Any Xsynth-specific changes are
applied as explicit, documented patches from `xsynth/platform/hdmi_patch.py`
rather than by editing the vendored files, so that re-vendoring stays cheap.

## Gowin notes

- The serializer selects its implementation with preprocessor defines. For
  Gowin we must define `GW_IDE` (which selects `OSER10`) and must **not**
  define `SYNTHESIS` (that branch is Xilinx-only).
- The Gowin branch drives `tmds_clock` directly from `clk_pixel`, which is
  correct because the TMDS clock is one bit period per pixel.
- Upstream uses `parameter real VIDEO_RATE` and floating point arithmetic to
  derive the audio clock regeneration N/CTS values. Open-source synthesis may
  not support this; the Apicula flow used here handles it.
