# Hermes

A personal AI assistant Telegram bot built for a Sheridan College student, designed to run on an EC2 instance. Hermes integrates with D2L Brightspace (Slate), Apple iCloud (Reminders and Calendar), browser automation, terminal execution, persistent memory, and more -- all orchestrated through a tool-calling LLM agent loop.

> **Inspired by** [Nous Research's Hermes](https://hermes-agent.nousresearch.com) — I liked the name and kept it.

```
┌──────────┐       ┌──────────────────┐       ┌─────────────────┐
│ Telegram  │◄─────►│   Telegram Bot    │◄─────►│   LLM Agent     │
│   User    │       │  (telegram_bot)   │       │   (agent.py)    │
└──────────┘       └──────────────────┘       └────────┬────────┘
                                                       │
                              ┌─────────────────────────┼─────────────────────────┐
                              │                         │                         │
                    ┌─────────▼────────┐    ┌───────────▼──────────┐   ┌──────────▼─────────┐
                    │   Slate (D2L)    │    │   Apple iCloud       │   │   Browser           │
                    │  Assignments     │    │  Reminders (CalDAV)  │   │  Playwright / BU /  │
                    │  Quizzes         │    │  Calendar  (CalDAV)  │   │  Browserbase        │
                    │  Grades          │    └──────────────────────┘   └──────────────────────┘
                    │  Announcements   │
                    │  Messages        │    ┌───────────────────────┐  ┌──────────────────────┐
                    └──────────────────┘    │   Terminal            │  │   Memory             │
                                           │  Shell commands       │  │  Markdown files      │
                    ┌──────────────────┐    │  systemd services     │  │  ~/.hermes/memory/   │
                    │   Tasks          │    └───────────────────────┘  └──────────────────────┘
                    │  SQLite-backed   │
                    │  ~/.hermes/      │    ┌───────────────────────┐  ┌──────────────────────┐
                    │  tasks.db        │    │   Reminders           │  │   Background Jobs    │
                    └──────────────────┘    │  APScheduler + SQLite │  │  Sub-agent tasks     │
                                           └───────────────────────┘  └──────────────────────┘
```

---

## Features

### LLM Agent Loop with Fallback Chain

The core agent (`bot/agent.py`) uses an OpenAI-compatible API to drive a multi-step tool-calling loop. Two LLM providers are supported:

- **OpenRouter** -- free-tier models with automatic fallback across multiple options (Qwen3 Next, Arcee Trinity, Step Flash, GPT-OSS, etc.)
- **Amazon Bedrock** -- via the Bedrock Mantle gateway (Qwen3 Next, GLM-5, DeepSeek V3.2, Mistral Large, Kimi K2.5, etc.)

If a model is rate-limited or returns an error, the agent automatically tries the next model in the fallback chain. The user can select the primary model via the `/models` Telegram command.

### D2L Brightspace Integration (Slate)

Full integration with Sheridan College's D2L Brightspace LMS:

- Pending assignments, quizzes, and discussions with urgency indicators
- Full assignment details including instructions and attachments
- Download and zip assignment documents
- Action plan generation for assignments
- Course announcements
- Grade updates with scores and percentages
- Internal Slate messages
- Calendar event merging to catch items not exposed by the assignments API
- 5-minute cache to avoid excessive API calls
- Automatic periodic checks with Telegram notifications for new items

### Apple iCloud Integration

Via CalDAV protocol:

- **Reminders** -- create, list, update, and delete Apple Reminders with support for priority levels, subtasks, location text, people tags, and alerts
- **Calendar** -- add, list, update, and delete Apple Calendar events with location, notes, and alert configuration
- Supports targeting specific reminder lists and calendars by name

### Browser Automation

Three interchangeable backends with automatic fallback:

- **Local Playwright** -- headless Chromium with persistent browser profile
- **Browser Use** -- cloud browser service with optional reusable profiles and proxy support
- **Browserbase** -- cloud browser with persistent contexts, ad blocking, CAPTCHA solving, stealth mode, and session recording

The agent can open URLs, click elements, type into forms, read page text, inspect interactive elements, take screenshots, upload files, download files, and check login state. Browser sessions have idle and max-session timeouts with automatic cleanup.

### Background Sub-Agent Jobs

Long-running browser or terminal tasks run in a separate agent conversation so the main chat stays responsive. The user can ask "how's that going?" to check progress, and finished results are automatically delivered. Incomplete results trigger automatic retries.

### Voice Transcription and Image Understanding

- **Voice notes** -- transcribed via OpenRouter (GPT Audio Mini) and fed to the agent as text
- **Images** -- analyzed via Bedrock vision models (Qwen3 VL) for deadlines, instructions, UI errors, or actionable text

### Persistent Memory System

Memories are stored as markdown files in `~/.hermes/memory/` with YAML frontmatter. Supports typed memories (user, feedback, routine, contact, project, note), tags, keyword search, and automatic injection into the agent system prompt for context-aware responses.

### Task Management

SQLite-backed task system (`~/.hermes/tasks.db`) with:

- Priority levels (high, medium, low)
- Due dates with overdue tracking
- Source linking for Slate-originated tasks
- Open/done status tracking

### Reminder Scheduling

APScheduler with SQLite persistence (`~/.hermes/reminders.db`):

- Natural language time parsing ("in 30 minutes", "tomorrow at 9am", "2026-03-25 14:00")
- Survives bot restarts
- Automatic Slate deadline reminders that sync when new items are detected
- Change detection for due date updates

### Terminal Command Execution

Bounded shell execution with process group management:

- Commands run in their own process group for clean cleanup
- Configurable timeout with aggressive kill on expiration
- Output truncation for large results
- systemd service inspection, restart, and log reading

### Pattern-Matched Skills

Common intents (checking assignments, setting reminders, listing tasks, greetings, weather, web search) are handled by regex pattern matching in `bot/skills.py` without hitting the LLM, making basic commands 100% reliable regardless of model quality.

### Web Search

- **Serper API** (Google) -- primary search backend with structured result parsing
- **DuckDuckGo** -- free fallback via the `ddgs` library
- **Hybrid lookup** -- combines web search with a direct browser visit for site-specific lookups (prices, menus, hours, product availability)

### AWS Secret Loading

`deploy/run_with_aws_env.py` loads secrets at startup from:

- **AWS SSM Parameter Store** -- hierarchical parameters under a configurable path prefix
- **AWS Secrets Manager** -- JSON object, dotenv-format, or single raw string secrets

This keeps credentials out of `.env` on the EC2 instance.

---

## Project Structure

```
hermes/
├── bot/                          # Core bot package
│   ├── __init__.py
│   ├── agent.py                  # LLM agent loop, fallback chain, conversation history
│   ├── apple.py                  # Apple iCloud Reminders & Calendar via CalDAV
│   ├── computer.py               # Browser automation (Playwright, Browser Use, Browserbase)
│   ├── jobs.py                   # Background sub-agent job manager
│   ├── media.py                  # Voice transcription & image understanding
│   ├── memory.py                 # Persistent memory system (markdown files)
│   ├── message_input.py          # Telegram message preprocessing (text, voice, images)
│   ├── reminders.py              # APScheduler reminder system with Slate sync
│   ├── skills.py                 # Pattern-matched skill router (no LLM needed)
│   ├── tasks.py                  # SQLite-backed task management
│   ├── telegram_bot.py           # Telegram bot handlers, commands, and main entry point
│   ├── terminal.py               # Bounded shell execution and systemd helpers
│   └── tools.py                  # All 40+ agent tool definitions and implementations
│
├── slate/                        # D2L Brightspace (Slate) client package
│   ├── __init__.py
│   ├── auth.py                   # Microsoft SSO session management (Playwright-based)
│   ├── cache.py                  # 5-minute JSON cache for Slate data
│   ├── checker.py                # CLI checker with Rich output and notification dispatch
│   ├── client.py                 # Async D2L API client (courses, assignments, quizzes, etc.)
│   ├── models.py                 # Data models (Assignment, Quiz, Discussion, Grade, etc.)
│   ├── notifier.py               # Notification backends (Telegram + Apple Reminders)
│   └── sync.py                   # SCP-based session sync from Mac to EC2
│
├── deploy/                       # Deployment configuration
│   ├── aws/                      # IAM policy for SSM/Secrets Manager access
│   ├── hermes-bot.service        # systemd unit for the Telegram bot
│   ├── slate-checker.service     # systemd unit for the Slate checker
│   ├── run_with_aws_env.py       # AWS secret loader + module runner
│   ├── setup_ec2.sh              # EC2 Ubuntu setup script
│   ├── com.hermes.slate-sync.plist  # macOS LaunchAgent for automated session sync
│   ├── install_slate_sync_launchagent.sh  # LaunchAgent installer
│   └── sync_slate_session.sh     # Manual session sync script
│
├── scripts/                      # Operational scripts
│   ├── push_runtime_env_to_ssm.sh          # Upload .env values to AWS SSM
│   ├── push_runtime_env_to_secrets_manager.sh  # Upload .env values to Secrets Manager
│   └── set_secret_env.sh                   # Set a single secret env var
│
├── skills/                       # Hermes agent skill definitions
│   └── slate-assistant/
│
├── tests/                        # Test suite
│   ├── test_agent_config.py      # Agent configuration and model selection
│   ├── test_apple_tools.py       # Apple Reminders/Calendar tool tests
│   ├── test_aws_env.py           # AWS secret loading tests
│   ├── test_computer_tools.py    # Browser automation tests
│   ├── test_jobs.py              # Background job manager tests
│   ├── test_media_and_telegram.py  # Voice/image and message input tests
│   ├── test_memory.py            # Memory system tests
│   ├── test_reminders.py         # Reminder scheduling tests
│   ├── test_slate_filters.py     # Slate data filtering tests
│   ├── test_slate_sync.py        # Session sync tests
│   ├── test_tasks.py             # Task management tests
│   └── test_terminal_tools.py    # Terminal execution tests
│
├── slate_cli.py                  # Standalone CLI for Slate queries
├── requirements.txt              # Python dependencies
├── .env.example                  # Environment variable template
└── .gitignore
```

---

## Setup and Installation

### Prerequisites

- Python 3.11 or later
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- An LLM provider API key (OpenRouter free tier or AWS Bedrock)

### Installation

```bash
# Clone the repository
git clone <repo-url> ~/hermes
cd ~/hermes

# Create and activate a virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers (needed for browser automation and Slate auth)
playwright install chromium
```

### Configuration

```bash
cp .env.example .env
# Edit .env with your credentials
```

### Slate Authentication

Slate uses Microsoft SSO, so initial authentication requires a headed browser:

```bash
# On your Mac or a machine with a display:
python -m slate.auth

# To sync the session to EC2:
python -m slate.sync --host ubuntu@your-server --key ~/.ssh/key.pem
```

---

## Configuration

All configuration is done via environment variables in `.env`. The groups are:

### LLM Provider

| Variable | Description |
|---|---|
| `LLM_PROVIDER` | `openrouter` or `bedrock` |
| `OPENROUTER_API_KEY` | OpenRouter API key (free tier available) |
| `OPENROUTER_MODEL` | Default OpenRouter model ID |
| `BEDROCK_API_KEY` | Bedrock Mantle API key |
| `BEDROCK_BASE_URL` | Bedrock Mantle endpoint |
| `BEDROCK_MODEL` | Default Bedrock model ID |
| `BEDROCK_VISION_MODEL` | Vision model for image understanding |
| `VOICE_TRANSCRIBE_MODEL` | Model for voice note transcription |

### D2L / Slate

| Variable | Description |
|---|---|
| `SLATE_URL` | Brightspace instance URL |
| `SLATE_EMAIL` | Microsoft SSO email |
| `SLATE_SESSION_FILE` | Path to saved session state |

### Telegram

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Allowed chat ID (restricts access to one user) |

### Apple iCloud

| Variable | Description |
|---|---|
| `APPLE_ID` | Apple ID email |
| `APPLE_APP_PASSWORD` | App-specific password (not your main password) |
| `APPLE_REMINDERS_NAME` | Target reminders list name (optional) |
| `APPLE_CALENDAR_NAME` | Target calendar name (optional) |

### Browser Automation

| Variable | Description |
|---|---|
| `BROWSER_BACKEND` | Primary backend: `local`, `browser-use`, or `browserbase` |
| `BROWSER_FALLBACK_BACKEND` | Fallback backend if primary fails |
| `BROWSER_HEADLESS` | Run local Playwright headlessly (`true`/`false`) |
| `BROWSER_TIMEOUT_MS` | Page action timeout in milliseconds |
| `BROWSER_IDLE_TIMEOUT_SECONDS` | Close browser after this many idle seconds |
| `BROWSER_MAX_SESSION_SECONDS` | Maximum browser session duration |
| `BROWSER_USE_API_KEY` | Browser Use API key |
| `BROWSER_USE_PROFILE_ID` | Reusable Browser Use profile |
| `BROWSERBASE_API_KEY` | Browserbase API key |
| `BROWSERBASE_PROJECT_ID` | Browserbase project ID |
| `BROWSERBASE_CONTEXT_ID` | Persistent context for login state |

### Checker Schedule

| Variable | Description |
|---|---|
| `CHECK_INTERVAL_MINUTES` | Slate polling interval (default: 60) |
| `REMINDER_DAYS_AHEAD` | Warn about items due within N days |
| `DOCS_DIR` | Download directory for assignment documents |

### AWS Secret Loading

| Variable | Description |
|---|---|
| `AWS_REGION` | AWS region for SSM/Secrets Manager |
| `HERMES_AWS_SSM_PATH` | SSM parameter path prefix (e.g., `/hermes/prod`) |
| `HERMES_AWS_SECRET_ID` | Secrets Manager secret ID or ARN |
| `HERMES_AWS_SECRET_ENV_NAME` | Env var name for single raw secrets |

### Session Sync (Mac to EC2)

| Variable | Description |
|---|---|
| `HERMES_HOST` | Remote SSH host (e.g., `ubuntu@1.2.3.4`) |
| `HERMES_SSH_KEY` | SSH private key path |
| `HERMES_REMOTE_REPO` | Remote Hermes repo path |

---

## Usage

### Running the Bot

```bash
# Direct run
python -m bot.telegram_bot

# With AWS secret loading (recommended on EC2)
python deploy/run_with_aws_env.py bot.telegram_bot
```

### Slate CLI

```bash
# Check pending assignments
python slate_cli.py assignments

# Only items due in the next 7 days
python slate_cli.py assignments --days-ahead 7

# Get full details for an assignment
python slate_cli.py details <id>

# Check announcements, grades, messages
python slate_cli.py announcements
python slate_cli.py grades
python slate_cli.py messages

# Force refresh from D2L
python slate_cli.py refresh
```

### Slate Checker (Background Watcher)

```bash
# Run the checker with Rich CLI output
python -m slate.checker

# Run on a schedule (used by the systemd service)
python -m slate.checker --watch
```

### Session Sync

```bash
# Authenticate locally
python -m slate.auth

# Sync to EC2
python -m slate.sync --host ubuntu@your-server --key ~/.ssh/key.pem
```

---

## Telegram Commands

| Command | Description |
|---|---|
| `/start` | Introduction and capabilities overview |
| `/schoolwork` | Check due assignments, quizzes, and discussions |
| `/slate` | Same as `/schoolwork` |
| `/models` | Choose the primary LLM model (inline keyboard) |
| `/calendar` | Show upcoming Apple Calendar events |
| `/tasks` | List current tasks |
| `/jobs` | List background sub-agent jobs |
| `/reminders` | List pending reminders |
| `/memory` | List stored memories |
| `/clear` | Clear conversation history |
| `/help` | Show help with usage examples |

Most interactions happen through natural language messages rather than commands.

---

## Tool Reference

The agent has access to 40+ tools, organized by category:

### Slate (D2L Brightspace)
- `slate_check_assignments` -- pending assignments/quizzes/discussions with optional day filter
- `slate_get_assignment_details` -- full instructions for a specific item
- `slate_download_docs` -- download attached files for an assignment
- `slate_action_plan` -- generate a study/work plan
- `slate_check_announcements` -- course announcements
- `slate_check_grades` -- recent grades and scores
- `slate_check_messages` -- unread Slate messages
- `slate_refresh` -- force a fresh fetch (bypass 5-minute cache)

### Apple Reminders
- `set_apple_reminder` -- create with priority, subtasks, location, people, list selection
- `list_apple_reminders` -- show reminders from iCloud
- `update_apple_reminder` -- update by UID or title match
- `delete_apple_reminder` -- delete by UID or title match

### Apple Calendar
- `add_apple_calendar_event` -- add with location, notes, alert
- `list_apple_calendar_events` -- show upcoming events
- `update_apple_calendar_event` -- update by UID or title match
- `delete_apple_calendar_event` -- delete by UID or title match

### Telegram Reminders
- `set_reminder` -- schedule a Telegram reminder with natural language time
- `list_reminders` -- show pending reminders
- `cancel_reminder` -- cancel by index or text match

### Browser
- `browser_open` -- navigate to a URL
- `browser_current_page` -- show current page title and URL
- `browser_interactives` -- list visible inputs, buttons, links with selectors
- `browser_read` -- read page text or extract from a CSS selector
- `browser_click` -- click an element
- `browser_type` -- type into an element (supports `env:VAR_NAME` for secrets)
- `browser_create_context` -- create a reusable Browserbase context
- `browser_screenshot` -- capture the current page
- `browser_upload_file` -- upload a file into a file input
- `browser_download` -- download via click or direct URL
- `browser_login_status` -- inspect login state
- `browser_reset` -- force-close the session

### Terminal
- `terminal_run` -- run a shell command with timeout
- `service_status` -- inspect a systemd service
- `service_restart` -- restart a systemd service
- `service_logs` -- read journalctl logs

### Background Jobs
- `list_background_jobs` -- list current/recent jobs
- `background_job_status` -- inspect a specific job
- `cancel_background_job` -- cancel a running job

### Tasks
- `add_task` -- create with optional due date, priority, notes
- `list_tasks` -- show open or completed tasks
- `complete_task` -- mark done
- `reopen_task` -- reopen a completed task
- `delete_task` -- delete a task
- `task_from_slate` -- convert a Slate item into a persistent task

### Memory
- `remember` -- save a fact/preference with type and tags
- `recall` -- search memories by keyword
- `list_memories` -- show stored memories
- `forget` -- delete a memory

### Web Search
- `web_search` -- Google search via Serper with DuckDuckGo fallback
- `hybrid_web_lookup` -- combined search + direct browser visit

### Utilities
- `get_current_time` -- current date/time in Toronto ET

---

## Browser Backends

Hermes supports three browser backends, configured via `BROWSER_BACKEND` and `BROWSER_FALLBACK_BACKEND`:

### Local Playwright (`local`)

Runs headless Chromium directly on the server. Uses a persistent browser profile at `~/.hermes/browser-profile/` to maintain cookies and login state across sessions. Best for self-hosted setups where you control the server.

### Browser Use (`browser-use`)

Cloud browser service accessed via WebSocket CDP connection. Supports reusable profiles (`BROWSER_USE_PROFILE_ID`) for persistent login state and optional proxy countries. Requires a `BROWSER_USE_API_KEY`.

### Browserbase (`browserbase`)

Enterprise cloud browser with advanced features: persistent contexts for login state, ad blocking, CAPTCHA solving, advanced stealth mode, session recording, and regional deployment. Requires `BROWSERBASE_API_KEY` and `BROWSERBASE_PROJECT_ID`.

### Fallback Chain

The backends form a fallback chain. If the primary backend fails to start (e.g., missing API key, connection error), the next backend in the chain is tried automatically. The default implicit fallback for Browser Use is Browserbase (if configured) then local Playwright.

---

## Deployment

### EC2 Setup

Run the setup script on a fresh Ubuntu instance:

```bash
bash deploy/setup_ec2.sh
```

This installs Python 3.11, creates a virtual environment, installs dependencies, sets up Playwright, and installs systemd services.

### systemd Services

Two service files are provided:

- **`hermes-bot.service`** -- the main Telegram bot (runs `deploy/run_with_aws_env.py bot.telegram_bot`)
- **`slate-checker.service`** -- the standalone Slate checker in watch mode

Both use `run_with_aws_env.py` to load secrets from AWS before starting.

```bash
# Start the bot
sudo systemctl start hermes-bot
sudo systemctl enable hermes-bot

# Check logs
sudo journalctl -u hermes-bot -f
```

### AWS Secret Management

On EC2, keep only non-secret config in `.env` and store credentials in AWS:

**Option A -- SSM Parameter Store:**
```bash
# Set in .env:
HERMES_AWS_SSM_PATH=/hermes/prod

# Push secrets:
bash scripts/push_runtime_env_to_ssm.sh
```

**Option B -- Secrets Manager:**
```bash
# Set in .env:
HERMES_AWS_SECRET_ID=hermes/prod/runtime

# Push secrets:
bash scripts/push_runtime_env_to_secrets_manager.sh
```

Attach the IAM policy from `deploy/aws/hermes-runtime-reader-policy.json` to the EC2 instance role.

### Session Sync

Slate authentication requires a real browser for Microsoft SSO. The workflow is:

1. Run `python -m slate.auth` on your Mac (opens a browser for SSO)
2. Run `python -m slate.sync` to SCP the session file to EC2
3. Optionally install the macOS LaunchAgent for automated periodic sync:
   ```bash
   bash deploy/install_slate_sync_launchagent.sh
   ```

---

## Testing

Run the test suite with pytest:

```bash
# Run all tests
python -m pytest tests/ -v

# Run a specific test file
python -m pytest tests/test_agent_config.py -v

# Run with coverage (if pytest-cov is installed)
python -m pytest tests/ --cov=bot --cov=slate -v
```

The test suite covers:

- Agent configuration, model selection, and fallback chain logic
- Apple Reminders and Calendar tool formatting
- AWS secret loading (SSM and Secrets Manager parsing)
- Browser automation (backend chain, URL validation, session lifecycle)
- Background job management (lifecycle, status queries, cancellation)
- Voice/image media processing and Telegram message input routing
- Memory system (save, recall, delete, scoring)
- Reminder scheduling and time parsing
- Slate data filtering, calendar merging, and urgency classification
- Session sync logic
- Task management (CRUD, priority, status)
- Terminal command execution and service helpers

---

## License

TBD
