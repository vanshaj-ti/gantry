"""Cost/token reporting: `gantry cost`."""
from __future__ import annotations

from ..cost import report_for_run, total_all_runs
from ..state import RunStore
from ._shared import _out, _target


def cmd_cost(args) -> int:
    """No --run: repo-wide total across every run, plus the most expensive
    runs. --run: that run's own per-stage breakdown."""
    store = RunStore(_target())
    if args.run:
        return _out({"run_id": args.run, **report_for_run(store, args.run)})
    return _out(total_all_runs(store))
