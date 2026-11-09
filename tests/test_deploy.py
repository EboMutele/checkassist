import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_settings_file_stays_out_of_the_docker_image():
    """Without this, `COPY . .` put the server's .env (secret key, payment keys) into the image, and
    once .env was locked to root the app's user couldn't read it and gunicorn failed to boot (502)."""
    rules = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()]
    assert ".env" in rules and ".env.*" in rules and "!.env.example" in rules
    assert ".git" in rules and "instance/" in rules
