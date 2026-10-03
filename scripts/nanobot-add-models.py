#!/usr/bin/env python3
"""Prueba los modelos de un proveedor YA configurado en nanobot (clave añadida desde la WebUI o
en config.json) y crea presets para /model, mostrando el motivo real de cada fallo.

Uso:  sudo nanobot-add-models PROVEEDOR [--free] [--tools] [--limit N] [--fallback N] [--models a,b,c]
  PROVEEDOR  nombre del proveedor en nanobot: openrouter, dashscope, groq, mistral, nvidia, deepseek...
  --free     (OpenRouter) solo modelos con precio cero
  --tools    solo modelos que declaran soporte de herramientas (el agente las necesita)
  --limit N  máximo de modelos a probar (por defecto 8)
  --fallback N  añade los N primeros que respondan al final de la cadena de respaldo
  --models   lista explícita de ids (en vez de leer el catálogo)
La base de la API se toma de la configuración del proveedor (útil para planes con URL propia,
p. ej. Qwen Cloud Token Plan) o de una tabla de valores por defecto.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

CFG = "/home/dietpi/.nanobot/config.json"
ENVF = "/home/dietpi/.nanobot/.env"
BASES = {
    "openrouter": "https://openrouter.ai/api/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
}


def load_env():
    env = {}
    try:
        for line in open(ENVF):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def pick(d, *names, default=None):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return default


def expand(value, env):
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), os.environ.get(m.group(1), "")), value)
    return value


def http(method, url, key, payload=None, timeout=45):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": {"message": str(e)}}


def error_text(body):
    err = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(err, dict):
        err = err.get("message") or json.dumps(err)
    return str(err)[:110]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("provider")
    ap.add_argument("--free", action="store_true")
    ap.add_argument("--tools", action="store_true")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--fallback", type=int, default=0)
    ap.add_argument("--models", default="")
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("Ejecuta con sudo")

    env = load_env()
    cfg = json.load(open(CFG))
    prov = cfg.get("providers", {}).get(args.provider)
    if not prov:
        sys.exit(f"El proveedor '{args.provider}' no está en config.json. Añádelo en la WebUI (Ajustes) primero.")
    key = expand(pick(prov, "apiKey", "api_key", default=""), env)
    if not key:
        sys.exit(f"El proveedor '{args.provider}' no tiene clave configurada.")
    base = (pick(prov, "apiBase", "api_base") or BASES.get(args.provider) or "").rstrip("/")
    if not base:
        sys.exit("No conozco la URL base de este proveedor: fíjala en su apiBase.")

    # 1) Candidatos
    ctx = {}
    if args.models:
        ids = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        code, body = http("GET", f"{base}/models", key)
        if code != 200:
            sys.exit(f"No se pudo leer el catálogo (HTTP {code}): {error_text(body)}")
        items = body.get("data", [])
        cand = []
        for m in items:
            mid = m.get("id", "")
            pr = m.get("pricing") or {}
            if args.free and not (mid.endswith(":free") or (pr.get("prompt") in ("0", 0) and pr.get("completion") in ("0", 0) and pr)):
                continue
            sp = m.get("supported_parameters")
            if args.tools and sp is not None and "tools" not in sp:
                continue
            # descartar modelos que no son de chat de texto (imagen, voz, embeddings, seguridad)
            if re.search(r"image|tts|audio|embed|wan\d|vision-exp|guard|safety|reward|parse|rerank|whisper", mid, re.I):
                continue
            arch = (m.get("architecture") or {}).get("output_modalities")
            if arch and "text" not in arch:
                continue
            ctx[mid] = m.get("context_length") or 128000
            cand.append((ctx[mid], mid))
        ids = [mid for _, mid in sorted(cand, reverse=True)]
    ids = ids[: args.limit]
    if not ids:
        sys.exit("No hay modelos que cumplan el filtro.")

    # 2) Probar
    print(f"Probando {len(ids)} modelos de '{args.provider}' en {base}")
    working = []
    for mid in ids:
        t0 = time.time()
        code, body = http("POST", f"{base}/chat/completions", key, {
            "model": mid, "max_tokens": 128,
            "messages": [{"role": "user", "content": "Responde solo: hola"}]})
        dt = time.time() - t0
        msg = None
        try:
            m = body["choices"][0]["message"]
            if m.get("content") or m.get("reasoning_content") or m.get("reasoning"):
                msg = "ok"
        except Exception:  # noqa: BLE001
            pass
        if code == 401:
            sys.exit("La clave fue rechazada (401). Revísala en la WebUI.")
        if msg:
            print(f"  OK    {mid}  ({dt:.1f}s)")
            working.append(mid)
        else:
            print(f"  falla {mid} -> HTTP {code} {error_text(body)}")
    if not working:
        sys.exit("Ningún modelo respondió; no se cambia nada.")

    # 3) Presets y respaldo
    shutil.copy2(CFG, CFG + ".bak.addmodels")
    pkey = "modelPresets" if "modelPresets" in cfg or "model_presets" not in cfg else "model_presets"
    presets = cfg.setdefault(pkey, {})
    added = []
    for mid in working:
        slug = re.sub(r"[^a-z0-9]+", "-", mid.split("/")[-1].replace(":free", "").lower()).strip("-")[:16]
        name = f"{args.provider[:3]}-{slug}"
        presets[name] = {"model": mid, "provider": args.provider, "maxTokens": 8192,
                         "contextWindowTokens": min(int(ctx.get(mid, 128000)), 1000000), "temperature": 0.1}
        added.append(name)
    if args.fallback:
        d = cfg["agents"]["defaults"]
        fkey = "fallbackModels" if "fallbackModels" in d or "fallback_models" not in d else "fallback_models"
        fb = d.setdefault(fkey, [])
        for mid in working[: args.fallback]:
            if not any(f.get("model") == mid and f.get("provider") == args.provider for f in fb):
                fb.append({"model": mid, "provider": args.provider})
    json.dump(cfg, open(CFG, "w"), indent=2, ensure_ascii=False)
    shutil.chown(CFG, "dietpi", "dietpi")
    os.chmod(CFG, 0o600)
    print("Presets creados:", ", ".join(added))
    if args.fallback:
        print(f"Añadidos {min(args.fallback, len(working))} al final de la cadena de respaldo.")
    subprocess.run(["systemctl", "restart", "nanobot"])
    print("nanobot reiniciado. En Telegram: /model para ver los presets, /model NOMBRE para cambiar.")
    print(f"Para revertir: cp {CFG}.bak.addmodels {CFG} && systemctl restart nanobot")


if __name__ == "__main__":
    main()
