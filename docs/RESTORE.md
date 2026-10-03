# Restoring an encrypted backup

🇬🇧 English · 🇪🇸 [Español](RESTAURAR.md)

Every day the Pi encrypts its configuration with [age](https://age-encryption.org) and uploads it to a private GitHub repository. Only the **public** key exists on the Pi; the **private** one (and its passphrase) is kept elsewhere, in your password manager or your keys folder. Without it the backups cannot be opened, not even from GitHub.

This guide uses placeholders in `< >`: replace them with your real values. Do not write them into the repository.

## What you need

- The private key the backup was encrypted for (`<PRIVATE_KEY_PATH>`) and its passphrase, if it has one.
- The backups repository: `<USER>/<BACKUPS_REPO>`.
- `age` and `tar` installed:
  - Windows (PowerShell): `winget install FiloSottile.age` (`tar` is already included).
  - Debian / Raspberry Pi OS / DietPi: `sudo apt install age`.
  - macOS: `brew install age`.

## 1. Download the encrypted file

```bash
gh repo clone <USER>/<BACKUPS_REPO>
cd <BACKUPS_REPO>
ls
```

You will see files named `YYYYMMDD-HHMMSS.tar.age`. Pick the most recent one (or the date you need).

## 2. Decrypt

```bash
age -d -i <PRIVATE_KEY_PATH> -o backup.tar <FILE>.tar.age
```

- If the key is an SSH key with a passphrase, `age` will ask for it.
- Always use `-o`. In PowerShell, redirecting with `>` corrupts binary data.
- If it says `no identity matched any of the recipients`, the key is not the one used to encrypt that file (for example it predates a key change).

## 3. Extract

```bash
tar -xf backup.tar
```

A folder is created with these files:

| File | Contents |
|---|---|
| `pi-hole_..._teleporter_....zip` | Pi-hole settings: lists, DHCP, local DNS, groups |
| `config.tar.gz` | System and service configuration (see below) |
| `root.crontab` | Root's scheduled tasks |
| `paquetes.txt` | List of installed packages |
| `sistema.txt` | System version, Pi-hole version and network addresses |

`config.tar.gz` stores absolute paths without the leading slash: the dashboard and its services, environment files with credentials, boot and network configuration, unbound, SSH, Syncthing's identity, the scripts in `/usr/local/bin` and the AI agent configuration. **It contains secrets**: treat it like a password.

## 4. Restore on a new Pi (recommended order)

1. Install DietPi (or the base system) and Pi-hole, and connect it to the network.
2. **Pi-hole:** in the web interface, Settings → Teleporter → Import the `.zip`. From the command line: `sudo pihole-FTL --teleporter <FILE>.zip`.
3. **Configuration:** first review what will be overwritten and extract to a temporary folder:
   ```bash
   tar -tzf config.tar.gz | less
   mkdir /tmp/restore && tar -xzf config.tar.gz -C /tmp/restore
   ```
   Copy into place only what you need (for example `etc/...` and `usr/local/bin/...`), keeping permissions: files with credentials must end up `600` and owned by root.
4. **Packages:** `sudo dpkg --set-selections < paquetes.txt && sudo apt-get dselect-upgrade`.
5. **Cron:** `sudo crontab root.crontab`.
6. **Services:** `sudo systemctl daemon-reload`, enable and start the services you restored, and reboot.
7. **Check:** DNS (`dig google.com @127.0.0.1`), the dashboard, Tailscale (you will need to authenticate the device again) and Syncthing.

## 5. After restoring

- Delete what you extracted: `shred -u backup.tar` and remove the temporary folder.
- If the backup reached a machine you do not trust, rotate the credentials it contains.

## Changing or losing the encryption key

- **Lost:** existing backups cannot be recovered. Generate a new key and register its public part on the Pi (the recipients file used by `pi-backup-push.sh`). New backups will use the new key.
- **Planned change:** register the new key, run a backup, check that it decrypts with the new key, and only then delete the old key and the old files in the remote repository.
