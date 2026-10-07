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

## Sandbox Agent clipboard

`sandbox-agent` runs agents in Herdr (default) or tmux, including inside an outer
tmux. Each session gets a private X server; the host's X11 socket and cookie stay
outside the sandbox. Native clipboard clients and ordinary `xclip` calls use
the same relay for text and images, with no agent-specific configuration.

Install the system dependencies:

```bash
sudo apt install python3-xlib xserver-xephyr xvfb xclip x11-utils x11-xserver-utils xdotool
```

Xephyr provides the GUI by default. `--no-gui` uses Xvfb without a window and
still supports the clipboard. The relay shares only the current `CLIPBOARD`
selection, reads its data on demand, and keeps transfer bytes in memory only.
There is no clipboard history, payload file, or focus-triggered copy. Replacing
or clearing the source, or closing its owner, cancels pending transfers.
`PRIMARY`, file transfers, and clipboard-manager history protocols are excluded.
Transfers are limited to 64 MiB of buffered data overall and 5 seconds without
progress.

Existing sessions must be stopped and relaunched with `sandbox-agent` to load
changes to the relay. Reattaching to a multiplexer or restarting only the agent
does not replace the session's host-side relay. No desktop restart is needed.

Clipboard tests use disposable X servers, not the real desktop clipboard:

```bash
/usr/bin/ruby tests/sandbox_x11_test.rb
/usr/bin/python3 -B -m unittest discover -s tests -p test_sandbox_clipboard.py
```

The integration suite also needs Bubblewrap, tmux and Herdr installed at
`~/.local/bin/herdr`, and permission to create local sockets and namespaces.
