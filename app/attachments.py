"""Screenshots and files attached to conversation messages, and images among reviewed files.

Images are re-encoded with Pillow before they're stored: that drops metadata (phone photos carry GPS
location), applies the camera's rotation, and means what we serve is a real PNG/JPEG/WebP and nothing else.
"""
import io

from flask import abort, current_app, g, send_file
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.datastructures import FileStorage

from .db import get_db, query, utcnow
from .security import ALLOWED_LABEL, validate_upload
from .storage import get_storage

# extension -> (Pillow format, MIME type, extension we store)
IMAGE_TYPES = {
    "png": ("PNG", "image/png", "png"),
    "jpg": ("JPEG", "image/jpeg", "jpg"),
    "jpeg": ("JPEG", "image/jpeg", "jpg"),
    "webp": ("WEBP", "image/webp", "webp"),
}
IMAGE_MIME = {ext: mime for _, (_, mime, ext) in IMAGE_TYPES.items()}
IMAGE_LABEL = "PNG, JPG or WebP"
ACCEPT = "image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp,.pdf,.doc,.docx,.odt,.rtf,.txt"
MAX_PER_MESSAGE = 5
MAX_PIXELS = 40_000_000  # a 4K screenshot is 8 MP; a modern phone photo up to ~50 MP


def _ext(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""


def is_image_name(filename):
    return _ext(filename) in IMAGE_TYPES


def clean_image(upload):
    """Re-encode an uploaded image. Returns (FileStorage with the clean image, ext, width, height), or raises
    ValueError with a message for the person uploading."""
    want_format, _, ext = IMAGE_TYPES[_ext(upload.filename)]
    try:
        with Image.open(upload.stream) as probe:
            fmt, (w, h) = probe.format, probe.size
            if w * h > MAX_PIXELS:
                raise ValueError(f"{upload.filename} is too large an image ({w}×{h}). Send a smaller screenshot.")
            probe.verify()
        upload.stream.seek(0)
        with Image.open(upload.stream) as img:
            img.load()
            img = ImageOps.exif_transpose(img)
            fmt = fmt if fmt in ("PNG", "JPEG", "WEBP") else want_format
            if fmt == "JPEG" and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            out = io.BytesIO()
            options = {"PNG": {"optimize": True}, "JPEG": {"quality": 90}, "WEBP": {"quality": 90}}[fmt]
            img.save(out, fmt, **options)  # no exif=, so metadata is dropped
            w, h = img.size
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise ValueError(f"{upload.filename} isn't an image we can open. Send a {IMAGE_LABEL} file.") from e
    ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[fmt]
    out.seek(0)
    name = upload.filename.rsplit(".", 1)[0] + "." + ext
    return FileStorage(stream=out, filename=name, content_type=IMAGE_MIME[ext]), ext, w, h


def check(uploads):
    """Validate a list of uploads for a message or for results. Returns (prepared, error) where prepared is
    a list of (file_storage, ext, width, height): images re-encoded, documents checked as for the main upload."""
    uploads = [u for u in uploads if u and u.filename]
    if len(uploads) > MAX_PER_MESSAGE:
        return [], f"Attach up to {MAX_PER_MESSAGE} files at a time."
    prepared = []
    for upload in uploads:
        if is_image_name(upload.filename):
            try:
                prepared.append(clean_image(upload))
            except ValueError as e:
                return [], str(e)
        else:
            ext, error = validate_upload(upload)
            if error:
                if error.startswith("That file type isn't supported"):
                    error = f"That file type isn't supported. Attach a screenshot ({IMAGE_LABEL}) or a {ALLOWED_LABEL} file."
                return [], f"{upload.filename}: {error}"
            prepared.append((upload, ext, None, None))
    return prepared, None


def save(prepared, submission_id, event_id, uploader_id):
    """Store checked uploads against a message. Caller commits; on error the stored files are removed."""
    from .user_views import clean_filename
    storage, saved = get_storage(), []
    try:
        for upload, ext, w, h in prepared:
            stored_name, size, sha = storage.save(upload)
            saved.append(stored_name)
            get_db().execute(
                "INSERT INTO attachments (submission_id, event_id, original_name, stored_name, ext, size, sha256,"
                " width, height, uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (submission_id, event_id, clean_filename(upload.filename), stored_name, ext, size, sha, w, h,
                 uploader_id, utcnow()),
            )
    except Exception:
        for name in saved:
            storage.delete(name)
        raise
    return len(saved)


def by_event(submission_id):
    """{event_id: [attachment, ...]} for a submission's messages."""
    grouped = {}
    for a in query("SELECT * FROM attachments WHERE submission_id = ? ORDER BY id", (submission_id,)):
        grouped.setdefault(a["event_id"], []).append(a)
    return grouped


def serve(attachment_id):
    """Send an attachment to someone allowed to see it: reviewers see all; customers see their own
    submission's attachments, except on internal notes."""
    a = query(
        "SELECT a.*, s.user_id AS owner_id, e.is_internal FROM attachments a"
        " JOIN submissions s ON s.id = a.submission_id JOIN events e ON e.id = a.event_id WHERE a.id = ?",
        (attachment_id,), one=True,
    )
    if a is None or (g.user["role"] != "admin" and (a["owner_id"] != g.user["id"] or a["is_internal"])):
        abort(404)
    path = get_storage().path(a["stored_name"])
    if not path.exists():
        abort(404)
    image = a["ext"] in IMAGE_MIME
    resp = send_file(path, mimetype=IMAGE_MIME[a["ext"]] if image else "application/octet-stream",
                     as_attachment=not image, download_name=a["original_name"], max_age=0)
    resp.headers["Cache-Control"] = "private, no-store"
    if image:
        resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'"
    return resp
