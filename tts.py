#!/usr/bin/env python3
"""TTS — Text-to-Speech with Piper (local) or OpenAI API (cloud).

Reads text from stdin and plays it aloud.

System deps:
    pip install piper-tts   (for Piper)
    sudo apt install alsa-utils
    OPENAI_API_KEY must be set (for OpenAI only)

Usage:
    echo "Bonjour, comment ça va ?" | python3 tts.py
    echo "Hello world" | python3 tts.py
"""

import os
import subprocess
import sys

# --- State directory ----------------------------------------------------------

STATE_DIR = os.path.join(
    os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'),
    'dictate'
)

TTS_BACKEND_FILE = os.path.join(STATE_DIR, 'tts-backend')

# ── Backend selection ─────────────────────────────────────────

def _read_backend():
    try:
        with open(TTS_BACKEND_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return 'piper'

USE_OPENAI = _read_backend() == 'openai'
# ─────────────────────────────────────────────────────────────

# ── OpenAI settings ──────────────────────────────────────────
OPENAI_VOICE = 'shimmer'
OPENAI_MODEL = 'tts-1'

# ── Piper settings ───────────────────────────────────────────
PIPER_MODEL_FR = '/tmp/piper-voices/fr_medium.onnx'
PIPER_MODEL_EN = '/tmp/piper-voices/en_medium.onnx'

text = sys.stdin.read().strip()
if not text:
    sys.exit(0)


def speak_openai(text):
    import requests

    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        sys.exit("ERROR: OPENAI_API_KEY not set.")

    resp = requests.post(
        'https://api.openai.com/v1/audio/speech',
        headers={'Authorization': f'Bearer {api_key}'},
        json={
            'model': OPENAI_MODEL,
            'voice': OPENAI_VOICE,
            'input': text,
            'response_format': 'wav',
        },
        stream=True,
    )
    resp.raise_for_status()

    proc = subprocess.Popen(['aplay', '-q'], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                proc.stdin.write(chunk)
        proc.stdin.close()
        proc.wait()
    except BrokenPipeError:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait()


def detect_language(text):
    """Simple French detection based on common French markers."""
    french_markers = ['à', 'é', 'è', 'ê', 'ë', 'ï', 'ô', 'ù', 'û', 'ü', 'ÿ', 'ç', 'œ', 'æ',
                      " le ", " la ", " les ", " des ", " du ", " un ", " une ",
                      " est ", " sont ", " dans ", " pour ", " avec ", " que ",
                      " qui ", " nous ", " vous ", " c'est ", " j'ai ", " n'est "]
    text_lower = text.lower()
    count = sum(1 for m in french_markers if m in text_lower)
    return 'fr' if count >= 3 else 'en'


def speak_piper(text):
    lang = detect_language(text)
    model = PIPER_MODEL_FR if lang == 'fr' else PIPER_MODEL_EN
    proc_piper = subprocess.Popen(
        ['piper', '--model', model, '--output-raw'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    proc_aplay = subprocess.Popen(
        ['aplay', '-q', '-r', '22050', '-f', 'S16_LE', '-c', '1'],
        stdin=proc_piper.stdout,
        stderr=subprocess.DEVNULL,
    )
    try:
        proc_piper.stdin.write(text.encode())
        proc_piper.stdin.close()
        proc_piper.wait()
        proc_aplay.wait()
    except BrokenPipeError:
        pass
    finally:
        if proc_piper.poll() is None:
            proc_piper.terminate()
            proc_piper.wait()
        if proc_aplay.poll() is None:
            proc_aplay.terminate()
            proc_aplay.wait()


if USE_OPENAI:
    speak_openai(text)
else:
    speak_piper(text)
