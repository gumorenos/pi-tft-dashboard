#!/bin/bash
# Monitor ligero (se ejecuta cada minuto con pi-monitor.timer).
# Avisa por ntfy cuando algo cae (tras 2 fallos seguidos) y cuando se recupera.
# Si el aviso no se pudo entregar (p. ej. sin internet), avisa después de que se resuelva.
STATE=/run/pi-monitor
BOOTS=/var/lib/dashboard/boots.log
BOOT_MARK=/var/lib/dashboard/boot_notified
HEALTH=/var/lib/dashboard/health.log
NOTIFY=/usr/local/bin/pi-notify.sh
FAILS_TO_ALERT=2
TEMP_MAX=75
DISK_MAX=90

# Reinicio de último recurso: si el DNS local lleva RESCUE_AFTER segundos caído (y el fallo
# es del Pi, no del proveedor), reiniciar. Protecciones contra bucles de reinicio:
# uptime mínimo y un solo reinicio de rescate cada RESCUE_COOLDOWN segundos.
PIHOLE_DNS="${PIHOLE_DNS:-127.0.0.1}"
RESCUE_AFTER="${RESCUE_AFTER:-900}"             # 15 min
RESCUE_MIN_UPTIME="${RESCUE_MIN_UPTIME:-1800}"  # 30 min
RESCUE_COOLDOWN="${RESCUE_COOLDOWN:-21600}"     # 6 h
RESCUE_MARK=/var/lib/dashboard/rescue_reboot
RESCUE_REASON=/var/lib/dashboard/rescue_reason

mkdir -p "$STATE"

