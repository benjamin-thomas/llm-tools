#!/usr/bin/env python3
# Type checking:
#   pyright --project pyrightconfig.json
#
# VS Code:
#   Install/enable the Python extension and Pylance. Pylance reads
#   pyrightconfig.json in this repository and checks this file when it is open.

"""Read web content aloud, paragraph by paragraph.

Supports plain articles (via w3m), Discourse forum posts (via the JSON API),
Reddit threads (via the JSON API), and Hacker News threads.
Uses OpenAI TTS with a local on-disk cache and background prefetching.

Usage:
    python3 read_aloud_web.py <url>

Controls (active at any time, including during playback):
    s = stop and wait (then any key resumes)
    r = replay current paragraph
    n = next paragraph
    p = previous paragraph
    q = quit
"""

from __future__ import annotations

import hashlib
import os
import re
import select
import subprocess
import sys
import tty
import termios
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Literal, TypedDict, cast

MAX_CHUNK = 4000  # OpenAI TTS limit is 4096 chars; leave margin

CACHE_DIR = os.path.join(
    os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache')),
    'read-article-tts',
)

ResponseType = Literal['raw', 'base64_json']
PayloadBuilder = Callable[[str], dict[str, str]]


class VoiceConfig(TypedDict):
    label: str
    url: str
    api_key_env: str
    response_type: ResponseType
    max_parallel: int
    payload: PayloadBuilder


VoiceEntry = tuple[str, VoiceConfig]


# ── Voice registry ──────────────────────────────────────────────
# Add a voice = append one tuple. Each entry owns its own payload
# builder because field names differ between providers.
def _mistral_clean(text: str) -> str:
    """Normalize Unicode to ASCII-safe text for Mistral's voxtral TTS."""
    replacements = {
        '\u201c': '"', '\u201d': '"',   # smart double quotes
        '\u2018': "'", '\u2019': "'",   # smart single quotes
        '\u2014': ' - ', '\u2013': '-', # em/en dash
        '\u2192': '->', '\u2190': '<-', # arrows
        '\u2026': '...',               # ellipsis
        '\u00ab': '"', '\u00bb': '"',  # guillemets
        '\u200b': '', '\u00a0': ' ',   # zero-width space, non-breaking space
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    # Strip markdown formatting
    text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text)  # bold/italic
    text = re.sub(r'_{1,3}(.+?)_{1,3}', r'\1', text)     # underscore bold/italic
    text = re.sub(r'~~(.+?)~~', r'\1', text)              # strikethrough
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)  # headings
    text = re.sub(r'^\s*>\s?', '', text, flags=re.MULTILINE)    # blockquotes
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)  # unordered lists
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)  # ordered lists
    text = re.sub(r'`([^`]+)`', r'\1', text)              # inline code
    text = re.sub(r'^---+$', '', text, flags=re.MULTILINE)  # horizontal rules
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)  # links
    return text


VOICES: list[VoiceEntry] = [
    ('openai_shimmer', {
        'label': 'OpenAI shimmer (EN)',
        'url': 'https://api.openai.com/v1/audio/speech',
        'api_key_env': 'OPENAI_API_KEY',
        'response_type': 'raw',  # stream raw audio bytes to file
        'max_parallel': 5,
        'payload': lambda text: {
            'model': 'tts-1', 'voice': 'shimmer',
            'input': text, 'response_format': 'wav',
        },
    }),
    ('openai_nova', {
        'label': 'OpenAI nova (FR)',
        'url': 'https://api.openai.com/v1/audio/speech',
        'api_key_env': 'OPENAI_API_KEY',
        'response_type': 'raw',
        'max_parallel': 5,
        'payload': lambda text: {
            'model': 'tts-1', 'voice': 'nova',
            'input': text, 'response_format': 'wav',
        },
    }),
    ('mistral_paul', {
        'label': 'Mistral en_paul_neutral',
        'url': 'https://api.mistral.ai/v1/audio/speech',
        'api_key_env': 'MISTRAL_API_KEY',
        'response_type': 'base64_json',  # {"audio_data": "<base64>"}
        'max_parallel': 2,
        'payload': lambda text: {
            'model': 'voxtral-mini-tts-latest',
            'voice_id': 'en_paul_neutral',
            'input': _mistral_clean(text),
            'response_format': 'wav',
        },
    }),
    ('mistral_marie', {
        'label': 'Mistral fr_marie_neutral',
        'url': 'https://api.mistral.ai/v1/audio/speech',
        'api_key_env': 'MISTRAL_API_KEY',
        'response_type': 'base64_json',
        'max_parallel': 2,
        'payload': lambda text: {
            'model': 'voxtral-mini-tts-latest',
            'voice_id': 'fr_marie_neutral',
            'input': _mistral_clean(text),
            'response_format': 'wav',
        },
    }),
]

