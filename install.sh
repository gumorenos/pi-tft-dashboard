#!/bin/bash
# Instalador del dashboard TFT. Uso: sudo ./install.sh [--backup] [--bootlog] [--watchdog] [--all]
#   (sin opciones)  dashboard + servicio
#   --backup        respaldo diario a /mnt/ssd/backups (timer systemd)
#   --bootlog       registro de arranques en /var/lib/dashboard/boots.log
#   --watchdog      watchdog de hardware vía systemd (seguro: sin pruebas de servicios)
#   --alerts        avisos push por ntfy (monitor cada minuto; requiere --bootlog para avisar reinicios)
#   --journal       journal de systemd en disco (en DietPi /var/log es tmpfs); activo tras reiniciar
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Ejecuta como root: sudo $0" >&2; exit 1; }
cd "$(dirname "$0")"

BACKUP=0; BOOTLOG=0; WATCHDOG=0; ALERTS=0; JOURNAL=0
for a in "$@"; do
    case "$a" in
        --backup) BACKUP=1 ;;
        --bootlog) BOOTLOG=1 ;;
        --watchdog) WATCHDOG=1 ;;
        --alerts) ALERTS=1 ;;
        --journal) JOURNAL=1 ;;
        --all) BACKUP=1; BOOTLOG=1; WATCHDOG=1; ALERTS=1; JOURNAL=1 ;;
        *) echo "Opción desconocida: $a" >&2; exit 1 ;;
    esac
done

echo "== Dependencias"
apt-get install -y python3-pil python3-psutil python3-evdev python3-numpy fonts-dejavu-core

echo "== Dashboard"
install -m 755 dashboard.py /root/dashboard.py
if [ ! -f /etc/dashboard.env ]; then
    install -m 600 -o root -g root dashboard.env.example /etc/dashboard.env
    echo "   Creado /etc/dashboard.env: edítalo con tus claves y luego: systemctl restart dashboard"
fi
install -m 644 dashboard.service.example /etc/systemd/system/dashboard.service

if [ "$BOOTLOG" -eq 1 ]; then
    echo "== Registro de arranques"
    install -m 755 scripts/pi-bootlog.sh /usr/local/bin/pi-bootlog.sh
    install -m 644 systemd/pi-bootlog.service /etc/systemd/system/pi-bootlog.service
    systemctl daemon-reload
    systemctl enable pi-bootlog.service
fi

if [ "$BACKUP" -eq 1 ]; then
    echo "== Respaldo diario"
    install -m 755 scripts/pi-backup.sh /usr/local/bin/pi-backup.sh
    install -m 644 systemd/pi-backup.service systemd/pi-backup.timer /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now pi-backup.timer
fi

if [ "$ALERTS" -eq 1 ]; then
    echo "== Alertas ntfy"
    install -m 755 scripts/pi-notify.sh scripts/pi-monitor.sh /usr/local/bin/
    install -m 644 systemd/pi-monitor.service systemd/pi-monitor.timer /etc/systemd/system/
    if [ ! -f /etc/pi-alert.env ]; then
        install -m 600 -o root -g root pi-alert.env.example /etc/pi-alert.env
        sed -i "s/^NTFY_TOPIC=.*/NTFY_TOPIC=pi-alert-$(openssl rand -hex 12)/" /etc/pi-alert.env
        echo "   Tema generado en /etc/pi-alert.env. Suscríbete a él en la app ntfy:"
        grep '^NTFY_TOPIC=' /etc/pi-alert.env
    fi
    systemctl daemon-reload
    systemctl enable --now pi-monitor.timer
    /usr/local/bin/pi-notify.sh "Alertas activadas" "El monitor del Pi está funcionando." default white_check_mark || echo "   (no se pudo enviar el aviso de prueba)"
fi

if [ "$JOURNAL" -eq 1 ]; then
    echo "== Journal persistente"
    # /var/log es tmpfs en DietPi: el journal vive en /var/lib/journal-disk y se monta encima.
    mkdir -p /var/lib/journal-disk /etc/systemd/journald.conf.d
    install -m 644 systemd/journald-persistente.conf /etc/systemd/journald.conf.d/50-persistente.conf
    # nofail: si el montaje fallara, el Pi arranca igual (con el journal en RAM como antes)
    grep -q ' /var/log/journal ' /etc/fstab || \
        echo '/var/lib/journal-disk /var/log/journal none bind,nofail,x-systemd.mkdir 0 0' >> /etc/fstab
    systemctl daemon-reload
    echo "   Se activa en el próximo reinicio (journalctl --list-boots mostrará los arranques anteriores)."
fi

if [ "$WATCHDOG" -eq 1 ]; then
    echo "== Watchdog de hardware (systemd)"
    grep -q '^dtparam=watchdog=on' /boot/firmware/config.txt 2>/dev/null || echo 'dtparam=watchdog=on' >> /boot/firmware/config.txt
    # Evita dos procesos usando /dev/watchdog
    systemctl disable --now watchdog.service 2>/dev/null || true
    mkdir -p /etc/systemd/system.conf.d
    install -m 644 systemd/watchdog.conf /etc/systemd/system.conf.d/watchdog.conf
    systemctl daemon-reexec
fi

systemctl daemon-reload
systemctl enable dashboard
echo "Listo. Inicia con: systemctl restart dashboard   (logs: journalctl -u dashboard -f)"
