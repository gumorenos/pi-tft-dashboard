#!/bin/bash
# Vigilante EXTERNO: corre en otro equipo (p. ej. otro Raspberry Pi tuyo) y avisa por ntfy si el Pi no
# responde por Tailscale. Cubre el caso en que el Pi está completamente caído y, por tanto,
# no puede avisar por sí mismo.
#
# Configuración en ~/.config/pi-watch.env (chmod 600):
#   WATCH_HOST=100.x.y.z        IP de Tailscale del Pi
#   WATCH_PORT=22               puerto TCP a probar
#   WATCH_NAME=Pi               nombre en los avisos
#   NTFY_TOPIC=...              mismo tema que usa el Pi
#   NTFY_URL=https://ntfy.sh
#   FAILS_TO_ALERT=3            fallos seguidos (uno por minuto) antes de avisar
# Programar con cron:  * * * * * $HOME/bin/pi-watch.sh >/dev/null 2>&1
set -u

CONF="${PI_WATCH_CONF:-$HOME/.config/pi-watch.env}"
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"

HOST="${WATCH_HOST:?falta WATCH_HOST}"
PORT="${WATCH_PORT:-22}"
TOPIC="${NTFY_TOPIC:?falta NTFY_TOPIC}"
URL="${NTFY_URL:-https://ntfy.sh}"
NAME="${WATCH_NAME:-Pi}"
FAILS_TO_ALERT="${FAILS_TO_ALERT:-3}"

STATE="${XDG_STATE_HOME:-$HOME/.local/state}/pi-watch"
mkdir -p "$STATE"
FAILS="$STATE/fails"; SINCE="$STATE/since"; ALERTED="$STATE/alerted"

notify() {   # notify "título" "mensaje" prioridad tags
    local payload
    payload=$(python3 - "$TOPIC" "$1" "$2" "${3:-3}" "${4:-}" <<'PY'
import json, sys
topic, title, msg, prio, tags = sys.argv[1:6]
p = {"topic": topic, "title": title, "message": msg, "priority": int(prio)}
if tags:
    p["tags"] = tags.split(",")
print(json.dumps(p))
PY
)
    curl -fsS -m 10 --retry 2 -H "Content-Type: application/json" -d "$payload" "$URL" >/dev/null
}

# Si este equipo no tiene internet no se puede juzgar ni avisar: no hacer nada.
ping -c1 -W3 1.1.1.1 >/dev/null 2>&1 || ping -c1 -W3 8.8.8.8 >/dev/null 2>&1 || exit 0

if timeout 5 bash -c "exec 3<>/dev/tcp/$HOST/$PORT" 2>/dev/null; then
    if [ -f "$ALERTED" ]; then
        mins=$(( ( $(date +%s) - $(cat "$SINCE" 2>/dev/null || date +%s) ) / 60 ))
        notify "✅ $NAME vuelve a responder" "Estuvo sin respuesta ~${mins} min." 3 white_check_mark && rm -f "$ALERTED"
    fi
    rm -f "$FAILS" "$SINCE"
    exit 0
fi

n=$(( $(cat "$FAILS" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAILS"
[ -f "$SINCE" ] || date +%s > "$SINCE"
if [ "$n" -ge "$FAILS_TO_ALERT" ] && [ ! -f "$ALERTED" ]; then
    notify "🔴 $NAME no responde" "Sin respuesta por Tailscale en ${HOST}:${PORT} desde hace ~${n} min. Puede estar apagado, sin red o sin luz." 5 rotating_light && touch "$ALERTED"
fi
exit 0
