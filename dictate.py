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
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
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

SAMPLE_RATE = 16000
API_URL = 'https://api.groq.com/openai/v1/audio/transcriptions'
MODEL = 'whisper-large-v3-turbo'

TERMINALS = frozenset({
    'gnome-terminal', 'xterm', 'urxvt', 'alacritty', 'kitty', 'konsole',
    'xfce4-terminal', 'terminator', 'tilix', 'st', 'sakura', 'guake',
    'terminology', 'wezterm', 'foot',
})


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


def make_beep_file(freq, duration=0.1):
    tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    tmp.write(generate_wav(freq, duration))
    tmp.close()
    return tmp.name


START_BEEP = make_beep_file(880, 0.1)
STOP_BEEP = make_beep_file(440, 0.1)
READY_BEEP = make_beep_file(660, 0.15)


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
    subprocess.run(['xclip', '-selection', 'clipboard'], input=text.encode(), check=True)
    if is_terminal():
        import time; time.sleep(0.05)
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
        open('/tmp/dictating', 'w').close()
        tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        tmpfile = tmp.name
        tmp.close()
        recording = True
        play_beep(START_BEEP)
        arecord_proc = subprocess.Popen(
            ['arecord', '-f', 'S16_LE', '-r', str(SAMPLE_RATE),
             '-c', '1', '-t', 'wav', '-q', tmpfile],
            stderr=subprocess.DEVNULL,
        )
        print('[recording...]', end='', flush=True)

    def stop_and_transcribe():
        nonlocal tmpfile
        stop_recording()
        play_beep(STOP_BEEP)
        if tmpfile and os.path.getsize(tmpfile) > 0:
            print(' transcribing...', end='', flush=True)
            try:
                text = transcribe(tmpfile)
                if text:
                    print(f' "{text}"')
                    copy_and_paste(text)
                    play_beep(READY_BEEP)
                else:
                    print(' (empty)')
            except Exception as e:
                print(f' ERROR: {e}')
            finally:
                os.unlink(tmpfile)
                tmpfile = None
                try:
                    os.unlink('/tmp/dictating')
                except FileNotFoundError:
                    pass
        else:
            print(' (no audio)')
            try:
                os.unlink('/tmp/dictating')
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
                    open('/tmp/tts-prev', 'w').close()
                else:
                    open('/tmp/tts-skip', 'w').close()
            elif key == keyboard.Key.f8:
                open('/tmp/tts-pause', 'w').close()
            elif key == keyboard.Key.f9:
                backend_file = '/tmp/tts-backend'
                try:
                    current = open(backend_file).read().strip()
                except FileNotFoundError:
                    current = 'piper'
                new_backend = 'openai' if current == 'piper' else 'piper'
                open(backend_file, 'w').write(new_backend)
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
