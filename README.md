# llm-tools

Push-to-Talk dictation (Speech-to-Text) and TTS for GNOME/X11.

Records audio via `arecord`, transcribes with Groq's Whisper API, and pastes the result into the active window. TTS reads text aloud via OpenAI.

## Dependencies

```bash
sudo apt install python3-requests alsa-utils xdotool xclip x11-utils
pip install pynput
```

API keys are stored in `pass` (password-store):

```bash
pass insert GROQ_API_KEY
pass insert OPENAI_API_KEY  # for OpenAI TTS
```

## Usage

```bash
./dictate_wrapper.sh
```

## Keybindings

| Shortcut | Action |
|---|---|
| Super+F5 | Start recording |
| Super+F6 | Stop recording & transcribe |

## Architecture

A long-running pynput listener captures Super+F5/F6 keypresses. State files live in `$XDG_RUNTIME_DIR/dictate/` (mode 0700, cleaned at logout).

The wrapper scripts avoid `export` so API keys are visible only to the Python process, not to child processes (`arecord`, `xdotool`, `xclip`, `aplay`).
