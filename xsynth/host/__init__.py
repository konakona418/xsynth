"""Host tools for the Xsynth board.

The command line lives in :mod:`xsynth.cli`; :class:`XsynthClient` is the
serial transport it drives and can also be used directly from Python.
:func:`analyse` measures a capture-card recording.
"""

from xsynth.host.analysis import ToneReport, analyse
from xsynth.host.client import Status, XsynthClient, find_port

__all__ = ["Status", "ToneReport", "XsynthClient", "analyse", "find_port"]
