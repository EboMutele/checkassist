"""Shared lookups, formatting and document text extraction."""
import html
import re
import zipfile
from datetime import datetime, timezone

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
