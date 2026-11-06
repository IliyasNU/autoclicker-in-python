"""Validated, immutable run settings and atomic JSON persistence (no UI imports)."""
from dataclasses import asdict, dataclass, fields
from decimal import Decimal, InvalidOperation
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


FUNCTION_KEYS = tuple(f"F{number}" for number in range(1, 13))


def validate_hotkeys(toggle_key, stop_key):
    errors = {}
    if toggle_key not in FUNCTION_KEYS:
        errors["toggle_key"] = "Choose a function key from F1 to F12."
    if stop_key not in FUNCTION_KEYS:
        errors["stop_key"] = "Choose a function key from F1 to F12."
    if not errors and toggle_key == stop_key:
        errors["stop_key"] = "Choose different keys for toggle and emergency stop."
    if errors:
        raise ValidationError(errors)


@dataclass(frozen=True)
class Settings:
    interval: str = "100"
    unit: str = "ms"
    button: str = "left"
    mode: str = "single"
    repeat: str = "infinite"
    count: str = "1"
    delay: str = "0"
    position: str = "cursor"
    x: str = "0"
    y: str = "0"
    toggle_key: str = "F6"
    stop_key: str = "F7"

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
        try:
            validate_hotkeys(self.toggle_key, self.stop_key)
        except ValidationError as exc:
            errors.update(exc.errors)
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


def split_interval(settings):
    """Convert saved seconds/ms to four display fields without float rounding."""
    settings.validated()
    total = Decimal(settings.interval) * (1000 if settings.unit == "s" else 1)
    hours, total = divmod(total, Decimal(3_600_000))
    minutes, total = divmod(total, Decimal(60_000))
    seconds, milliseconds = divmod(total, Decimal(1000))
    ms = format(milliseconds, "f")
    if "." in ms:
        ms = ms.rstrip("0").rstrip(".")
    return dict(hours=str(int(hours)), minutes=str(int(minutes)),
                seconds=str(int(seconds)), milliseconds=ms)


def combine_interval(parts):
    """Validate the four visible fields and return total milliseconds as text."""
    values = {}
    for name in ("hours", "minutes", "seconds", "milliseconds"):
        text = parts.get(name, "")
        if not isinstance(text, str) or not text.strip():
            raise ValidationError({"interval": f"Enter {name}; use 0 for unused fields."})
        text = text.strip()
        if name != "milliseconds":
            if not re.fullmatch(r"[0-9]{1,12}", text):
                raise ValidationError({"interval": f"{name.capitalize()} must be a non-negative whole number."})
            number = Decimal(text)
            if name != "hours" and number > 59:
                raise ValidationError({"interval": f"{name.capitalize()} must be between 0 and 59."})
        else:
            try:
                number = Decimal(text)
                if not number.is_finite() or not 0 <= number < 1000:
                    raise ValueError
            except (InvalidOperation, ValueError):
                raise ValidationError({"interval": "Milliseconds must be a finite number from 0 to less than 1000."}) from None
        values[name] = number
    total = (values["hours"] * 3_600_000 + values["minutes"] * 60_000
             + values["seconds"] * 1000 + values["milliseconds"])
    if not 1 <= total <= Decimal(str(threading.TIMEOUT_MAX)) * 1000:
        raise ValidationError({"interval": "Total interval must be at least 1 ms and within the system timer limit."})
    return format(total, "f")


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
        if not isinstance(document, dict) or document.get("version") not in (1, 2):
            raise ValueError("неподдерживаемая версия настроек")
        data = document.get("settings")
        names = {f.name for f in fields(Settings)}
        if document["version"] == 1:
            names -= {"toggle_key", "stop_key"}
        if not isinstance(data, dict) or set(data) != names:
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
            json.dump({"version": 2, "settings": asdict(settings)}, stream,
                      ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
