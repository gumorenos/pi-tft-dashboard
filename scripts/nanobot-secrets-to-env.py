#!/usr/bin/env python3
"""Mueve los secretos en texto plano de config.json de nanobot a su .env y deja referencias ${VAR}.

La WebUI guarda las claves de proveedores tal cual en config.json; así cualquier auditoría o copia
del archivo las expone. Con ${VAR}, config.json queda legible sin secretos (Gemini, Telegram y el
servidor MCP ya funcionan así).

Uso:  sudo nanobot-secrets-to-env [--dry-run]
Recorre providers.* y channels.* (claves apiKey/token/secret/password). Es idempotente.
"""
import json
import os
import re
import shutil
import subprocess
import sys

CFG = "/home/dietpi/.nanobot/config.json"
ENVF = "/home/dietpi/.nanobot/.env"
SECRET_KEY = re.compile(r"^(api_?key|token|token_issue_secret|secret|password)$", re.I)


def snake_upper(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).replace("-", "_").upper()


def env_name(section: str, owner: str, key: str) -> str:
    if section == "providers":
        return f"{snake_upper(owner)}_API_KEY"
    return f"{snake_upper(owner)}_{snake_upper(key)}"


def main():
    dry = "--dry-run" in sys.argv
    if os.geteuid() != 0 and not dry:
        sys.exit("Ejecuta con sudo")
    cfg = json.load(open(CFG))
    env_lines = open(ENVF).read().splitlines() if os.path.exists(ENVF) else []
    env_names = {line.split("=", 1)[0] for line in env_lines if "=" in line}

    moves = []
    for section in ("providers", "channels"):
        for owner, body in (cfg.get(section) or {}).items():
            if not isinstance(body, dict):
                continue
            for key, value in body.items():
                if SECRET_KEY.match(key) and isinstance(value, str) and value and not value.startswith("${"):
                    moves.append((section, owner, key, value, env_name(section, owner, key)))

    if not moves:
        print("No hay secretos en texto plano en providers/channels.")
        return
    for section, owner, key, value, name in moves:
        print(f"  {section}.{owner}.{key} ({value[:4]}...) -> ${{{name}}}")
    if dry:
        print("(simulacro: no se cambió nada)")
        return

    shutil.copy2(CFG, CFG + ".bak.secrets")
    shutil.copy2(ENVF, ENVF + ".bak.secrets")
    for section, owner, key, value, name in moves:
        if name in env_names:
            sys.exit(f"{name} ya existe en .env con otro valor; revisa a mano antes de continuar.")
        env_lines.append(f"{name}={value}")
        env_names.add(name)
        cfg[section][owner][key] = "${" + name + "}"

    with open(ENVF, "w") as f:
        f.write("\n".join(env_lines) + "\n")
    shutil.chown(ENVF, "dietpi", "dietpi")
    os.chmod(ENVF, 0o600)
    json.dump(cfg, open(CFG, "w"), indent=2, ensure_ascii=False)
    shutil.chown(CFG, "dietpi", "dietpi")
    os.chmod(CFG, 0o600)
    print(f"{len(moves)} secretos movidos a {ENVF}. Respaldos: *.bak.secrets")
    subprocess.run(["systemctl", "restart", "nanobot"])
    print("nanobot reiniciado.")


if __name__ == "__main__":
    main()
