"""Narrator TTS via Gemini Flash TTS (Google AI Studio API key).

Server-side wrapper called by /tts in dnd-display-app.py. Reads the API key
from DND_TTS_KEY env var → GEMINI_API_KEY env var → ~/.config/claude-dnd/tts.key
(in that order). Returns None on any failure so the caller can silently
degrade to text-only narration.

stdlib only — no requests, no google-cloud-sdk, no dependencies.
Setup walkthrough: docs/SKILL-tts.md.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# ── Configuration ───────────────────────────────────────────────────────────

GEMINI_TTS_MODEL = os.environ.get("DND_TTS_MODEL", "").strip() or "gemini-2.5-flash-preview-tts"
GEMINI_TTS_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_TTS_MODEL}:generateContent"
)

KEY_FILE = Path.home() / ".config" / "claude-dnd" / "tts.key"

# Curated 9-voice catalog. The full Gemini API exposes 30 voices; this
# subset keeps the per-block dropdown scannable. Extend VALID_VOICES and
# the matching arrays in display/templates/index.html if you want the
# full catalog.
VALID_VOICES = frozenset({
    # Male
    "Charon", "Enceladus", "Fenrir", "Umbriel",
    # Female
    "Aoede", "Gacrux", "Kore", "Vindemiatrix", "Zephyr",
    # Rest of the Gemini prebuilt catalog (usable from voices.json).
    "Puck", "Leda", "Orus", "Callirrhoe", "Autonoe", "Iapetus", "Algieba", "Despina",
    "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar", "Alnilam", "Schedar",
    "Pulcherrima", "Achird", "Zubenelgenubi", "Sadachbia", "Sadaltager", "Sulafat",
})
DEFAULT_VOICE = "Enceladus"

# Gemini Flash TTS responses degrade in latency past this point and may
# truncate. Bigger blocks should be chunked by the caller; the narration
# path naturally chunks at block flush.
MAX_TEXT_CHARS = 2000

# Request timeout — Gemini Flash TTS typical latency is 1-3s; we give it
# substantial headroom for cold-start or transient slowness.
DEFAULT_TIMEOUT = 60.0


# ── Engine selection ────────────────────────────────────────────────────────
# DND_TTS_ENGINE=voicestudio routes speech to a local VoiceStudio server
# (OpenAI-style /v1/audio/speech) instead of Google Gemini.
ENGINE = (os.environ.get("DND_TTS_ENGINE", "").strip().lower() or "gemini")
VOICESTUDIO_URL = os.environ.get("DND_VOICESTUDIO_URL", "http://127.0.0.1:3900").rstrip("/")
VOICESTUDIO_KEY = os.environ.get("DND_VOICESTUDIO_KEY", "").strip()


def engine_id() -> str:
    """Identifies the engine+model for cache keys."""
    return "voicestudio" if ENGINE == "voicestudio" else GEMINI_TTS_MODEL


# ── Key resolution ──────────────────────────────────────────────────────────

def _get_api_key() -> Optional[str]:
    """Resolve the Gemini API key. Env vars take precedence over the key file."""
    for env in ("DND_TTS_KEY", "GEMINI_API_KEY"):
        v = os.environ.get(env)
        if v and v.strip():
            return v.strip()
    if KEY_FILE.exists():
        try:
            return KEY_FILE.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def key_source() -> str:
    """Return a human description of where the key came from, for diagnostics.

    Used by the verify path and the /dnd load surface — does not return the key.
    """
    if ENGINE == "voicestudio":
        return "voicestudio"
    for env in ("DND_TTS_KEY", "GEMINI_API_KEY"):
        v = os.environ.get(env)
        if v and v.strip():
            return f"env:{env}"
    if KEY_FILE.exists() and KEY_FILE.read_text(encoding="utf-8").strip():
        return f"file:{KEY_FILE}"
    return "unset"


# ── Synthesis ───────────────────────────────────────────────────────────────

class TtsError(Exception):
    """Raised by synthesize_strict; caught by synthesize to fail silently."""


def synthesize_strict(
    text: str,
    voice: str = DEFAULT_VOICE,
    timeout: float = DEFAULT_TIMEOUT,
    style: str = "",
) -> bytes:
    """Call Gemini Flash TTS and return raw L16 PCM bytes (24 kHz mono).

    Raises TtsError for any failure. Use synthesize() for the silent-fail path.
    """
    if ENGINE == "voicestudio":
        return _voicestudio_pcm(text, style, timeout=max(timeout, 300.0))
    key = _get_api_key()
    if not key:
        raise TtsError("no api key configured")

    text = (text or "").strip()
    if not text:
        raise TtsError("empty text")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]

    if voice not in VALID_VOICES:
        voice = DEFAULT_VOICE

    # Gemini TTS follows a plain-language direction placed before the text.
    spoken = f"{style.strip()}:\n{text}" if style and style.strip() else text
    body = {
        "contents": [{"role": "user", "parts": [{"text": spoken}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice}
                }
            },
        },
    }
    body_bytes = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        f"{GEMINI_TTS_URL}?key={key}",
        data=body_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise TtsError(f"http {resp.status}")
            raw = resp.read()
    except urllib.error.HTTPError as e:
        snippet = ""
        try:
            snippet = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise TtsError(f"http {e.code}: {snippet}") from e
    except urllib.error.URLError as e:
        raise TtsError(f"network: {e.reason}") from e
    except Exception as e:
        raise TtsError(f"unexpected: {e}") from e

    try:
        data = json.loads(raw)
        b64 = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        raise TtsError(f"bad response shape: {e}") from e

    try:
        pcm = base64.b64decode(b64)
    except Exception as e:
        raise TtsError(f"bad base64: {e}") from e

    if not pcm:
        raise TtsError("empty pcm payload")
    return pcm


def _voicestudio_pcm(text: str, instruct: str, timeout: float = 300.0) -> bytes:
    """Local VoiceStudio: designed voice from an `instruct` description.
    Returns 24 kHz mono 16-bit PCM (what the browser player expects)."""
    import io
    import wave
    text = (text or "").strip()[:MAX_TEXT_CHARS]
    if not text:
        raise TtsError("empty text")
    body = {"model": "tts-1", "input": text, "voice": "alloy", "response_format": "wav"}
    if instruct and instruct.startswith("profile:"):
        body["voice"] = instruct.split(":", 1)[1].strip()
        body["seed"] = 42
        instruct = ""
    if instruct and instruct.strip():
        body["instruct"] = instruct.strip()
        # Same description -> same seed -> the same voice every time (otherwise
        # VoiceStudio designs a slightly different person on every request).
        import zlib
        body["seed"] = zlib.crc32(instruct.strip().lower().encode()) % 2_000_000_000
    headers = {"Content-Type": "application/json"}
    if VOICESTUDIO_KEY:
        headers["Authorization"] = f"Bearer {VOICESTUDIO_KEY}"
    req = urllib.request.Request(f"{VOICESTUDIO_URL}/v1/audio/speech",
                                 data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        snippet = ""
        try:
            snippet = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise TtsError(f"voicestudio http {e.code}: {snippet}") from e
    except Exception as e:
        raise TtsError(f"voicestudio: {e}") from e
    try:
        with wave.open(io.BytesIO(raw)) as w:
            ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            frames = w.readframes(w.getnframes())
    except Exception as e:
        raise TtsError(f"voicestudio: bad wav: {e}") from e
    import audioop  # stdlib up to Python 3.12 (CT 103 runs 3.11)
    if width != 2:
        frames = audioop.lin2lin(frames, width, 2)
    if ch == 2:
        frames = audioop.tomono(frames, 2, 0.5, 0.5)
    if rate != 24000:
        frames, _ = audioop.ratecv(frames, 2, 1, rate, 24000, None)
    if not frames:
        raise TtsError("voicestudio: empty audio")
    # Level every clip to the same, fairly quiet loudness (RMS target), so
    # characters don't jump in volume and nothing blasts.
    target = int(os.environ.get("DND_TTS_RMS", "1800"))
    rms = audioop.rms(frames, 2)
    if rms > 0:
        gain = min(target / rms, 2.0)
        peak = audioop.max(frames, 2)
        if peak * gain > 30000:
            gain = 30000 / peak
        frames = audioop.mul(frames, 2, gain)
    return frames


def health(timeout: float = 2.0) -> dict:
    """Is the voice engine reachable right now? {"engine", "up", "detail"}.

    VoiceStudio: up when its HTTP server answers at all (any status code means
    it is listening); connection refused / timeout means it is not running.
    Gemini: up when an API key is configured (no network call)."""
    if ENGINE == "voicestudio":
        req = urllib.request.Request(f"{VOICESTUDIO_URL}/v1/models", method="GET")
        if VOICESTUDIO_KEY:
            req.add_header("Authorization", f"Bearer {VOICESTUDIO_KEY}")
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                return {"engine": "voicestudio", "up": True, "detail": "ok"}
        except urllib.error.HTTPError as e:
            return {"engine": "voicestudio", "up": True, "detail": f"http {e.code}"}
        except Exception as e:
            reason = getattr(e, "reason", e)
            return {"engine": "voicestudio", "up": False, "detail": str(reason)[:120]}
    ok = key_source() != "unset"
    return {"engine": "gemini", "up": ok, "detail": "key set" if ok else "no api key"}


def synthesize(text: str, voice: str = DEFAULT_VOICE) -> Optional[bytes]:
    """Silent-fail wrapper. Returns L16 PCM bytes or None on any failure."""
    try:
        return synthesize_strict(text, voice)
    except TtsError:
        return None


# ── CLI for verification ────────────────────────────────────────────────────

def _cli() -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(
        description="Verify Gemini Flash TTS setup for the DnD display companion.",
    )
    p.add_argument(
        "--test", action="store_true",
        help="Check key source + run a synthesis call. Writes nothing.",
    )
    p.add_argument(
        "--speak", action="store_true",
        help="Also play the synthesized audio via afplay (macOS).",
    )
    p.add_argument(
        "--text", default="Hello, narrator voice test. The torchlit hall awaits.",
        help="Override the default test phrase.",
    )
    p.add_argument(
        "--voice", default=DEFAULT_VOICE,
        help=f"Voice name (default: {DEFAULT_VOICE}). Valid: {sorted(VALID_VOICES)}",
    )
    args = p.parse_args()

    if not args.test:
        p.print_help()
        return 0

    src = key_source()
    print(f"API key source: {src}")
    if src == "unset":
        print("  → no key found. See docs/SKILL-tts.md to configure one.")
        return 2

    print(f"Model: {GEMINI_TTS_MODEL}")
    print(f"Voice: {args.voice}")
    print(f"Text:  {args.text!r}")
    print("Calling Gemini Flash TTS…")
    try:
        pcm = synthesize_strict(args.text, args.voice)
    except TtsError as e:
        print(f"  FAIL: {e}")
        return 1

    print(f"  OK — received {len(pcm)} bytes of L16 PCM (24 kHz mono).")

    if args.speak:
        import struct
        import subprocess
        import tempfile

        sample_rate = 24000
        n_samples = len(pcm) // 2
        byte_rate = sample_rate * 2
        wav_header = (
            b"RIFF"
            + struct.pack("<I", 36 + len(pcm))
            + b"WAVE"
            + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16)
            + b"data"
            + struct.pack("<I", len(pcm))
        )
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_header + pcm)
            wav_path = f.name
        print(f"  Playing via afplay: {wav_path}")
        try:
            subprocess.run(["afplay", wav_path], check=False)
        except FileNotFoundError:
            print("  (afplay not found — non-macOS? PCM file kept at the path above.)")
            return 0
        os.unlink(wav_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
