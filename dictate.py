#!/usr/bin/env python3
# Type checking:
#   pyright --project pyrightconfig.json
#
# VS Code:
#   Install/enable the Python extension and Pylance. Pylance reads
#   pyrightconfig.json in this repository and checks this file when it is open.

"""Dictate — Push-to-Talk Speech-to-Text (X11).

Hold Super+F5 to record, release to transcribe and paste.
Supports multiple providers: groq, openai, mistral.

NOTE: pynput uses XRecord (X11 extension) for global key listening. This won't
work on Wayland. A future migration path: replace the keyboard listener with a
long-running daemon that reacts to Unix signals (SIGUSR1, SIGUSR2, etc.).
GNOME keybindings (configured via dconf custom-keybindings) would run
`kill -SIGUSRx <pid>` on each shortcut press.

System deps (no venv needed):
    sudo apt install python3-requests alsa-utils xdotool xclip x11-utils
    pip install pynput

Set API key for your chosen provider:
    export GROQ_API_KEY="gsk_..."
    export OPENAI_API_KEY="sk-..."
    export MISTRAL_API_KEY="..."

Usage:
    python3 dictate.py groq         # use Groq
    python3 dictate.py openai       # use OpenAI
    python3 dictate.py mistral      # use Mistral
"""

from __future__ import annotations

import io
import math
import os
import pathlib
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from typing import Any, TypedDict

_MISSING: list[str] = []
keyboard: Any = None
requests: Any = None
try:
    from pynput import keyboard as _keyboard  # type: ignore[reportMissingModuleSource]
    keyboard = _keyboard
except ImportError:
    _MISSING.append('pynput (pip install pynput)')
try:
    import requests as _requests  # type: ignore[reportMissingModuleSource]
    requests = _requests
except ImportError:
    _MISSING.append('python3-requests')

for _cmd, _pkg in [('arecord', 'alsa-utils'), ('xdotool', 'xdotool'),
                    ('xclip', 'xclip'), ('xprop', 'x11-utils')]:
    if not shutil.which(_cmd):
        _MISSING.append(_pkg)

if _MISSING:
    sys.exit(
        "ERROR: missing dependencies: " + ", ".join(_MISSING) + "\n"
        "  Install with:\n"
        "    sudo apt install python3-requests alsa-utils xdotool xclip x11-utils\n"
        "    pip install pynput"
    )

# --- Provider configuration ---------------------------------------------------

class ProviderConfig(TypedDict):
    api_url: str
    model: str
    env_key: str


PROVIDERS: dict[str, ProviderConfig] = {
    'groq': {
        'api_url': 'https://api.groq.com/openai/v1/audio/transcriptions',
        'model': 'whisper-large-v3-turbo',
        'env_key': 'GROQ_API_KEY',
    },
    'openai': {
        'api_url': 'https://api.openai.com/v1/audio/transcriptions',
        'model': 'gpt-4o-mini-transcribe',
        'env_key': 'OPENAI_API_KEY',
    },
    'mistral': {
        'api_url': 'https://api.mistral.ai/v1/audio/transcriptions',
        'model': 'voxtral-mini-latest',
        'env_key': 'MISTRAL_API_KEY',
    },
}

if len(sys.argv) < 2 or sys.argv[1] not in PROVIDERS:
    sys.exit(f"Usage: {sys.argv[0]} <provider>\n  Providers: {', '.join(PROVIDERS)}")
PROVIDER = sys.argv[1]

_conf = PROVIDERS[PROVIDER]
API_URL = _conf['api_url']
MODEL = _conf['model']
API_KEY = os.environ.get(_conf['env_key'])
if not API_KEY:
    sys.exit(f"ERROR: {_conf['env_key']} not set.\n  export {_conf['env_key']}='...'")

# --- State directory ----------------------------------------------------------

STATE_DIR = os.path.join(
    os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'),
    'dictate'
)

DICTATING_FILE = os.path.join(STATE_DIR, 'dictating')
LAST_TRANSCRIPTION_FILE = os.path.join(STATE_DIR, 'last-transcription.txt')

BEEP_START = os.path.join(STATE_DIR, 'beep-start.wav')
BEEP_STOP = os.path.join(STATE_DIR, 'beep-stop.wav')
BEEP_READY = os.path.join(STATE_DIR, 'beep-ready.wav')

SAMPLE_RATE = 16000
WAV_HEADER_BYTES = 44
BYTES_PER_SECOND = SAMPLE_RATE * 2  # S16_LE, mono

# arecord takes ~130ms to open the capture device, and until then it has
# written only the WAV header. Sending such a file makes the transcription
# APIs reject it ("Audio file is too short. Minimum audio length is 0.01
# seconds."), so treat anything that brief as a missed keypress.
MIN_AUDIO_SECONDS = 0.05

TERMINALS = frozenset({
    'gnome-terminal', 'xterm', 'urxvt', 'alacritty', 'kitty', 'konsole',
    'xfce4-terminal', 'terminator', 'tilix', 'st', 'sakura', 'guake',
    'terminology', 'wezterm', 'foot',
})


def ensure_state_dir() -> None:
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)


# --- Audio feedback -----------------------------------------------------------

def generate_wav(freq: float, duration: float = 0.1, volume: float = 0.15) -> bytes:
    n = int(SAMPLE_RATE * duration)
    samples = struct.pack(
        f'<{n}h',
        *(int(math.sin(2 * math.pi * freq * i / SAMPLE_RATE) * 32767 * volume)
          for i in range(n))
    )
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples)
    return buf.getvalue()


