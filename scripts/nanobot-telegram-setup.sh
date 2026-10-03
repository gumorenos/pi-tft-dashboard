#!/bin/bash
# Configura el canal de Telegram de nanobot sin dejar el token en el historial ni en el chat.
# Uso: sudo nanobot-telegram-setup
# Necesita: el token del bot (de @BotFather) y los IDs numéricos de quienes pueden hablarle
# (cada persona puede obtener el suyo escribiéndole a @userinfobot).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Ejecuta con sudo: sudo $0" >&2; exit 1; }

ENVF=/home/dietpi/.nanobot/.env
CFG=/home/dietpi/.nanobot/config.json

read -rsp "Token del bot (no se muestra): " TOKEN; echo
[[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || { echo "Formato de token no válido" >&2; exit 1; }
ME=$(curl -fsS -m 10 "https://api.telegram.org/bot${TOKEN}/getMe") || { echo "Telegram rechazó el token" >&2; exit 1; }
echo "Bot verificado: @$(echo "$ME" | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["username"])')"

read -rp "IDs numéricos autorizados, separados por coma: " IDS
[[ "$IDS" =~ ^[0-9]+(,[0-9]+)*$ ]] || { echo "IDs no válidos (solo números separados por coma)" >&2; exit 1; }

cp -a "$CFG" "$CFG.bak.telegram"
{ grep -v '^TELEGRAM_BOT_TOKEN=' "$ENVF" || true; printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TOKEN"; } > "$ENVF.tmp"
install -m 600 -o dietpi -g dietpi "$ENVF.tmp" "$ENVF"
rm -f "$ENVF.tmp"

IDS="$IDS" python3 - <<'PY'
import json, os
p = "/home/dietpi/.nanobot/config.json"
c = json.load(open(p))
c.setdefault("channels", {})["telegram"] = {
    "enabled": True,
    "token": "${TELEGRAM_BOT_TOKEN}",
    "allow_from": os.environ["IDS"].split(","),
    "group_policy": "mention",
}
json.dump(c, open(p, "w"), indent=2, ensure_ascii=False)
PY
chown dietpi:dietpi "$CFG"
chmod 600 "$CFG"

systemctl restart nanobot
sleep 15
systemctl is-active nanobot
journalctl -u nanobot -n 40 --no-pager | grep -i telegram | tail -3 || true
echo "Listo. Escríbele al bot desde una cuenta autorizada."
echo "Para revertir: cp $CFG.bak.telegram $CFG && systemctl restart nanobot"
