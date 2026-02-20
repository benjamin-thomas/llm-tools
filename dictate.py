#!/usr/bin/env python3
"""Dictate — Push-to-Talk Speech-to-Text via Groq API (X11).

Hold Super+F5 to record, release to transcribe and paste.
Uses Groq's Whisper large-v3-turbo with auto language detection.

System deps (no venv needed):
    sudo apt install python3-requests alsa-utils xdotool xclip x11-utils
    pip install pynput
    export GROQ_API_KEY="gsk_..."

Usage:
    python3 dictate.py
    (or chmod +x dictate.py && ./dictate.py)
"""

import io
import math
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

_MISSING = []
try:
    from pynput import keyboard
except ImportError:
    _MISSING.append('pynput (pip install pynput)')
try:
    import requests
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

GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
if not GROQ_API_KEY:
    sys.exit("ERROR: GROQ_API_KEY not set.\n  export GROQ_API_KEY='gsk_...'")

# --- State directory ----------------------------------------------------------

STATE_DIR = os.path.join(
    os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'),
    'dictate'
)

DICTATING_FILE = os.path.join(STATE_DIR, 'dictating')
TTS_SKIP_FILE = os.path.join(STATE_DIR, 'tts-skip')
TTS_PREV_FILE = os.path.join(STATE_DIR, 'tts-prev')
TTS_PAUSE_FILE = os.path.join(STATE_DIR, 'tts-pause')
TTS_BACKEND_FILE = os.path.join(STATE_DIR, 'tts-backend')

BEEP_START = os.path.join(STATE_DIR, 'beep-start.wav')
BEEP_STOP = os.path.join(STATE_DIR, 'beep-stop.wav')
BEEP_READY = os.path.join(STATE_DIR, 'beep-ready.wav')

SAMPLE_RATE = 16000
API_URL = 'https://api.groq.com/openai/v1/audio/transcriptions'
MODEL = 'whisper-large-v3-turbo'

TERMINALS = frozenset({
    'gnome-terminal', 'xterm', 'urxvt', 'alacritty', 'kitty', 'konsole',
    'xfce4-terminal', 'terminator', 'tilix', 'st', 'sakura', 'guake',
    'terminology', 'wezterm', 'foot',
})


def ensure_state_dir():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)


# --- Audio feedback -----------------------------------------------------------

def generate_wav(freq, duration=0.1, volume=0.15):
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


def ensure_beep_files():
    """Generate beep WAV files in STATE_DIR if they don't already exist."""
    for path, freq, duration in [
        (BEEP_START, 880, 0.1),
        (BEEP_STOP, 440, 0.1),
        (BEEP_READY, 660, 0.15),
    ]:
        if not os.path.exists(path):
            with open(path, 'wb') as f:
                f.write(generate_wav(freq, duration))


def play_beep(path):
    threading.Thread(
        target=lambda: subprocess.run(['aplay', '-q', path], stderr=subprocess.DEVNULL),
        daemon=True,
    ).start()


# --- Window detection & paste -------------------------------------------------

def is_terminal():
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


def copy_and_paste(text):
    # Strip control characters to prevent command injection in terminals
    text = ''.join(c for c in text if c >= ' ')
    subprocess.run(['xclip', '-selection', 'clipboard'], input=text.encode(), check=True)
    if is_terminal():
        time.sleep(0.05)
        subprocess.run(['xdotool', 'key', 'ctrl+shift+v'], check=True)


# --- Transcription ------------------------------------------------------------

def transcribe(wav_path):
    with open(wav_path, 'rb') as f:
        resp = requests.post(
            API_URL,
            headers={'Authorization': f'Bearer {GROQ_API_KEY}'},
            files={'file': ('audio.wav', f, 'audio/wav')},
            data={'model': MODEL},
        )
    resp.raise_for_status()
    return resp.json()['text'].strip()


# --- Main ---------------------------------------------------------------------

def main():
    ensure_state_dir()
    ensure_beep_files()

    print('Dictate ready!')
    print('  Super+F5 = start recording (press again to restart)')
    print('  Super+F6 = stop & transcribe')
    print('  Super+F7 = TTS skip to next paragraph')
    print('  Super+Shift+F7 = TTS go to previous paragraph')
    print('  Super+F8 = TTS pause/resume')
    print('  Super+F9 = Toggle TTS backend (Piper/OpenAI)')
    print('  Press Ctrl+C to quit.')
    print()

    super_held = False
    shift_held = False
    recording = False
    arecord_proc = None
    tmpfile = None
    lock = threading.Lock()

    def stop_recording():
        nonlocal recording, arecord_proc
        if arecord_proc:
            arecord_proc.send_signal(subprocess.signal.SIGINT)
            arecord_proc.wait()
            arecord_proc = None
        recording = False

    def start_recording():
        nonlocal recording, arecord_proc, tmpfile
        if recording:
            stop_recording()
            if tmpfile:
                os.unlink(tmpfile)
            print(' (restarted)')
        # Signal that we're dictating (TTS extension watches this)
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

    def stop_and_transcribe():
        nonlocal tmpfile
        stop_recording()
        play_beep(BEEP_STOP)
        try:
            if tmpfile and os.path.getsize(tmpfile) > 0:
                print(' transcribing...', end='', flush=True)
                text = transcribe(tmpfile)
                if text:
                    print(f' "{text}"')
                    copy_and_paste(text)
                    play_beep(BEEP_READY)
                else:
                    print(' (empty)')
            else:
                print(' (no audio)')
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
    SHIFT_KEYS = {keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r}

    def on_press(key):
        nonlocal super_held, shift_held
        with lock:
            if key in SUPER_KEYS:
                super_held = True
                return
            if key in SHIFT_KEYS:
                shift_held = True
                return
            if not super_held:
                return

            if key == keyboard.Key.f5:
                start_recording()
            elif key == keyboard.Key.f6 and recording:
                stop_and_transcribe()
            elif key == keyboard.Key.f7:
                if shift_held:
                    pathlib.Path(TTS_PREV_FILE).touch()
                else:
                    pathlib.Path(TTS_SKIP_FILE).touch()
            elif key == keyboard.Key.f8:
                pathlib.Path(TTS_PAUSE_FILE).touch()
            elif key == keyboard.Key.f9:
                try:
                    current = pathlib.Path(TTS_BACKEND_FILE).read_text().strip()
                except FileNotFoundError:
                    current = 'piper'
                new_backend = 'openai' if current == 'piper' else 'piper'
                pathlib.Path(TTS_BACKEND_FILE).write_text(new_backend)
                print(f'\n[TTS backend: {new_backend}]')

    def on_release(key):
        nonlocal super_held, shift_held
        if key in SUPER_KEYS:
            super_held = False
        elif key in SHIFT_KEYS:
            shift_held = False

    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        try:
            listener.join()
        except KeyboardInterrupt:
            print('\nBye!')


if __name__ == '__main__':
    main()
