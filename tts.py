#!/usr/bin/env python3
# Type checking:
#   pyright --project pyrightconfig.json
#
# VS Code:
#   Install/enable the Python extension and Pylance. Pylance reads
#   pyrightconfig.json in this repository and checks this file when it is open.

"""TTS — Text-to-Speech with OpenAI API.

Reads text from stdin and plays it aloud.

System deps:
    sudo apt install alsa-utils
    OPENAI_API_KEY must be set

Usage:
    echo "Bonjour, comment ça va ?" | python3 tts.py
    echo "Hello world" | python3 tts.py
"""

from __future__ import annotations

import os
import subprocess
import sys

# ── OpenAI settings ──────────────────────────────────────────
OPENAI_VOICE = 'shimmer'
OPENAI_MODEL = 'tts-1'

def speak_openai(text: str) -> None:
    import requests  # type: ignore[reportMissingModuleSource]

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
    assert proc.stdin is not None
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


if __name__ == '__main__':
    text = sys.stdin.read().strip()
    if not text:
        sys.exit(0)
    speak_openai(text)
