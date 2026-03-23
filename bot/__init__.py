"""
Hermes bot package.

This package implements a Telegram-based personal assistant ("Hermes") that
integrates with Sheridan College's Slate (D2L Brightspace), Apple iCloud
(Reminders and Calendar via CalDAV), a persistent browser session (Playwright),
a bounded terminal runner, a memory system, and background sub-agents for
long-running tasks.

Package layout:
    agent.py         - LLM agent loop with multi-step tool calling via OpenRouter/Bedrock
    apple.py         - iCloud CalDAV integration for Apple Reminders and Calendar
    computer.py      - Playwright-based browser automation with backend fallback chain
    jobs.py          - Background job manager for long-running sub-agent tasks
    media.py         - Voice transcription and image understanding via LLM APIs
    memory.py        - Persistent markdown-file memory store with frontmatter
    message_input.py - Telegram message normalisation (text, voice, image)
    reminders.py     - APScheduler-based reminder system with SQLite persistence
    skills.py        - Pattern-matched skill router for common intents (no LLM needed)
    tasks.py         - SQLite-backed persistent task tracker
    telegram_bot.py  - Telegram bot entry point, command handlers, message routing
    terminal.py      - Bounded shell execution with process-group cleanup
    tools.py         - Tool registry and implementations exposed to the LLM agent
"""
