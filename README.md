# Telegram to Notesnook bridge (v1.1.0)

A small private Telegram bot that saves every message you send it as a note in
Notesnook, using the Notesnook Inbox API. It runs on Fly.io.

## Quick start

1. **Telegram:** message @BotFather, send `/newbot`, and copy the bot token.
   Message @userinfobot and copy your numeric user ID.
2. **Notesnook:** open Settings > Account > Inbox (or Settings > Inbox), turn on
   Enable Inbox API, then create an API key. Pick a long expiry, because the
   bot stops working with a 401 error when the key expires.
3. **Fly.io:** install the Fly CLI, then run these from this folder:

   ```
   fly auth login
   fly apps create your-unique-app-name
   fly secrets set TELEGRAM_BOT_TOKEN="..." NOTESNOOK_INBOX_KEY="..." ALLOWED_USER_ID="..."
   fly deploy
   fly scale count 1
   ```

   Edit `app = "..."` in `fly.toml` first so it matches the name you created.
4. **Test:** message your bot. It replies "Saved to Notesnook", and the note
   appears after Notesnook syncs. Send `/help` for a usage guide.

## Optional settings

Set these the same way as the secrets above (`fly secrets set NAME="value"`).

| Setting | Default | Purpose |
| --- | --- | --- |
| `BOT_TIMEZONE` | `America/New_York` | Time zone used in generic note titles |
| `EMBED_MEDIA` | on | Set to `0` to stop embedding photos and files and save placeholders only |

## Commands

| Command | What it does |
| --- | --- |
| `/help` | Shows how to use the bot, including the markdown shortcuts |
| `/ver` | Shows the bot version |

## Files

| File | Purpose |
| --- | --- |
| `bot.py` | The bot |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | How Fly builds the bot |
| `fly.toml` | Fly app settings (change the app name) |
| `.dockerignore` | Keeps Fly from uploading anything else in the folder |

`.dockerignore` starts with a dot, so it may be hidden. On a Mac, press
Cmd+Shift+. in Finder to show hidden files.

## Writing notes

- Telegram formatting (bold, italic, links, code, quotes) is kept.
- `- item`, `* item` or `• item` makes a bullet list; `1. item` a numbered list.
- `- [ ] item` and `- [x] item` make todos. A line directly above the todos
  becomes the checklist's header title.
- `# Heading` makes a heading.
- A message that starts with a list item gets a generic title like
  "Telegram note, Oct 15, 8:15 AM"; otherwise the first line is the title.

## Photos and files

The Inbox API has no attachment field, so media is embedded in the note's HTML
the way the Notesnook email worker does it:

- Photos and image files (JPEG, PNG, GIF, WebP) are embedded as base64
  `data:` images.
- Text files (.txt, .md, .csv, .json, code and similar) become code blocks.
- Each file can be up to 5 MB, and a whole note up to 8 MB.
- Videos, voice notes and other file types get a placeholder line plus the
  caption.
- Embedded images are part of the note, not Notesnook attachments. Each photo
  in an album becomes its own note.

Notesnook's documentation says it strips `data:` addresses from images, so
embedded images may not appear. Test with a small photo. If it arrives without
the image, run `fly secrets set EMBED_MEDIA=0` to switch embedding off.

## Limits

- Lists can't be nested, and numbering always starts at 1.
- A message that contains a code block, a quote, or bold text spanning several
  lines is saved as simple text, without list conversion.

## Security

Never commit or share your bot token or Inbox key. If either leaks, send
`/revoke` to @BotFather and delete the key in Notesnook's Inbox settings.
