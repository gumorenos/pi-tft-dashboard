#!/bin/bash
# Respaldo diario de la configuración del Pi hacia el SSD.
# Guarda: teleporter de Pi-hole, archivos de configuración y lista de paquetes.
# Variables opcionales: BACKUP_DIR (por defecto /mnt/ssd/backups), BACKUP_KEEP (14).
set -u

DEST_ROOT="${BACKUP_DIR:-/mnt/ssd/backups}"
KEEP="${BACKUP_KEEP:-14}"
LOG="$DEST_ROOT/backup.log"          # en el SSD: /var/log es tmpfs y se pierde al reiniciar
STAMP="$(date +%Y%m%d-%H%M%S)"
DIR="$DEST_ROOT/$STAMP"

if ! mountpoint -q /mnt/ssd; then
    echo "$(date '+%F %T') ERROR: el SSD no está montado, se cancela" >&2
    exit 1
fi

umask 077
mkdir -p "$DIR" || exit 1
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 200000 ]; then mv -f "$LOG" "$LOG.old"; fi

FAIL=0

# 1) Pi-hole (ajustes, listas, DHCP, DNS locales)
if ! ( cd "$DIR" && pihole-FTL --teleporter >/dev/null 2>&1 ) || ! ls "$DIR"/*teleporter*.zip >/dev/null 2>&1; then
    log "ERROR: teleporter de Pi-hole falló"; FAIL=1
fi

# 2) Configuración del sistema y servicios (solo lo que exista)
CANDIDATES=(
    /root/dashboard.py
    /etc/dashboard.env
    /etc/pi-alert.env
    /etc/pi-backup.env
    /etc/pi-backup.age.pub
    /etc/nanobot-ui.env
    /etc/systemd/system/dashboard.service
    /etc/systemd/system/pi-monitor.service
    /etc/systemd/system/pi-monitor.timer
    /etc/systemd/system/pi-backup.service
    /etc/systemd/system/pi-backup.timer
    /etc/systemd/system/pi-bootlog.service
    /etc/systemd/system.conf.d
    /etc/watchdog.conf
    /etc/rc.local
    /boot/firmware/config.txt
    /etc/minidlna.conf
    /etc/unbound/unbound.conf.d
    /etc/ssh/sshd_config.d
    /root/.ssh/authorized_keys
    /usr/local/bin
    /var/lib/dashboard
    /home/dietpi/.nanobot/config.json
    /home/dietpi/.nanobot/.env
    /home/dietpi/.nanobot/workspace
    /home/dietpi/nanobot-ui/main.py
    /home/dietpi/nanobot-ui/static
    /home/dietpi/pi-tools
    /mnt/dietpi_userdata/syncthing/config.xml
    /mnt/dietpi_userdata/syncthing/cert.pem
    /mnt/dietpi_userdata/syncthing/key.pem
)
REL=()
for f in "${CANDIDATES[@]}"; do [ -e "$f" ] && REL+=("${f#/}"); done
if ! tar -czf "$DIR/config.tar.gz" -C / "${REL[@]}" 2>>"$LOG"; then
    log "ERROR: tar de configuración falló"; FAIL=1
fi

crontab -l > "$DIR/root.crontab" 2>/dev/null
dpkg --get-selections > "$DIR/paquetes.txt" 2>/dev/null
{ hostname; uname -a; pihole -v 2>/dev/null; ip -br addr; } > "$DIR/sistema.txt" 2>&1

# 3) Rotación: conservar solo las últimas $KEEP copias
find "$DEST_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20??????-??????' | sort | head -n -"$KEEP" | xargs -r rm -rf

if [ "$FAIL" -eq 0 ]; then
    log "OK $STAMP ($(du -sh "$DIR" | cut -f1))"
    # Copia cifrada fuera del equipo (opcional: requiere /etc/pi-backup.env)
    if [ -f /etc/pi-backup.env ] && [ -x /usr/local/bin/pi-backup-push.sh ]; then
        /usr/local/bin/pi-backup-push.sh || /usr/local/bin/pi-notify.sh "⚠️ Respaldo no subido a GitHub" \
            "El respaldo local está bien, pero falló la subida cifrada. Revisa $LOG." high warning
    fi
    exit 0
fi
log "FALLÓ $STAMP"
exit 1
