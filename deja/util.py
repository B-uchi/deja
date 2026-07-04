"""Small shared formatting helpers."""
from __future__ import annotations

import time


def fmt_age(ts: float, now: float | None = None) -> str:
    """Compact relative time: 'now', '42s', '5m', '3h', '2d', '3w', '5mo'."""
    delta = (now or time.time()) - ts
    if delta < 5:
        return "now"
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    if delta < 7 * 86400:
        return f"{int(delta // 86400)}d"
    if delta < 30 * 86400:
        return f"{int(delta // (7 * 86400))}w"
    return f"{int(delta // (30 * 86400))}mo"


def preview(content: str, width: int = 64) -> str:
    """One printable line, whitespace collapsed, ellipsized."""
    flat = " ".join(content.split())
    if len(flat) > width:
        flat = flat[: width - 1] + "…"
    return flat


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"
