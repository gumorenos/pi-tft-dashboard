#!/bin/bash
# Envía una notificación push por ntfy.
# Uso: pi-notify.sh "Título" "Mensaje" [prioridad: min|low|default|high|urgent] [tag1,tag2]
# Configuración en /etc/pi-alert.env: NTFY_TOPIC (obligatorio), NTFY_URL, NTFY_TOKEN.
ENV=/etc/pi-alert.env
# shellcheck disable=SC1090
[ -f "$ENV" ] && . "$ENV"
[ -n "${NTFY_TOPIC:-}" ] || { echo "pi-notify: falta NTFY_TOPIC en $ENV" >&2; exit 2; }

URL="${NTFY_URL:-https://ntfy.sh}"
TITLE="${1:-Pi}"
MSG="${2:-}"
PRIO="${3:-default}"
TAGS="${4:-}"

PAYLOAD=$(python3 - "$NTFY_TOPIC" "$TITLE" "$MSG" "$PRIO" "$TAGS" <<'PY'
import json, sys
topic, title, msg, prio, tags = sys.argv[1:6]
p = {"topic": topic, "title": title, "message": msg,
     "priority": {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}.get(prio, 3)}
if tags:
    p["tags"] = tags.split(",")
print(json.dumps(p))
PY
)

AUTH=()
[ -n "${NTFY_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer $NTFY_TOKEN")

curl -fsS -m 10 --retry 2 "${AUTH[@]}" -H "Content-Type: application/json" -d "$PAYLOAD" "$URL" >/dev/null
