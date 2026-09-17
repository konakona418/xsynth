"""Phase 3: the PicoRV32 soft core on the board.

Phase 3 is phase 2 plus the SoC. The UART protocol, the command FIFO and the
voice are untouched; what is new is the loader, which lets the host upload a
program into the CPU's memory and start it, and the CPU's status, which rides
along in the status reply.
"""

from __future__ import annotations

from xsynth.hdl.phase2 import DEFAULT_BAUD, DEFAULT_FIFO_DEPTH, Phase2, Phase2Core
from xsynth.hdl.soc import DEFAULT_MEM_WORDS, SoC
from xsynth.hdl.video_modes import DEFAULT_MODE


class Phase3(Phase2):
    """Phase 3 on the board: phase 2 plus the PicoRV32 soft core."""

    def __init__(self, mode=DEFAULT_MODE, *, mem_words: int = DEFAULT_MEM_WORDS,
                 baud: int = DEFAULT_BAUD,
                 fifo_depth: int = DEFAULT_FIFO_DEPTH, **kwargs):
        super().__init__(mode, baud=baud, fifo_depth=fifo_depth, **kwargs)
        self.mem_words = mem_words

    def make_core(self):
        return Phase2Core(
            baud=self.baud,
            fifo_depth=self.fifo_depth,
            soc=SoC(mem_words=self.mem_words),
        )
