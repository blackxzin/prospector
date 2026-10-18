"""Notificação no desktop (notify-send), com fallback para o log."""

from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger("prospector")


def notify(title: str, body: str, *, urgent: bool = False, enabled: bool = True) -> bool:
    log.warning("%s: %s", title, body) if urgent else log.info("%s: %s", title, body)
    if not enabled:
        return False
    exe = shutil.which("notify-send")
    if not exe:
        return False
    cmd = [exe, "--app-name=prospector", f"--urgency={'critical' if urgent else 'normal'}", title, body[:500]]
    try:
        subprocess.run(cmd, check=False, timeout=5, capture_output=True)  # noqa: S603 (args fixos)
        return True
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("notify-send falhou: %s", exc)
        return False
