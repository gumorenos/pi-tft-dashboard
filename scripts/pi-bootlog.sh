#!/bin/bash
# Registra cada arranque en /var/lib/dashboard/boots.log (disco, sobrevive reinicios).
# /var/log es tmpfs en DietPi, por eso journalctl solo ve el arranque actual.
# Formato: epoch fecha wdt=<bootstatus> throttled=<flags>
# wdt distinto de 0 = el último reinicio lo provocó el watchdog de hardware.
LOG=/var/lib/dashboard/boots.log
mkdir -p "$(dirname "$LOG")"
WDT=$(cat /sys/class/watchdog/watchdog0/bootstatus 2>/dev/null || echo "-")
THR=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
echo "$(date +%s) $(date '+%F_%T') wdt=${WDT} throttled=${THR:--}" >> "$LOG"
tail -n 200 "$LOG" > "$LOG.tmp" && mv -f "$LOG.tmp" "$LOG"
