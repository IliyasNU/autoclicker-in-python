"""Validated, immutable run settings and atomic JSON persistence (no UI imports)."""
from dataclasses import asdict, dataclass, fields
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading


class ValidationError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(errors.values()))


@dataclass(frozen=True)
class Settings:
    interval: str = "100"
    unit: str = "ms"
    button: str = "left"
    mode: str = "single"
    repeat: str = "infinite"
    count: str = "100"
    delay: str = "0"
    position: str = "cursor"
    x: str = "0"
    y: str = "0"

    @property
    def seconds(self):
        return float(self.interval) / (1000 if self.unit == "ms" else 1)

    @property
    def limit(self):
        return int(self.count) if self.repeat == "count" else None

    @property
    def clicks_per_repeat(self):
        return 2 if self.mode == "double" else 1

    @property
    def coordinates(self):
        return (int(self.x), int(self.y)) if self.position == "fixed" else None

    def validated(self):
        # Validate even inactive fields so corrupt persisted values never hide.
        errors = {}
        for field in fields(self):
            if not isinstance(getattr(self, field.name), str):
                errors[field.name] = "Введите текстовое значение."
        if errors:
            raise ValidationError(errors)
        for name, options in {
            "unit": ("ms", "s"), "button": ("left", "right", "middle"),
            "mode": ("single", "double"), "repeat": ("infinite", "count"),
            "position": ("cursor", "fixed"),
        }.items():
            if getattr(self, name) not in options:
                errors[name] = "Выберите значение из списка."
        for name in ("interval", "delay"):
            try:
                value = float(getattr(self, name))
                seconds = value / (1000 if name == "interval" and self.unit == "ms" else 1)
                if not math.isfinite(seconds) or seconds < 0 or seconds > threading.TIMEOUT_MAX:
                    raise ValueError
                if name == "interval" and seconds < 0.001:
                    errors[name] = "Минимальный интервал — 1 мс (0.001 с)."
            except (ValueError, OverflowError):
                errors[name] = "Введите конечное неотрицательное число (точка для дробей)."
        for name in ("count", "x", "y"):
            text = getattr(self, name).strip()
            if not re.fullmatch(r"[+-]?[0-9]{1,10}", text):
                errors[name] = "Введите целое число."
                continue
            value = int(text)
            if name == "count" and not 1 <= value <= 2_147_483_647:
                errors[name] = "Количество: от 1 до 2147483647."
            elif name != "count" and not -2_147_483_648 <= value <= 2_147_483_647:
                errors[name] = "Координата вне допустимого диапазона."
        if errors:
            raise ValidationError(errors)
        return self


def config_path():
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "autoclicker-in-python" / "settings.json"


def load_settings(path):
    """Return safe defaults and a visible warning on corrupt/unsupported files."""
    try:
        with Path(path).open(encoding="utf-8") as stream:
            document = json.load(stream)
        if not isinstance(document, dict) or document.get("version") != 1:
            raise ValueError("неподдерживаемая версия настроек")
        data = document.get("settings")
        if not isinstance(data, dict) or set(data) != {f.name for f in fields(Settings)}:
            raise ValueError("неверный набор настроек")
        return Settings(**data).validated(), ""
    except FileNotFoundError:
        return Settings(), ""
    except (OSError, ValueError, TypeError) as exc:
        return Settings(), f"Настройки не прочитаны; использованы стандартные: {exc}"


def save_settings(path, settings):
    """Replace only after a fully written valid document; never persist run state."""
    settings.validated()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"version": 1, "settings": asdict(settings)}, stream,
                      ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
