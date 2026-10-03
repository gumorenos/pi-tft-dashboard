# Nanobot on the Pi (optional)

🇬🇧 English · 🇪🇸 [Español](README.es.md)

Support files for the [nanobot](https://github.com/HKUDS/nanobot) agent running on the same Pi.

## Run mode: gateway with the native WebUI

Nanobot ships its own web interface (bundle in `nanobot/web/dist`, API in `nanobot/webui/`), so no custom UI is needed. The service runs the gateway, which serves the WebUI, the channels (Telegram) and the agent:

```
ExecStart=/home/dietpi/.local/bin/nanobot gateway --foreground --workspace /home/dietpi/.nanobot/workspace --config /home/dietpi/.nanobot/config.json
```

The WebUI is the `websocket` channel. To use it from the LAN or Tailscale, change `host` and set a password (`token_issue_secret`):

```json
"channels": {
  "websocket": {
    "enabled": true, "host": "0.0.0.0", "port": 8765,
    "websocket_requires_token": true,
    "token_issue_secret": "LONG-RANDOM-PASSWORD"
  }
}
```

Open `http://PI-IP:8765` and it asks for that password. It is plain HTTP: on the LAN the password travels unencrypted; from outside, use Tailscale.

## Pi tools (MCP)

`pi-tools/server.py` is an MCP server with scoped tools: Pi status, network, Pi-hole summary, top clients and blocked domains, DHCP devices, recent queries, and pausing blocking for up to 10 minutes. It is installed in `/home/dietpi/pi-tools/` and registered in `~/.nanobot/config.json`:

```json
"tools": {
  "exec": {"sandbox": "bwrap", "timeout": 30},
  "restrict_to_workspace": true,
  "mcp_servers": {
    "pi-tools": {
      "command": "/home/dietpi/.local/share/uv/tools/nanobot-ai/bin/python",
      "args": ["/home/dietpi/pi-tools/server.py"],
      "env": {"PIHOLE_PASSWORD": "${PIHOLE_PASSWORD}"},
      "tool_timeout": 30
    }
  }
}
```

`PIHOLE_PASSWORD` goes in `~/.nanobot/.env` (and `PIHOLE_URL` in the same `env` block if your Pi-hole web interface is not on port 8089). The MCP server runs outside the shell sandbox, so the LLM never sees the password. Nanobot 0.3.x needs `nanobot plugins enable api` (aiohttp) and `nanobot plugins enable telegram` for that channel.

## Agent instructions

`workspace/AGENTS.md.example` and `workspace/USER.md.example` (Spanish) or `AGENTS.en.md.example` and `USER.en.md.example` (English): templates for the agent's instructions and the user profile (fill in the `<...>`). Copy them without the `.example` (and without `.en`) into `~/.nanobot/workspace/`. The language of these files is the language the agent answers in.

## Telegram

`scripts/nanobot-telegram-setup.sh` (installed as `/usr/local/bin/nanobot-telegram-setup`): asks for the bot token and the authorized IDs, verifies the token against Telegram, stores it in `~/.nanobot/.env` and enables the channel. `allow_from` is mandatory: only those users can talk to the bot.

## LLM providers

- Switching models from Telegram (or the WebUI): `/model` shows the current model and the presets; `/model NAME` changes the model for that conversation (not permanent). Presets are defined in `model_presets` in `config.json` (`{"model": ..., "provider": ..., "max_tokens": ..., "context_window_tokens": ...}`); `default` is `agents.defaults`. Other commands: `/new`, `/stop`, `/status`, `/help`.
- Gemini (free key): each model has its own free quota and it runs out quickly (every request carries ~9,000 tokens of tools and instructions). That is why there is a fallback chain: `gemini-3.8-flash` → `3.5-flash-lite` → `3.1-flash-lite` → `3.6-flash` → `3.5-flash`. `gemini-3.5-flash` answers in ~2 s but can return 429 for quota. Gemma models emit their reasoning as text, take ~25 s and cut the stream ("Model stream ended before a finish reason was received"); do not use them as the main model. `gemini-2.5-pro` is no longer available to new accounts.
- Telegram: with `"streaming": false` failover to other models works; with streaming, if the model cuts mid-answer there is no retry.
- `/model` only offers the presets defined in `model_presets` (plus `default`); it does not list the provider's whole catalog. To have more options, add presets.
- OpenCode Zen: it is migrating from API keys (`sk-...`) to a console OAuth login with client identity. Old `sk-` keys still work, but new accounts may not be able to create one; with the OAuth login nanobot cannot connect. The free models through `/chat/completions` are `nemotron-3-ultra-free`, `mimo-v2.6-flash-free`, `nemotron-3.5-lightning-free`, `big-pickle`, `mimo-v2.5-free`, `space-bunny-free`, `longcat-2.5-preview-free` and `ling-3.0-flash-fin-free`. With an `sk-` key: `scripts/nanobot-opencode-setup.sh` (installed as `/usr/local/bin/nanobot-opencode-setup`) asks for the API key, checks the catalog, probes the free models (ids ending in `-free`) and sets them as main and fallback, with Gemini Flash last. The key goes in `~/.nanobot/.env` as `OPENCODE_API_KEY` and the provider is called `opencode`. Free models may use the conversations to improve their models.
- Other providers (OpenRouter, Qwen/DashScope, NVIDIA...): add the key in the WebUI and run `scripts/nanobot-add-models.py PROVIDER`, which tests the provider's models, shows the real reason for each failure and creates `/model` presets.

## Session compaction and idle usage

- By default nanobot **compacts (summarizes with a model call) every session that has been idle for 15 minutes** (`agents.defaults.idleCompactAfterMinutes`, checked every 60 s). In a chat used in bursts that happens constantly: it burns quota and loses conversation detail. Here it is set to 240 min (`0` disables it) with a 300 s check.
- Without idle compaction the history grows until it is compacted by context size: every message carries more tokens. Use `/new` to start a clean conversation.
- "Dream" (memory consolidation) runs every 6 h with the cheap `lite` preset (`agents.defaults.dream`: `intervalH`, `modelOverride`). With "nothing to process" it does not call the model. The heartbeat (`gateway.heartbeat`, every 30 min) ends immediately if `HEARTBEAT.md` has no tasks.

## Secrets outside `config.json`

The WebUI stores provider keys in plain text in `config.json`. `scripts/nanobot-secrets-to-env.py` (installed as `/usr/local/bin/nanobot-secrets-to-env`, supports `--dry-run`) moves them to `~/.nanobot/.env` and leaves references like `${OPENROUTER_API_KEY}`. That way `config.json` can be read, copied or audited without exposing keys. After adding a new provider in the WebUI, run it again. If an audit read the file earlier, consider rotating those keys.

## Recommended security

- With `sandbox: bwrap` the shell only sees the workspace: the service must use `--workspace /home/dietpi/.nanobot/workspace` (not the parent directory, or the sandbox exposes `config.json` and `.env`).
- Install `bubblewrap` and add `NoNewPrivileges=true` and `ProtectSystem=full` to the service (prevents `sudo` from the agent even if the user has it).
- Any web interface with access to the agent must have authentication.
