"""Agent 2 - RUG CHECK: nothing executes without a PASS."""
from __future__ import annotations

from ..config import RugCheckConfig
from ..models import PairSnapshot, RugReport


class RugChecker:
    name = "RUG CHECK"

    def __init__(self, cfg: RugCheckConfig):
        self.cfg = cfg
        self.status = "idle"
        self.passed = 0
        self.failed = 0

    def check(self, s: PairSnapshot) -> RugReport:
        c = self.cfg
        fails: list[str] = []
        if s.dev_pct >= c.max_dev_pct:
            fails.append(f"dev wallet {s.dev_pct:.1f}%")
        if s.top10_pct >= c.max_top10_pct:
            fails.append(f"top10 {s.top10_pct:.1f}%")
        if s.bundled_launch:
            fails.append("bundled launch")
        if not (s.lp_burned or (s.lp_locked and s.lp_lock_days >= c.min_lp_lock_days)):
            fails.append(f"LP not burned / locked only {s.lp_lock_days}d")
        if s.honeypot:
            fails.append("sell simulation failed (honeypot)")
        report = RugReport(passed=not fails, failures=fails)
        if report.passed:
            self.passed += 1
        else:
            self.failed += 1
        self.status = f"{self.passed} pass / {self.failed} fail"
        return report
