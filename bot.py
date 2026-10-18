"""Telegram -> Notesnook Inbox API bridge.

Send a message to your Telegram bot and it shows up as a note in Notesnook
after your next sync. Bold, italic, links, code and quotes are kept.

The Inbox API has no attachment field, so media is embedded in the note's
HTML, the way the Notesnook email worker does it: photos and image files
become base64 data: URIs and text files become code blocks. Videos, voice
notes and other files get a placeholder line (plus the caption, if any).

Env vars:
  TELEGRAM_BOT_TOKEN   token from @BotFather
  NOTESNOOK_INBOX_KEY  key from Notesnook > Settings > Account > Inbox
  NOTESNOOK_SERVER_URL optional, self-hosted sync server, e.g.
                       https://notesnook.example.com (unset = hosted inbox)
  ALLOWED_USER_ID      your numeric Telegram user ID (only you can use the bot)
  BOT_TIMEZONE         optional, default America/New_York (generic note titles)
  EMBED_MEDIA          optional, set to 0 to turn off embedding (placeholders only)
  TAGS_FILE            optional, where saved tags live (default /data/tags.json
                       when a Fly volume is mounted at /data)
  NOTESNOOK_TAGS       optional, JSON of tag names to IDs used once to seed the file,
                       e.g. {"groceries": "67aecf3b9e1398484554bc90"}
"""

import asyncio
import base64
import html
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from telegram import BotCommand, Message, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

HOSTED_INBOX_URL = "https://inbox.notesnook.com/"
# Self-hosted sync server address, e.g. https://notesnook.example.com
# When set, notes are PGP-encrypted here and sent to <server>/inbox/items.
# When empty, the bot uses the hosted Notesnook inbox as before.
SERVER_URL = os.environ.get("NOTESNOOK_SERVER_URL", "").strip().rstrip("/")
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
INBOX_KEY = os.environ["NOTESNOOK_INBOX_KEY"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
TIMEZONE = os.environ.get("BOT_TIMEZONE", "America/New_York")  # for generic titles
__version__ = "1.4.1"  # bump when you change the bot; shown by /ver

ATTACHMENT_SIZE_LIMIT = 5 * 1024 * 1024  # per file, same as the email worker
NOTE_SIZE_LIMIT = 8_000_000  # bytes of HTML per note (the Inbox API allows 10 MB)
EMBED_MEDIA = os.environ.get("EMBED_MEDIA", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_TEXT_MIMES = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "application/toml",
    "application/javascript",
    "application/x-sh",
}
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
    ".toml", ".ini", ".log", ".py", ".js", ".ts", ".html", ".css", ".sh", ".sql",
}
# File extension -> Notesnook code block language (names from Notesnook's own
# language list). Extensions not listed stay plain text.
_CODE_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sh": "bash",
    ".sql": "sql",
    ".md": "markdown",
    ".markdown": "markdown",
    ".css": "css",
    ".toml": "toml",
    ".ini": "ini",
    ".csv": "csv",
    ".html": "markup",  # Notesnook's name for HTML/XML
    ".xml": "markup",
}

# Telegram-only tags Notesnook doesn't know; drop the tag, keep the text.
_TG_ONLY_TAGS = re.compile(r"</?tg-(?:spoiler|emoji)[^>]*>")
_PRE_BLOCK = re.compile(r"(<pre>.*?</pre>)", re.DOTALL)
_BLOCK_TAGS = ("<pre>", "<blockquote")


def media_label(msg: Message) -> str | None:
    """Return a human label if the message carries media, else None."""
    if msg.photo:
        return "Photo"
    for attr, label in (
        ("video", "Video"),
        ("video_note", "Video message"),
        ("animation", "GIF"),
        ("voice", "Voice note"),
        ("audio", "Audio"),
        ("document", "File"),
        ("sticker", "Sticker"),
    ):
        if getattr(msg, attr, None):
            return label
    return None


_TAG = re.compile(r"<(/?)([a-z][a-z0-9-]*)[^>]*>")
_TASK = re.compile(r"^[-*•]\s+\[([ xX]?)\]\s+(.+)$")
_BULLET = re.compile(r"^[-*•]\s+(.+)$")
_ORDERED = re.compile(r"^\d+[.)]\s+(.+)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_LIST_START = re.compile(r"^(?:[-*•]\s+|\d+[.)]\s+)\S")
_HEADING_MARK = re.compile(r"^#{1,6}\s+")


def _balanced(fragment: str) -> bool:
    """True if every tag opened in the fragment is closed inside it."""
    stack = []
    for m in _TAG.finditer(fragment):
        closing, name = m.group(1), m.group(2)
        if closing:
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


def _plain_title(line_html: str) -> str:
    """Plain-text version of a line of Telegram HTML, minus a trailing colon."""
    return html.unescape(_TAG.sub("", line_html)).strip().rstrip(":").strip()


def _title_attr(line_html: str | None) -> str:
    """data-title attribute: Notesnook shows it as a checklist's header title."""
    plain = _plain_title(line_html) if line_html else ""
    return f' data-title="{html.escape(plain[:80], quote=True)}"' if plain else ""


