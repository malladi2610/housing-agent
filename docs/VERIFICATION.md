# Verification record

Build date: 18 October 2026.

- The Python test suite passes in the managed Linux build environment. Tests cover cost/date/distance rules, stale draft rejection, exact manual-send recording, reply/follow-up races, viewing confirmations/conflicts/cancellation, duplicate imports, atomic job claims and lease recovery, persistent backups, LangGraph recovery, credential non-disclosure, CSRF/authentication, HTML/CSV escaping, and the real ChatOllama HTTP serialization contract with a fake transport.
- A real Chromium session exercised seven UI pages at 1440px and 390px widths: 14 successful page checks, no horizontal overflow and no JavaScript page errors. It also submitted profile and listing forms. Screenshots contain synthetic QA data only.
- The Docker image builds with locked Python dependencies and a non-root runtime user.
- Two test containers verified web/UI authentication, shared SQLite worker heartbeat, database writes and persistence after restarting the web container. The Compose configuration is validated separately.
- No Ollama credential was present for live inference. Model quality, account access, real image interpretation, rate limits and costs have not been measured. Fake responses are used by tests; they are not presented as live model results.
- No real mailbox, Windows laptop, Cua desktop, Android emulator or rental-platform account was connected. IMAP/device integrations require the documented pilot on the user's hardware. Windows launcher scripts are supplied for review but were not executed on Windows in this build environment.
- Platform message sending, unattended GUI actions and live commute calculation are not implemented. The UI labels these limits explicitly.

Run `uv run pytest -q` to reproduce the Python tests. GitHub Actions is configured to run them on both Linux and Windows once the repository is published.
