"""Schedules: validation and occurrence computation (Europe/Paris, DST aware)."""
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")
UTC = ZoneInfo("UTC")
NAME_RE = re.compile(r"^[\w][\w .,'’!?()&+-]{0,59}$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
FLAVOURS = ("sd", "hd")


class ValidationError(ValueError):
    pass


def _req(d, key, typ):
    if key not in d:
        raise ValidationError(f"missing field: {key}")
    v = d[key]
    # bool is a subclass of int: refuse it explicitly where an int is expected
    if typ is int and isinstance(v, bool) or not isinstance(v, typ):
        raise ValidationError(f"field {key} must be {typ.__name__}")
    return v


def validate(d):
    """Return a clean schedule dict, or raise ValidationError. Never coerces."""
    if not isinstance(d, dict):
        raise ValidationError("schedule must be an object")
    allowed = {"id", "name", "service", "channel", "flavour", "kind", "days", "date",
               "start", "end", "margin_before", "margin_after", "enabled"}
    extra = set(d) - allowed
    if extra:
        raise ValidationError(f"unknown fields: {', '.join(sorted(extra))}")
    s = {}
    s["name"] = _req(d, "name", str).strip()
    if not NAME_RE.match(s["name"]) or ".." in s["name"]:
        raise ValidationError("name: 1-60 letters, digits, spaces and simple punctuation")
    s["service"] = _req(d, "service", int)
    if not 1 <= s["service"] <= 99999:
        raise ValidationError("service out of range")
    s["channel"] = _req(d, "channel", str).strip()
    if not s["channel"] or len(s["channel"]) > 60 or re.search(r"[\x00-\x1f\x7f]", s["channel"]):
        raise ValidationError("channel: 1-60 printable characters")
    s["flavour"] = _req(d, "flavour", str)
    if s["flavour"] not in FLAVOURS:
        raise ValidationError("flavour must be sd or hd")
    s["kind"] = _req(d, "kind", str)
    if s["kind"] == "weekly":
        days = _req(d, "days", list)
        if not days or any(isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= 6 for x in days) \
                or len(set(days)) != len(days):
            raise ValidationError("days: distinct integers 0 (Monday) to 6 (Sunday)")
        s["days"] = sorted(days)
    elif s["kind"] == "once":
        ds = _req(d, "date", str)
        try:
            date.fromisoformat(ds)
        except ValueError:
            raise ValidationError("date must be YYYY-MM-DD") from None
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", ds):
            raise ValidationError("date must be YYYY-MM-DD")
        s["date"] = ds
    else:
        raise ValidationError("kind must be weekly or once")
    for k in ("start", "end"):
        s[k] = _req(d, k, str)
        if not HHMM_RE.match(s[k]):
            raise ValidationError(f"{k} must be HH:MM")
    if s["start"] == s["end"]:
        raise ValidationError("start and end must differ")
    for k in ("margin_before", "margin_after"):
        s[k] = _req(d, k, int)
        if not 0 <= s[k] <= 60:
            raise ValidationError(f"{k}: 0 to 60 minutes")
    s["enabled"] = _req(d, "enabled", bool)
    s["id"] = d.get("id") or uuid.uuid4().hex[:12]
    if not isinstance(s["id"], str) or not re.match(r"^[0-9a-f]{12}$", s["id"]):
        raise ValidationError("bad id")
    return s


@dataclass(frozen=True)
class Occurrence:
    schedule_id: str
    name: str
    service: int
    flavour: str
    start: datetime   # UTC, margin included
    end: datetime     # UTC, margin included
    show_start: datetime
    channel: str

    @property
    def key(self):
        return f"{self.schedule_id}@{self.show_start.isoformat()}"


def _local(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), TZ)


def occurrence_on(s, day):
    """Occurrence of schedule s whose show starts on `day`, or None."""
    if s["kind"] == "weekly" and day.weekday() not in s["days"]:
        return None
    if s["kind"] == "once" and day != date.fromisoformat(s["date"]):
        return None
    st = _local(day, s["start"])
    end_day = day + timedelta(days=1) if s["end"] < s["start"] else day
    en = _local(end_day, s["end"])
    # Kept in UTC: Python subtracts and compares two datetimes sharing the same tzinfo on
    # wall-clock time, which is wrong across a DST change. now() is in TZ (another tzinfo),
    # so comparisons and differences with these values use real elapsed time.
    st_u = st.astimezone(UTC) - timedelta(minutes=s["margin_before"])
    en_u = en.astimezone(UTC) + timedelta(minutes=s["margin_after"])
    return Occurrence(s["id"], s["name"], s["service"], s["flavour"], st_u, en_u, st, s["channel"])


def upcoming(schedules, now, limit=20, horizon_days=31):
    """Occurrences not yet finished at `now`, soonest first."""
    out = []
    today = now.astimezone(TZ).date()
    for s in schedules:
        if not s["enabled"]:
            continue
        for i in range(-1, horizon_days):
            o = occurrence_on(s, today + timedelta(days=i))
            if o and o.end > now:
                out.append(o)
    out.sort(key=lambda o: o.start)
    return out[:limit]


def due(schedules, now):
    """Occurrences whose recording window contains `now`."""
    return [o for o in upcoming(schedules, now, limit=1000, horizon_days=1) if o.start <= now < o.end]
