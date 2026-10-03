#!/bin/bash
LOG="/var/log/pi-resilience.log"
MAX_LOG=500000

rotate_log() {
    if [ -f "$LOG" ] && [ $(stat -c%s "$LOG") -gt $MAX_LOG ]; then
        mv "$LOG" "${LOG}.bak"
    fi
}

check_and_restart() {
    SERVICE=$1
    if ! systemctl is-active --quiet "$SERVICE"; then
        echo "$(date): $SERVICE caído, reiniciando..." >> $LOG
        systemctl restart "$SERVICE"
        sleep 3
        if systemctl is-active --quiet "$SERVICE"; then
            echo "$(date): $SERVICE recuperado OK" >> $LOG
        else
            echo "$(date): $SERVICE no se pudo recuperar" >> $LOG
        fi
    fi
}

rotate_log

# Servicios críticos — en orden de dependencia
check_and_restart pihole-FTL
check_and_restart unbound
check_and_restart tailscaled

# Verificar que Pi-hole responde DNS
if ! dig +short +time=2 google.com @127.0.0.1 > /dev/null 2>&1; then
    echo "$(date): DNS no responde, reiniciando pihole-FTL..." >> $LOG
    systemctl restart pihole-FTL
fi

# Verificar DHCP — si pihole-FTL está activo el DHCP está activo
# No hay acción adicional necesaria