# check <id> <ok:1|0> <título> <detalle>
check() {
    local id="$1" ok="$2" title="$3" detail="$4"
    local fails="$STATE/$id.fails" alerted="$STATE/$id.alerted" since="$STATE/$id.since"
    local n mins
    if [ "$ok" = 1 ]; then
        n=$(cat "$fails" 2>/dev/null || echo 0)
        mins=$(( ( $(date +%s) - $(cat "$since" 2>/dev/null || date +%s) ) / 60 ))
        if [ -f "$alerted" ]; then
            "$NOTIFY" "✅ Recuperado: $title" "Volvió a la normalidad tras ~${mins} min." default white_check_mark && rm -f "$alerted"
        elif [ "$n" -ge "$FAILS_TO_ALERT" ]; then
            "$NOTIFY" "ℹ️ Ya resuelto: $title" "Duró ~${mins} min y no se pudo avisar antes." low information_source
        fi
        rm -f "$fails" "$since"
        return
    fi
    n=$(( $(cat "$fails" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$fails"
    [ -f "$since" ] || date +%s > "$since"
    if [ "$n" -ge "$FAILS_TO_ALERT" ] && [ ! -f "$alerted" ]; then
        "$NOTIFY" "⚠️ $title" "$detail" high warning && touch "$alerted"
    fi
}

# --- Red y DNS ---------------------------------------------------------------
# dig +short imprime los errores de timeout en stdout: se descartan las líneas que empiezan por ";"
ok=0; dig +short +time=2 +tries=1 google.com @"$PIHOLE_DNS" 2>/dev/null | grep -v '^;' | grep -q . && ok=1
dns_ok=$ok
check dns "$ok" "Pi-hole no resuelve DNS" "El DNS local ($PIHOLE_DNS) no responde. Los equipos de la casa pueden quedarse sin internet."

ok=0; { ping -c1 -W2 1.1.1.1 >/dev/null 2>&1 || ping -c1 -W2 8.8.8.8 >/dev/null 2>&1; } && ok=1
inet_ok=$ok
check internet "$ok" "Sin internet" "El Pi no alcanza 1.1.1.1 ni 8.8.8.8. Revisa el módem o el proveedor."

# --- Reinicio de último recurso -----------------------------------------------
# Problema = Pi-hole no contesta en absoluto (timeout) o no resuelve aunque hay internet.
# Si el proveedor está caído (sin internet) NO se reinicia: no es culpa del Pi.
problem=0
if ! dig +time=2 +tries=1 pi.hole @"$PIHOLE_DNS" 2>/dev/null | grep -q "status:"; then
    problem=1; why="Pi-hole no contesta consultas"
elif [ "$dns_ok" = 0 ] && [ "$inet_ok" = 1 ]; then
    problem=1; why="Pi-hole no resuelve nombres aunque hay internet"
fi
rescue_since="$STATE/rescue.since"
if [ "$problem" = 0 ]; then
    rm -f "$rescue_since"
else
    [ -f "$rescue_since" ] || date +%s > "$rescue_since"
    now=$(date +%s)
    up=$(cut -d. -f1 /proc/uptime)
    last=$(cat "$RESCUE_MARK" 2>/dev/null || echo 0)
    if [ $((now - $(cat "$rescue_since"))) -ge "$RESCUE_AFTER" ] \
        && [ "$up" -ge "$RESCUE_MIN_UPTIME" ] \
        && [ $((now - last)) -ge "$RESCUE_COOLDOWN" ]; then
        echo "$now" > "$RESCUE_MARK"
        echo "$why durante más de $((RESCUE_AFTER / 60)) min" > "$RESCUE_REASON"
        if [ -n "${RESCUE_DRYRUN:-}" ]; then
            "$NOTIFY" "🧪 (simulacro) Reiniciaría el Pi" "$why durante más de $((RESCUE_AFTER / 60)) min. No se reinicia: modo simulacro." low test_tube
            rm -f "$RESCUE_MARK" "$RESCUE_REASON" "$rescue_since"
        else
            "$NOTIFY" "🚨 Reiniciando el Pi (último recurso)" "$why durante más de $((RESCUE_AFTER / 60)) min y reiniciar servicios no bastó." urgent rotating_light
            sync
            systemctl reboot
        fi
    fi
fi

# --- Servicios ---------------------------------------------------------------
for s in pihole-FTL unbound tailscaled syncthing dashboard; do
    ok=0; systemctl is-active --quiet "$s" && ok=1
    check "svc-$s" "$ok" "Servicio caído: $s" "El servicio $s no está activo (el script de resiliencia intenta reiniciarlo)."
done

# --- Hardware ----------------------------------------------------------------
t=$(( $(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo 0) / 1000 ))
ok=1; [ "$t" -ge "$TEMP_MAX" ] && ok=0
check temp "$ok" "Temperatura alta" "CPU a ${t} °C (límite ${TEMP_MAX} °C). Revisa ventilación."

# Subtensión (vcgencmd get_throttled): bits 0/16 = voltaje bajo ahora / desde el arranque. El bit
# "desde el arranque" se mantiene hasta reiniciar, así que el aviso sale una vez por arranque.
# La limitación por temperatura (bits 1/17/3/19) es normal en una 3B+ y no avisa.
thr=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
if [ -n "$thr" ]; then
    ok=1; [ $(( thr & 0x10001 )) -ne 0 ] && ok=0
    check undervolt "$ok" "Voltaje bajo en la alimentación" "El Pi detectó subtensión (get_throttled=$thr). Revisa la fuente y el cable USB: es una causa típica de reinicios sin explicación."
    # Historial en disco (sobrevive reinicios): una línea por cada cambio de get_throttled
    if [ "$thr" != "$(cat "$STATE/throttled.last" 2>/dev/null)" ]; then
        echo "$(date '+%F_%T') throttled=$thr temp=${t}C" >> "$HEALTH"
        echo "$thr" > "$STATE/throttled.last"
        tail -n 200 "$HEALTH" > "$HEALTH.tmp" && mv -f "$HEALTH.tmp" "$HEALTH"
    fi
fi

pct=$(df --output=pcent / | tail -1 | tr -dc '0-9')
ok=1; [ "${pct:-0}" -ge "$DISK_MAX" ] && ok=0
check disk-root "$ok" "Tarjeta SD casi llena" "La partición raíz está al ${pct}%."

if mountpoint -q /mnt/ssd; then
    pct=$(df --output=pcent /mnt/ssd | tail -1 | tr -dc '0-9')
    ok=1; [ "${pct:-0}" -ge "$DISK_MAX" ] && ok=0
    check disk-ssd "$ok" "SSD casi lleno" "El SSD está al ${pct}%."
    ok=0; find /mnt/ssd/backups -mindepth 1 -maxdepth 1 -type d -name '20??????-??????' -mmin -3000 2>/dev/null | grep -q . && ok=1
    # el respaldo solo es exigible si el Pi lleva más de 2 días encendido o ya existe alguno
    [ "$ok" = 0 ] && ! find /mnt/ssd/backups -mindepth 1 -maxdepth 1 -type d -name '20??????-??????' 2>/dev/null | grep -q . && [ "$(cut -d. -f1 /proc/uptime)" -lt 172800 ] && ok=1
    check backup "$ok" "Respaldo atrasado" "No hay un respaldo de las últimas 50 horas en /mnt/ssd/backups."
else
    check disk-ssd 0 "SSD no montado" "/mnt/ssd no está montado."
fi

# --- Reinicio detectado ------------------------------------------------------
if [ -s "$BOOTS" ]; then
    last=$(tail -n1 "$BOOTS")
    ep=${last%% *}
    done_ep=$(cat "$BOOT_MARK" 2>/dev/null)
    if [ -z "$done_ep" ]; then
        echo "$ep" > "$BOOT_MARK"          # primera ejecución: solo registrar
    elif [ "$ep" != "$done_ep" ]; then
        hora=$(date -d "@$ep" '+%H:%M')
        cause="corte de luz, reinicio manual o botón"
        case "$last" in *"wdt=0 "*) ;; *"wdt=-"*) ;; *) cause="reinicio forzado por el watchdog de hardware" ;; esac
        if [ -s "$RESCUE_REASON" ]; then
            cause="reinicio de rescate automático ($(cat "$RESCUE_REASON"))"
            rm -f "$RESCUE_REASON"
        fi
        "$NOTIFY" "🔄 El Pi se reinició" "Arrancó a las ${hora}. Causa probable: ${cause}. Detalle: ${last#* }" default arrows_counterclockwise && echo "$ep" > "$BOOT_MARK"
    fi
fi
exit 0
