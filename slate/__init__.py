"""
Slate package — a D2L Brightspace client for Sheridan College's "Slate" LMS.

This package provides:
  - auth.py    — Microsoft SSO browser-based login + session persistence
  - client.py  — async HTTP client wrapping the D2L REST API
  - models.py  — dataclasses for assignments, quizzes, discussions, grades, etc.
  - cache.py   — JSON file-based cache with TTL to avoid hammering the API
  - checker.py — Rich CLI for viewing pending work + scheduled watcher with notifications
  - notifier.py — multi-channel notification dispatch (Telegram + Apple Reminders)
  - sync.py    — SCP-based session sync from local Mac to remote EC2 server

Architecture:
  The user authenticates once via a real browser (auth.py) on their Mac.
  The resulting session cookies are saved to ~/.hermes/slate_session.json.
  All subsequent API calls (client.py) reuse those cookies via httpx.
  On a headless server, the session file is copied over via sync.py.
"""
