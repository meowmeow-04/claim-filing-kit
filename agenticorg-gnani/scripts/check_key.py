"""Check a Gnani API key against the live speech-to-text endpoint.

Needs nothing installed: Python's standard library only.

    GNANI_API_KEY=your-key python3 scripts/check_key.py [clip.wav] [language]

It sends the same requests the connector sends and prints what Gnani answers:
  1. half a second of silence with your key   (the connector's health probe)
  2. the same with a wrong key                (must be refused)
  3. your clip, if you pass one               (prints the transcript)

The key is never printed. Set GNANI_BASE_URL only if Gnani gave you an
endpoint other than https://api.vachana.ai.

If this Python cannot verify HTTPS certificates (common on a fresh python.org
install on macOS), the requests are sent with the system's curl instead.
Certificate checking is never turned off.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

MIME = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
}


def silent_wav(seconds: float = 0.5, rate: int = 16_000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def curl_post(url: str, key: str, filename: str, audio: bytes, language: str) -> tuple[int, str]:
    """The same request through curl, which uses the system's certificates.

    The key travels in a config read from stdin, so it never appears in the
    process list.
    """
    suffix = Path(filename).suffix.lower()
    mime = MIME.get(suffix, "application/octet-stream")
    safe_name = filename if re.fullmatch(r"[A-Za-z0-9._-]+", filename) else f"audio{suffix}"
    with tempfile.TemporaryDirectory() as folder:
        upload = Path(folder) / "upload.bin"
        upload.write_bytes(audio)
        config = "\n".join(
            [
                f"url = {_quoted(url)}",
                'request = "POST"',
                f"header = {_quoted('X-API-Key-ID: ' + key)}",
                f"header = {_quoted('X-API-Request-ID: req_' + uuid.uuid4().hex[:12])}",
                f"form = {_quoted('language_code=' + language)}",
                'form = "format=verbatim"',
                f"form = {_quoted(f'audio_file=@{upload};type={mime};filename={safe_name}')}",
                "silent",
                "show-error",
                "max-time = 60",
                'write-out = "\\n%{http_code}"',
            ]
        )
        done = subprocess.run(  # noqa: S603 - fixed argv; the request comes from stdin
            [shutil.which("curl") or "curl", "--config", "-"],
            input=config.encode(),
            capture_output=True,
            check=False,
        )
    if done.returncode != 0:
        raise urllib.error.URLError(done.stderr.decode("utf-8", "replace").strip() or f"curl exit {done.returncode}")
    body, _, status = done.stdout.decode("utf-8", "replace").rpartition("\n")
    return int(status), body


_use_curl = False


def post(url: str, key: str, filename: str, audio: bytes, language: str) -> tuple[int, str]:
    """Send one transcription request. Returns (status, body)."""
    global _use_curl  # noqa: PLW0603 - remember the fallback for the later requests
    if not _use_curl:
        try:
            return urllib_post(url, key, filename, audio, language)
        except urllib.error.URLError as error:
            if not isinstance(error.reason, ssl.SSLCertVerificationError) or not shutil.which("curl"):
                raise
            print("(This Python cannot verify HTTPS certificates; using the system curl instead.)\n")
            _use_curl = True
    return curl_post(url, key, filename, audio, language)


def urllib_post(url: str, key: str, filename: str, audio: bytes, language: str) -> tuple[int, str]:
    """POST multipart/form-data exactly as Gnani's SDK does. Returns (status, body)."""
    boundary = uuid.uuid4().hex
    mime = MIME.get(Path(filename).suffix.lower(), "application/octet-stream")
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="language_code"\r\n\r\n{language}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="format"\r\n\r\nverbatim\r\n'.encode(),
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="audio_file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode(),
        audio,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    request = urllib.request.Request(  # noqa: S310 - https URL built below
        url,
        data=b"".join(parts),
        method="POST",
        headers={
            "X-API-Key-ID": key,
            "X-API-Request-ID": f"req_{uuid.uuid4().hex[:12]}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def main() -> int:
    key = os.environ.get("GNANI_API_KEY", "").strip()
    if not key:
        print(__doc__)
        return 2
    base = os.environ.get("GNANI_BASE_URL", "https://api.vachana.ai").strip().rstrip("/")
    if base.endswith("/stt/v3"):
        base = base[: -len("/stt/v3")]
    if not base.startswith("https://"):
        print("GNANI_BASE_URL must start with https://")
        return 2
    url = f"{base}/stt/v3"
    print(f"Endpoint: {url}\n")
    failures = 0

    try:
        status, body = post(url, key, "probe.wav", silent_wav(), "en-IN")
        ok = 200 <= status < 300
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] silence with your key    -> HTTP {status}  {body[:200]}")

        status, body = post(url, "invalid-" + "0" * 24, "probe.wav", silent_wav(), "en-IN")
        ok = status in (401, 403)
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] silence with a wrong key -> HTTP {status}  {body[:200]}")

        if len(sys.argv) > 1:
            clip = Path(sys.argv[1]).expanduser()
            language = sys.argv[2] if len(sys.argv) > 2 else "en-IN"
            status, body = post(url, key, clip.name, clip.read_bytes(), language)
            ok = status == 200
            failures += not ok
            try:
                shown = json.loads(body).get("transcript", body) if ok else body[:300]
            except ValueError:
                shown = body[:300]
            print(f"[{'PASS' if ok else 'FAIL'}] {clip.name} ({language}) -> HTTP {status}  {shown}")
        else:
            print("[SKIP] no clip given: add an audio file (60 s or less) to see a transcript")
    except urllib.error.URLError as error:
        print(f"Could not reach {url}: {error.reason}")
        if isinstance(error.reason, ssl.SSLError):
            print("Python cannot verify HTTPS certificates and no curl was found to use instead.")
            print("On a python.org install for macOS, run 'Install Certificates.command' from the")
            print("Python folder in Applications, then retry.")
        return 1

    print("\nAll checks passed." if not failures else f"\n{failures} check(s) failed. Send this output back.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
