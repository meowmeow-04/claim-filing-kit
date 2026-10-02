"""Check a Gnani API key against the live REALTIME speech-to-text endpoint.

Run it with the kit's environment (it needs the ``websockets`` package):

    GNANI_API_KEY=your-key .venv/bin/python agenticorg-gnani/scripts/check_realtime.py [clip.wav] [language]

What it does, and what Gnani must answer:
  1. opens a realtime session with your key   -> Gnani says "connected"
  2. tries the same with a wrong key          -> Gnani must refuse
  3. streams your clip at speaking speed      -> prints each piece of transcript as it arrives
     (with no clip: streams a second of silence, to prove audio is accepted)

Line 1 answers "does my key have the realtime API": PASS means yes.

The clip must be a 16-bit WAV file; any sample rate, mono or stereo. Any other
file is left out and the key is still checked. To make a WAV on a Mac:
    afconvert -f WAVE -d LEI16@16000 -c 1 voice.m4a voice.wav

The key is never printed. Set GNANI_BASE_URL only if Gnani gave you an
endpoint other than https://api.vachana.ai.
"""

from __future__ import annotations

import array
import asyncio
import os
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    from agenticorg_gnani.realtime import (
        DEFAULT_BASE_URL,
        MAX_SESSION_SECONDS,
        SAMPLE_RATE,
        SILENT_FRAME,
        RealtimeError,
        RealtimeRefused,
        RealtimeSession,
        frames_from_pcm,
        normalise_language,
        stream_url,
    )
except ModuleNotFoundError as exc:
    sys.exit(
        f"Cannot import {exc.name!r}. Run this with the kit's Python, which has it: "
        ".venv/bin/python agenticorg-gnani/scripts/check_realtime.py  (scripts/setup_and_test.sh builds .venv)"
    )


def wav_to_pcm16k(path: Path) -> bytes:
    """Any 16-bit WAV as 16 kHz mono 16-bit PCM. Standard library only."""
    try:
        with wave.open(str(path), "rb") as src:
            channels, width, rate, count = src.getnchannels(), src.getsampwidth(), src.getframerate(), src.getnframes()
            raw = src.readframes(count)
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"{path.name} is not a WAV file this script can read ({exc})") from exc
    if width != 2:
        raise ValueError(f"{path.name} is {width * 8}-bit; a 16-bit WAV is needed")
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if channels > 1:
        samples = array.array(
            "h", (sum(samples[i : i + channels]) // channels for i in range(0, len(samples) - channels + 1, channels))
        )
    if rate != SAMPLE_RATE and len(samples) > 1:
        step = rate / SAMPLE_RATE
        out = array.array("h", bytes(2 * int(len(samples) / step)))
        last = len(samples) - 1
        for i in range(len(out)):
            position = i * step
            left = int(position)
            right = min(left + 1, last)
            fraction = position - left
            out[i] = int(samples[left] * (1 - fraction) + samples[right] * fraction)
        samples = out
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


async def _print_segments(session: RealtimeSession) -> None:
    async for event in session.stream():
        if event.get("type") == "transcript":
            print(f'       {len(session.segments) - 1}: "{event.get("text", "")}"')
        elif event.get("type") == "error":
            print(f"       Gnani reported an error: {event.get('message')}")


async def main() -> int:
    key = os.environ.get("GNANI_API_KEY", "").strip()
    if not key:
        print(__doc__)
        return 2
    base = os.environ.get("GNANI_BASE_URL", "").strip() or DEFAULT_BASE_URL
    clip = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else None
    try:
        language = normalise_language(sys.argv[2] if len(sys.argv) > 2 else "en-IN")
        url = stream_url(base)
    except ValueError as exc:
        print(f"Cannot start: {exc}")
        return 2
    pcm = b""
    if clip:
        try:
            pcm = wav_to_pcm16k(clip)
        except (ValueError, OSError) as exc:
            print(f"Leaving the clip out: {exc}. The key is still checked.\n")
            clip = None
    seconds = len(pcm) / 2 / SAMPLE_RATE
    if seconds > MAX_SESSION_SECONDS - 30:
        print(f"Cannot start: the clip is {seconds / 60:.0f} minutes long; a realtime session ends at 15 minutes")
        return 2
    print(f"Endpoint: {url}\n")
    failures = 0

    # 1. your key
    try:
        session = await RealtimeSession.open(key, language, base_url=base)
    except RealtimeRefused as exc:
        print(f"[FAIL] your key on the realtime endpoint -> {exc}")
        print("\nGnani did not accept this key for realtime. Ask Gnani whether realtime speech-to-text")
        print("is enabled on your account, and send them the line above.")
        return 1
    except RealtimeError as exc:
        print(f"[FAIL] could not connect -> {exc}")
        return 1
    config = session.connected.get("config") or {}
    print(f"[PASS] your key is accepted for realtime -> connected, sample rate {config.get('sample_rate', '?')}")

    # 3. audio (run on the open session before the wrong-key check, so one session does both)
    printer = asyncio.create_task(_print_segments(session))
    try:
        if clip:
            print(f"       streaming {clip.name} ({language}, {seconds:.1f} s) at speaking speed...")
            await session.send_paced(frames_from_pcm(pcm))
        else:
            await session.send_paced([SILENT_FRAME] * 32)
        await session.finish()
    except RealtimeError as exc:
        session.errors.append(str(exc))
    await printer
    problem = "; ".join(session.errors) or session.closed_reason
    if problem:
        failures += 1
        print(f"[FAIL] sending audio -> {problem}")
    elif clip and not session.transcript:
        failures += 1
        print("[FAIL] the clip was streamed but no transcript came back (is there speech in it, in this language?)")
    elif clip:
        print(f"[PASS] transcript -> {session.transcript}")
    else:
        print("[PASS] audio frames accepted (no clip given: add a WAV file to see a transcript)")

    # 2. a wrong key
    try:
        wrong = await RealtimeSession.open("invalid-" + "0" * 24, language, base_url=base)
    except RealtimeRefused as exc:
        print(f"[PASS] a wrong key is refused -> {exc}")
    except RealtimeError as exc:
        failures += 1
        print(f"[FAIL] wrong-key check could not connect -> {exc}")
    else:
        await wrong.close()
        failures += 1
        print("[FAIL] Gnani opened a realtime session for a wrong key, so line 1 does not prove your key works")

    print("\nAll checks passed." if not failures else f"\n{failures} check(s) failed. Send this output back.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
