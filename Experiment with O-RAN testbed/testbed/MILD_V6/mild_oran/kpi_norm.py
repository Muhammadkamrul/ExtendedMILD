"""kpi_norm.py -- history-free form of ram_pct (v2.2).

Why. ram_pct is the resident memory of the three Python xApp process trees. On this testbed it does not respond
to the injected faults (its mean inside Telemetry episodes equals its healthy mean), but it steps whenever a
process restarts: a fresh Python process holds less memory than one that has run for hours. One such restart
(segment 51 of the first collection, t = 15814) moved the healthy level from 8.6 % to 6.8 % for the rest of the
run, so the held-out test period looked different from the development period (shift 2.9 IQR) for reasons
unrelated to any fault.

What. ram_pct (dataset column) = raw ram_pct minus its minimum over the last RAM_WINDOW ticks of the same
segment (current tick included): the memory the xApps hold above their own recent floor. It is causal (only
past ticks), so the deployed model computes it online the same way (RamExcess). A restart lowers the floor at
once; a slow creep moves the floor within RAM_WINDOW ticks; growth during an episode (14-28 ticks) shows as a
positive excess. The raw value is kept in the dataset column ram_raw_pct (context, not a feature).
"""
from collections import deque

import numpy as np

RAM_WINDOW = 60          # ticks (4 min at 4 s per tick)


def ram_excess_series(raw, groups, window=RAM_WINDOW):
    """Vectorised form for a dataset: raw ram_pct values and the segment of each row (same order)."""
    import pandas as pd
    s = pd.Series(np.asarray(raw, dtype=float))
    g = pd.Series(np.asarray(groups))
    floor = s.groupby(g, sort=False).transform(lambda x: x.rolling(window, min_periods=1).min())
    return (s - floor).values


class RamExcess:
    """Online form (one value per tick), identical to ram_excess_series within one segment."""

    def __init__(self, window=RAM_WINDOW):
        self.buf = deque(maxlen=window)

    def reset(self):
        self.buf.clear()

    def update(self, raw):
        if raw is None or (isinstance(raw, float) and np.isnan(raw)):
            return raw
        self.buf.append(float(raw))
        return float(raw) - min(self.buf)
