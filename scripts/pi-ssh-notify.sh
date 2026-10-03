#!/bin/bash
# Lo llama pam_exec al abrir una sesión SSH. Avisa por ntfy cuando entra cualquier usuario
# desde una IP fuera de la LAN y de Tailscale, o uno de los usuarios de SSH_NOTIFY_USERS
# (lista separada por espacios en /etc/pi-alert.env, p. ej. un agente automático).
# Activar con la línea:  session optional pam_exec.so quiet /usr/local/bin/pi-ssh-notify.sh
# en /etc/pam.d/sshd (optional: si falla, el login no se ve afectado).
[ "${PAM_TYPE:-}" = "open_session" ] || exit 0
# shellcheck disable=SC1091
[ -f /etc/pi-alert.env ] && . /etc/pi-alert.env
WHO="${PAM_USER:-?}"
FROM="${PAM_RHOST:-local}"

trusted=0
case "$FROM" in
    192.168.*|10.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*|127.*|100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*|local|"") trusted=1 ;;
esac
watched=0
case " ${SSH_NOTIFY_USERS:-} " in *" $WHO "*) watched=1 ;; esac

if [ "$watched" = 1 ] || [ "$trusted" = 0 ]; then
    [ "$FROM" = "127.0.0.1" ] && FROM="Tailscale (llega como 127.0.0.1)"
    ( /usr/local/bin/pi-notify.sh "🔑 SSH: $WHO desde $FROM" "Sesión abierta en $(hostname) a las $(date +%H:%M)." low key ) >/dev/null 2>&1 &
fi
exit 0