def _structured_html(tg_html: str) -> str | None:
    """Convert markdown-style lines (lists, todos, headings) to real HTML.

    Returns None when the message has no such lines, or when it isn't safe to
    split it into lines (formatting that spans lines, code blocks, quotes);
    the caller then falls back to the simple <br> conversion.
    """
    if "<pre" in tg_html or "<blockquote" in tg_html:
        return None
    lines = tg_html.split("\n")
    if not all(_balanced(line) for line in lines):
        return None

    parsed = []  # (kind, text, extra)
    for line in lines:
        s = line.strip()
        if not s:
            parsed.append(("blank", "", None))
        elif m := _TASK.match(s):
            parsed.append(("task", m.group(2), m.group(1).lower() == "x"))
        elif m := _BULLET.match(s):
            parsed.append(("bullet", m.group(1), None))
        elif m := _ORDERED.match(s):
            parsed.append(("ordered", m.group(1), None))
        elif m := _HEADING.match(s):
            parsed.append(("heading", m.group(2), len(m.group(1))))
        else:
            parsed.append(("text", s, None))

    if not any(k in ("task", "bullet", "ordered", "heading") for k, _, _ in parsed):
        return None

    def next_is_task(idx: int) -> bool:
        return idx < len(parsed) and parsed[idx][0] == "task"

    # "header": Notesnook's task list (title bar and checked/total count).
    # "simple": the plain checklist, no header, so a line above stays as text.
    header_style = get_todo_style() == "header"
    item_class = "checklist--item" if header_style else "simple-checklist--item"
    list_class = "checklist" if header_style else "simple-checklist"

    out = []
    pending_title = None  # line directly above a checklist -> its header title
    i = 0
    while i < len(parsed):
        kind, text, extra = parsed[i]
        if kind == "blank":
            i += 1
        elif kind == "heading":
            if header_style and next_is_task(i + 1) and _plain_title(text):
                pending_title = text
            else:
                out.append(f"<h{extra}>{text}</h{extra}>")
            i += 1
        elif kind in ("task", "bullet", "ordered"):
            items = []
            while i < len(parsed) and parsed[i][0] == kind:
                _, t, checked = parsed[i]
                if kind == "task":
                    # Same classes Notesnook itself writes. Without the item
                    # class, the task list header counts 0 items.
                    cls = f"{item_class} checked" if checked else item_class
                    items.append(f'<li class="{cls}">{t}</li>')
                else:
                    items.append(f"<li>{t}</li>")
                i += 1
            opener = {
                "task": f'<ul class="{list_class}"'
                + (_title_attr(pending_title) if header_style else "")
                + ">",
                "bullet": "<ul>",
                "ordered": "<ol>",
            }[kind]
            closer = "</ol>" if kind == "ordered" else "</ul>"
            out.append(opener + "".join(items) + closer)
            pending_title = None
        else:  # run of plain text lines -> one paragraph
            run = []
            while i < len(parsed) and parsed[i][0] == "text":
                run.append(parsed[i][1])
                i += 1
            if header_style and next_is_task(i) and _plain_title(run[-1]):
                pending_title = run.pop()
            if run:
                out.append("<p>" + "<br>".join(run) + "</p>")
    return "".join(out)


def telegram_html_to_note_html(tg_html: str) -> str:
    """Turn Telegram's HTML (tags already balanced and escaped) into note HTML.

    Markdown-style lists, todos and headings become real HTML lists when it is
    safe to (see _structured_html). Otherwise newlines become <br> outside
    <pre> blocks. We deliberately avoid splitting into multiple <p> tags in
    that case: a bold span can cross a blank line, and Notesnook turns the
    whole note into a code block if any tags are unbalanced.
    """
    tg_html = _TG_ONLY_TAGS.sub("", tg_html.strip())
    structured = _structured_html(tg_html)
    if structured is not None:
        return structured
    parts = _PRE_BLOCK.split(tg_html)
    out = [
        part if part.startswith("<pre>") else part.replace("\n", "<br>")
        for part in parts
    ]
    body = "".join(out)
    if any(tag in body for tag in _BLOCK_TAGS):
        return body  # block elements can't live inside <p>
    return f"<p>{body}</p>"


def generic_title(msg: Message) -> str:
    """e.g. 'Telegram note, Oct 15, 8:15 AM' in the BOT_TIMEZONE zone."""
    dt = msg.date
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    d = dt.astimezone(ZoneInfo(TIMEZONE))
    return f"Telegram note, {d:%b} {d.day}, {d.hour % 12 or 12}:{d.minute:02d} {d:%p}"


async def embed_attachment(msg: Message) -> tuple[str | None, str | None]:
    """Download a photo, image file or text file and return (html, None).

    Mirrors the Notesnook email worker: images become base64 data: URIs, text
    files become code blocks. Anything else, or anything over
    ATTACHMENT_SIZE_LIMIT, returns (None, reason) and gets a placeholder.
    """
    if not EMBED_MEDIA:
        return None, "embedding is turned off"

    name = None
    if msg.photo:
        tg_file, mime, kind = msg.photo[-1], "image/jpeg", "image"
    elif msg.document:
        tg_file = msg.document
        mime = (tg_file.mime_type or "").lower()
        name = tg_file.file_name or "file"
        ext = os.path.splitext(name)[1].lower()
        if mime in _IMAGE_MIMES:
            kind = "image"
        elif mime.startswith("text/") or mime in _TEXT_MIMES or ext in _TEXT_EXTS:
            kind = "text"
        else:
            return None, "only images and text files can be embedded"
    else:
        return None, "only photos, images and text files can be embedded"

    limit_mb = ATTACHMENT_SIZE_LIMIT // (1024 * 1024)
    if tg_file.file_size and tg_file.file_size > ATTACHMENT_SIZE_LIMIT:
        return None, f"too big ({tg_file.file_size / 1048576:.1f} MB, limit {limit_mb} MB)"
    try:
        downloaded = await tg_file.get_file()
        data = bytes(await downloaded.download_as_bytearray())
    except Exception as e:  # Telegram API or network error
        return None, f"download failed ({type(e).__name__})"
    if len(data) > ATTACHMENT_SIZE_LIMIT:
        return None, f"too big (limit {limit_mb} MB)"

    heading = f"<h3>{html.escape(name)}</h3>" if name else ""
    if kind == "image":
        b64 = base64.b64encode(data).decode("ascii")
        return f'{heading}<p><img src="data:{mime};base64,{b64}" /></p>', None
    if b"\x00" in data:
        return None, "file looks binary, not text"
    text = data.decode("utf-8", errors="replace")
    lang = _CODE_LANGUAGES.get(os.path.splitext(name or "")[1].lower())
    code_open = f'<code class="language-{lang}">' if lang else "<code>"
    return f"{heading}<pre>{code_open}{html.escape(text, quote=False)}</code></pre>", None


