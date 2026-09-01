"""Deterministic mode selection and independent Router compatibility checks."""
from __future__ import annotations

from .models import ModeDecision, ResearchRoute


MONITOR_SIGNALS = ("持续监控", "持续跟踪", "定期监控", "定期跟踪", "监控", "跟踪", "订阅", "告警", "提醒", "monitor", "track", "alert", "notify", "continuously", "periodic")
VERIFY_SIGNALS = ("验证", "核实", "确认", "是否属实", "真假", "verify", "validate", "confirm", "whether", "is it true")


def select_mode(brief: str, requested_mode: str = "auto") -> ModeDecision:
    if requested_mode != "auto":
        return ModeDecision(requested_mode=requested_mode, selected_mode=requested_mode, confidence=1.0,
                            signals=["explicit_mode"])
    lowered = brief.lower()
    # A change-oriented noun alone is normally a one-off investigation
    # ("compare recent pricing changes"), not an instruction to schedule work.
    monitor = [signal for signal in MONITOR_SIGNALS if signal in lowered]
    verify = [signal for signal in VERIFY_SIGNALS if signal in lowered]
    if monitor and len(monitor) >= len(verify):
        selected, signals = "monitor", monitor
    elif verify:
        selected, signals = "verify", verify
    else:
        selected, signals = "investigation", ["open_ended_research"]
    return ModeDecision(requested_mode="auto", selected_mode=selected, confidence=min(0.98, 0.72 + 0.08 * len(signals)), signals=signals)


def route_matches_mode(mode: str, route: ResearchRoute) -> bool:
    allowed = {
        "investigation": {"extract", "compare", "deep_research"},
        "monitor": {"monitor"},
        "verify": {"verify", "extract"},
    }
    return route.task_kind in allowed[mode]
