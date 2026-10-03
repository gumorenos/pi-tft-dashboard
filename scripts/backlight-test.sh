#!/bin/bash
# Prueba guiada para saber qué control apaga físicamente el backlight de la pantalla TFT.
# Uso (con la pantalla a la vista):  sudo backlight-test
# Cada etapa dura 12 s y dice en pantalla qué se está apagando; al terminar todo se restaura.
#   1) GPIO18 en bajo (el que usa el dashboard)
#   2) backlight del kernel (/sys/class/backlight, GPIO22 en esta pantalla)
#   3) ambos
[ "$(id -u)" -eq 0 ] || { echo "Ejecuta con sudo" >&2; exit 1; }

BL=/sys/class/backlight/fb_ili9486/bl_power

restore() {
    pinctrl set 18 op dh 2>/dev/null
    [ -w "$BL" ] && echo 0 > "$BL"
}
trap restore EXIT

stage() {   # stage "titulo" comando_apagar
    echo
    echo "[$(date +%T)] ETAPA $1: $2 -> mira la pantalla (12 s)"
    eval "$3"
    sleep 12
    restore
    echo "[$(date +%T)] restaurado (5 s de pausa)"
    sleep 5
}

echo "Preparado. Todo debería estar ENCENDIDO ahora."
restore
sleep 4
stage 1 "GPIO18 en BAJO"                          "pinctrl set 18 op dl"
stage 2 "backlight del kernel APAGADO (bl_power)" "echo 4 > $BL"
stage 3 "AMBOS apagados"                          "pinctrl set 18 op dl; echo 4 > $BL"
echo
echo "Fin. Dime en qué etapa(s) se apagó la pantalla: 1, 2, 3 o ninguna."
