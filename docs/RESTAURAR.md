# Restaurar un respaldo cifrado

🇬🇧 [English](RESTORE.md) · 🇪🇸 Español

Cada día el Pi cifra su configuración con [age](https://age-encryption.org) y la sube a un repositorio privado de GitHub. En el Pi solo existe la clave **pública**; la **privada** (y su passphrase) está guardada fuera, en tu gestor de contraseñas o en tu carpeta de claves. Sin ella los respaldos no se pueden abrir, ni siquiera desde GitHub.

En esta guía se usan marcadores entre `< >`: sustitúyelos por tus valores reales. No los escribas en el repositorio.

## Qué necesitas

- La clave privada con la que se cifró (`<RUTA_CLAVE_PRIVADA>`) y su passphrase, si la tiene.
- El repositorio de respaldos: `<USUARIO>/<REPO_RESPALDOS>`.
- `age` y `tar` instalados:
  - Windows (PowerShell): `winget install FiloSottile.age` (`tar` ya viene incluido).
  - Debian / Raspberry Pi OS / DietPi: `sudo apt install age`.
  - macOS: `brew install age`.

## 1. Descargar el archivo cifrado

```bash
gh repo clone <USUARIO>/<REPO_RESPALDOS>
cd <REPO_RESPALDOS>
ls
```

Verás archivos `AAAAMMDD-HHMMSS.tar.age`. Elige el más reciente (o el de la fecha que necesites).

## 2. Descifrar

```bash
age -d -i <RUTA_CLAVE_PRIVADA> -o respaldo.tar <ARCHIVO>.tar.age
```

- Si la clave es SSH con passphrase, `age` la pedirá.
- Usa siempre `-o`. En PowerShell, redirigir con `>` corrompe los datos binarios.
- Si dice `no identity matched any of the recipients`, la clave no es la que se usó al cifrar ese archivo (por ejemplo, es de antes de un cambio de clave).

## 3. Extraer

```bash
tar -xf respaldo.tar
```

Se crea una carpeta con estos archivos:

| Archivo | Contenido |
|---|---|
| `pi-hole_..._teleporter_....zip` | Ajustes de Pi-hole: listas, DHCP, DNS locales, grupos |
| `config.tar.gz` | Configuración del sistema y de los servicios (ver abajo) |
| `root.crontab` | Tareas programadas del usuario root |
| `paquetes.txt` | Lista de paquetes instalados |
| `sistema.txt` | Versión del sistema, de Pi-hole y direcciones de red |

`config.tar.gz` guarda rutas absolutas sin la barra inicial: el dashboard y sus servicios, archivos de entorno con credenciales, configuración de arranque y de red, unbound, SSH, la identidad de Syncthing, los scripts de `/usr/local/bin` y la configuración del agente de IA. **Contiene secretos**: trátalo como una contraseña.

## 4. Restaurar en un Pi nuevo (orden recomendado)

1. Instala DietPi (o el sistema base) y Pi-hole, y conéctalo a la red.
2. **Pi-hole:** en la interfaz web, Ajustes → Teleporter → Importar el `.zip`. Por línea de comandos: `sudo pihole-FTL --teleporter <ARCHIVO>.zip`.
3. **Configuración:** primero revisa qué se va a sobrescribir y extrae a una carpeta temporal:
   ```bash
   tar -tzf config.tar.gz | less
   mkdir /tmp/restaurar && tar -xzf config.tar.gz -C /tmp/restaurar
   ```
   Copia a su sitio solo lo que necesites (por ejemplo `etc/...` y `usr/local/bin/...`), respetando permisos: los archivos con credenciales deben quedar en modo `600` y de root.
4. **Paquetes:** `sudo dpkg --set-selections < paquetes.txt && sudo apt-get dselect-upgrade`.
5. **Cron:** `sudo crontab root.crontab`.
6. **Servicios:** `sudo systemctl daemon-reload`, habilita y arranca los servicios que restauraste, y reinicia.
7. **Comprueba:** DNS (`dig google.com @127.0.0.1`), el dashboard, Tailscale (habrá que volver a autenticar el equipo) y Syncthing.

## 5. Después de restaurar

- Borra lo extraído: `shred -u respaldo.tar` y elimina la carpeta temporal.
- Si el respaldo llegó a un equipo que no es de confianza, rota las credenciales que contiene.

## Cambiar o perder la clave de cifrado

- **Perdida:** los respaldos existentes no se pueden recuperar. Genera una clave nueva y registra su parte pública en el Pi (archivo de destinatarios de `pi-backup-push.sh`). Los respaldos nuevos usarán la clave nueva.
- **Cambio planificado:** registra la clave nueva, ejecuta un respaldo, comprueba que se descifra con la clave nueva y solo entonces elimina la clave vieja y los archivos antiguos del repositorio remoto.