def ensure_beep_files() -> None:
    """Generate beep WAV files in STATE_DIR if they don't already exist."""
    for path, freq, duration in [
        (BEEP_START, 880, 0.1),
        (BEEP_STOP, 440, 0.1),
        (BEEP_READY, 660, 0.15),
    ]:
        if not os.path.exists(path):
            with open(path, 'wb') as f:
                f.write(generate_wav(freq, duration))


def play_beep(path: str) -> None:
    threading.Thread(
        target=lambda: subprocess.run(['aplay', '-q', path], stderr=subprocess.DEVNULL),
        daemon=True,
    ).start()


# --- Window detection & paste -------------------------------------------------

def is_terminal() -> bool:
    try:
        wid = subprocess.check_output(
            ['xdotool', 'getactivewindow'], stderr=subprocess.DEVNULL
        ).strip()
        wm_class = subprocess.check_output(
            ['xprop', '-id', wid, 'WM_CLASS'], stderr=subprocess.DEVNULL
        ).decode().lower()
        return any(t in wm_class for t in TERMINALS)
    except Exception:
        return False


def copy_and_paste(text: str) -> None:
    # Strip control characters to prevent command injection in terminals
    text = ''.join(c for c in text if c >= ' ')
    subprocess.run(['xclip', '-selection', 'clipboard'], input=text.encode(), check=True)
    if is_terminal():
        time.sleep(0.05)
        subprocess.run(['xdotool', 'key', 'ctrl+shift+v'], check=True)


# --- Transcription ------------------------------------------------------------

def recorded_seconds(wav_path: str) -> float:
    """Duration of audio actually captured.

    Derived from the file size rather than the WAV header: arecord is stopped
    with SIGINT and so never seeks back to patch its length field, leaving a
    2GiB placeholder there.
    """
    payload = max(0, os.path.getsize(wav_path) - WAV_HEADER_BYTES)
    return payload / BYTES_PER_SECOND


def transcribe(wav_path: str) -> str:
    with open(wav_path, 'rb') as f:
        resp = requests.post(
            API_URL,
            headers={'Authorization': f'Bearer {API_KEY}'},
            files={'file': ('audio.wav', f, 'audio/wav')},
            data={'model': MODEL},
        )
    if not resp.ok:
        raise RuntimeError(f'{resp.status_code} from {PROVIDER}: {resp.text.strip()}')
    payload: Any = resp.json()
    return str(payload['text']).strip()


# --- Main ---------------------------------------------------------------------

def main() -> None:
    ensure_state_dir()
    ensure_beep_files()

    print(f'Dictate ready!  [provider: {PROVIDER}, model: {MODEL}]')
    print('  Super+F5 = start recording (press again to restart)')
    print('  Super+F6 = stop & transcribe')
    print('  Press Ctrl+C to quit.')
    print()

    super_held = False
    recording = False
    arecord_proc: subprocess.Popen[bytes] | None = None
    tmpfile: str | None = None
    lock = threading.Lock()

    def stop_recording() -> None:
        nonlocal recording, arecord_proc
        if arecord_proc:
            arecord_proc.send_signal(signal.SIGINT)
            arecord_proc.wait()
            arecord_proc = None
        recording = False

    def start_recording() -> None:
        nonlocal recording, arecord_proc, tmpfile
        if recording:
            stop_recording()
            if tmpfile:
                os.unlink(tmpfile)
            print(' (restarted)')
        # Signal that recording is active for external status integrations.
        pathlib.Path(DICTATING_FILE).touch()
        tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        tmpfile = tmp.name
        tmp.close()
        recording = True
        play_beep(BEEP_START)
        arecord_proc = subprocess.Popen(
            ['arecord', '-f', 'S16_LE', '-r', str(SAMPLE_RATE),
             '-c', '1', '-t', 'wav', '-q', tmpfile],
            stderr=subprocess.DEVNULL,
        )
        print('[recording...]', end='', flush=True)

    def stop_and_transcribe() -> None:
        nonlocal tmpfile
        stop_recording()
        play_beep(BEEP_STOP)
        try:
            captured = recorded_seconds(tmpfile) if tmpfile else 0.0
            if tmpfile and captured >= MIN_AUDIO_SECONDS:
                print(' transcribing...', end='', flush=True)
                text = transcribe(tmpfile)
                if text:
                    print(f' "{text}"')
                    pathlib.Path(LAST_TRANSCRIPTION_FILE).write_text(text, encoding='utf-8')
                    copy_and_paste(text)
                    play_beep(BEEP_READY)
                else:
                    print(' (empty)')
            else:
                print(f' (no audio: captured {captured:.3f}s — is the mic muted or'
                      f' still waking up? check `pactl info | grep "Default Source"`)')
        except Exception as e:
            print(f' ERROR: {e}')
        finally:
            if tmpfile:
                os.unlink(tmpfile)
                tmpfile = None
            try:
                os.unlink(DICTATING_FILE)
            except FileNotFoundError:
                pass

    SUPER_KEYS = {keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r}

    def on_press(key: Any) -> None:
        nonlocal super_held
        with lock:
            if key in SUPER_KEYS:
                super_held = True
                return
            if not super_held:
                return

            if key == keyboard.Key.f5:
                start_recording()
            elif key == keyboard.Key.f6 and recording:
                stop_and_transcribe()

    def on_release(key: Any) -> None:
        nonlocal super_held
        if key in SUPER_KEYS:
            super_held = False

    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        try:
            listener.join()
        except KeyboardInterrupt:
            print('\nBye!')


if __name__ == '__main__':
    main()
