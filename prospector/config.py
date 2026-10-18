"""Configuração lida do .env (ou do ambiente), com os padrões pedidos."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> dict[str, str]:
    """Lê um .env simples (KEY=VALUE, # comentários). Não sobrescreve o ambiente."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def parse_windows(spec: str) -> tuple[tuple[time, time], ...]:
    """'09:00-11:30,14:00-17:30' -> ((09:00, 11:30), (14:00, 17:30))."""
    windows = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        start_s, _, end_s = part.partition("-")
        start, end = time.fromisoformat(start_s.strip()), time.fromisoformat(end_s.strip())
        if end <= start:
            raise ValueError(f"Janela inválida: {part}")
        windows.append((start, end))
    if not windows:
        raise ValueError("JANELAS vazio")
    return tuple(sorted(windows))


def parse_weekdays(spec: str) -> frozenset[int]:
    """'0,1,2,3,4' (0 = segunda) -> frozenset."""
    days = frozenset(int(d) for d in spec.split(",") if d.strip())
    if not days or any(d < 0 or d > 6 for d in days):
        raise ValueError(f"DIAS_UTEIS inválido: {spec}")
    return days


@dataclass(frozen=True)
class Settings:
    db_path: Path
    data_dir: Path
    templates_path: Path
    listas_path: Path
    financeiro_path: Path
    tz: ZoneInfo
    windows: tuple[tuple[time, time], ...]
    weekdays: frozenset[int]
    max_per_day: int
    max_per_hour: int
    interval_min_s: int
    interval_max_s: int
    typing_min_s: int
    typing_max_s: int
    followup_business_days: int
    gmaps_url: str
    gmaps_depth: int
    gmaps_max_time_s: int
    waakg_url: str
    waakg_session: str
    waakg_api_key: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    web_host: str = "127.0.0.1"
    web_port: int = 5050
    notify: bool = True

    @property
    def has_whatsapp(self) -> bool:
        return bool(self.waakg_session and self.waakg_api_key)


def _int(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} precisa ser inteiro, veio {raw!r}") from exc


def _path(env: dict[str, str], key: str, default: Path) -> Path:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    p = Path(raw).expanduser()
    return p if p.is_absolute() else ROOT / p


def load_settings(env_file: Path | None = None, overrides: dict[str, str] | None = None) -> Settings:
    env = {**load_dotenv(env_file or ROOT / ".env"), **os.environ, **(overrides or {})}
    data_dir = _path(env, "DATA_DIR", ROOT / "data")
    settings = Settings(
        db_path=_path(env, "DB_PATH", data_dir / "prospector.db"),
        data_dir=data_dir,
        templates_path=_path(env, "TEMPLATES_PATH", ROOT / "templates.yaml"),
        listas_path=_path(env, "LISTAS_PATH", ROOT / "listas.yaml"),
        financeiro_path=_path(env, "FINANCEIRO_PATH", ROOT / "financeiro.yaml"),
        tz=ZoneInfo(env.get("TZ_PROSPECTOR", "America/Sao_Paulo")),
        windows=parse_windows(env.get("JANELAS", "09:00-11:30,14:00-17:30")),
        weekdays=parse_weekdays(env.get("DIAS_UTEIS", "0,1,2,3,4")),
        max_per_day=_int(env, "MAX_POR_DIA", 8),
        max_per_hour=_int(env, "MAX_POR_HORA", 3),
        interval_min_s=_int(env, "INTERVALO_MIN_SEG", 4 * 60),
        interval_max_s=_int(env, "INTERVALO_MAX_SEG", 12 * 60),
        typing_min_s=_int(env, "DIGITANDO_MIN_SEG", 5),
        typing_max_s=_int(env, "DIGITANDO_MAX_SEG", 20),
        followup_business_days=_int(env, "FOLLOWUP_DIAS_UTEIS", 3),
        gmaps_url=env.get("GMAPS_URL", "http://localhost:8080").rstrip("/"),
        gmaps_depth=_int(env, "GMAPS_DEPTH", 5),
        gmaps_max_time_s=_int(env, "GMAPS_MAX_TIME_SEG", 300),
        waakg_url=env.get("WAAKG_URL", "http://localhost:3000").rstrip("/"),
        waakg_session=env.get("WAAKG_SESSION", "").strip(),
        waakg_api_key=env.get("WAAKG_API_KEY", "").strip(),
        webhook_secret=env.get("WEBHOOK_SECRET", "").strip(),
        web_host=env.get("WEB_HOST", "127.0.0.1"),
        web_port=_int(env, "WEB_PORT", 5050),
        notify=env.get("NOTIFICAR", "1").strip() not in ("0", "false", "nao", "não"),
    )
    _validate(settings)
    return settings


def _validate(s: Settings) -> None:
    if s.max_per_day < 1 or s.max_per_hour < 1:
        raise ValueError("MAX_POR_DIA e MAX_POR_HORA precisam ser >= 1")
    if not 0 < s.interval_min_s <= s.interval_max_s:
        raise ValueError("INTERVALO_MIN_SEG precisa ser > 0 e <= INTERVALO_MAX_SEG")
    if not 0 < s.typing_min_s <= s.typing_max_s:
        raise ValueError("DIGITANDO_MIN_SEG precisa ser > 0 e <= DIGITANDO_MAX_SEG")
    if s.gmaps_max_time_s < 180:
        raise ValueError("GMAPS_MAX_TIME_SEG precisa ser >= 180 (mínimo do scraper)")
