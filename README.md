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

## Agent catalog and picker

`agent-catalog` prints the coding agents this machine can start now, one per
line: `harness:model:effort`, the slash command that clears the agent's
context, and its command line, with permission prompts off. The harnesses are
claude, codex, grok, vibe, opencode and pi; their models are asked of them at
each run. `agent-lib.rb` is the only place that knows a harness's flags.
opencode's TUI takes no reasoning effort, so its agents start through
`opencode-variant <provider/model> <variant>`, which sets the variant the TUI
remembers for that model first. pi offers its scoped models (`enabledModels`),
opencode and pi without them only the models that think.

`agent-pick --choices <file> <role>...` picks an agent per role with fzf,
remembers the picks in `<file>` and keeps them on Enter at the next run. It
prints `<role><TAB>` followed by the agent's catalog line. Symlink the three
to `~/.local/bin/`.

```bash
/usr/bin/ruby tests/agent_catalog_test.rb
/usr/bin/ruby tests/agent_pick_test.rb
```

## Token recap and quota gauges

`token-recap` totals this machine's tokens from the agents' local logs, one
box per subscription over its current window (Claude's week, Mistral's month,
Hyper's billing month from the 12th), priced at each provider's API list rate.
Each box's `used` bar is the provider's own meter (Hyper's is daily, from
18:00 Paris), with a tick where an even pace would be.
`quota-gauges` prints just those meters, one line each (`--json` for scripts).

Mistral serves its Vibe budget only to a logged-in browser, so a Chrome
extension reads it with your session and a native messaging host keeps it in
`~/.cache/quota-gauges/mistral.json`. The session cookie never leaves Chrome.

Chrome runs that host outside any sandbox, so it never runs from the repo,
which sandboxed agents can edit. `./quota-gauges install` copies the extension
and the host to `~/.local/lib/quota-gauges/` and points Chrome at the copy; it
says which files changed, so read those changes before you run it.
`sandbox-lib.rb` refuses to mount that directory, or Chrome's
`NativeMessagingHosts`, writable. Then in `chrome://extensions`, Developer
mode → Load unpacked → `~/.local/lib/quota-gauges/chrome/` (or reload it after
a reinstall). It reads the budget within a few seconds, then every minute.

```bash
python3 -m pytest tests/test_token_recap_*.py tests/test_quota_gauges*.py
```
