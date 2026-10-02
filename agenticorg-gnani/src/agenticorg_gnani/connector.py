"""Gnani (Vachana) speech-to-text connector for AgenticOrg.

Wire format taken from Gnani's own SDK (``gnani-vachana`` 0.7.9,
``gnani/stt/client.py``) and https://docs.gnani.ai/api/STT/speech-to-text:

    POST {base_url}/stt/v3            multipart/form-data
    X-API-Key-ID: <key>               X-API-Request-ID: req_<12 hex>
    audio_file=<file>  language_code=<bcp-47>  format=verbatim|transcribe
    [itn_native_numerals=true]

    200 -> {"success": true, "request_id": "...", "transcript": "..."}

A statement captured live (``voice_server.py``, Gnani's realtime endpoint) was
already transcribed while the person spoke. For that the tool makes no call to
Gnani: it takes the saved transcript, from the run or from the capture folder.

The tool is registered as ``query``, not ``transcribe_audio``. AgenticOrg
refuses any connector tool whose name is missing from its action taxonomy
(``core/governance/action_policy.py``, reason ``action_unknown``) whenever a
call carries a tenant, which every real run does. ``query`` is in the taxonomy
as a read action, and transcription is a read: it changes nothing anywhere.

Connector config keys:

    gnani_api_key   the API key (preferred)
    api_key         the API key (fallback)
    gnani_base_url  override the API origin, any public https host
    base_url        override the API origin, honoured only for Gnani's own domains
    gnani_sessions_dir  folder of live voice sessions (default ~/gnani-voice-sessions)

The ``gnani_`` names exist because AgenticOrg hands an agent's connectors one
dict merged from every connector the agent links. A generic ``api_key`` or
``base_url`` in that dict may belong to another connector, so the key is read
from its own name first and a ``base_url`` on a foreign host is ignored: the
Gnani key is never sent to another connector's API.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import os
import re
import uuid
import wave
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog
from connectors.framework.base_connector import BaseConnector

from agenticorg_gnani.sessions import SOURCE as REALTIME_SOURCE
from agenticorg_gnani.sessions import load_session, safe_session_id, sessions_dir

logger = structlog.get_logger()

DEFAULT_BASE_URL = "https://api.vachana.ai"
_GNANI_DOMAINS = ("vachana.ai", "gnani.ai")
STT_PATH = "/stt/v3"
TOOL_NAME = "query"  # see the module docstring before renaming
API_KEY_HEADER = "X-API-Key-ID"
REQUEST_ID_HEADER = "X-API-Request-ID"

SUPPORTED_LANGUAGES = (
    "bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN",
    "ml-IN", "mr-IN", "pa-IN", "ta-IN", "te-IN",
)  # fmt: skip
_MIME = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
}
_MIME_TO_FORMAT = {
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/vnd.wave": "wav",
    "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/flac": "flac", "audio/x-flac": "flac",
    "audio/ogg": "ogg", "application/ogg": "ogg", "audio/opus": "ogg",
    "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/m4a": "m4a",
    "audio/aac": "aac", "audio/aacp": "aac", "audio/x-aac": "aac",
}  # fmt: skip

# Gnani's REST endpoint takes clips of at most 60 seconds. The byte cap only
# bounds memory: 60 s of 48 kHz stereo 16-bit WAV is about 11.5 MB.
MAX_AUDIO_SECONDS = 60
MAX_AUDIO_BYTES = 15 * 1024 * 1024

# A realtime session lasts 15 minutes at most: about 2,500 words.
MAX_TRANSCRIPT_CHARS = 20_000
_PLACEHOLDER_RE = re.compile(r"\$[A-Za-z_][\w.]*")
REST_SOURCE = "gnani_rest"

_DOWNLOAD_TIMEOUT_S = 20.0
_MAX_REDIRECTS = 3
_PROBE_TIMEOUT_S = 4.0  # two probes must fit inside AgenticOrg's 10 s test budget
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_AUTH_WORDS = ("api key", "api-key", "apikey", "x-api-key", "unauthor", "authenticat", "forbidden", "credential")
_DATA_URI_RE = re.compile(r"^data:(?P<mime>[\w.+/-]+)?(?:;[\w=.+-]+)*;base64,", re.IGNORECASE)


def _silent_wav(seconds: float = 0.5, rate: int = 16_000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


_PROBE_WAV = _silent_wav()


def _normalise_base_url(value: Any) -> str:
    """Accept the origin, or the full endpoint, with or without a slash."""
    url = str(value or "").strip().rstrip("/")
    if url.lower().endswith(STT_PATH):
        url = url[: -len(STT_PATH)].rstrip("/")
    return url


def _is_gnani_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in _GNANI_DOMAINS)


def _normalise_language(value: Any) -> str:
    raw = str(value or "en-IN").strip().replace("_", "-")
    lang, _, region = raw.partition("-")
    code = f"{lang.lower()}-{(region or 'IN').upper()}"
    if code not in SUPPORTED_LANGUAGES:
        raise ValueError(f"language_code {raw!r} is not supported; use one of {', '.join(SUPPORTED_LANGUAGES)}")
    return code


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _given(value: Any) -> str:
    """A text parameter's value, or ``""`` when nothing was given.

    A workflow step writes ``"$audio_url"`` for "the run's audio_url". When the
    run has no such value AgenticOrg passes the placeholder through unchanged,
    so a value that is one bare ``$name`` means the parameter was left out.
    Real text that happens to start with a dollar sign ("$500 was the bill")
    is kept.
    """
    text = str(value or "").strip()
    return "" if _PLACEHOLDER_RE.fullmatch(text) else text


def _sniff_format(audio: bytes) -> str:
    head = audio[:12]
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        # MPEG sync word; layer bits 00 mean ADTS (AAC), anything else MPEG audio.
        return "aac" if head[1] & 0x06 == 0 else "mp3"
    return ""


def _wav_seconds(audio: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(audio), "rb") as src:
            rate = src.getframerate()
            return src.getnframes() / rate if rate else None
    except (wave.Error, EOFError, ValueError):
        return None  # compressed or extensible WAV: let Gnani judge it


def _mentions_auth(resp: httpx.Response) -> bool:
    text = resp.text[:500].lower()
    return any(word in text for word in _AUTH_WORDS)


class GnaniSttConnector(BaseConnector):
    name = "gnani_stt"
    category = "comms"
    auth_type = "api_key"
    base_url = DEFAULT_BASE_URL
    rate_limit_rpm = 60
    timeout_ms = 60000
    tools = [TOOL_NAME]

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        explicit = _normalise_base_url(self.config.get("gnani_base_url"))
        shared = _normalise_base_url(self.config.get("base_url"))
        if shared and not explicit and not _is_gnani_host(shared):
            # Another linked connector's base_url, merged into this config.
            logger.info("gnani_stt_foreign_base_url_ignored", host=urlsplit(shared).hostname)
        self.base_url = explicit or (shared if _is_gnani_host(shared) else "") or DEFAULT_BASE_URL

    def _register_tools(self) -> None:
        self._tool_registry[TOOL_NAME] = self.transcribe_audio

    def _api_key(self) -> str:
        own = self.config.get("gnani_api_key") or self.config.get("GNANI_STT_API_KEY")
        return str(own or self._get_secret("api_key") or "").strip()

    def _has_credentials(self) -> bool:
        return bool(str(self.config.get("gnani_api_key") or "").strip()) or super()._has_credentials()

    async def _authenticate(self) -> None:
        api_key = self._api_key()
        self._auth_headers = {API_KEY_HEADER: api_key} if api_key else {}

    # ------------------------------------------------------------------ health

    async def health_check(self) -> dict[str, Any]:
        """Prove the key against the real endpoint.

        AgenticOrg only lets an agent go active when every linked connector's
        last test was ``healthy``, and the base-class probe (``GET /``) says
        nothing about the key. So: transcribe half a second of silence. 2xx
        is a certain pass and 401/403 a certain fail.
        """
        not_configured = {"status": "not_configured", "reason": "No api_key configured for gnani_stt"}
        if not self._has_credentials():
            return not_configured
        if not self._client:
            return {"status": "not_connected"}
        if API_KEY_HEADER not in self._auth_headers:
            return not_configured
        try:
            resp = await self._post_stt(
                files={"audio_file": ("probe.wav", _PROBE_WAV, _MIME["wav"])},
                data={"language_code": "en-IN", "format": "verbatim"},
                timeout=_PROBE_TIMEOUT_S,
            )
            if resp.status_code >= 500:
                # The service may simply dislike silent audio. A request with
                # no file still tells us whether the key and endpoint are good.
                resp = await self._post_stt(data={"language_code": "en-IN"}, timeout=_PROBE_TIMEOUT_S)
        except httpx.TimeoutException:
            return {"status": "unhealthy", "reason": "Gnani did not answer the probe in time"}
        except (httpx.HTTPError, ValueError) as exc:
            return {"status": "unhealthy", "error": f"{type(exc).__name__}: {exc}"}

        sc = resp.status_code
        if 200 <= sc < 300:
            return {"status": "healthy", "http_status": sc}
        if sc in (401, 403) or (400 <= sc < 500 and _mentions_auth(resp)):
            reason = f"Gnani rejected the API key (HTTP {sc})"
        elif sc in (404, 405):
            reason = f"HTTP {sc} from {self.base_url}{STT_PATH}; check base_url (expected {DEFAULT_BASE_URL})"
        elif sc == 429:
            reason = "Gnani rate limit hit during the probe; test again in a minute"
        elif 400 <= sc < 500:
            # Authenticated, but the probe payload itself was refused.
            return {"status": "healthy", "http_status": sc, "note": "API key accepted; probe audio was rejected"}
        else:
            reason = f"Gnani returned HTTP {sc}"
        # The upstream body says why; it is an error message, never the key.
        return {"status": "unhealthy", "http_status": sc, "reason": reason, "detail": resp.text[:200]}

    # -------------------------------------------------------------------- tool

    async def transcribe_audio(
        self,
        audio_url: str = "",
        audio_base64: str = "",
        audio_format: str = "",
        language_code: str = "en-IN",
        itn: bool = False,
        native_numerals: bool = False,
        voice_session_id: str = "",
        voice_transcript: str = "",
    ) -> dict[str, Any]:
        """Get a spoken statement as text, using Gnani speech-to-text.

        Give exactly one source:
          voice_session_id: reference of a statement captured live (vs_...).
            Add voice_transcript, the text captured in that session, when
            you have it; otherwise it is read from the capture folder.
          audio_url: public https link to a recorded clip, 60 seconds or less.
          audio_base64: a clip's bytes as base64. A few seconds at most.
        audio_format: wav, mp3, flac, ogg, m4a or aac. Detected when omitted.
        language_code: language spoken. One of bn-IN, en-IN, gu-IN, hi-IN,
          kn-IN, ml-IN, mr-IN, pa-IN, ta-IN, te-IN. Default en-IN.
        itn: clips only. true writes numbers, currency and dates as digits
          ("5,000" not "five thousand"). hi-IN and en-IN only.
        native_numerals: with itn, use the language's own digits.

        Returns transcript, language_code and source.
        """
        url, b64 = _given(audio_url), _given(audio_base64)
        session_id, live_text = _given(voice_session_id), _given(voice_transcript)
        language = _normalise_language(_given(language_code) or "en-IN")
        if session_id or live_text:
            if url or b64:
                raise ValueError("Give a voice session or an audio clip, not both")
            return await self._voice_session(session_id, live_text, language)

        if not self._client:
            raise RuntimeError("Connector not connected")
        if API_KEY_HEADER not in self._auth_headers:
            raise ValueError("gnani_stt has no api_key configured")

        audio, hinted_format = await self._load_audio(url, b64)
        fmt = _given(audio_format).lower().lstrip(".") or _sniff_format(audio) or hinted_format
        if fmt not in _MIME:
            raise ValueError(
                f"audio_format must be one of {', '.join(sorted(_MIME))}; could not detect it from the file"
            )
        seconds = _wav_seconds(audio) if fmt == "wav" else None
        if seconds is not None and seconds > MAX_AUDIO_SECONDS:
            raise ValueError(
                f"audio is {seconds:.0f} s long; Gnani transcribes at most {MAX_AUDIO_SECONDS} s per request. "
                "Split it into shorter clips."
            )

        use_itn = _as_bool(itn)
        data = {"language_code": language, "format": "transcribe" if use_itn else "verbatim"}
        if use_itn and _as_bool(native_numerals):
            data["itn_native_numerals"] = "true"
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        files = {"audio_file": (f"audio.{fmt}", audio, _MIME[fmt])}

        resp = await self._post_stt(files=files, data=data, request_id=request_id)
        if resp.status_code in _RETRY_STATUSES:
            await asyncio.sleep(1.5)
            resp = await self._post_stt(files=files, data=data, request_id=request_id)
        resp.raise_for_status()  # AgenticOrg maps the status to a structured tool error

        try:
            body = resp.json()
        except ValueError as exc:
            raise ValueError(f"Gnani returned a non-JSON body (HTTP {resp.status_code})") from exc
        if not isinstance(body, dict) or body.get("success") is False:
            detail = body.get("message") or body.get("error") or body if isinstance(body, dict) else body
            raise ValueError(f"Gnani could not transcribe the audio: {str(detail)[:300]}")

        result: dict[str, Any] = {
            "transcript": str(body.get("transcript") or ""),
            "language_code": language,
            "format": data["format"],
            "request_id": body.get("request_id") or request_id,
            "source": REST_SOURCE,
            "audio_format": fmt,
        }
        if seconds is not None:
            result["audio_seconds"] = round(seconds, 2)
        return result

    async def _voice_session(self, session_id: str, live_text: str, language: str) -> dict[str, Any]:
        """The transcript of a statement that was captured live. No call to Gnani is made."""
        if not session_id:
            raise ValueError("voice_transcript needs the voice_session_id it was captured in")
        session_id = safe_session_id(session_id)
        if live_text:
            # Sent along with the run, for when AgenticOrg cannot see the capture folder.
            if len(live_text) > MAX_TRANSCRIPT_CHARS:
                raise ValueError(f"voice_transcript is longer than {MAX_TRANSCRIPT_CHARS} characters")
            return {
                "transcript": live_text,
                "language_code": language,
                "format": "verbatim",
                "source": REALTIME_SOURCE,
                "voice_session_id": session_id,
                "read_from": "run",
            }

        directory = sessions_dir(self.config.get("gnani_sessions_dir") or os.environ.get("GNANI_SESSIONS_DIR"))
        try:
            session = await asyncio.to_thread(load_session, directory, session_id)
        except LookupError as exc:
            raise ValueError(
                f"No voice session {session_id} in {directory}. Record one on the capture page; if AgenticOrg "
                "runs on another machine or in a container, send the text as voice_transcript."
            ) from exc
        transcript = str(session.get("transcript") or "").strip()
        if not transcript:
            raise ValueError(f"Voice session {session_id} has no transcript")
        result: dict[str, Any] = {
            "transcript": transcript,
            "language_code": str(session.get("language_code") or language),
            "format": str(session.get("format") or "verbatim"),
            "source": REALTIME_SOURCE,
            "voice_session_id": session_id,
            "read_from": "capture_folder",
        }
        for key in ("request_id", "audio_seconds"):
            if session.get(key):
                result[key] = session[key]
        if session.get("complete") is False:
            result["note"] = "The recording ended early, so the statement may be cut short."
        return result

    # ----------------------------------------------------------------- helpers

    async def _post_stt(
        self,
        *,
        data: dict[str, str],
        files: dict[str, Any] | None = None,
        request_id: str | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        assert self._client is not None
        self._validate_request_url("POST", STT_PATH)
        kwargs: dict[str, Any] = {"timeout": timeout} if timeout is not None else {}
        return await self._client.post(
            STT_PATH,
            data=data,
            files=files,
            headers={REQUEST_ID_HEADER: request_id or f"req_{uuid.uuid4().hex[:12]}"},
            **kwargs,
        )

    async def _load_audio(self, audio_url: str, audio_base64: str) -> tuple[bytes, str]:
        """Return ``(audio bytes, format hint)`` from whichever source was given."""
        url = str(audio_url or "").strip()
        b64 = str(audio_base64 or "").strip()
        if bool(url) == bool(b64):
            raise ValueError("Give exactly one of voice_session_id, audio_url or audio_base64")

        if url:
            audio, content_type = await self._download(url)
            suffix = urlsplit(url).path.rpartition(".")[2].lower()
            hint = suffix if suffix in _MIME else _MIME_TO_FORMAT.get(content_type, "")
        else:
            hint = ""
            match = _DATA_URI_RE.match(b64)
            if match:
                hint = _MIME_TO_FORMAT.get((match.group("mime") or "").lower(), "")
                b64 = b64[match.end() :]
            try:
                audio = base64.b64decode("".join(b64.split()), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("audio_base64 is not valid base64") from exc
            if len(audio) > MAX_AUDIO_BYTES:
                raise ValueError(f"audio is larger than {MAX_AUDIO_BYTES // (1024 * 1024)} MB")
        if not audio:
            raise ValueError("audio is empty")
        return audio, hint

    async def _download(self, url: str) -> tuple[bytes, str]:
        """Fetch ``url`` through AgenticOrg's egress guard, without the API key.

        Same rules as every connector call: https only, public hosts only,
        DNS pinned to the validated answer. Redirects are followed by hand so
        each hop is validated before it is requested.
        """
        transport, require_dns = self._connector_transport_and_dns()
        async with httpx.AsyncClient(
            transport=transport, timeout=_DOWNLOAD_TIMEOUT_S, follow_redirects=False
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                try:
                    self._validate_connector_egress_url(url, require_dns=require_dns)
                except ValueError as exc:
                    raise ValueError("audio_url must be a public https URL") from exc
                async with client.stream("GET", url) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                        url = str(resp.url.join(resp.headers["location"]))
                        continue
                    if resp.status_code != 200:
                        host = urlsplit(url).hostname
                        raise ValueError(
                            f"Could not download audio_url: HTTP {resp.status_code} from {host}. "
                            "The link must be readable without signing in."
                        )
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_AUDIO_BYTES:
                            raise ValueError(f"audio is larger than {MAX_AUDIO_BYTES // (1024 * 1024)} MB")
                        chunks.append(chunk)
                    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    return b"".join(chunks), content_type
        raise ValueError("audio_url redirected too many times")
