#!/bin/bash
# Cifra el respaldo más reciente con age y lo sube a un repo privado de GitHub.
# Solo se sube ciphertext: la clave privada de age NO está en el Pi (solo la pública).
# Configuración en /etc/pi-backup.env:
#   BACKUP_REPO=git@github.com:usuario/repo-de-respaldos.git
#   BACKUP_KEY=/root/.ssh/backup_deploy        # llave de despliegue con permiso de escritura
#   BACKUP_KEEP_REMOTE=14
# Clave pública de age en /etc/pi-backup.age.pub.
set -u

ENV=/etc/pi-backup.env
# shellcheck disable=SC1090
[ -f "$ENV" ] && . "$ENV"
[ -n "${BACKUP_REPO:-}" ] || { echo "pi-backup-push: falta BACKUP_REPO en $ENV" >&2; exit 2; }
RECIPIENT_FILE=/etc/pi-backup.age.pub
[ -s "$RECIPIENT_FILE" ] || { echo "pi-backup-push: falta $RECIPIENT_FILE" >&2; exit 2; }

KEY="${BACKUP_KEY:-/root/.ssh/backup_deploy}"
KEEP="${BACKUP_KEEP_REMOTE:-14}"
SRC_ROOT="${BACKUP_DIR:-/mnt/ssd/backups}"
WORK="$SRC_ROOT/.repo"
LOG="$SRC_ROOT/backup.log"
log() { echo "$(date '+%F %T') push: $*" >> "$LOG"; }

LATEST=$(find "$SRC_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20??????-??????' | sort | tail -1)
[ -n "$LATEST" ] || { log "ERROR: no hay respaldo local que subir"; exit 1; }
NAME=$(basename "$LATEST")

export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
umask 077

if [ ! -d "$WORK/.git" ]; then
    rm -rf "$WORK"
    git clone -q "$BACKUP_REPO" "$WORK" 2>>"$LOG" || { log "ERROR: no se pudo clonar el repo"; exit 1; }
fi
cd "$WORK" || exit 1
git config user.name "pi-backup"
git config user.email "pi-backup@users.noreply.github.com"
git fetch -q origin 2>/dev/null && git reset -q --hard origin/main 2>/dev/null || true

# Cifrar: tar del directorio de respaldo -> age (solo destinatario público)
if ! tar -C "$SRC_ROOT" -cf - "$NAME" | age -R "$RECIPIENT_FILE" -o "$WORK/$NAME.tar.age"; then
    log "ERROR: falló el cifrado"; exit 1
fi

# Conservar solo las últimas $KEEP copias cifradas
ls -1 "$WORK"/20??????-??????.tar.age 2>/dev/null | sort | head -n -"$KEEP" | xargs -r rm -f

cat > "$WORK/README.md" <<'EOF'
# Respaldos cifrados del Pi

Archivos `AAAAMMDD-HHMMSS.tar.age`: cifrados con [age](https://age-encryption.org). Sin la clave privada no se pueden leer.

Restaurar (con la clave privada correspondiente al destinatario, sea clave de age o clave SSH ed25519):

    age -d -i CLAVE_PRIVADA -o respaldo.tar AAAAMMDD-HHMMSS.tar.age
    tar -xf respaldo.tar

La clave privada NO está en el Pi ni en este repo. Guárdala en un gestor de contraseñas.
EOF

git add -A
if git diff --cached --quiet; then log "sin cambios que subir"; exit 0; fi

# Historial de un solo commit: no crece y no acumula versiones antiguas
git checkout -q --orphan tmp-squash
git commit -q -m "Respaldo $NAME"
git branch -M main
if git push -q --force origin main 2>>"$LOG"; then
    log "OK $NAME subido ($(du -h "$WORK/$NAME.tar.age" | cut -f1))"
    exit 0
fi
log "ERROR: falló el push a GitHub"
exit 1
