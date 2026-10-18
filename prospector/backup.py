"""Backup do SQLite (API de backup online: seguro mesmo com o painel rodando)."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PREFIX = "prospector-"
CONFIG_FILES = ("templates.yaml", "listas.yaml", "financeiro.yaml", "buscas.yaml")


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class BackupResult:
    path: Path
    size: int
    removed: tuple[Path, ...]


def make_backup(conn: sqlite3.Connection, dest: Path, *, keep: int, config_root: Path | None = None,
                now: datetime | None = None) -> BackupResult:
    """Copia o banco para `dest/prospector-AAAAMMDD-HHMMSS/`, confere a integridade e mantém só os `keep` mais novos."""
    if keep < 1:
        raise BackupError("Precisa manter pelo menos 1 backup")
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    folder = dest.expanduser() / f"{PREFIX}{stamp}"
    folder.mkdir(parents=True, exist_ok=False)
    target = folder / "prospector.db"
    copy = sqlite3.connect(str(target))
    try:
        conn.backup(copy)
        status = copy.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        copy.close()
    if status != "ok":
        shutil.rmtree(folder, ignore_errors=True)
        raise BackupError(f"Backup corrompido (integrity_check: {status})")
    if config_root:
        for name in CONFIG_FILES:
            src = config_root / name
            if src.exists():
                shutil.copy2(src, folder / name)
    return BackupResult(path=folder, size=target.stat().st_size, removed=_rotate(dest.expanduser(), keep))


def _rotate(dest: Path, keep: int) -> tuple[Path, ...]:
    backups = sorted(p for p in dest.glob(f"{PREFIX}*") if p.is_dir())
    old = backups[:-keep] if len(backups) > keep else []
    for p in old:
        shutil.rmtree(p)
    return tuple(old)


def last_backup_time(dest: Path) -> datetime | None:
    backups = list_backups(dest)
    if not backups:
        return None
    try:
        return datetime.strptime(backups[0].name.removeprefix(PREFIX), "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def list_backups(dest: Path) -> list[Path]:
    return sorted((p for p in dest.expanduser().glob(f"{PREFIX}*") if p.is_dir()), reverse=True)
