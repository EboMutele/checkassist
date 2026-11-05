"""File storage. Local disk now; swap in an S3/R2 class with the same methods later."""
import hashlib
import secrets
from pathlib import Path

from flask import current_app


class LocalStorage:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, stored_name: str) -> Path:
        # stored_name is always generated here as 32 hex chars, so it can't escape root.
        if len(stored_name) != 32 or not all(c in "0123456789abcdef" for c in stored_name):
            raise ValueError("invalid stored name")
        return self.root / stored_name[:2] / stored_name

    def save(self, file_storage):
        """Stream an upload to disk. Returns (stored_name, size_bytes, sha256_hex)."""
        stored_name = secrets.token_hex(16)
        path = self._path(stored_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with open(path, "wb") as out:
            while chunk := file_storage.stream.read(64 * 1024):
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
        return stored_name, size, digest.hexdigest()

    def path(self, stored_name: str) -> Path:
        return self._path(stored_name)

    def delete(self, stored_name: str):
        try:
            self._path(stored_name).unlink()
        except FileNotFoundError:
            pass


def get_storage() -> LocalStorage:
    return LocalStorage(current_app.config["UPLOAD_DIR"])
