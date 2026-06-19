<!--
  Pointer file. Gemini CLI reads GEMINI.md by default (and only reads AGENTS.md if the user adds a
  context block to .gemini/settings.json). To keep one source of truth without forcing that config,
  the real rules live in AGENTS.md and this file just forwards to them. Google Antigravity reads
  AGENTS.md directly, so this file is specifically for Gemini CLI users.
-->
# Project AI Rules

The authoritative AI agent guide for this project is **[AGENTS.md](./AGENTS.md)**.

Read it before generating or editing any code. It covers the stack, project layout, the canonical
workflow for adding a test, coding rules (Selenium 4 / Behave / POM), how to run the suite, and how
credentials are handled.
