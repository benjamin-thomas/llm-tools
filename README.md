# llm-tools

Push-to-Talk dictation (Speech-to-Text) and TTS for GNOME/X11.

Records audio via `arecord`, transcribes with the selected speech-to-text provider, and pastes the result into the active window. TTS reads text aloud via OpenAI or Mistral.

## Dependencies

```bash
sudo apt install python3-requests python3-bs4 alsa-utils xdotool xclip x11-utils w3m
pip install pynput
```

API keys are stored in `pass` (password-store):

```bash
pass insert GROQ_API_KEY
pass insert OPENAI_API_KEY  # for OpenAI TTS
pass insert MISTRAL_API_KEY
```

## Usage

```bash
./dictate_wrapper.sh groq
# or:
./dictate_wrapper.sh openai
./dictate_wrapper.sh mistral
```

Read a web page or thread aloud:

```bash
./read_aloud_web_wrapper.sh https://news.ycombinator.com/item?id=8863
```

## Keybindings

| Shortcut | Action |
|---|---|
| Super+F5 | Start recording |
| Super+F6 | Stop recording & transcribe |

## Architecture

A long-running pynput listener captures Super+F5/F6 keypresses. State files live in `$XDG_RUNTIME_DIR/dictate/` (mode 0700, cleaned at logout).

Wrapper scripts load API keys from `pass` for the selected provider before starting Python.
