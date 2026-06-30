# telebot — DEPRECATED

This legacy Telegram bot (PyGithub + Gemini direct calls) was retired on 2026-06-30.
It is fully replaced by the new orchestrator-routed bridge:

    ~/service/agents/telegram-bridge/   (systemd: telegram-bridge.service)

The bridge never writes files directly — it routes every command to the
Orchestrator via `claude -p`. The original source remains in git history
(before this commit) for reference.

Do not revive this directory. Add new commands to the bridge instead.
