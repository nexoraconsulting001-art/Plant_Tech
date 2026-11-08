"""Small shared helpers: ids, India time, Indian mobile numbers, money, a per-process throttle."""
import re
import uuid
from datetime import datetime, timedelta, timezone

from cachetools import TTLCache

IST = timezone(timedelta(hours=5, minutes=30))


def new_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ist() -> datetime:
    return datetime.now(IST)


def date_key(dt: datetime | None = None) -> str:
    """Calendar day in India, e.g. '2026-10-16' (what "today" means for a nursery)."""
    return (dt or now_ist()).astimezone(IST).strftime("%Y-%m-%d")


def normalize_in_phone(raw: str | None) -> str | None:
    """'98111 22334', '+91-98111-22334' or '098111…' -> '+919811122334'. None if not an Indian mobile."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "6789":
        return None
    return "+91" + digits


def money(x: float) -> float:
    return round(float(x) + 1e-9, 2)


class Throttle:
    """Lets each key through at most once every `seconds` in this process, e.g. a safety-net check per shop that
    should not cost a database call on every keystroke. One event loop per process, so no lock is needed."""

    def __init__(self, seconds: float, maxsize: int = 50_000):
        self._seen: TTLCache = TTLCache(maxsize=maxsize, ttl=seconds)

    def due(self, key) -> bool:
        """True (and the key is counted) when the key was not let through in the last `seconds`."""
        if key in self._seen:
            return False
        self._seen[key] = True
        return True

    def forget(self, key=None):
        """Let `key` (or every key) through again next time, e.g. after the check failed."""
        if key is None:
            self._seen.clear()
        else:
            self._seen.pop(key, None)


FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_text(value):
    """Typed text (a customer's or item's name) for a CSV cell: a spreadsheet would run a cell starting with = + - @
    as a formula (e.g. =HYPERLINK sending the next cells away), so such text gets a leading ' (OWASP CSV
    injection). Anything that is not a string is returned as it is."""
    if isinstance(value, str) and value.startswith(FORMULA_START):
        return "'" + value
    return value


def name_key(name: str) -> str:
    """Case/spacing-insensitive key for matching product names ('Money  plant' == 'money plant')."""
    return re.sub(r"[^a-z0-9ऀ-ॿ]+", " ", (name or "").lower()).strip()