def build_note(
    msg: Message,
    attachment_html: str | None = None,
    problem: str | None = None,
    text_override: tuple[str, str] | None = None,
) -> tuple[str, str] | None:
    """Return (title, html) for a message, or None if there's nothing to save.

    attachment_html is an embedded photo/file block from embed_attachment; when
    a message has media but none is given, a placeholder line is added instead
    (with `problem` as the reason it wasn't embedded).
    """
    label = media_label(msg)
    plain = (msg.text or msg.caption or "").strip()
    rich = (msg.text_html if msg.text else msg.caption_html) or ""
    if text_override is not None:  # message with its #tag line removed
        plain, rich = text_override

    if not plain and not label:
        return None

    body_parts = []
    if plain:
        body_parts.append(telegram_html_to_note_html(rich))
    if label:
        if attachment_html:
            body_parts.append(attachment_html)
        else:
            why = f", not embedded: {html.escape(problem)}" if problem else ""
            body_parts.append(f"<p><i>[{label} attached in Telegram{why}]</i></p>")

    if plain:
        first_line = plain.splitlines()[0].strip()
        if _LIST_START.match(first_line):
            # Starts with a list item: a generic title avoids duplicating it.
            title = generic_title(msg)
        else:
            first_line = _HEADING_MARK.sub("", first_line) or first_line
            first_line = first_line.rstrip(":").strip() or first_line
            title = (first_line[:80] + "…") if len(first_line) > 80 else first_line
    else:
        file_name = msg.document.file_name if msg.document else None
        title = file_name or f"{label} from Telegram"

    return title, "".join(body_parts)


_gpg_home: str | None = None
_gpg_fpr: str | None = None
_gpg_lock = threading.Lock()


