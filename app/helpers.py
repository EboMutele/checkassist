"""Shared lookups, formatting and document text extraction."""
import html
import math
import re
import zipfile
from datetime import datetime, timedelta, timezone

STATUSES = {
    "submitted": {"label": "Submitted", "tone": "neutral", "user_hint": "Waiting for a reviewer to pick it up."},
    "in_review": {"label": "In review", "tone": "info", "user_hint": "A reviewer is working on your document."},
    "needs_info": {"label": "Needs your input", "tone": "warn", "user_hint": "Your reviewer has a question. Reply below."},
    "completed": {"label": "Completed", "tone": "good", "user_hint": "Your feedback is ready."},
}
OPEN_STATUSES = ("submitted", "in_review", "needs_info")


def ref(sub_id: int) -> str:
    return f"CA-{sub_id:05d}"


def parse_ts(value):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def human_date(value) -> str:
    dt = parse_ts(value)
    if not dt:
        return "—"
    return f"{dt:%b} {dt.day}, {dt:%Y}, {dt:%H:%M} UTC"


def time_ago(value) -> str:
    dt = parse_ts(value)
    if not dt:
        return "—"
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if secs >= size:
            return f"{secs // size} {unit} ago"
    return "just now"


def file_size(num) -> str:
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


# ---- text preview for reviewers -------------------------------------------------

PREVIEW_LIMIT = 20_000
_MAX_XML_BYTES = 20 * 1024 * 1024  # guard against zip bombs
_TAG_RE = re.compile(r"<[^>]+>")


def _zip_xml_text(path, member, para_close):
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(member)
        if info.file_size > _MAX_XML_BYTES:
            return None
        xml = zf.read(member).decode("utf-8", "replace")
    xml = xml.replace(para_close, "\n").replace("<w:tab/>", "\t").replace("<w:br/>", "\n")
    return html.unescape(_TAG_RE.sub("", xml))


def extract_preview(path, ext):
    """Best-effort plain text for an at-a-glance preview. Returns None when unavailable."""
    try:
        if ext == "txt":
            with open(path, "rb") as f:
                text = f.read(PREVIEW_LIMIT * 4).decode("utf-8", "replace")
        elif ext == "docx":
            text = _zip_xml_text(path, "word/document.xml", "</w:p>")
        elif ext == "odt":
            text = _zip_xml_text(path, "content.xml", "</text:p>")
        elif ext == "pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return None
            reader = PdfReader(str(path))
            parts, total = [], 0
            for page in reader.pages[:30]:
                chunk = page.extract_text() or ""
                parts.append(chunk)
                total += len(chunk)
                if total > PREVIEW_LIMIT:
                    break
            text = "\n\n".join(parts)
        else:
            return None
    except Exception:
        return None
    if text is None:
        return None
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return None
    if len(text) > PREVIEW_LIMIT:
        text = text[:PREVIEW_LIMIT] + "\n\n[Preview truncated. Download the file to read the rest.]"
    return text


def word_count(text) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


# ---- review-time promise -------------------------------------------------------

def minutes_label(n) -> str:
    n = int(n)
    return f"{n} minute" if n == 1 else f"{n} minutes"


def clock(seconds) -> str:
    """Stopwatch text: 7:05, 12:05, 1:02:05."""
    s = max(0, int(math.floor(seconds or 0)))
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def paused_seconds(status_events, until=None):
    """Seconds spent waiting on the customer, from (created_at, new_status) pairs in time order.

    The clock stops when a reviewer marks a document "needs_info" and restarts on the next
    status change. `until` caps the count (the first send, or now).
    """
    until = until or datetime.now(timezone.utc)
    total, started = 0.0, None
    for created_at, new_status in status_events:
        ts = parse_ts(created_at)
        if ts is None or ts > until:
            break
        if new_status == "needs_info":
            started = started or ts
        elif started is not None:
            total += (ts - started).total_seconds()
            started = None
    if started is not None:
        total += (until - started).total_seconds()
    return total


def timing(created_at, status, first_sent_at=None, review_minutes=20, now=None, status_events=()):
    """How long a submission has been in review, excluding time spent waiting on the customer.

    Customers only ever see elapsed time (`elapsed_seconds`, ticking from `started_at`).
    The promise (`review_minutes`) is used internally: reviewers' highlighting and stats.

    state is one of:
      "done"     feedback was sent; `took` is active minutes from upload to first send
      "waiting"  the reviewer asked the customer something; the clock is paused at `elapsed_seconds`
      "due"      running and within the promise
      "overdue"  running and past the promise (reviewers only; customers see the same timer)
      "running"  running, no promise configured
      None       nothing to time
    `running` is True for the last three.
    """
    now = now or datetime.now(timezone.utc)
    created = parse_ts(created_at)
    t = {"state": None, "promise": review_minutes, "running": False}
    if created is None:
        return t
    if first_sent_at:
        sent = parse_ts(first_sent_at)
        active = (sent - created).total_seconds() - paused_seconds(status_events, until=sent)
        took = max(1, round(active / 60))
        t.update(state="done", took=took, on_time=bool(review_minutes) and took <= review_minutes)
        return t
    if status == "completed":
        return t
    paused = paused_seconds(status_events, until=now)
    elapsed = max(0.0, (now - created).total_seconds() - paused)
    t["elapsed_seconds"] = elapsed
    if status == "needs_info":
        t["state"] = "waiting"
        return t
    # The moment the clock would have started had it never paused: elapsed = now - started_at.
    t["started_at"] = (created + timedelta(seconds=paused)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t["running"] = True
    if not review_minutes:
        t["state"] = "running"
        return t
    limit = review_minutes * 60
    t["limit_seconds"] = limit
    t["state"] = "due" if elapsed <= limit else "overdue"
    return t


def load_timings(rows, review_minutes, now=None):
    """timing() for many submission rows at once: {submission_id: timing}.

    Rows need id, created_at, status and first_sent_at (see FIRST_SENT_SQL).
    """
    from .db import query  # local import keeps helpers free of app state at import time

    rows = list(rows)
    if not rows:
        return {}
    ids = [r["id"] for r in rows]
    by_sub = {i: [] for i in ids}
    for i in range(0, len(ids), 500):  # stay well under SQLite's parameter limit
        chunk = ids[i:i + 500]
        for e in query(
            f"SELECT submission_id, created_at, body FROM events WHERE type = 'status'"
            f" AND submission_id IN ({','.join('?' * len(chunk))}) ORDER BY created_at, id",
            chunk,
        ):
            by_sub[e["submission_id"]].append((e["created_at"], e["body"]))
    return {
        r["id"]: timing(r["created_at"], r["status"], r["first_sent_at"], review_minutes, now, by_sub[r["id"]])
        for r in rows
    }


def status_events_of(events):
    """(created_at, new_status) pairs from a list of event rows."""
    return [(e["created_at"], e["body"]) for e in events if e["type"] == "status"]


FIRST_SENT_SQL = (
    "(SELECT MIN(e.created_at) FROM events e WHERE e.submission_id = s.id AND e.type = 'results_sent')"
)
