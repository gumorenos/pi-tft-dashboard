# Nanobot en el Pi (opcional)

🇬🇧 [English](README.md) · 🇪🇸 Español

Archivos de apoyo para el agente [nanobot](https://github.com/HKUDS/nanobot) que corre en el mismo Pi.

## Modo de ejecución: gateway con WebUI nativa

Nanobot trae su propia interfaz web (bundle en `nanobot/web/dist`, API en `nanobot/webui/`). No hace falta una UI propia. El servicio ejecuta el gateway, que sirve la WebUI, los canales (Telegram) y el agente:

```
ExecStart=/home/dietpi/.local/bin/nanobot gateway --foreground --workspace /home/dietpi/.nanobot/workspace --config /home/dietpi/.nanobot/config.json
```

La WebUI es el canal `websocket`. Para usarla desde la LAN o Tailscale hay que cambiar el `host` y fijar una contraseña (`token_issue_secret`):

```json
"channels": {
  "websocket": {
    "enabled": true, "host": "0.0.0.0", "port": 8765,
    "websocket_requires_token": true,
    "token_issue_secret": "CONTRASEÑA-LARGA-Y-ALEATORIA"
  }
}
```

Se abre en `http://IP-DEL-PI:8765` y pide esa contraseña. Es HTTP: por la LAN la contraseña viaja sin cifrar; desde fuera usa Tailscale.

## Herramientas del Pi (MCP)

`pi-tools/server.py` es un servidor MCP con herramientas acotadas: estado del Pi, red, resumen de Pi-hole, top de clientes y dominios bloqueados, dispositivos DHCP, consultas recientes y pausar el bloqueo hasta 10 minutos. Se instala en `/home/dietpi/pi-tools/` y se registra en `~/.nanobot/config.json`:

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

`PIHOLE_PASSWORD` va en `~/.nanobot/.env` (y `PIHOLE_URL` en el mismo bloque `env` si la interfaz web de tu Pi-hole no está en el puerto 8089). El servidor MCP corre fuera del sandbox del shell, así el LLM nunca ve la contraseña. Nanobot 0.3.x necesita `nanobot plugins enable api` (aiohttp) y `nanobot plugins enable telegram` para ese canal.

## Instrucciones del agente

`workspace/AGENTS.md.example` y `workspace/USER.md.example` (español) o `AGENTS.en.md.example` y `USER.en.md.example` (inglés): plantillas de instrucciones y perfil (rellena los `<...>`). Se copian sin `.example` (ni `.en`) a `~/.nanobot/workspace/`. El idioma de estos archivos es el idioma en que responde el agente.

## Telegram

`scripts/nanobot-telegram-setup.sh` (se instala como `/usr/local/bin/nanobot-telegram-setup`): pide el token del bot y los IDs autorizados, verifica el token contra Telegram, lo guarda en `~/.nanobot/.env` y activa el canal. `allow_from` es obligatorio: solo esos usuarios pueden hablarle al bot.

## Proveedores de LLM

- Cambiar de modelo desde Telegram (o la WebUI): `/model` muestra el modelo y los presets; `/model NOMBRE` cambia el modelo de esa conversación (no es permanente). Los presets se definen en `model_presets` de `config.json` (`{"model": ..., "provider": ..., "max_tokens": ..., "context_window_tokens": ...}`); `default` es `agents.defaults`. Otros comandos: `/new`, `/stop`, `/status`, `/help`.
- Gemini (clave gratuita): cada modelo tiene su propia cuota gratuita y se agota rápido (cada petición lleva ~9 000 tokens de herramientas e instrucciones). Por eso hay una cadena de respaldo: `gemini-3.8-flash` → `3.5-flash-lite` → `3.1-flash-lite` → `3.6-flash` → `3.5-flash`. `gemini-3.5-flash` responde en ~2 s pero puede dar 429 por cuota. Los modelos Gemma emiten su razonamiento como texto, tardan ~25 s y cortan el streaming ("Model stream ended before a finish reason was received"); no usarlos como principal. `gemini-2.5-pro` ya no está disponible para cuentas nuevas.
- Otros proveedores (OpenRouter, Qwen/DashScope, NVIDIA...): añade la clave en la WebUI y ejecuta `scripts/nanobot-add-models.py PROVEEDOR`, que prueba los modelos del proveedor, muestra el motivo real de cada fallo y crea presets para `/model`.
- Telegram: con `"streaming": false` el failover a otros modelos funciona; con streaming, si el modelo corta a mitad de respuesta no hay reintento.
- `/model` solo ofrece los presets definidos en `model_presets` (más `default`); no lista todo el catálogo del proveedor. Para tener más opciones hay que añadir presets.
- OpenCode Zen: está migrando de API keys (`sk-...`) a un login OAuth de consola con identidad de cliente. Las claves `sk-` antiguas siguen funcionando, pero las cuentas nuevas pueden no poder crear una; con el login OAuth nanobot no puede conectarse. Los modelos gratuitos por `/chat/completions` son `nemotron-3-ultra-free`, `mimo-v2.6-flash-free`, `nemotron-3.5-lightning-free`, `big-pickle`, `mimo-v2.5-free`, `space-bunny-free`, `longcat-2.5-preview-free` y `ling-3.0-flash-fin-free`. Con clave `sk-`: `scripts/nanobot-opencode-setup.sh` (se instala como `/usr/local/bin/nanobot-opencode-setup`) pide la API key, verifica el catálogo, prueba los modelos gratuitos (ids terminados en `-free`) y los deja como principal y respaldo, con Gemini Flash al final. La clave va en `~/.nanobot/.env` como `OPENCODE_API_KEY` y el proveedor se llama `opencode`. Los modelos gratuitos pueden usar las conversaciones para mejorar sus modelos.

## Compactación de sesiones y consumo en reposo

- Por defecto nanobot **compacta (resume con una llamada al modelo) cada sesión inactiva durante 15 minutos** (`agents.defaults.idleCompactAfterMinutes`, revisión cada 60 s). En un chat que se usa a ratos eso ocurre constantemente: gasta cuota y pierde el detalle de la conversación. Aquí está en 240 min (`0` lo desactiva) con revisión cada 300 s.
- Sin compactación por inactividad, el historial crece hasta que se compacta por tamaño de contexto: cada mensaje lleva más tokens. Usa `/new` para empezar una conversación limpia.
- "Dream" (consolidación de memoria) corre cada 6 h con el preset barato `lite` (`agents.defaults.dream`: `intervalH`, `modelOverride`). Con "nothing to process" no llama al modelo. El latido (`gateway.heartbeat`, cada 30 min) termina al instante si `HEARTBEAT.md` no tiene tareas.

## Secretos fuera de `config.json`

La WebUI guarda las claves de los proveedores en texto plano en `config.json`. `scripts/nanobot-secrets-to-env.py` (se instala como `/usr/local/bin/nanobot-secrets-to-env`, admite `--dry-run`) las mueve a `~/.nanobot/.env` y deja referencias `${OPENROUTER_API_KEY}`, etc. Así `config.json` se puede leer, copiar o auditar sin exponer claves. Tras añadir un proveedor nuevo en la WebUI, vuelve a ejecutarlo. Si una auditoría leyó el archivo antes, considera rotar esas claves.

## Seguridad recomendada

- Con `sandbox: bwrap` el shell solo ve el workspace: el servicio debe usar `--workspace /home/dietpi/.nanobot/workspace` (no el directorio padre, o el sandbox expone `config.json` y `.env`).
- Instalar `bubblewrap` y añadir al servicio `NoNewPrivileges=true` y `ProtectSystem=full` (impide `sudo` desde el agente aunque el usuario lo tenga).
- Cualquier interfaz web con acceso al agente debe tener autenticación.
