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
    """12:05 style countdown text."""
    s = max(0, int(math.ceil(seconds)))
    return f"{s // 60}:{s % 60:02d}"


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
    """Where a submission stands against the review-time promise.

    Time spent waiting on the customer (status "needs_info") doesn't count.
    state is one of:
      "done"     feedback was sent; `took` is active minutes from upload to first send
      "waiting"  the reviewer asked the customer something; the clock is paused
      "due"      within the promise; `seconds_left`, `minutes_left`, `due_at`, `total_seconds`
      "overdue"  past the promise; `minutes_over`, `due_at`
      None       no promise configured, or nothing to say
    """
    now = now or datetime.now(timezone.utc)
    created = parse_ts(created_at)
    t = {"state": None, "promise": review_minutes}
    if created is None:
        return t
    if first_sent_at:
        sent = parse_ts(first_sent_at)
        active = (sent - created).total_seconds() - paused_seconds(status_events, until=sent)
        took = max(1, round(active / 60))
        t.update(state="done", took=took, on_time=bool(review_minutes) and took <= review_minutes)
        return t
    if status == "needs_info":
        t["state"] = "waiting"
        return t
    if not review_minutes or status == "completed":
        return t
    paused = paused_seconds(status_events, until=now)
    due = created + timedelta(minutes=review_minutes, seconds=paused)
    secs = (due - now).total_seconds()
    t["due_at"] = due.strftime("%Y-%m-%dT%H:%M:%SZ")
    t["due_hhmm"] = due.strftime("%H:%M")
    t["total_seconds"] = review_minutes * 60
    if secs >= 0:
        t.update(state="due", seconds_left=secs, minutes_left=max(1, math.ceil(secs / 60)))
    else:
        t.update(state="overdue", minutes_over=max(1, math.floor(-secs / 60)))
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
