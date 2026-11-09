import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_settings_file_stays_out_of_the_docker_image():
    """Without this, `COPY . .` put the server's .env (secret key, payment keys) into the image, and
    once .env was locked to root the app's user couldn't read it and gunicorn failed to boot (502)."""
    rules = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()]
    assert ".env" in rules and ".env.*" in rules and "!.env.example" in rules
    assert ".git" in rules and "instance/" in rules


def test_app_starts_when_the_code_folder_is_read_only(tmp_path, monkeypatch):
    """In the image the code is root's and the app runs as appuser: startup must not write next to the
    code (it used to create instance/ there, which crashed gunicorn and gave a 502)."""
    import os
    from app import create_app

    code_dir = str(ROOT)
    real_makedirs = os.makedirs

    def guarded(path, *args, **kwargs):
        if os.path.abspath(path).startswith(code_dir + os.sep):
            raise PermissionError(13, "Permission denied", path)
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(os, "makedirs", guarded)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "x")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "data" / "n.sqlite3"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "data" / "uploads"))
    app = create_app()
    assert app.test_client().get("/").status_code == 200
