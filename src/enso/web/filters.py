"""Presentation helpers and Jinja filters shared by the viewer's templates and models."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..transport_registry import TRANSPORTS
from . import files

if TYPE_CHECKING:
    from .. import runs


def human_bytes(size: int | None) -> str:
    """``0 B``, ``12 KB``, ``1.2 MB``: enough precision to decide whether to clean up."""
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            break
        value /= 1024
    return f"{int(value)} {unit}" if unit == "B" or value >= 10 else f"{value:.1f} {unit}"


def elapsed(seconds: int) -> str:
    """45s, 2m 05s, 1h 12m."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def duration(duration_ms: int | None) -> str:
    return elapsed(duration_ms // 1000) if duration_ms is not None else "-"


def when(stamp: str | datetime | None) -> str:
    """A stored UTC timestamp (or a datetime) as local wall-clock time, to the second."""
    moment = parse_time(stamp)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S") if moment else "-"


def clock(stamp: str | datetime | None) -> str:
    """Just the wall-clock time; every row that shows it also shows a relative time,
    and a feed's hour heading carries the day, so the column stays one word wide."""
    moment = parse_time(stamp)
    return moment.astimezone().strftime("%H:%M") if moment else "-"


def iso(stamp: str | datetime | None) -> str:
    """The same instant for ``<time datetime>``: ISO 8601 with its offset."""
    moment = parse_time(stamp)
    return moment.astimezone().isoformat(timespec="seconds") if moment else ""


def ago(stamp: str | datetime | None) -> str:
    moment = parse_time(stamp)
    if moment is None:
        return "-"
    return since(stamp) + " ago"


def since(stamp: str | datetime | None) -> str:
    """How long ago, without the word: the time a task has spent in its stage."""
    moment = parse_time(stamp)
    if moment is None:
        return "-"
    seconds = int((datetime.now(UTC) - moment.astimezone(UTC)).total_seconds())
    return elapsed(max(0, seconds))


def until(stamp: str | datetime | None) -> str:
    moment = parse_time(stamp)
    if moment is None:
        return "-"
    seconds = int((moment.astimezone(UTC) - datetime.now(UTC)).total_seconds())
    return "in " + elapsed(max(0, seconds))


def heartbeat_next(stamp: str | datetime | None, state: str) -> str:
    moment = parse_time(stamp)
    if state != "active" or moment is None:
        return "-"
    return "due" if moment <= datetime.now(UTC) else until(stamp)


def parse_time(stamp: str | datetime | None) -> datetime | None:
    """Read a stored timestamp, treating a missing offset as UTC; invalid input is absent."""
    if stamp is None or stamp == "":
        return None
    if isinstance(stamp, datetime):
        moment = stamp
    else:
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def status_class(status: str | None) -> str:
    """The tone for any status word the pages show; never the only signal.

    ``timeout`` is amber rather than red: the run did not fail, it ran out of time,
    and the schedule chart is much easier to read when the two are distinguishable.
    Both still count as failures wherever failures are counted.
    """
    return {
        "ok": "ok",
        "active": "ok",
        "fulfilled": "ok",
        "expired": "warning",
        "cancelled": "warning",
        "paused": "muted",
        "ready": "ok",
        "quiet": "muted",
        "running": "running",
        "error": "error",
        "timeout": "warning",
        "prerun_error": "error",
        "warning": "warning",
        "no_work": "muted",
        "skipped": "muted",
    }.get(status or "", "muted")


def status_word(status: str | None) -> str:
    """The status as prose: ``no_work`` is a database value, ``no work`` is a label."""
    return (status or "").replace("_", " ") or "-"


def heartbeat_event_word(kind: str) -> str:
    return {
        "observed": "Source update",
        "noted": "Note",
        "waited": "Waiting",
        "fulfilled": "Completed",
        "resumed": "Checks resumed",
        "run_failed": "Assessment failed",
        "run_recovered": "Assessments recovered",
        "notice_delivered": "Notification delivered",
    }.get(kind, status_word(kind).capitalize())


def heartbeat_actor(actor: str | None) -> str:
    """Who acted, as a reader can use it.

    Actors are recorded as their origin: ``slack:U0AETSSDDEF`` or ``telegram:8140``
    for a person in chat, ``job:default:nightly`` for a job run, ``beat:HB-002`` for the beat
    acting under its own authority, ``user:gavin`` for this machine's CLI, and plain
    ``heartbeat`` for Enso itself. Only the chat forms need help: the member ID says
    nothing on a page, and what a reader wants is that a person did this from Slack.
    Every other form already reads, so it is left exactly as it was recorded, and the
    expanded event keeps the raw value either way.
    """
    transport, _, identity = (actor or "").partition(":")
    if identity and transport in TRANSPORTS:
        return f"via {transport.capitalize()}"
    return actor or "-"


# A string that starts this way is a filesystem path and reads as code rather than as
# prose; so does one under a key that names a file, because a provider may be configured
# as a bare command that `shutil.which` resolves rather than as a path with slashes.
PATH_STARTS = ("/", "~")
FILE_KEYS = ("path", "unit")
Piece = tuple[str, str]  # how to draw it, and what it says


def fact(value: object, key: str = "") -> tuple[list[Piece], list[Piece]]:
    """One of the doctor's facts as the pieces the Health page draws.

    ``doctor`` hands the viewer whatever a check happens to know -- a path, a flag, a
    list of names, or a map of those per provider -- so the shape of the value picks its
    rendering and no section has to know its own keys. The answer is the line you read
    first and the quiet line under it: a per-item map splits across both, anything else
    is a single piece.
    """
    if isinstance(value, Mapping):
        return _item(value)
    return [_piece(value, key)], []


def _item(item: Mapping) -> tuple[list[Piece], list[Piece]]:
    """A map of one thing's facts: what it is, then what is true of it.

    A flag reads as its own key, because ``executable`` says more under a provider's
    path than ``yes`` does. With nothing to lead on, as a transport has, the flags
    become the line themselves rather than leaving an empty one above them.
    """
    lead: list[Piece] = []
    quiet: list[Piece] = []
    for key, value in item.items():
        label = fact_label(key)
        if isinstance(value, bool):
            quiet.append(("word", label if value else f"not {label}"))
        elif isinstance(value, str) or not isinstance(value, Sequence):
            lead.append(_piece(value, key))
        else:
            names = _names(value)
            quiet.append(("names", names) if names else ("quiet", f"no {label}"))
    return (lead, quiet) if lead else (quiet, [])


def _piece(value: object, key: str = "") -> Piece:
    if value is None:
        return "quiet", "none"
    if isinstance(value, bool):
        return "word", "yes" if value else "no"
    if isinstance(value, str):
        path = key in FILE_KEYS or value.startswith(PATH_STARTS)
        return ("path", value) if path else ("word", value)
    if isinstance(value, Sequence):
        names = _names(value)
        return ("names", names) if names else ("quiet", "none")
    return "word", str(value)


def _names(values: Sequence) -> str:
    return ", ".join(str(value) for value in values)


def fact_label(key: str) -> str:
    """A doctor's key as a label: its own name, minus the underscores."""
    return key.replace("_", " ")


TASK_TONES = {
    "needs-you": "warning",
    "blocked": "error",
    "active": "running",
    "ready": "ok",
    "backlog": "muted",
    "done": "ok",
    "cancelled": "muted",
}


def task_tone(state: str) -> str:
    """The dot tone for a task state (``needs-you``, ``blocked``, ``active``, ...)."""
    return TASK_TONES.get(state, "muted")


BAR_RUNS = 30  # outcomes behind a reliability bar chart
BAR_HEIGHT = 22  # SVG user units, matching svg.bars in app.css
SPARK_RUNS = 18  # outcomes behind a list-row sparkline
QUIET_BAR = 5  # a no-work poll is a stub, not a full bar


def bar_series(summaries: list[runs.RunSummary]) -> list[tuple[str, int]]:
    """``(tone, height)`` per run, oldest first, height proportional to duration."""
    recent = list(reversed(summaries[:BAR_RUNS]))
    longest = max((summary.duration_ms or 0 for summary in recent), default=0)
    series = []
    for summary in recent:
        if summary.status in ("no_work", "skipped"):
            height = QUIET_BAR
        elif longest:
            height = max(6, round((summary.duration_ms or 0) / longest * BAR_HEIGHT))
        else:
            height = BAR_HEIGHT
        series.append((status_class(summary.status), height))
    return series


def spark_tones(summaries: list[runs.RunSummary]) -> list[str]:
    """Just the tones, oldest first: the same history at list-row size."""
    return [status_class(summary.status) for summary in reversed(summaries[:SPARK_RUNS])]


FILTERS: dict[str, Callable] = {
    "bytes": human_bytes,
    "duration": duration,
    "when": when,
    "clock": clock,
    "iso": iso,
    "ago": ago,
    "since": since,
    "until": until,
    "heartbeat_next": heartbeat_next,
    "status_class": status_class,
    "status_word": status_word,
    "heartbeat_event_word": heartbeat_event_word,
    "heartbeat_actor": heartbeat_actor,
    "task_tone": task_tone,
    "fact": fact,
    "fact_label": fact_label,
    # The only filter that emits markup: it wraps nothing but the safe renderer's output.
    "output_markdown": files.render_output,
}
