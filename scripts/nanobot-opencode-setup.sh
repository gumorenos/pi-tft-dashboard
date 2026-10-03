#!/bin/bash
# Añade OpenCode Zen a nanobot usando SOLO modelos gratuitos (ids que terminan en -free).
# Uso: sudo nanobot-opencode-setup
# - Pide la API key (oculta) y prueba cada modelo gratuito mostrando el motivo si falla.
# - Si alguno responde: pasa a ser el modelo principal (respaldo: otros gratuitos + Gemini Flash).
# - Si ninguno responde (p. ej. están caídos en ese momento): deja el proveedor y los presets
#   listos (/model oc-...), pero mantiene Gemini como principal para no romper el bot.
# En Telegram se cambia de modelo con /model NOMBRE_DEL_PRESET (solo afecta a esa conversación).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Ejecuta con sudo: sudo $0" >&2; exit 1; }

ENVF=/home/dietpi/.nanobot/.env
CFG=/home/dietpi/.nanobot/config.json
BASE=https://opencode.ai/zen/v1
# Solo modelos que OpenCode sirve por /chat/completions (jev-* usa /systemone y muse-spark-* usa
# /responses: nanobot no los soporta con este proveedor). big-pickle es gratuito pero no termina en -free.
PREFERRED="nemotron-3-ultra-free mimo-v2.6-flash-free nemotron-3.5-lightning-free big-pickle mimo-v2.5-free space-bunny-free longcat-2.5-preview-free ling-3.0-flash-fin-free"
# Nota: OpenCode está migrando de API keys (sk-...) a un login OAuth de consola. Las claves sk-
# existentes siguen funcionando; las cuentas nuevas pueden no poder crear una.

read -rsp "API key de OpenCode Zen (no se muestra): " KEY; echo
[ -n "$KEY" ] || { echo "Clave vacía" >&2; exit 1; }

# La lista de modelos es pública: no valida la clave. La validación real es la primera llamada.
FREE=$(curl -fsS -m 15 "$BASE/models" | python3 -c "import sys,json;print(' '.join(m['id'] for m in json.load(sys.stdin)['data'] if m['id'].endswith('-free')))") \
    || { echo "No se pudo leer el catálogo de OpenCode" >&2; exit 1; }
[ -n "$FREE" ] || { echo "No hay modelos gratuitos en el catálogo" >&2; exit 1; }

ORDERED=""
ALL=$(curl -fsS -m 15 "$BASE/models" | python3 -c "import sys,json;print(' '.join(m['id'] for m in json.load(sys.stdin)['data']))")
for m in $PREFERRED; do case " $ALL " in *" $m "*) ORDERED="$ORDERED $m" ;; esac; done

probe() {   # probe MODELO -> imprime "OK" o "ERR: motivo"; devuelve el HTTP en $CODE
    local resp body
    resp=$(curl -s -m 45 -w '\n%{http_code}' "$BASE/chat/completions" \
        -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
        -d "{\"model\":\"$1\",\"messages\":[{\"role\":\"user\",\"content\":\"Responde solo: hola\"}],\"max_tokens\":256}" || true)
    CODE=$(echo "$resp" | tail -1)
    body=$(echo "$resp" | head -n -1)
    BODY="$body" CODE_="$CODE" python3 - <<'PY'
import json, os
code, body = os.environ["CODE_"], os.environ["BODY"]
try:
    d = json.loads(body)
    m = d["choices"][0]["message"]
    print("OK" if (m.get("content") or m.get("reasoning_content") or m.get("reasoning")) else "ERR: respuesta vacía")
except Exception:
    try:
        e = json.loads(body).get("error", {})
        msg = e.get("message", body) if isinstance(e, dict) else str(e)
    except Exception:
        msg = body
    print(f"ERR: HTTP {code} {str(msg)[:110]}")
PY
}

echo "Modelos gratuitos (probando):"
WORKING=""
for m in $ORDERED; do
    CODE=""
    R=$(probe "$m")
    if [ "$CODE" = "401" ]; then
        echo "La clave fue rechazada por OpenCode (401). Revisa que la copiaste completa." >&2
        exit 1
    fi
    if [ "$R" = "OK" ]; then echo "  OK    $m"; WORKING="$WORKING $m"; else echo "  falla $m -> ${R#ERR: }"; fi
done

cp -a "$CFG" "$CFG.bak.opencode"
{ grep -v '^OPENCODE_API_KEY=' "$ENVF" || true; printf 'OPENCODE_API_KEY=%s\n' "$KEY"; } > "$ENVF.tmp"
install -m 600 -o dietpi -g dietpi "$ENVF.tmp" "$ENVF"
rm -f "$ENVF.tmp"

WORKING="$WORKING" ORDERED="$ORDERED" python3 - <<'PY'
import json, os, re
p = "/home/dietpi/.nanobot/config.json"
c = json.load(open(p))
working = os.environ["WORKING"].split()
ordered = os.environ["ORDERED"].split()
c.setdefault("providers", {})["opencode"] = {"api_key": "${OPENCODE_API_KEY}"}

# Presets para /model (funcionan o no, para poder probarlos desde Telegram)
presets = c.setdefault("model_presets", {})
for m in (working or ordered[:4]):
    name = "oc-" + re.sub(r"-free$", "", re.sub(r"[^a-z0-9]+", "-", m.lower())).strip("-")
    presets[name] = {"model": m, "provider": "opencode", "max_tokens": 8192,
                     "context_window_tokens": 128000, "temperature": 0.1}

d = c["agents"]["defaults"]
if working:
    d["model"], d["provider"] = working[0], "opencode"
    fb = [{"model": m, "provider": "opencode"} for m in working[1:3]]
    fb.append({"model": "models/gemini-3.8-flash", "provider": "gemini"})
    d["fallback_models"] = fb
    print("Principal:", d["model"], "| respaldo:", [f["model"] for f in fb])
else:
    print("Ningún modelo respondió: Gemini sigue como principal; presets creados:", [n for n in presets if n.startswith("oc-")])
json.dump(c, open(p, "w"), indent=2, ensure_ascii=False)
PY
chown dietpi:dietpi "$CFG"
chmod 600 "$CFG"

systemctl restart nanobot
sleep 20
systemctl is-active nanobot
echo "Listo. En Telegram: /model muestra los presets y /model NOMBRE cambia el modelo de esa conversación."
echo "Nota: los modelos gratuitos de OpenCode pueden usar tus conversaciones para mejorar sus modelos."
echo "Para revertir: cp $CFG.bak.opencode $CFG && systemctl restart nanobot"