# Selected voice (set in main() after prompting). Module-level so
# download_chunk() / _cache_key() can read it.
selected_voice_key = VOICES[0][0]
selected_voice = VOICES[0][1]


def extract_text(url: str) -> str:
    result = subprocess.run(
        ['w3m', '-dump', url],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        sys.exit(f"w3m failed: {result.stderr.strip()}")
    return result.stdout


def extract_file(path: str) -> str:
    """Read plain text from a local file (markdown, txt, etc.)."""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        sys.exit(f"File not found: {path}")
    print(f"Reading file: {path}")
    with open(path, encoding='utf-8') as f:
        return f.read()


def extract_reddit(url: str) -> str:
    """Fetch a Reddit thread via its .json endpoint and return plain text
    with per-post / per-comment headers. Skips 'more' placeholders."""
    import json
    import urllib.request

    # Normalize: strip query string, strip trailing slash, append .json
    # Query params request a deeper/wider tree in a single response.
    url = url.split('?')[0].rstrip('/')
    if not url.endswith('.json'):
        url = url + '.json'
    url = url + '?limit=500&depth=20&sort=old'

    print(f"Fetching Reddit JSON: {url}")
    req = urllib.request.Request(url, headers={'User-Agent': 'read-aloud-web/1.0'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data: Any = json.load(resp)

    parts: list[str] = []
    post = data[0]['data']['children'][0]['data']
    post_author = post.get('author', 'unknown')
    parts.append(f"{post.get('title', '').strip()}")
    parts.append(f"Original post by {post_author}:\n{post.get('selftext', '').strip()}")

    def walk(children: list[Any], parent_author: str) -> None:
        for c in children:
            kind = c.get('kind')
            if kind == 'more':
                count = c.get('data', {}).get('count', 0)
                if count > 0:
                    parts.append(f"[{count} more replies not loaded]")
                else:
                    parts.append("[thread continues deeper]")
                continue
            if kind != 't1':
                continue
            cd = c['data']
            body = (cd.get('body') or '').strip()
            if not body or body == '[deleted]' or body == '[removed]':
                continue
            author = cd.get('author', 'unknown')
            header = f"{author} replies to {parent_author}:"
            # single \n keeps header glued to the first body paragraph
            parts.append(f"{header}\n{body}")
            replies = cd.get('replies')
            if isinstance(replies, dict):
                replies_dict = cast(dict[str, Any], replies)
                reply_data = replies_dict.get('data', {})
                if isinstance(reply_data, dict):
                    reply_data_dict = cast(dict[str, Any], reply_data)
                    children = reply_data_dict.get('children', [])
                    walk(children, str(author))

    walk(data[1]['data']['children'], post_author)
    return '\n\n'.join(parts)


def _plain_text(node: Any, *, separator: str = ' ') -> str:
    text = str(node.get_text(separator=separator)).strip()
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n[ \t]+', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def _int_attr(node: Any, name: str, default: int = 0) -> int:
    if node is None:
        return default
    raw: object = cast(object, node.get(name, default))
    if isinstance(raw, list):
        raw = cast(object, raw[0]) if raw else default
    try:
        return int(str(raw))
    except ValueError:
        return default


def parse_hacker_news_html(html: str) -> str:
    """Return a readable Hacker News item page in conversation order.

    HN renders comments as a pre-order tree. The `td.ind indent=N` attribute
    gives each comment's nesting level, so a stack is enough to recover the
    parent speaker while preserving the on-page conversation order.
    """
    from bs4 import BeautifulSoup as _BeautifulSoup  # type: ignore[reportMissingImports]
    BeautifulSoup = cast(Any, _BeautifulSoup)

    soup = BeautifulSoup(html, 'html.parser')
    parts: list[str] = []

    story = soup.select_one('tr.athing.submission')
    if story is not None:
        title_node = story.select_one('.titleline > a') or story.select_one('td.title a')
        title = _plain_text(title_node) if title_node is not None else ''
        if title:
            parts.append(title)

        subtext_row = story.find_next_sibling('tr')
        author_node = subtext_row.select_one('.subtext .hnuser') if subtext_row is not None else None
        author = _plain_text(author_node) if author_node is not None else 'unknown'
        toptext_node = soup.select_one('.toptext')
        toptext = _plain_text(toptext_node, separator='\n\n') if toptext_node is not None else ''
        if toptext:
            parts.append(f"Original post by {author}:\n{toptext}")

    author_stack: list[str] = []
    for row in cast(list[Any], soup.select('tr.athing.comtr')):
        indent = _int_attr(row.select_one('td.ind'), 'indent')
        author_node = row.select_one('.hnuser')
        author = _plain_text(author_node) if author_node is not None else 'unknown'

        comment_node = row.select_one('.comment')
        if comment_node is None:
            continue
        for reply_node in cast(list[Any], comment_node.select('.reply')):
            reply_node.decompose()
        body_node = comment_node.select_one('.commtext') or comment_node
        body = _plain_text(body_node, separator='\n\n')
        if not body or body in {'[deleted]', '[dead]', '[flagged]'}:
            continue

        if indent <= 0 or not author_stack:
            header = f"Comment by {author}:"
        else:
            parent_author = author_stack[indent - 1] if indent - 1 < len(author_stack) else 'parent comment'
            header = f"{author} replies to {parent_author}:"
        parts.append(f"{header}\n{body}")

        if indent < len(author_stack):
            author_stack[indent] = author
            del author_stack[indent + 1:]
        else:
            author_stack.extend(['parent comment'] * (indent - len(author_stack)))
            author_stack.append(author)

    return '\n\n'.join(parts)


def extract_hacker_news(url: str) -> str:
    """Fetch a Hacker News item page and return a readable comment thread."""
    import urllib.request

    print(f"Fetching Hacker News thread: {url}")
    req = urllib.request.Request(url, headers={'User-Agent': 'read-aloud-web/1.0'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode('utf-8', errors='replace')
    return parse_hacker_news_html(html)


def extract_discourse(url: str) -> str:
    """Fetch a Discourse topic via its JSON API and return plain text
    with per-post headers. Strips 'in reply to' quote asides."""
    import json
    import urllib.request
    from bs4 import BeautifulSoup as _BeautifulSoup  # type: ignore[reportMissingImports]
    BeautifulSoup = cast(Any, _BeautifulSoup)

    # Normalize: strip trailing /<post_number>, then append .json
    url = re.sub(r'/\d+/?$', '', url.rstrip('/'))
    if not url.endswith('.json'):
        url = url + '.json'

    print(f"Fetching Discourse JSON: {url}")
    req = urllib.request.Request(url, headers={'User-Agent': 'read-article/1.0'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data: Any = json.load(resp)

    title = data.get('title', '').strip()
    posts = data.get('post_stream', {}).get('posts', [])

    parts: list[str] = []
    if title:
        parts.append(title)

    for idx, post in enumerate(posts):
        username = post.get('username', 'unknown')
        cooked = post.get('cooked', '')
        soup = BeautifulSoup(cooked, 'html.parser')

        # Strip 'in reply to' recaps
        for aside in cast(list[Any], soup.find_all('aside', class_='quote')):
            aside.decompose()

        body = str(soup.get_text(separator='\n\n')).strip()
        body = re.sub(r'\n{3,}', '\n\n', body)

        header = f"Original post by {username}:" if idx == 0 else f"Reply by {username}:"
        parts.append(f"{header}\n\n{body}")

    return '\n\n'.join(parts)


def split_paragraphs(text: str) -> list[str]:
    paragraphs = re.split(r'\n\n+', text)
    return [p.strip() for p in paragraphs if p.strip()]


def chunk_text(text: str, max_len: int = MAX_CHUNK) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        cut = text.rfind('. ', 0, max_len)
        if cut == -1:
            cut = text.rfind(' ', 0, max_len)
        if cut == -1:
            cut = max_len
        else:
            cut += 1
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    return chunks


def _cache_key(text: str) -> str:
    """Hash voice key + text to get a stable cache filename."""
    h = hashlib.sha256(f"{selected_voice_key}:{text}".encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{h}.wav")


def play_wav(path: str) -> str | None:
    """Play a WAV file, checking for keypresses while it plays.
    Returns the key pressed, or None if playback finished normally."""
    proc = subprocess.Popen(['aplay', '-q', path], stderr=subprocess.DEVNULL)
    while proc.poll() is None:
        if select.select([sys.stdin], [], [], 0.1)[0]:
            key = sys.stdin.read(1)
            proc.terminate()
            proc.wait()
            return key
    return None


def download_chunk(text: str) -> str:
    """Ensure WAV is on disk (cached or freshly downloaded). Returns path."""
    path = _cache_key(text)
    if os.path.exists(path):
        return path

    import time
    import requests  # type: ignore[reportMissingModuleSource]
    api_key = os.environ.get(selected_voice['api_key_env'])
    if not api_key:
        sys.exit(f"ERROR: {selected_voice['api_key_env']} not set.")

    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = path + '.tmp'
    headers = {'Authorization': f'Bearer {api_key}'}
    body = selected_voice['payload'](text)
    tts_input = body.get('input', text)

    # Retry on 5xx / transient network errors (e.g. Mistral "Service unavailable")
    MAX_ATTEMPTS = 4
    for attempt in range(MAX_ATTEMPTS):
        try:
            if selected_voice['response_type'] == 'raw':
                resp = requests.post(selected_voice['url'], headers=headers, json=body, stream=True, timeout=60)
                if 500 <= resp.status_code < 600:
                    raise requests.HTTPError(f"{resp.status_code}: {resp.text[:200]}", response=resp)
                resp.raise_for_status()
                with open(tmp, 'wb') as f:
                    for c in resp.iter_content(chunk_size=4096):
                        if c:
                            f.write(c)
            elif selected_voice['response_type'] == 'base64_json':
                import base64
                resp = requests.post(selected_voice['url'], headers=headers, json=body, timeout=60)
                if 500 <= resp.status_code < 600:
                    raise requests.HTTPError(f"{resp.status_code}: {resp.text[:200]}", response=resp)
                resp.raise_for_status()
                with open(tmp, 'wb') as f:
                    f.write(base64.b64decode(resp.json()['audio_data']))
            else:
                sys.exit(f"ERROR: unknown response_type: {selected_voice['response_type']}")
            break
        except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as e:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            delay = 0.5 * (2 ** attempt)  # 0.5, 1.0, 2.0
            preview = tts_input[:120] + ('...' if len(tts_input) > 120 else '')
            print(f"\n\033[33m[retry {attempt+1}/{MAX_ATTEMPTS-1}] {e} — waiting {delay}s\n  text: {preview!r}\033[0m", file=sys.stderr)
            time.sleep(delay)

    os.rename(tmp, path)
    return path


def check_key() -> str | None:
    """Non-blocking single keypress check. Returns the key or None."""
    if select.select([sys.stdin], [], [], 0)[0]:
        return sys.stdin.read(1)
    return None


def main() -> None:
    global selected_voice_key, selected_voice

    if len(sys.argv) < 2:
        sys.exit("Usage: python3 read_aloud_web.py <url>")

    url = sys.argv[1]

    print(f"\033[2mCache: {CACHE_DIR}\033[0m\n")

    # Auto-detect local files and thread URLs with unambiguous hostnames.
    is_file = url.startswith(('~', '/', './', '../')) or not url.startswith(('http://', 'https://'))
    parsed_url = urllib.parse.urlparse(url)
    host = parsed_url.netloc.lower()
    if is_file:
        print("Detected: local file")
        choice = 'file'
    elif 'reddit.com' in url:
        print("Detected: Reddit thread")
        choice = '3'
    elif host in {'news.ycombinator.com', 'www.news.ycombinator.com'} and parsed_url.path == '/item':
        print("Detected: Hacker News thread")
        choice = '4'
    else:
        print("What kind of content?")
        print("  [1] Article / web page (default)")
        print("  [2] Discourse forum post")
        print("  [3] Reddit thread")
        print("  [4] Hacker News thread")
        choice = input("> ").strip()

    print("\nWhich voice?")
    for i, (_, v) in enumerate(VOICES, start=1):
        suffix = " (default)" if i == 1 else ""
        print(f"  [{i}] {v['label']}{suffix}")
    vchoice = input("> ").strip()
    if vchoice.isdigit() and 1 <= int(vchoice) <= len(VOICES):
        selected_voice_key, selected_voice = VOICES[int(vchoice) - 1]
    print(f"Using: {selected_voice['label']}\n")

    if choice == 'file':
        text = extract_file(url)
    elif choice == '2':
        text = extract_discourse(url)
    elif choice == '3':
        text = extract_reddit(url)
    elif choice == '4':
        text = extract_hacker_news(url)
    else:
        print(f"Fetching: {url}")
        text = extract_text(url)

    paragraphs = split_paragraphs(text)
    total = len(paragraphs)
    print(f"Found {total} paragraphs.")
    print("\033[2m(s=stop/replay, n=next, p=prev, q=quit — auto-advances)\033[0m\n")

    # Build a flat list of (paragraph_index, chunk_text) for sequential playback
    all_chunks: list[tuple[int, str]] = []
    for pi, para in enumerate(paragraphs):
        for chunk in chunk_text(para):
            all_chunks.append((pi, chunk))

    prefetch = selected_voice.get('max_parallel', 5)

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        pool = ThreadPoolExecutor(max_workers=prefetch)
        futures: dict[int, Future[str]] = {}

        def ensure_prefetched(from_idx: int) -> None:
            for ci in range(from_idx, min(from_idx + prefetch, len(all_chunks))):
                if ci not in futures:
                    futures[ci] = pool.submit(download_chunk, all_chunks[ci][1])

        def cache_status(from_idx: int) -> str:
            """Return a string like [###..] showing which upcoming chunks are ready."""
            icons: list[str] = []
            for ci in range(from_idx, min(from_idx + prefetch, len(all_chunks))):
                path = _cache_key(all_chunks[ci][1])
                if os.path.exists(path):
                    icons.append('\033[32m#\033[0m')  # green = ready
                elif ci in futures and futures[ci].done():
                    icons.append('\033[32m#\033[0m')  # green = just finished
                else:
                    icons.append('\033[2m.\033[0m')   # dim = pending
            return '[' + ''.join(icons) + ']'

        ci = 0
        current_para = -1
        while ci < len(all_chunks):
            pi, chunk = all_chunks[ci]

            # Prefetch upcoming chunks
            ensure_prefetched(ci)

            # Print paragraph header when we enter a new paragraph
            if pi != current_para:
                current_para = pi
                para = paragraphs[pi]
                print(f"\033[1m[{pi+1}/{total}]\033[0m {cache_status(ci)}\n{para}\n")

            # Wait for current chunk to be ready, then play
            if ci in futures:
                path = futures.pop(ci).result()
            else:
                path = download_chunk(chunk)
            key = play_wav(path)

            # Also check for keypress right after playback ends
            if key is None:
                key = check_key()
            if key == 'q':
                print("\nDone.")
                pool.shutdown(wait=False)
                return
            elif key == 's':
                # Stop and wait. Any key restarts the current paragraph,
                # except n/p/q which navigate/quit.
                print("\033[2m-- stopped (s=stop/restart, n=next, p=prev, q=quit) --\033[0m")
                key = sys.stdin.read(1)
                if key == 'q':
                    print("\nDone.")
                    pool.shutdown(wait=False)
                    return
                elif key == 'n':
                    target = current_para + 1
                    if target < total:
                        ci = next(j for j, (p, _) in enumerate(all_chunks) if p == target)
                        current_para = -1
                    else:
                        ci = len(all_chunks)
                elif key == 'p':
                    target = max(0, current_para - 1)
                    ci = next(j for j, (p, _) in enumerate(all_chunks) if p == target)
                    current_para = -1
                else:
                    # Replay current paragraph from the start
                    ci = next(j for j, (p, _) in enumerate(all_chunks) if p == current_para)
                continue
            elif key == 'n':
                # Skip to start of next paragraph
                target = current_para + 1
                if target < total:
                    ci = next(j for j, (p, _) in enumerate(all_chunks) if p == target)
                    current_para = -1
                else:
                    ci = len(all_chunks)  # end
            elif key == 'p':
                # Jump to start of previous paragraph
                target = max(0, current_para - 1)
                ci = next(j for j, (p, _) in enumerate(all_chunks) if p == target)
                current_para = -1
            else:
                ci += 1

        pool.shutdown(wait=False)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    print("End of article.")


if __name__ == '__main__':
    main()
