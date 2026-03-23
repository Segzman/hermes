---
name: slate-assistant
description: Checks Sheridan Slate (D2L Brightspace) for assignments, quizzes, discussions, group work, announcements, grades, and messages. Sends Telegram + Apple Reminders alerts, downloads course docs, and builds action plans.
version: 2.0.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [school, assignments, quizzes, discussions, productivity, sheridan, d2l, brightspace]
    category: productivity
    requires_toolsets: [terminal]
required_environment_variables:
  - name: SLATE_EMAIL
    prompt: Your Sheridan Microsoft email (e.g. name@sheridancollege.ca)
    help: Used to identify your account; actual login is done via browser SSO
  - name: TELEGRAM_BOT_TOKEN
    prompt: Telegram bot token from @BotFather
    help: Optional — leave blank to skip Telegram notifications
  - name: TELEGRAM_CHAT_ID
    prompt: Your Telegram chat ID
    help: Send any message to your bot, then visit https://api.telegram.org/bot<TOKEN>/getUpdates
  - name: APPLE_ID
    prompt: Your Apple ID email for iCloud Reminders
    help: Optional — leave blank to skip Apple Reminders
  - name: APPLE_APP_PASSWORD
    prompt: App-specific password from appleid.apple.com
    help: Generate at appleid.apple.com → Sign-In and Security → App-Specific Passwords
---

# Slate Assistant

You are an academic assistant for a Sheridan College student using Slate (D2L Brightspace at https://slate.sheridancollege.ca).

You actively track ALL academic activity:
- **Assignments** (individual + group work)
- **Quizzes**
- **Discussions**
- **Announcements**
- **Grades**
- **Slate internal messages (email)**
- **Calendar** (all due dates in one view)

And you provide:
- Automatic Telegram + Apple Reminders notifications for upcoming/overdue items
- Document download and zip packaging
- Step-by-step action plans

## Project location

Tools live at `~/hermes/`. All commands below are run from there with the venv active:
```bash
cd ~/hermes
source .venv/bin/activate
```

## Authentication (first time only)

```bash
python -m slate.auth
```
Opens a browser for Microsoft SSO (Sheridan login). Session saved to `~/.hermes/slate_session.json`.

On EC2 without a display:
```bash
xvfb-run --auto-servernum python -m slate.auth
```

Verify session is still valid:
```bash
python -m slate.auth --check
```

## Commands reference

### Show everything pending (default)
```bash
python -m slate.checker
```
Shows a combined table of all pending assignments, quizzes, discussions — sorted by due date with urgency colour:
- 🔴 Overdue · 🚨 Due today · 🟠 1–2 days · 🟡 3–7 days · 🟢 Later · ✅ Done

Also shows new announcements and unread messages.

### Filter by type
```bash
python -m slate.checker --assignments     # assignments + group work only
python -m slate.checker --quizzes         # quizzes only
python -m slate.checker --discussions     # discussions only
python -m slate.checker --announcements   # new announcements
python -m slate.checker --grades          # all graded items with scores
python -m slate.checker --messages        # unread Slate messages
python -m slate.checker --calendar        # calendar view (next 30 days)
```

### Get full context for an assignment
```bash
python -m slate.checker --context <assignment_id>
```
Prints full instructions, attachments, due date, submission status.
Use the ID from the table shown by `python -m slate.checker`.

### Download assignment documents
```bash
python -m slate.checker --download <assignment_id>
```
Downloads all attachments → saves to `~/hermes-docs/<course>/<assignment>/` → creates a zip.

### Generate an action plan
```bash
python -m slate.checker --plan <assignment_id>
```
Prints a ready-to-paste prompt. Paste it back into this Hermes session to get:
1. Plain-language summary of requirements
2. List of deliverables
3. Step-by-step plan with time estimates
4. Risks and tips

### Run background watcher (on EC2)
```bash
python -m slate.checker --watch
```
Checks Slate every 60 minutes (configurable in `.env`).

Automatically notifies via Telegram + Apple Reminders when:
- Any assignment/quiz/discussion is due within 3 days (configurable)
- Any item becomes overdue
- New announcements are posted
- Grades are updated
- Unread messages arrive

On EC2 this runs as a systemd service:
```bash
sudo systemctl start slate-checker
sudo journalctl -u slate-checker -f
```

## Workflow when the student asks about their work

1. Run `python -m slate.checker` to see all pending items
2. For a specific item: `--context <id>` to read the full details
3. If they need the files: `--download <id>` → give them the zip path
4. If they need a plan: `--plan <id>` → paste the output back here for analysis
5. For grades: `--grades` to see what's been marked
6. For new messages: `--messages` to read unread Slate mail
7. Answer any questions using the context fetched above

## Configuration (.env)

Key variables (all in `~/hermes/.env`):
| Variable | Default | Description |
|---|---|---|
| `SLATE_URL` | `https://slate.sheridancollege.ca` | D2L base URL |
| `CHECK_INTERVAL_MINUTES` | `60` | How often the watcher polls |
| `REMINDER_DAYS_AHEAD` | `3` | Alert threshold (days before due) |
| `DOCS_DIR` | `~/hermes-docs` | Where downloads are saved |

## Troubleshooting

**Session expired** — Re-run `python -m slate.auth`.

**No data for some courses** — Some courses may not use certain D2L features (e.g. no dropbox). The client silently skips unavailable endpoints.

**Telegram not sending** — Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. The chat ID comes from a conversation you started with the bot.

**Apple Reminders not working** — Use an app-specific password (not your Apple ID password). Generate at appleid.apple.com → Sign-In and Security.

**EC2 auth with no display** — Use `xvfb-run` as shown above, or authenticate locally and `scp` the session file to EC2:
```bash
scp ~/.hermes/slate_session.json ec2-user@<ip>:~/.hermes/slate_session.json
```