def _gpg(*args: str, stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(
        ["gpg", "--batch", "--yes", "--homedir", _gpg_home, *args],
        input=stdin,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gpg failed: {proc.stderr.decode(errors='replace')[:200]}")
    return proc.stdout


def _ensure_public_key(refresh: bool = False) -> str:
    """Fetch the account's inbox public key from the server and import it."""
    global _gpg_home, _gpg_fpr
    with _gpg_lock:
        if _gpg_fpr and not refresh:
            return _gpg_fpr
        if _gpg_home is None:
            _gpg_home = tempfile.mkdtemp(prefix="gnupg-")
            os.chmod(_gpg_home, 0o700)
        r = requests.get(
            f"{SERVER_URL}/inbox/public-encryption-key",
            headers={"Authorization": INBOX_KEY},
            timeout=20,
        )
        r.raise_for_status()
        key_text = r.json()["key"] if r.headers.get(
            "content-type", ""
        ).startswith("application/json") else r.text
        _gpg("--import", stdin=key_text.encode())
        listing = _gpg("--list-keys", "--with-colons").decode()
        _gpg_fpr = next(
            line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
        )
        return _gpg_fpr


def _encrypt(plaintext: bytes) -> str:
    fpr = _ensure_public_key()
    return _gpg(
        "--trust-model", "always", "--armor", "--cipher-algo", "AES256",
        "-r", fpr, "-e",
        stdin=plaintext,
    ).decode()


TAG_NAME = re.compile(r"^[\w-]{1,40}$")
TAG_ID = re.compile(r"^[A-Za-z0-9]{12,64}$")
TAG_TOKEN = re.compile(r"^#[\w-]{1,40}$")
TAGS_FILE = os.environ.get("TAGS_FILE") or (
    "/data/tags.json" if os.path.isdir("/data") else "tags.json"
)
# True when the tag file sits on a mounted Fly volume, so it survives redeploys.
TAGS_PERSISTENT = os.path.ismount(os.path.dirname(os.path.abspath(TAGS_FILE)))
HISTORY_LIMIT = 500

_tags_lock = threading.Lock()
_tags_state: dict | None = None


def _now_iso() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds")


def _save_tags_locked() -> None:
    """Write the tag file atomically: temp file, then rename over the old one."""
    folder = os.path.dirname(os.path.abspath(TAGS_FILE))
    os.makedirs(folder, exist_ok=True)
    tmp = TAGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_tags_state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, TAGS_FILE)


def _tags_locked() -> dict:
    """Load the tag file on first use. Caller must hold _tags_lock."""
    global _tags_state
    if _tags_state is not None:
        return _tags_state
    state: dict = {}
    existed = os.path.exists(TAGS_FILE)
    if existed:
        try:
            with open(TAGS_FILE, encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict) or not isinstance(state.get("tags", {}), dict):
                raise ValueError("bad tag file")
        except (OSError, ValueError):
            try:  # keep the unreadable file instead of overwriting it
                os.replace(TAGS_FILE, TAGS_FILE + ".corrupt")
            except OSError:
                pass
            state, existed = {}, False
    state.setdefault("tags", {})
    state.setdefault("history", [])
    _tags_state = state
    if not existed:  # first run: optionally seed from the NOTESNOOK_TAGS secret
        try:
            seed = json.loads(os.environ.get("NOTESNOOK_TAGS", "") or "{}")
        except ValueError:
            seed = {}
        for name, tag_id in (seed.items() if isinstance(seed, dict) else []):
            name = str(name).lower().lstrip("#")
            if TAG_NAME.match(name) and TAG_ID.match(str(tag_id)):
                state["tags"][name] = str(tag_id)
                state["history"].append({"at": _now_iso(), "action": "seed", "name": name})
        try:
            _save_tags_locked()
        except OSError:
            pass
    return state


def get_tags() -> dict:
    with _tags_lock:
        return dict(_tags_locked()["tags"])


def tags_snapshot() -> dict:
    with _tags_lock:
        state = _tags_locked()
        return {"tags": dict(state["tags"]), "history": list(state["history"])}


def _log(state: dict, action: str, name: str) -> None:
    state["history"].append({"at": _now_iso(), "action": action, "name": name})
    del state["history"][:-HISTORY_LIMIT]


def set_tag(name: str, tag_id: str) -> str:
    with _tags_lock:
        state = _tags_locked()
        action = "updated" if name in state["tags"] else "added"
        state["tags"][name] = tag_id
        _log(state, action, name)
        _save_tags_locked()
        return action


def remove_tag(name: str) -> bool:
    with _tags_lock:
        state = _tags_locked()
        if name not in state["tags"]:
            return False
        del state["tags"][name]
        _log(state, "removed", name)
        _save_tags_locked()
        return True


TODO_STYLES = ("header", "simple")


def get_todo_style() -> str:
    """How todos are saved: "header" (task list with title and count, the default)
    or "simple" (plain checklist). Stored in the same file as the tags."""
    with _tags_lock:
        style = _tags_locked().get("todo_style")
    return style if style in TODO_STYLES else "header"


def set_todo_style(style: str) -> None:
    with _tags_lock:
        state = _tags_locked()
        state["todo_style"] = style
        _log(state, "todo style", style)
        _save_tags_locked()


def replace_tags(tags: dict) -> int:
    with _tags_lock:
        state = _tags_locked()
        state["tags"] = dict(tags)
        _log(state, "restored", f"{len(tags)} tags")
        _save_tags_locked()
        return len(tags)


def extract_tag_line(msg: Message) -> tuple[list[str], tuple[str, str] | None]:
    """Find a #tag line at the end of a message.

    Returns (tag names, text_override). text_override is (plain, rich) with the
    tag line removed, or None when the line should stay in the note (it is
    only removed when every tag on it is known and the line can be cut cleanly).
    Needs at least one other line of text, or media, so a message that is just
    "#groceries" is saved as a normal note.
    """
    plain = msg.text or msg.caption or ""
    lines = plain.rstrip().split("\n")
    if len(lines) < 2 and not media_label(msg):
        return [], None
    tokens = lines[-1].split()
    if not tokens or not all(TAG_TOKEN.match(t) for t in tokens):
        return [], None
    rest_plain = "\n".join(lines[:-1]).strip()
    if not rest_plain and not media_label(msg):
        return [], None
    names = list(dict.fromkeys(t[1:].lower() for t in tokens))

    if not all(n in get_tags() for n in names):
        return names, None
    rich = ((msg.text_html if msg.text else msg.caption_html) or "").rstrip()
    rich_lines = rich.split("\n")
    last_rich = html.unescape(re.sub(r"<[^>]+>", "", rich_lines[-1])).strip()
    if len(rich_lines) != len(lines) or last_rich != lines[-1].strip():
        return names, None
    return names, (rest_plain, "\n".join(rich_lines[:-1]).strip())


def send_to_notesnook(
    title: str, content_html: str, tag_ids: list[str] | None = None
) -> requests.Response:
    payload = {
        "title": title,
        "type": "note",
        "source": "telegram",
        "version": 1,
        "content": {"type": "html", "data": content_html},
    }
    if tag_ids:
        payload["tagIds"] = tag_ids
    if not SERVER_URL:  # hosted Notesnook inbox: the proxy encrypts for us
        return requests.post(
            HOSTED_INBOX_URL,
            json=payload,
            headers={"Authorization": INBOX_KEY},
            timeout=20,
        )

    plaintext = json.dumps(payload).encode("utf-8")
    try:
        cipher = _encrypt(plaintext)
    except Exception:
        _ensure_public_key(refresh=True)  # key may have been rotated; retry once
        cipher = _encrypt(plaintext)
    envelope = {"v": 1, "alg": "pgp-aes256", "cipher": cipher}
    return requests.post(
        f"{SERVER_URL}/inbox/items",
        json=envelope,
        headers={"Authorization": INBOX_KEY},
        timeout=20,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None or update.effective_user.id != ALLOWED_USER_ID:
        return  # ignore everyone else

    if await handle_pending(update, context):
        return  # this message answered a /tag or /untag prompt

    attachment_html, problem = None, None
    if media_label(msg):
        attachment_html, problem = await embed_attachment(msg)

    tag_names, text_override = extract_tag_line(msg)
    known = get_tags()
    applied = [n for n in tag_names if n in known]
    unknown = [n for n in tag_names if n not in known]

    note = build_note(msg, attachment_html, problem, text_override)
    if note is None:
        await msg.reply_text("Nothing to save in that message.")
        return
    title, content_html = note

    if attachment_html and len(content_html.encode("utf-8")) > NOTE_SIZE_LIMIT:
        attachment_html, problem = None, "the note would be too large"
        title, content_html = build_note(msg, None, problem, text_override)

    tag_ids = [known[n] for n in applied]
    try:
        resp = await asyncio.to_thread(send_to_notesnook, title, content_html, tag_ids)
    except requests.RequestException as e:
        await msg.reply_text(f"Network error: {e}")
        return

    if resp.status_code == 200:
        suffix = ""
        if media_label(msg):
            suffix = " (media embedded)" if attachment_html else f" (media not embedded: {problem})"
        if applied:
            suffix += f" (tags: {', '.join(applied)})"
        text = f"✓ Saved to Notesnook{suffix}"
        if unknown:
            names = ", ".join("#" + n for n in unknown)
            text += f"\nTag not found: {names}. Add it with /tag name id"
        await msg.reply_text(text)
    else:
        await msg.reply_text(f"Failed ({resp.status_code}): {resp.text[:300]}")


HELP_TEXT = """Telegram -> Notesnook bridge

Send this bot a message to save it as a note in Notesnook. Notes appear in Notesnook after its next sync.

COMMANDS
/help   show this guide
/setup  how to connect the bot to Notesnook (default or self-hosted)
/ver    show the bot version and where notes are going
/html   save raw HTML as a note (see HTML below)
/callout  save a callout box (see CALLOUTS below)
/todostyle  switch todos between a header task list and a simple checklist
/tag    save a tag: /tag name id (or just /tag, and the bot asks)
/untag  forget a tag: /untag name (or just /untag)
/cancel stop a /tag, /untag or /callout prompt
/tags   list saved tags
/tagsfile  send the saved tags as a backup file

LISTS, TODOS AND HEADINGS
Start a line with one of these:
- item (or * item, • item)  ->  bullet list
1. item (or 1) item)  ->  numbered list
- [ ] item  ->  todo
- [x] item  ->  completed todo
# Heading (up to ###### Heading)  ->  heading

Example:
Groceries:
- [ ] milk
- [x] eggs

A line directly above a todo list, with no blank line between, becomes the title of that checklist. In the example, "Groceries" is the checklist title.

TODO STYLE
Todos are saved in one of two styles. Send /todostyle to switch between them (or /todostyle header, /todostyle simple). The choice is remembered.
• header (default): Notesnook's task list, with a title bar, a progress bar and a checked/total count.
• simple: a plain checklist with no header or count. The line above the todos stays in the note as normal text.

NOTE TITLES
The first line of a message is the note title. A message that starts with a list item gets a title with the date and time instead, like "Telegram note, Oct 15, 8:15 AM".

FORMATTING
Select text and use Telegram's formatting options to apply bold, italic, underline, strikethrough, links, monospace (code) and quotes. All of these carry over to the note. Spoiler text is saved as plain text.

HTML
In a normal message, tags typed by hand, such as <b>, are not interpreted and appear in the note as plain text. To save real HTML, start the message with /html:
/html <h1>Groceries</h1><ul class="checklist"><li class="checklist--item">milk</li><li class="checklist--item checked">eggs</li></ul>
The HTML is sent exactly as typed. The note title is the first heading (<h1> to <h6>), or the date and time if there is none. Every tag must be closed, or the bot refuses to save it (Notesnook would turn the note into a code block). A #tag on the last line works as usual. Checklist items need class="checklist--item", plus "checked" when done.

CALLOUTS
Send /callout on its own and the bot asks for the type, the title and the text, one message each (send skip to leave one out, /cancel to stop). Or do it in one message: start with /callout, then an optional type and a title, then the body on the next lines:
/callout warning Back up first
Export your notes before upgrading.
Types: note, abstract, summary, tldr, info, todo, tip, hint, important, success, check, done, question, help, faq, warning, warn, caution, attention, failure, fail, missing, danger, error, bug, example, quote, cite. With no type, or an unknown one, the callout is info. With no title, the type name is used. The body can use lists, todos and Telegram formatting. The whole message becomes the callout, and the note title is the callout title. A #tag on the last line works as usual.

TAGS
Put tags on the last line of a message, after your text:
Groceries:
- [ ] milk

#groceries #home

The tag line is removed from the note and the matching Notesnook tags are applied. Tags only count on the last line, and the message needs other text above it.

Notesnook can't list your tags, so save each one once. In Notesnook, right-click a tag and choose Copy ID, then send:
/tag groceries 67aecf3b9e1398484554bc90

A tag the bot doesn't know stays in the note as text, and the reply says it was not found. After every /tag and /untag the bot sends you the tag file. To restore it, send that file back with the caption /restore.

PHOTOS AND FILES
• Photos and image files (JPEG, PNG, GIF, WebP) are embedded in the note.
• Text files (.txt, .md, .csv, .json, code and similar) are added as code blocks. Common types (.py, .js, .ts, .json, .yaml, .sh, .sql, .md, .css, .toml, .ini, .csv, .html, .xml) get syntax highlighting.
• Files can be up to 5 MB each.
• Videos, voice notes and other file types are not embedded. The note shows a placeholder line, plus the caption if there is one.
• Embedded images are part of the note itself, not Notesnook attachments. To save a real attachment, share the file to Notesnook from your phone's share sheet.
• Each photo in an album becomes its own note.

LIMITS
• Lists can't be nested, and numbering always starts at 1.
• A message that contains a code block, a quote, or bold text spanning several lines is saved as simple text, without list conversion."""


SETUP_TEXT = """Setup: connecting the bot to Notesnook (v1.4.1)

The bot can send notes to ONE of two places. Which one is decided by a single setting, NOTESNOOK_SERVER_URL.

OPTION A: DEFAULT NOTESNOOK (hosted)
Use this if you sync with Notesnook's own servers.
1. In Notesnook: Settings > Account > Inbox, turn it on and create an Inbox API key.
2. Set the key on the bot:
   fly secrets set NOTESNOOK_INBOX_KEY=<your key>
3. Leave NOTESNOOK_SERVER_URL unset. If it was set before, remove it:
   fly secrets unset NOTESNOOK_SERVER_URL
4. /ver should say: Destination: hosted Notesnook inbox
Notesnook's server encrypts your notes for you.

OPTION B: SELF-HOSTED NOTESNOOK
Use this if you run your own sync server.
1. Log in to the Notesnook app that is connected to YOUR server (not the default one). The key must come from this account.
2. Open Settings > Account > Inbox, turn it on, and create an Inbox API key. Copy it so you can paste it in the next step.
3. Set both settings on the bot. Use your own server address, with no slash at the end:
   fly secrets set NOTESNOOK_SERVER_URL=https://notesnook.example.com
   fly secrets set NOTESNOOK_INBOX_KEY=<your self-hosted key>
4. Deploy the new code: fly deploy
5. /ver should show your server address under Destination.
6. Send a test message, then sync Notesnook.

IMPORTANT FOR SELF-HOSTED
- A key from the default Notesnook account will NOT work on your server (the bot reports 401). Create a new key on the self-hosted account.
- The bot encrypts every note with your account's public key before sending. It fetches that key from your server on its own using the Inbox key. You do not paste any other key.
- The server address is the main sync address (the notesnook. host), not the auth, events, monograph or attachments addresses.
- If you reset your self-hosted account's encryption keys, create a new Inbox key and set it again. The bot re-fetches the public key automatically.

TROUBLESHOOTING
401  wrong or missing key, or key from the other account
404  wrong server address
Network error  server is unreachable
Saved but no note  sync Notesnook; check you are logged in to the matching account"""


def _authorized(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == ALLOWED_USER_ID


def _chunks(text: str, limit: int = 3800) -> list[str]:
    """Split text at blank lines into pieces under Telegram's 4096-character cap."""
    parts, current = [], ""
    for block in text.split("\n\n"):
        if current and len(current) + len(block) + 2 > limit:
            parts.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        parts.append(current)
    return parts


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _authorized(update):
        for part in _chunks(HELP_TEXT):
            await update.effective_message.reply_text(part)


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _authorized(update):
        for part in _chunks(SETUP_TEXT):
            await update.effective_message.reply_text(part)


async def send_backup(msg: Message, reason: str) -> None:
    """Send the tag file to the chat, so Telegram holds an off-Fly copy."""
    try:
        data = json.dumps(tags_snapshot(), indent=2, ensure_ascii=False).encode("utf-8")
        stamp = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d_%H%M")
        await msg.reply_document(
            document=io.BytesIO(data),
            filename=f"notesnook-tags-{stamp}.json",
            caption=f"Tag backup ({reason}). To restore, send this file back with the caption /restore",
        )
    except Exception as e:  # never let a failed backup break the command
        await msg.reply_text(f"Could not send the tag backup ({type(e).__name__}).")


PENDING = "pending"  # key in context.user_data: a command waiting for your next message
PENDING_SECONDS = 300  # a forgotten prompt stops swallowing messages after 5 minutes
ASK_NAME = "Which tag name? Use letters, numbers, - and _. Send /cancel to stop."
ASK_ID = (
    'Now send the tag ID for "{name}". In Notesnook, right-click the tag and '
    "choose Copy ID. Send /cancel to stop."
)


def _wait_for(context: ContextTypes.DEFAULT_TYPE, cmd: str, **extra) -> None:
    context.user_data[PENDING] = {"cmd": cmd, "at": time.time(), **extra}


async def save_tag(msg: Message, name_raw: str, id_raw: str) -> bool:
    """Validate and save a tag, then send the backup. False if input was invalid."""
    name, tag_id = name_raw.lstrip("#").lower(), id_raw.strip()
    if not TAG_NAME.match(name):
        await msg.reply_text("Tag names can use letters, numbers, - and _ (up to 40).")
        return False
    if not TAG_ID.match(tag_id):
        await msg.reply_text("That doesn't look like a tag ID. Use Copy ID in Notesnook.")
        return False
    result = set_tag(name, tag_id)
    await msg.reply_text(f'Tag "{name}" {result}.')
    await send_backup(msg, f"after /tag {name}")
    return True


async def forget_tag(msg: Message, name_raw: str) -> bool:
    name = name_raw.lstrip("#").lower()
    if remove_tag(name):
        await msg.reply_text(f'Tag "{name}" forgotten. It is not deleted in Notesnook.')
        await send_backup(msg, f"after /untag {name}")
        return True
    await msg.reply_text(f'No saved tag named "{name}". Send /tags to see them.')
    return False


async def handle_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Use the message as the answer to a /tag, /untag or /callout prompt. True if it was."""
    pend = context.user_data.get(PENDING)
    if not pend:
        return False
    msg = update.effective_message
    text = (msg.text or "").strip()
    if time.time() - pend["at"] > PENDING_SECONDS or not text:
        context.user_data.pop(PENDING, None)  # expired, or not text: save it as a note
        return False
    if pend["cmd"] == "tag_name":
        name = text.lstrip("#").lower()
        if not TAG_NAME.match(name):
            await msg.reply_text("Tag names can use letters, numbers, - and _ (up to 40). Try again, or /cancel.")
        else:
            _wait_for(context, "tag_id", name=name)
            await msg.reply_text(ASK_ID.format(name=name))
    elif pend["cmd"] == "tag_id":
        if await save_tag(msg, pend["name"], text):
            context.user_data.pop(PENDING, None)
        else:
            await msg.reply_text("Try again, or /cancel.")
    elif pend["cmd"] == "untag":
        context.user_data.pop(PENDING, None)
        await forget_tag(msg, text)
    elif pend["cmd"] == "callout_type":
        word = text.lower()
        if word in ("skip", "-"):
            word = "info"
        if word not in _CALLOUT_TYPES:
            await msg.reply_text(
                "That is not a callout type. Try note, info, tip, warning or danger "
                "(full list in /help), send skip for info, or /cancel."
            )
        else:
            _wait_for(context, "callout_title", ctype=word)
            await msg.reply_text(ASK_CALLOUT_TITLE)
    elif pend["cmd"] == "callout_title":
        title = "" if text.lower() == "skip" else " ".join((msg.text_html or "").split())
        _wait_for(context, "callout_body", ctype=pend["ctype"], title=title)
        await msg.reply_text(ASK_CALLOUT_BODY)
    elif pend["cmd"] == "callout_body":
        context.user_data.pop(PENDING, None)
        if text.lower() == "skip":
            tag_names, body = [], ""
        else:
            tag_names, body = _callout_tags(msg.text_html or "")
        note_title, content_html = _callout_html(pend["ctype"], pend["title"], body)
        await _save_callout(msg, note_title, content_html, tag_names)
    return True


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    had = context.user_data.pop(PENDING, None)
    await update.effective_message.reply_text("Cancelled." if had else "Nothing to cancel.")


async def cmd_tag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tag name id saves a tag. With less, the bot asks for the rest."""
    if not _authorized(update):
        return
    msg = update.effective_message
    args = context.args or []
    context.user_data.pop(PENDING, None)
    if len(args) == 2:
        await save_tag(msg, args[0], args[1])
    elif len(args) == 1:
        name = args[0].lstrip("#").lower()
        if not TAG_NAME.match(name):
            await msg.reply_text("Tag names can use letters, numbers, - and _ (up to 40).")
            return
        _wait_for(context, "tag_id", name=name)
        await msg.reply_text(ASK_ID.format(name=name))
    elif not args:
        _wait_for(context, "tag_name")
        await msg.reply_text(ASK_NAME)
    else:
        await msg.reply_text("Usage: /tag name id")


async def cmd_untag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/untag name forgets a tag. With no name, the bot lists them and asks."""
    if not _authorized(update):
        return
    msg = update.effective_message
    args = context.args or []
    context.user_data.pop(PENDING, None)
    if len(args) == 1:
        await forget_tag(msg, args[0])
    elif not args:
        tags = get_tags()
        if not tags:
            await msg.reply_text("No tags saved yet.")
            return
        _wait_for(context, "untag")
        await msg.reply_text(
            "Which tag should be forgotten? Send its name, or /cancel.\n\n"
            + "\n".join(sorted(tags))
        )
    else:
        await msg.reply_text("Usage: /untag name")


async def cmd_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    tags = get_tags()
    if tags:
        lines = [f"{n}  {i[:8]}…{i[-4:]}" for n, i in sorted(tags.items())]
        text = "Saved tags:\n" + "\n".join(lines)
    else:
        text = "No tags saved yet. Add one with /tag name id."
    if not TAGS_PERSISTENT:
        text += (
            "\n\nWarning: no Fly volume is mounted, so tags are lost when the bot "
            "redeploys. Restore from your last backup file with /restore."
        )
    await update.effective_message.reply_text(text)


async def cmd_tagsfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _authorized(update):
        await send_backup(update.effective_message, "on request")


async def cmd_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A tag backup file sent with the caption /restore replaces the saved tags."""
    if not _authorized(update):
        return
    msg = update.effective_message
    doc = msg.document
    if doc is None or (doc.file_size or 0) > 1_000_000:
        await msg.reply_text("Send a tag backup file (under 1 MB) with the caption /restore.")
        return
    try:
        tg_file = await doc.get_file()
        data = json.loads(bytes(await tg_file.download_as_bytearray()).decode("utf-8"))
        raw = data["tags"]
        if not isinstance(raw, dict):
            raise ValueError("tags must be an object")
        clean = {
            str(n).lower(): str(i)
            for n, i in raw.items()
            if TAG_NAME.match(str(n).lower()) and TAG_ID.match(str(i))
        }
        if raw and not clean:
            raise ValueError("no valid tags")
    except Exception:
        await msg.reply_text("Could not read that file. Send a tag backup file with the caption /restore.")
        return
    count = replace_tags(clean)
    await msg.reply_text(f"Restored {count} tags.")


async def cmd_ver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    lines = [f"Telegram -> Notesnook bridge v{__version__}"]
    if region := os.environ.get("FLY_REGION"):
        lines.append(f"Running on Fly.io (region: {region})")
    lines.append(f"Destination: {SERVER_URL or 'hosted Notesnook inbox'}")
    lines.append(
        f"Saved tags: {len(get_tags())} "
        f"({'on a Fly volume' if TAGS_PERSISTENT else 'NOT persistent: no volume mounted'})"
    )
    lines.append(f"Time zone for generic titles: {TIMEZONE}")
    lines.append(f"Media embedding: {'on' if EMBED_MEDIA else 'off'}")
    lines.append(f"Todo style: {get_todo_style()} (change with /todostyle)")
    await update.effective_message.reply_text("\n".join(lines))


_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}
_HTML_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*?(/?)>")
_HTML_HEADING = re.compile(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", re.IGNORECASE | re.DOTALL)
_HTML_COMMAND = re.compile(r"^/html(?:@\w+)?[ \t]*\n?", re.IGNORECASE)


def _html_balanced(raw: str) -> bool:
    """True if every non-void tag in raw HTML is closed in the right order.

    Notesnook saves a note as a code block when tags are unbalanced, so the bot
    checks first and refuses instead of saving a mangled note.
    """
    stripped = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
    stack = []
    for m in _HTML_TAG.finditer(stripped):
        closing, name, self_closing = m.group(1), m.group(2).lower(), m.group(3)
        if name in _VOID_TAGS or self_closing:
            continue
        if closing:
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


def _html_title(msg: Message, raw: str) -> str:
    """Title for a /html note: the first heading's text, else a generic title."""
    if m := _HTML_HEADING.search(raw):
        plain = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip().rstrip(":").strip()
        if plain:
            return (plain[:80] + "…") if len(plain) > 80 else plain
    return generic_title(msg)


HTML_EXAMPLE = """Send /html followed by your HTML. Example (a task list with three items and a tag):

/html <h1>Groceries</h1>
<ul class="checklist" data-title="Groceries">
  <li class="checklist--item">Milk</li>
  <li class="checklist--item checked">Eggs</li>
  <li class="checklist--item">Bread</li>
</ul>
#todo

The note title is the first heading. Each item needs class="checklist--item", plus "checked" when done. data-title sets the checklist's header title. A saved tag on the last line is applied and removed from the note (save one first with /tag name id). Every tag must be closed."""


async def cmd_html(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/html <raw HTML>: save the HTML exactly as typed (no escaping)."""
    msg = update.effective_message
    if msg is None or not _authorized(update):
        return
    raw = _HTML_COMMAND.sub("", msg.text or "", count=1).strip()
    if not raw:
        await msg.reply_text(HTML_EXAMPLE)
        return

    # Optional #tag line at the end, same rule as normal notes.
    lines = raw.split("\n")
    tag_names: list[str] = []
    if len(lines) >= 2:
        tokens = lines[-1].split()
        if tokens and all(TAG_TOKEN.match(t) for t in tokens):
            tag_names = list(dict.fromkeys(t[1:].lower() for t in tokens))
    known = get_tags()
    applied = [n for n in tag_names if n in known]
    unknown = [n for n in tag_names if n not in known]
    if tag_names and not unknown:
        raw = "\n".join(lines[:-1]).strip()  # remove the tag line

    if not _html_balanced(raw):
        await msg.reply_text(
            "Not saved: the HTML has an unclosed or mismatched tag. "
            "Notesnook would turn the whole note into a code block. "
            "Fix the tags and send it again."
        )
        return

    title = _html_title(msg, raw)
    try:
        resp = await asyncio.to_thread(
            send_to_notesnook, title, raw, [known[n] for n in applied]
        )
    except requests.RequestException as e:
        await msg.reply_text(f"Network error: {e}")
        return

    if resp.status_code == 200:
        text = "✓ Saved to Notesnook (raw HTML)"
        if applied:
            text += f" (tags: {', '.join(applied)})"
        if unknown:
            names = ", ".join("#" + n for n in unknown)
            text += f"\nTag not found: {names}. Add it with /tag name id"
        await msg.reply_text(text)
    else:
        await msg.reply_text(f"Failed ({resp.status_code}): {resp.text[:300]}")


# Callout types Notesnook knows (from its editor source). Unknown types fall back to info.
_CALLOUT_TYPES = {
    "note", "abstract", "summary", "tldr", "info", "todo", "tip", "hint",
    "important", "success", "check", "done", "question", "help", "faq",
    "warning", "warn", "caution", "attention", "failure", "fail", "missing",
    "danger", "error", "bug", "example", "quote", "cite",
}
_CALLOUT_COMMAND = re.compile(r"^/callout(?:@\w+)?[ \t]*\n?", re.IGNORECASE)
_CALLOUT_FIRST = re.compile(r"^([A-Za-z]+)(?:[ \t]+(.*))?$")


def build_callout(rich: str) -> tuple[str, str] | None:
    """Turn the text after /callout into (note title, callout HTML).

    First line: optional type, then the title. Remaining lines: the body, which
    can use lists, todos and Telegram formatting like a normal message.
    Returns None when there is nothing after the command.
    """
    rest = _CALLOUT_COMMAND.sub("", rich.strip(), count=1).strip()
    if not rest:
        return None
    first, _, body = rest.partition("\n")
    first, body = first.strip(), body.strip()
    if not (_balanced(first) and _balanced(body)):
        # Formatting runs across the title/body split; keep the text, drop the
        # tags so the HTML stays balanced (Notesnook rejects unbalanced HTML).
        first, body = _TAG.sub("", first), _TAG.sub("", body)

    ctype, title = "info", first
    if (m := _CALLOUT_FIRST.match(first)) and m.group(1).lower() in _CALLOUT_TYPES:
        ctype, title = m.group(1).lower(), (m.group(2) or "").strip()
    return _callout_html(ctype, title, body)


def _callout_html(ctype: str, title: str, body: str) -> tuple[str, str]:
    """(note title, callout HTML) from a type, a title and a body (Telegram HTML)."""
    if not title:
        title = ctype.upper()  # Notesnook's default title is the type name
    body_html = telegram_html_to_note_html(_TG_ONLY_TAGS.sub("", body)) if body else "<p></p>"
    callout = (
        f'<div class="callout" data-callout-type="{ctype}">'
        f"<h4>{title}</h4>{body_html}</div>"
    )
    plain_title = _plain_title(title) or ctype.upper()
    note_title = (plain_title[:80] + "…") if len(plain_title) > 80 else plain_title
    return note_title, callout


def _callout_tags(rich: str) -> tuple[list[str], str]:
    """Find tags at the end of a callout message (Telegram HTML in, HTML out).

    Accepts a last line made only of #tags (removed when they are all saved
    tags), or saved #tags at the end of the last line, so
    "/callout tip Title #home" works too. Returns (tag names, text without them).
    """
    lines = rich.rstrip().split("\n")
    known = get_tags()
    last = lines[-1]
    tokens = html.unescape(_TAG.sub("", last)).split()
    if tokens and "<" not in last and all(TAG_TOKEN.match(t) for t in tokens):
        names = list(dict.fromkeys(t[1:].lower() for t in tokens))
        if all(n in known for n in names):
            return names, "\n".join(lines[:-1]).strip()
        return names, rich  # an unknown tag: keep the text, the reply says so
    found: list[str] = []
    text = last
    while (m := re.search(r"(?:^|\s)#([\w-]{1,40})\s*$", text)) and m.group(1).lower() in known:
        found.insert(0, m.group(1).lower())
        text = text[: m.start()].rstrip()
    if found:
        return found, "\n".join(lines[:-1] + [text]).strip()
    return [], rich


ASK_CALLOUT_TYPE = (
    "Which callout type? For example: note, info, tip, warning, danger, success, "
    "question, example (the full list is in /help). Send skip for info, or /cancel to stop."
)
ASK_CALLOUT_TITLE = "What is the callout's title? Send skip to use the type name, or /cancel to stop."
ASK_CALLOUT_BODY = (
    "What is the callout's text? Lists, todos and Telegram formatting work. "
    "A #tag on the last line is applied. Send skip for no text, or /cancel to stop."
)


async def _save_callout(
    msg: Message, title: str, content_html: str, tag_names: list[str]
) -> None:
    """Send a callout note to Notesnook and report the result."""
    known = get_tags()
    applied = [n for n in tag_names if n in known]
    unknown = [n for n in tag_names if n not in known]
    try:
        resp = await asyncio.to_thread(
            send_to_notesnook, title, content_html, [known[n] for n in applied]
        )
    except requests.RequestException as e:
        await msg.reply_text(f"Network error: {e}")
        return

    if resp.status_code == 200:
        text = "✓ Saved to Notesnook (callout)"
        if applied:
            text += f" (tags: {', '.join(applied)})"
        if unknown:
            names = ", ".join("#" + n for n in unknown)
            text += f"\nTag not found: {names}. Add it with /tag name id"
        await msg.reply_text(text)
    else:
        await msg.reply_text(f"Failed ({resp.status_code}): {resp.text[:300]}")


async def cmd_callout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/callout [type] [title] then body lines. With nothing after it, the bot asks."""
    msg = update.effective_message
    if msg is None or not _authorized(update):
        return
    context.user_data.pop(PENDING, None)

    tag_names, rich = _callout_tags(msg.text_html or "")

    built = build_callout(rich)
    if built is None:
        _wait_for(context, "callout_type")
        await msg.reply_text(ASK_CALLOUT_TYPE)
        return
    title, content_html = built
    await _save_callout(msg, title, content_html, tag_names)


async def cmd_todostyle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/todostyle switches between the header task list and the simple checklist."""
    if not _authorized(update):
        return
    msg = update.effective_message
    args = [a.lower() for a in (context.args or [])]
    current = get_todo_style()
    if args and args[0] in TODO_STYLES:
        new = args[0]
    elif not args:
        new = "simple" if current == "header" else "header"  # no argument: toggle
    else:
        await msg.reply_text("Usage: /todostyle, /todostyle header or /todostyle simple")
        return
    if new != current:
        try:
            set_todo_style(new)
        except OSError as e:
            await msg.reply_text(f"Could not save the setting ({type(e).__name__}).")
            return
    desc = (
        "header: a task list with a title and a checked/total count"
        if new == "header"
        else "simple: a plain checklist, with no header or count"
    )
    note = "" if new != current else " (no change)"
    if not TAGS_PERSISTENT:
        note += "\nNo Fly volume is mounted, so this resets when the bot redeploys."
    await msg.reply_text(f"Todo style is now {desc}.{note}\nSend /todostyle again to switch.")


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _authorized(update):
        await update.effective_message.reply_text("Unknown command. Send /help.")


async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("help", "How to use this bot"),
            BotCommand("setup", "Connect to default or self-hosted Notesnook"),
            BotCommand("html", "Save raw HTML as a note"),
            BotCommand("callout", "Save a callout (type, title, body)"),
            BotCommand("todostyle", "Switch todos: header or simple checklist"),
            BotCommand("tag", "Save a tag (the bot asks for details)"),
            BotCommand("untag", "Forget a saved tag"),
            BotCommand("tags", "List saved tags"),
            BotCommand("tagsfile", "Send the tag backup file"),
            BotCommand("ver", "Show the bot version"),
        ]
    )


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)  # file downloads can be slow
        .write_timeout(60)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("tag", cmd_tag))
    app.add_handler(CommandHandler("untag", cmd_untag))
    app.add_handler(CommandHandler("tags", cmd_tags))
    app.add_handler(CommandHandler("tagsfile", cmd_tagsfile))
    app.add_handler(CommandHandler("ver", cmd_ver))
    app.add_handler(CommandHandler("html", cmd_html))
    app.add_handler(CommandHandler("callout", cmd_callout))
    app.add_handler(CommandHandler("todostyle", cmd_todostyle))
    app.add_handler(
        MessageHandler(filters.Document.ALL & filters.CaptionRegex(r"^/restore\b"), cmd_restore)
    )
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.add_handler(MessageHandler(~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
