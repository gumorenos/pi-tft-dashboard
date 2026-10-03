#!/usr/bin/env python3
"""TFT Dashboard — Raspberry Pi 3B+, DietPi Trixie, /dev/fb1, 480×320
Renderizado directo al framebuffer vía mmap + PIL.
Sin pygame, sin SDL, sin X11, sin Tkinter.

Copyright (C) 2026 gumorenos. Software libre bajo la GNU GPL v3: ver el archivo LICENSE.
"""

import os
import sys
import time
import socket
import subprocess
import signal
import json
import mmap
import traceback
import urllib.request
import urllib.error
import datetime
import threading
import math
from collections import deque
from functools import lru_cache

import psutil
from PIL import Image, ImageDraw, ImageFont

try:
    import numpy as np   # acelera la conversión RGB565 y permite escribir solo lo que cambió
except ImportError:
    np = None

# ── Zona horaria ──────────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("DASHBOARD_TZ", "America/Lima"))
except Exception:
    TZ = None

# ── Idioma de la pantalla (DASHBOARD_LANG=es|en) ──────────────────────────────
# Screen language. Texts of the TFT UI live here; logs and code comments stay in Spanish.
LANG = os.environ.get("DASHBOARD_LANG", "es").strip().lower()[:2]
if LANG not in ("es", "en"):
    LANG = "es"

# Mascota de la página 1 (DASHBOARD_PET). Pet drawn on page 1; unknown values fall back to the beagle.
PET = os.environ.get("DASHBOARD_PET", "beagle").strip().lower()
if PET not in ("beagle", "shepherd", "tabby", "white-cat", "none"):
    PET = "beagle"

STRINGS = {
    "es": {
        "btn_pihole": "MANTENER: Reiniciar Pi-hole",
        "btn_reboot": "MANTENER: Reiniciar Raspberry Pi",
        "graph_wait": "juntando muestras",
        "age_none": "sin actualizar", "age_lt1": "hace <1 min",
        "age_min": "hace {n} min", "age_h": "hace {n} h",
        "volt_low": "VOLTAJE BAJO", "cpu_lim": "CPU LIMITADA",
        "bk_err": "RESPALDO: error", "bk_none": "RESPALDO: ninguno",
        "bk_h": "RESPALDO hace {n}h", "bk_d": "RESPALDO hace {n}d",
        "dias": ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"],
        "meses": ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"],
        "weather_na": "Clima: no disponible", "owm_lang": "es",
        "lbl_lan": "RED LOCAL", "getting": "Obteniendo...",
        "ts_off": "DESCONECTADO", "down": "✗ CAÍDO", "ph_down": "CAÍDO",
        "lbl_reboots": "REINICIOS 24H", "ssd_off": "No montado",
        "reboot_in": "Reiniciando en {n}...",
        "no_data": "sin datos",
        "night_title": "Pantalla negra de noche", "night_range": "de {a:02d}:00 - {b:02d}:00",
        "yes": "SÍ", "no": "NO",
        "ph_summary": "{n} consultas · {p:.0f}% bloq", "no_conn": "sin conexión",
        "ph_on": "ON | {b}blq | {p:.1f}%",
        "top_clients": "TOP CLIENTES", "top_blocked": "TOP BLOQUEADOS",
        "ph_restarted": "Pi-hole reiniciado ✓", "ph_err": "Error reiniciando Pi-hole",
    },
    "en": {
        "btn_pihole": "HOLD: Restart Pi-hole",
        "btn_reboot": "HOLD: Reboot Raspberry Pi",
        "graph_wait": "collecting samples",
        "age_none": "not updated", "age_lt1": "<1 min ago",
        "age_min": "{n} min ago", "age_h": "{n} h ago",
        "volt_low": "LOW VOLTAGE", "cpu_lim": "CPU THROTTLED",
        "bk_err": "BACKUP: error", "bk_none": "BACKUP: none",
        "bk_h": "BACKUP {n}h ago", "bk_d": "BACKUP {n}d ago",
        "dias": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "meses": ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        "weather_na": "Weather: unavailable", "owm_lang": "en",
        "lbl_lan": "LOCAL NET", "getting": "Loading...",
        "ts_off": "DISCONNECTED", "down": "✗ DOWN", "ph_down": "DOWN",
        "lbl_reboots": "REBOOTS 24H", "ssd_off": "Not mounted",
        "reboot_in": "Rebooting in {n}...",
        "no_data": "no data",
        "night_title": "Black screen at night", "night_range": "{a:02d}:00 - {b:02d}:00",
        "yes": "YES", "no": "NO",
        "ph_summary": "{n} queries · {p:.0f}% blocked", "no_conn": "offline",
        "ph_on": "ON | {b} blk | {p:.1f}%",
        "top_clients": "TOP CLIENTS", "top_blocked": "TOP BLOCKED",
        "ph_restarted": "Pi-hole restarted ✓", "ph_err": "Error restarting Pi-hole",
    },
}


def tr(key, **kw):
    """Texto de la UI en el idioma configurado (con .format si se pasan valores)."""
    s = STRINGS[LANG][key]
    return s.format(**kw) if kw else s

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE = "/var/log/dashboard.log"

def log_error(msg):
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 500_000:
            os.rename(LOG_FILE, LOG_FILE + ".bak")
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} ERROR: {msg}\n")
    except Exception:
        pass

# ── Señales ───────────────────────────────────────────────────────────────────
_renderer = None   # referencia global para cierre limpio

def cleanup(sig, frame):
    global _renderer
    if _renderer:
        try:
            _renderer.close()
        except Exception:
            pass
    sys.exit(0)

signal.signal(signal.SIGTERM, cleanup)
signal.signal(signal.SIGINT,  cleanup)

# ── Touch resistivo ───────────────────────────────────────────────────────────
TOUCH_EVENT = threading.Event()   # seteado por _touch_thread, consumido por main()
TOUCH_LOCK  = threading.Lock()

# Calibracion ADS7846 -> pantalla 480x320. Ajustar estos limites si el boton
# queda desplazado; el driver reporta raw 0..4095 en ambos ejes.
TOUCH_RAW_X_MIN = 0
TOUCH_RAW_X_MAX = 4095
TOUCH_RAW_Y_MIN = 0
TOUCH_RAW_Y_MAX = 4095
TOUCH_SWAP_XY   = False
TOUCH_INVERT_X  = False
TOUCH_INVERT_Y  = True
TOUCH_X_OFFSET  = 0
TOUCH_Y_OFFSET  = 16

TOUCH_STATE = {
    "down": False,
    "x_raw": None,
    "y_raw": None,
    "x": None,
    "y": None,
    "updated": 0.0,
}
TOUCH_PRESS = {
    "x_raw": None,
    "y_raw": None,
    "x": None,
    "y": None,
    "updated": 0.0,
}

ACTION_LOG_FILE = "/var/log/dashboard-actions.log"
PIHOLE_BUTTON = {"id": "pihole", "label": tr("btn_pihole"),
                 "rect": (20, 224, 440, 30), "hold": 3}
REBOOT_BUTTON = {"id": "reboot", "label": tr("btn_reboot"),
                 "rect": (20, 259, 440, 30), "hold": 5}
ACTION_BUTTONS = [PIHOLE_BUTTON, REBOOT_BUTTON]

active_action = None
active_action_started = 0.0
action_message = ""
action_message_until = 0.0
pending_reboot_at = 0.0

def _touch_thread():
    """Lee /dev/input/event0 (ADS7846) y señaliza TOUCH_EVENT en cada toque.
    Se reconecta automáticamente si el dispositivo desaparece temporalmente."""
    try:
        import evdev
    except ImportError:
        log_error("evdev no instalado — touch desactivado (pip install evdev)")
        return

    last_err = None
    while True:
        dev = None
        try:
            dev         = evdev.InputDevice("/dev/input/event0")
            last_err    = None
            finger_down = False
            press_pending = False
            x_raw = None
            y_raw = None
            for event in dev.read_loop():
                if event.type == evdev.ecodes.EV_ABS:
                    if event.code == evdev.ecodes.ABS_X:
                        x_raw = event.value
                    elif event.code == evdev.ecodes.ABS_Y:
                        y_raw = event.value
                    _update_touch_state(finger_down, x_raw, y_raw)
                elif (event.type == evdev.ecodes.EV_KEY and
                        event.code == evdev.ecodes.BTN_TOUCH):
                    if event.value == 1 and not finger_down:
                        # Flanco de subida — primer contacto
                        finger_down = True
                        press_pending = True
                        _update_touch_state(True, x_raw, y_raw)
                    elif event.value == 0:
                        # Flanco de bajada — dedo levantado, permitir próximo toque
                        finger_down = False
                        press_pending = False
                        _update_touch_state(False, x_raw, y_raw)
                elif event.type == evdev.ecodes.EV_SYN and event.code == evdev.ecodes.SYN_REPORT:
                    _update_touch_state(finger_down, x_raw, y_raw)
                    if press_pending:
                        _record_touch_press(x_raw, y_raw)
                        TOUCH_EVENT.set()
                        press_pending = False
        except Exception as e:
            msg = repr(e)
            if msg != last_err:          # registrar solo cuando cambia el error
                log_error(f"touch: {msg}")
                last_err = msg
            _update_touch_state(False, None, None)   # no dejar un "dedo abajo" atascado
            time.sleep(1)   # espera antes de reintentar la reconexión
        finally:
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass


def _scale_touch(value, raw_min, raw_max, screen_max):
    if value is None or raw_max == raw_min:
        return None
    value = max(raw_min, min(raw_max, value))
    return int((value - raw_min) * screen_max / (raw_max - raw_min))


def _update_touch_state(down: bool, x_raw, y_raw):
    x, y = _touch_to_screen(x_raw, y_raw)
    with TOUCH_LOCK:
        TOUCH_STATE.update({
            "down": down,
            "x_raw": x_raw,
            "y_raw": y_raw,
            "x": x,
            "y": y,
            "updated": time.time(),
        })


def _record_touch_press(x_raw, y_raw):
    x, y = _touch_to_screen(x_raw, y_raw)
    with TOUCH_LOCK:
        TOUCH_PRESS.update({
            "x_raw": x_raw,
            "y_raw": y_raw,
            "x": x,
            "y": y,
            "updated": time.time(),
        })


def _touch_to_screen(x_raw, y_raw):
    x_val, y_val = x_raw, y_raw
    if TOUCH_SWAP_XY:
        x_val, y_val = y_val, x_val
    x = _scale_touch(x_val, TOUCH_RAW_X_MIN, TOUCH_RAW_X_MAX, FB_W - 1)
    y = _scale_touch(y_val, TOUCH_RAW_Y_MIN, TOUCH_RAW_Y_MAX, FB_H - 1)
    if x is not None and TOUCH_INVERT_X:
        x = FB_W - 1 - x
    if y is not None and TOUCH_INVERT_Y:
        y = FB_H - 1 - y
    if x is not None:
        x = max(0, min(FB_W - 1, x + TOUCH_X_OFFSET))
    if y is not None:
        y = max(0, min(FB_H - 1, y + TOUCH_Y_OFFSET))
    return x, y

# ── Colores RGB888 ────────────────────────────────────────────────────────────
NEGRO    = (0,   0,   0)
VERDE    = (0,   255, 127)
ROJO     = (255, 68,  68)
AMARILLO = (255, 215, 0)
BLANCO   = (255, 255, 255)
GRIS     = (136, 136, 136)
SEP_COL  = (51,  51,  51)
BAR_COL  = (17,  17,  17)

# ── Config ────────────────────────────────────────────────────────────────────
PIHOLE_PASSWORD = os.environ.get("PIHOLE_PASSWORD", "")
OWM_API_KEY     = os.environ.get("OWM_API_KEY", "")
OWM_CITY        = os.environ.get("OWM_CITY", "Lima,PE")
PIHOLE_URL      = os.environ.get("PIHOLE_URL", "http://localhost:8089").rstrip("/")   # sin /api
HOSTNAME        = socket.gethostname()

NUM_PAGES          = 3
NIGHT_START        = int(os.environ.get("NIGHT_START", "23"))   # hora local en que se pone en negro
NIGHT_END          = int(os.environ.get("NIGHT_END", "6"))      # hora local en que se enciende
NIGHT_WAKE_SECONDS = 30                                         # encendido temporal al tocar
STATE_FILE         = "/var/lib/dashboard/state.json"
BOOTS_LOG          = "/var/lib/dashboard/boots.log"
BACKUP_DIR         = os.environ.get("BACKUP_DIR", "/mnt/ssd/backups")

FB_W    = 480
FB_H    = 320
BAR_H   = 24          # barra inferior fija
BODY_H  = FB_H - BAR_H  # 296 px de área útil


# =============================================================================
#  FBRenderer — escribe directamente a /dev/fb1 vía mmap
# =============================================================================

@lru_cache(maxsize=512)
def _text_mask(font, text: str):
    """Máscara de texto en caché (fuente, texto): pegar una máscara ya renderizada es
    mucho más barato que volver a rasterizar el texto en cada cuadro."""
    if not text:
        return None
    pad = 4
    try:
        bb = font.getbbox(text)
    except AttributeError:
        return None
    w, h = bb[2] + 2 * pad, bb[3] + 2 * pad
    if w <= 2 * pad or h <= 2 * pad:
        return None
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((pad, pad), text, font=font, fill=255)
    return mask, pad


class FBRenderer:
    FB_WIDTH  = 480
    FB_HEIGHT = 320
    FB_BPP    = 2                                  # bytes/pixel (16bpp RGB565)
    FB_SIZE   = FB_WIDTH * FB_HEIGHT * FB_BPP      # 307 200 bytes
    FULL_REFRESH_S = 60    # refresco completo periódico por si el panel pierde algún dato
    MERGE_GAP_ROWS = 6     # une bloques de filas cambiadas separados por pocas filas

    def __init__(self):
        self.fb   = open("/dev/fb1", "r+b")
        self.mm   = mmap.mmap(self.fb.fileno(), self.FB_SIZE)
        self.img  = Image.new("RGB", (self.FB_WIDTH, self.FB_HEIGHT), (0, 0, 0))
        self.draw = ImageDraw.Draw(self.img)
        self.prev = None          # último cuadro RGB565 enviado (numpy)
        self.last_full = 0.0
        self.force_full = True

    def invalidate(self):
        """Fuerza que el próximo flush reescriba todo el framebuffer."""
        self.force_full = True

    # ── Primitivas de dibujo ─────────────────────────────────────────────────
    def fill_rect(self, x, y, w, h, r, g, b):
        x1 = max(0, x)
        y1 = max(0, y)
        x2 = min(x + w - 1, self.FB_WIDTH  - 1)
        y2 = min(y + h - 1, self.FB_HEIGHT - 1)
        if x1 <= x2 and y1 <= y2:
            self.draw.rectangle([x1, y1, x2, y2], fill=(r, g, b))

    def draw_line(self, x0, y0, x1, y1, r, g, b, width=1):
        self.draw.line([(x0, y0), (x1, y1)], fill=(r, g, b), width=width)

    def draw_text(self, x, y, text, font, r, g, b):
        """Pega texto (máscara en caché) sobre la imagen PIL (buffer principal)."""
        text = str(text)
        cached = _text_mask(font, text)
        if cached is None:
            if text:
                self.draw.text((x, y), text, font=font, fill=(r, g, b))
            return
        mask, pad = cached
        self.img.paste((r, g, b), (x - pad, y - pad), mask)

    def clear(self, r=0, g=0, b=0):
        self.draw.rectangle(
            [0, 0, self.FB_WIDTH - 1, self.FB_HEIGHT - 1], fill=(r, g, b))

    # ── Flush: PIL Image → RGB565 → mmap (solo las filas que cambiaron) ─────
    def flush(self):
        if np is None:
            return self._flush_full_python()
        arr = np.asarray(self.img)
        r = arr[:, :, 0].astype(np.uint16)
        g = arr[:, :, 1].astype(np.uint16)
        b = arr[:, :, 2].astype(np.uint16)
        new = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)

        now = time.monotonic()
        if self.force_full or self.prev is None or now - self.last_full >= self.FULL_REFRESH_S:
            self._write_rows(0, self.FB_HEIGHT, new)
            self.last_full  = now
            self.force_full = False
        else:
            changed = np.flatnonzero((new != self.prev).any(axis=1))
            if changed.size:
                start = last = int(changed[0])
                for row in changed[1:]:
                    row = int(row)
                    if row - last > self.MERGE_GAP_ROWS:
                        self._write_rows(start, last + 1, new)
                        start = row
                    last = row
                self._write_rows(start, last + 1, new)
        self.prev = new

    def _write_rows(self, y0: int, y1: int, frame):
        off = y0 * self.FB_WIDTH * self.FB_BPP
        data = frame[y0:y1].astype("<u2", copy=False).tobytes()
        self.mm[off:off + len(data)] = data

    def _flush_full_python(self):
        """Sin numpy: conversión en Python puro (~150 ms) y escritura completa."""
        raw = self.img.tobytes()
        out = bytearray(self.FB_SIZE)
        for i in range(self.FB_WIDTH * self.FB_HEIGHT):
            j = i * 3
            c = ((raw[j] & 0xF8) << 8) | ((raw[j + 1] & 0xFC) << 3) | (raw[j + 2] >> 3)
            out[i * 2]     = c & 0xFF
            out[i * 2 + 1] = c >> 8
        self.mm[0:self.FB_SIZE] = bytes(out)

    def close(self):
        try: self.mm.close()
        except Exception: pass
        try: self.fb.close()
        except Exception: pass


# =============================================================================
#  Fuentes PIL
# =============================================================================

def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Carga DejaVu Sans TTF; fallback a fuente bitmap por defecto."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/ttf-dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()

FONT_HORA  = _load_font(48)
FONT_FECHA = _load_font(20)
FONT_DATO  = _load_font(21)
FONT_LABEL = _load_font(17)
FONT_BAR   = _load_font(16)
FONT_SMALL = _load_font(13)


@lru_cache(maxsize=1024)
def _tw_cached(font, text: str) -> int:
    try:
        bb = font.getbbox(text)
        return bb[2] - bb[0]
    except AttributeError:
        return font.getsize(text)[0]   # Pillow < 9.2

def _tw(font, text) -> int:
    """Ancho en píxeles del texto renderizado (con caché: getbbox es caro en el Pi)."""
    return _tw_cached(font, str(text))

@lru_cache(maxsize=1024)
def _th(font, sample: str = "Ag0") -> int:
    """Alto en píxeles (bounding box visual) del texto de muestra."""
    try:
        bb = font.getbbox(sample)
        return bb[3] - bb[1]
    except AttributeError:
        return font.getsize(sample)[1]


# Alturas de fila con relleno vertical
ROW_HORA  = _th(FONT_HORA,  "09:00:00") + 8   # ≈ 56 px
ROW_FECHA = _th(FONT_FECHA, "Miércoles") + 5   # ≈ 25 px
ROW_DATO  = _th(FONT_DATO,  "0.0°C")    + 6   # ≈ 28 px
ROW_SVC   = _th(FONT_DATO,  "ACTIVO")   + 4   # ≈ 26 px
ROW_BAR   = _th(FONT_BAR,   "0/2")      + 4   # ≈ 21 px

LBL_X = 6    # columna izquierda para etiquetas
VAL_X = 110  # columna de valores


# =============================================================================
#  Helpers de renderizado
# =============================================================================

def _hline(fb: FBRenderer, y: int):
    fb.draw_line(0, y, FB_W - 1, y, *SEP_COL)

def _bottom_bar(fb: FBRenderer, page_n: int):
    fb.fill_rect(0, BODY_H, FB_W, BAR_H, *BAR_COL)
    ty = BODY_H + (BAR_H - ROW_BAR) // 2
    phase = (math.sin(time.time() * math.tau) + 1) / 2
    pulse = 3 + int(phase * 3)
    cx = 10
    cy = BODY_H + BAR_H // 2
    fb.draw.ellipse([cx - pulse, cy - pulse, cx + pulse, cy + pulse], fill=VERDE)
    fb.draw_text(22, ty, HOSTNAME, FONT_BAR, *GRIS)
    s = f"{page_n}/{NUM_PAGES}"
    fb.draw_text(FB_W - _tw(FONT_BAR, s) - 6, ty, s, FONT_BAR, *GRIS)

def _text_center(fb: FBRenderer, y: int, text: str, font, color: tuple):
    x = (FB_W - _tw(font, text)) // 2
    fb.draw_text(x, y, text, font, *color)

def _text_right(fb: FBRenderer, y: int, text: str, font, color: tuple, margin: int = 6):
    x = FB_W - _tw(font, text) - margin
    fb.draw_text(x, y, text, font, *color)

def _label_row(fb: FBRenderer, y: int,
               label: str, value: str, vcolor: tuple,
               badge: tuple | None = None):
    """
    Dibuja una fila de datos:
      ETIQUETA (GRIS, pequeña, izq.)  |  VALOR (color, normal)  [BADGE (derecha)]
    """
    lh = _th(FONT_LABEL, label)
    vh = _th(FONT_DATO,  value)
    # Centrar verticalmente la etiqueta respecto al valor
    fb.draw_text(LBL_X, y + max(0, (vh - lh) // 2), label, FONT_LABEL, *GRIS)
    fb.draw_text(VAL_X, y, value, FONT_DATO, *vcolor)
    if badge:
        btxt, bcol = badge
        bw = _tw(FONT_LABEL, btxt) + 10
        bh = lh + 4
        bx = FB_W - bw - 6
        by = y + max(0, (vh - bh) // 2)
        fb.fill_rect(bx, by, bw, bh, *bcol)
        fb.draw_text(bx + 5, by + 2, btxt, FONT_LABEL, *NEGRO)


def _rect_contains(rect: tuple, x, y) -> bool:
    if x is None or y is None:
        return False
    rx, ry, rw, rh = rect
    return rx <= x < rx + rw and ry <= y < ry + rh


def _touch_snapshot():
    with TOUCH_LOCK:
        return dict(TOUCH_STATE)


def _touch_press_snapshot():
    with TOUCH_LOCK:
        return dict(TOUCH_PRESS)


def _action_log(action: str, result: str):
    try:
        with open(ACTION_LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {action}: {result}\n")
    except Exception as e:
        log_error(f"action_log: {e}")


def _draw_button(fb: FBRenderer, button: dict, progress: float = 0.0, active: bool = False):
    x, y, w, h = button["rect"]
    border = AMARILLO if active else (84, 84, 84)
    fill = (34, 34, 34) if button["id"] == "pihole" else (38, 30, 30)
    fb.fill_rect(x, y, w, h, *fill)
    for i in range(2):
        fb.draw_line(x + i, y + i, x + w - 1 - i, y + i, *border)
        fb.draw_line(x + i, y + h - 1 - i, x + w - 1 - i, y + h - 1 - i, *border)
        fb.draw_line(x + i, y + i, x + i, y + h - 1 - i, *border)
        fb.draw_line(x + w - 1 - i, y + i, x + w - 1 - i, y + h - 1 - i, *border)
    icon_col = ROJO if button["id"] == "reboot" else AMARILLO
    fb.draw.polygon([(x + 14, y + h // 2), (x + 24, y + 8), (x + 24, y + h - 8)], fill=icon_col)
    if progress > 0:
        pw = max(1, min(w - 4, int((w - 4) * progress)))
        fb.fill_rect(x + 2, y + h - 6, pw, 4, *(ROJO if button["id"] == "reboot" else AMARILLO))
    fb.draw_text(x + 36, y + 5, button["label"], FONT_BAR, *BLANCO)


def _draw_temp_graph(fb: FBRenderer, x: int, y: int, w: int, h: int):
    fb.fill_rect(x, y, w, h, 6, 10, 10)
    fb.draw_line(x, y + h - 1, x + w - 1, y + h - 1, *SEP_COL)
    for ref, col in ((60, (58, 58, 26)), (70, (64, 32, 32))):
        ry = int(y + h - 2 - ((ref - 40.0) / 40.0) * (h - 4))
        fb.draw_line(x, ry, x + w - 1, ry, *col)
    if len(temp_history) < 2:
        dot_x = x + w - 16
        dot_y = int(y + h - 2 - ((hw_temp - 40.0) / 40.0) * (h - 4))
        dot_y = max(y + 5, min(y + h - 6, dot_y))
        fb.draw.ellipse([dot_x - 4, dot_y - 4, dot_x + 4, dot_y + 4], fill=(64, 255, 170))
        fb.draw_text(x + 8, y + 13, tr("graph_wait"), FONT_SMALL, 210, 220, 220)
        return
    vals = list(temp_history)
    lo = min(40.0, min(vals))
    hi = max(80.0, max(vals))
    span = max(1.0, hi - lo)
    step = w / max(1, len(vals) - 1)
    points = []
    for i, val in enumerate(vals):
        px = int(x + i * step)
        py = int(y + h - 2 - ((val - lo) / span) * (h - 4))
        points.append((px, py, val))
    for p0, p1 in zip(points, points[1:]):
        col = (64, 255, 170) if p1[2] < 60 else (255, 232, 70) if p1[2] <= 70 else (255, 84, 84)
        fb.draw_line(p0[0], p0[1], p1[0], p1[1], *col, width=3)


def _age_text(ts: float) -> str:
    if not ts:
        return tr("age_none")
    age = max(0, int(time.time() - ts))
    if age < 60:
        return tr("age_lt1")
    mins = age // 60
    if mins < 60:
        return tr("age_min", n=mins)
    return tr("age_h", n=mins // 60)


def _draw_spinner(fb: FBRenderer, cx: int, cy: int, frame: int):
    pts = 6
    for i in range(pts):
        ang = (i / pts) * math.tau
        alpha = (i - frame) % pts
        shade = 70 + alpha * 28
        r = 2 if alpha < 3 else 1
        x = int(cx + math.cos(ang) * 9)
        y = int(cy + math.sin(ang) * 9)
        fb.draw.ellipse([x - r, y - r, x + r, y + r], fill=(shade, shade, shade))


def _weather_kind():
    text = (clima_desc or "").lower()
    if any(w in text for w in ("lluv", "rain", "drizzle", "torment")):
        return "rain"
    if any(w in text for w in ("nube", "cloud", "cubierto")):
        return "cloud"
    if any(w in text for w in ("algo", "parcial", "dispers")):
        return "partly"
    return "sun"


def _draw_weather_icon(fb: FBRenderer, x: int, y: int, frame: int):
    kind = _weather_kind()
    wobble = 1 if frame % 2 else 0
    if kind in ("sun", "partly"):
        cx, cy = x + 16, y + 16 + wobble
        fb.draw.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], fill=(255, 210, 48))
        for a in range(0, 360, 45):
            ang = math.radians(a + frame * 8)
            fb.draw_line(cx + int(math.cos(ang) * 11), cy + int(math.sin(ang) * 11),
                         cx + int(math.cos(ang) * 16), cy + int(math.sin(ang) * 16),
                         255, 210, 48, width=2)
    if kind in ("cloud", "partly", "rain"):
        ox = x + (10 if kind == "partly" else 2)
        oy = y + 18
        fb.draw.ellipse([ox + 4, oy - 8, ox + 22, oy + 10], fill=(178, 188, 196))
        fb.draw.ellipse([ox + 16, oy - 13, ox + 35, oy + 10], fill=(198, 207, 214))
        fb.fill_rect(ox + 8, oy, 32, 10, 198, 207, 214)
    if kind == "rain":
        for i in range(3):
            rx = x + 12 + i * 8 + (frame % 2) * 2
            fb.draw_line(rx, y + 31, rx - 3, y + 38, 80, 180, 255, width=2)


def _draw_beagle(fb: FBRenderer, x: int, y: int, frame: int):
    ear = 1 if frame % 2 else 0
    blink = frame % 8 == 0
    # cuerpo y cabeza pixel-art simple
    fb.fill_rect(x + 8, y + 18, 22, 12, 215, 170, 92)
    fb.fill_rect(x + 27, y + 13, 11, 12, 215, 170, 92)
    fb.fill_rect(x + 29, y + 10, 7, 5, 245, 236, 206)
    fb.fill_rect(x + 24, y + 15 + ear, 6, 12, 95, 58, 35)
    fb.fill_rect(x + 36, y + 15 + (1 - ear), 5, 11, 95, 58, 35)
    fb.fill_rect(x + 10, y + 30, 4, 6, 245, 236, 206)
    fb.fill_rect(x + 24, y + 30, 4, 6, 245, 236, 206)
    fb.draw_line(x + 7, y + 20, x + 2, y + 16 + ear, 245, 236, 206, width=2)
    if blink:
        fb.draw_line(x + 32, y + 14, x + 35, y + 14, 0, 0, 0)
    else:
        fb.fill_rect(x + 33, y + 13, 2, 2, 0, 0, 0)
    fb.fill_rect(x + 38, y + 17, 2, 2, 0, 0, 0)


def _draw_shepherd(fb: FBRenderer, x: int, y: int, frame: int):
    """German shepherd: tan body with a black saddle, dark muzzle, tall pointed ears."""
    wag = frame % 2
    blink = frame % 8 == 0
    tan, dark, paw = (196, 142, 72), (40, 34, 32), (150, 104, 50)
    # bushy tail hanging down, swinging
    fb.draw.polygon([(x + 9, y + 19), (x + 2 + wag * 2, y + 31), (x + 6 + wag * 2, y + 34), (x + 12, y + 25)],
                    fill=paw)
    fb.fill_rect(x + 8, y + 18, 24, 12, *tan)                      # body
    fb.fill_rect(x + 10, y + 18, 18, 5, *dark)                     # black saddle
    for lx in (x + 10, x + 18, x + 27):                            # legs
        fb.fill_rect(lx, y + 30, 4, 7, *tan)
        fb.fill_rect(lx, y + 35, 4, 2, *paw)
    fb.fill_rect(x + 30, y + 11, 12, 12, *tan)                     # head
    fb.fill_rect(x + 38, y + 16, 7, 6, 78, 58, 44)                 # muzzle
    fb.fill_rect(x + 43, y + 16, 2, 2, 0, 0, 0)                    # nose
    fb.draw.polygon([(x + 30, y + 12), (x + 31, y + 2 + wag), (x + 36, y + 11)], fill=dark)   # ears
    fb.draw.polygon([(x + 36, y + 11), (x + 39, y + 1 + (1 - wag)), (x + 42, y + 12)], fill=dark)
    if blink:
        fb.draw_line(x + 34, y + 15, x + 37, y + 15, 0, 0, 0)
    else:
        fb.fill_rect(x + 35, y + 14, 2, 2, 0, 0, 0)


def _draw_cat(fb: FBRenderer, x: int, y: int, frame: int, fur, shade, stripe, eye, inner):
    """Cat sitting and looking at the viewer. `stripe` is None for a plain coat."""
    wag = frame % 2
    blink = frame % 8 == 0
    # tail curling up, swaying
    fb.draw_line(x + 9, y + 26, x + 3, y + 22, *fur, width=3)
    fb.draw_line(x + 3, y + 22, x + 3 + wag * 2, y + 13, *fur, width=3)
    if stripe:
        fb.draw_line(x + 3 + wag * 2, y + 15, x + 3 + wag * 2, y + 13, *stripe, width=3)
    fb.fill_rect(x + 8, y + 20, 22, 12, *fur)                      # body
    fb.fill_rect(x + 8, y + 29, 22, 3, *shade)                     # belly shade
    if stripe:
        for sx in (x + 12, x + 17, x + 22, x + 27):
            fb.fill_rect(sx, y + 20, 2, 6, *stripe)
    for lx in (x + 10, x + 25):                                    # front and back paws
        fb.fill_rect(lx, y + 32, 5, 4, *shade)
    fb.fill_rect(x + 28, y + 14, 14, 13, *fur)                     # head
    fb.draw.polygon([(x + 28, y + 15), (x + 29, y + 7), (x + 34, y + 14)], fill=fur)           # ears
    fb.draw.polygon([(x + 36, y + 14), (x + 41, y + 7 + wag), (x + 42, y + 15)], fill=fur)
    fb.draw.polygon([(x + 30, y + 14), (x + 30, y + 10), (x + 33, y + 14)], fill=inner)
    fb.draw.polygon([(x + 37, y + 14), (x + 40, y + 10 + wag), (x + 40, y + 14)], fill=inner)
    if stripe:                                                      # forehead "M"
        for fx in (x + 32, x + 35, x + 38):
            fb.fill_rect(fx, y + 14, 1, 3, *stripe)
    for ex in (x + 31, x + 37):                                    # eyes
        if blink:
            fb.draw_line(ex, y + 19, ex + 2, y + 19, 0, 0, 0)
        else:
            fb.fill_rect(ex, y + 18, 3, 3, *eye)
            fb.fill_rect(ex + 1, y + 18, 1, 3, 0, 0, 0)
    fb.fill_rect(x + 34, y + 22, 3, 2, 255, 150, 165)              # nose
    fb.draw_line(x + 42, y + 23, x + 46, y + 22, *shade)           # whisker (the left one would sit on the body)


def _draw_tabby(fb: FBRenderer, x: int, y: int, frame: int):
    _draw_cat(fb, x, y, frame, (214, 146, 66), (176, 108, 44), (120, 70, 26), (90, 200, 90), (255, 170, 170))


def _draw_white_cat(fb: FBRenderer, x: int, y: int, frame: int):
    _draw_cat(fb, x, y, frame, (246, 246, 248), (196, 200, 212), None, (90, 160, 225), (255, 190, 200))


PETS = {
    "beagle": _draw_beagle,
    "shepherd": _draw_shepherd,
    "tabby": _draw_tabby,
    "white-cat": _draw_white_cat,
}


def _draw_pet(fb: FBRenderer, x: int, y: int, frame: int):
    """Draws the pet chosen with DASHBOARD_PET (beagle | shepherd | tabby | white-cat | none)."""
    draw = PETS.get(PET)
    if draw:
        draw(fb, x, y, frame)


# =============================================================================
#  Estado global
# =============================================================================

net_local_iface = ""
net_local_ip    = ""
net_ts_ip       = ""
net_gw_ok       = False
net_gw_ms       = 0.0
net_inet_ok     = False
net_inet_ms     = 0.0

pihole_sid     = ""
pihole_blocked = 0
pihole_pct     = 0.0
pihole_ok      = False
pihole_total   = 0
top_clients    = []   # [(nombre, consultas)]
top_blocked    = []   # [(dominio, bloqueos)]
top_updated    = 0.0
loading_top    = False
last_update_top = 0.0

night_enabled  = True    # interruptor en pantalla (persistente en STATE_FILE)
backlight_on   = None    # True = con contenido, False = en negro, None = desconocido
wake_until     = 0.0     # encendido temporal durante la noche
NIGHT_TOGGLE_RECT = (20, 254, 440, 36)

clima_temp = None   # float o None si nunca se obtuvo
clima_desc = ""
loading_clima = False
loading_pihole = False

hw_temp         = 0.0
hw_ram_used_mib = 0
hw_ram_tot_mib  = 1024
hw_uptime_s     = 0
temp_history    = deque(maxlen=60)
last_temp_sample = 0.0
reboots_24h     = 0
ssd_used_gb     = 0.0
ssd_tot_gb      = 0.0
ssd_mounted     = False
hw_throttled    = None    # entero de `vcgencmd get_throttled`; None si no se pudo leer
backup_age_h    = None    # horas desde el último respaldo local; None si no hay ninguno
backup_failed   = False   # la última línea del log de respaldos es un error

svc_syncthing  = False
svc_pihole_svc = False
svc_tailscale  = False
svc_nanobot    = False

last_update_red    = 0.0
last_update_clima  = 0.0
last_update_pihole = 0.0
last_pihole_auth   = 0.0
last_update_hw     = 0.0
last_update_reboots = 0.0
last_update_services = 0.0
last_update_health = 0.0
last_page_change   = 0.0

current_page = 0


# =============================================================================
#  Fetchers de datos
# =============================================================================

def _iface_ip(iface: str):
    """(ip, True) si la interfaz tiene IPv4 asignada, si no ("", False)."""
    try:
        r = subprocess.run(
            ["ip", "addr", "show", iface],
            capture_output=True, text=True, timeout=2
        )
        for line in r.stdout.splitlines():
            s = line.strip()
            if s.startswith("inet "):
                return s.split()[1].split("/")[0], True
    except Exception as e:
        log_error(f"_iface_ip({iface}): {e}")
    return "", False


def _tailscale_ip():
    try:
        r = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=2
        )
        for line in r.stdout.splitlines():
            ip = line.strip()
            if ip:
                return ip, True
    except Exception as e:
        log_error(f"_tailscale_ip: {e}")

    return _iface_ip("tailscale0")


_async_running = set()


def run_async(name: str, fn):
    """Ejecuta fn en un hilo (sin duplicarlo si la ejecución anterior sigue viva) para que
    subprocesos o peticiones lentas no bloqueen el bucle de dibujo ni el touch."""
    if name in _async_running:
        return
    _async_running.add(name)

    def worker():
        try:
            fn()
        except Exception as e:
            log_error(f"{name}: {e!r}")
        finally:
            _async_running.discard(name)

    threading.Thread(target=worker, daemon=True, name=name).start()


def _iface_operstate_up(iface: str) -> bool:
    try:
        with open(f"/sys/class/net/{iface}/operstate") as f:
            return f.read().strip() == "up"
    except Exception:
        return False


def fetch_network():
    global net_local_iface, net_local_ip, net_ts_ip
    global net_gw_ok, net_gw_ms, net_inet_ok, net_inet_ms

    # Interfaz local: eth0 y, si no está activa, wlan0 (con enlace real, no solo IP asignada)
    local_iface, local_ip = "", ""
    for iface, label in (("eth0", "ETH"), ("wlan0", "WiFi")):
        if _iface_operstate_up(iface):
            ip, ok = _iface_ip(iface)
            if ok:
                local_iface, local_ip = label, ip
                break

    ts_ip, ts_ok = _tailscale_ip()

    # Gateway por defecto
    gw = ""
    try:
        r = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=2
        )
        for line in r.stdout.splitlines():
            parts = line.split()
            if "via" in parts:
                gw = parts[parts.index("via") + 1]
                break
    except Exception as e:
        log_error(f"fetch_network gw: {e}")

    gw_ok, gw_ms = _ping(gw) if gw else (False, 0.0)
    inet_ok, inet_ms = _ping("8.8.8.8")

    # Asignar todo al final: el dibujo nunca ve un estado a medias
    net_local_iface, net_local_ip = local_iface, local_ip
    net_ts_ip = ts_ip if ts_ok else ""
    net_gw_ok, net_gw_ms = gw_ok, gw_ms
    net_inet_ok, net_inet_ms = inet_ok, inet_ms


def _ping(ip: str):
    """(alcanzable, ms)."""
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "2", ip],
            capture_output=True, text=True, timeout=3
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if "time=" in line:
                    return True, float(line.split("time=")[1].split()[0])
            return True, 0.0
    except Exception as e:
        log_error(f"_ping({ip}): {e}")
    return False, 0.0


def pihole_auth():
    global pihole_sid, pihole_ok
    try:
        url  = PIHOLE_URL + "/api/auth"
        body = json.dumps({"password": PIHOLE_PASSWORD}).encode()
        req  = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data       = json.loads(resp.read().decode())
            pihole_sid = data["session"]["sid"]
    except Exception as e:
        log_error(f"pihole_auth: {e}")
        pihole_sid = ""
        pihole_ok  = False


def _pihole_get(path: str):
    """GET a la API de Pi-hole; si la sesión expiró (401), renueva el SID y reintenta una vez."""
    for attempt in (1, 2):
        headers = {"X-FTL-SID": pihole_sid} if pihole_sid else {}
        req = urllib.request.Request(PIHOLE_URL + "/api" + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 1:
                pihole_auth()
                continue
            raise


def fetch_pihole_top():
    global top_clients, top_blocked, top_updated
    try:
        c = _pihole_get("/stats/top_clients?count=4")
        d = _pihole_get("/stats/top_domains?blocked=true&count=4")
        clients = []
        for item in c.get("clients", []):
            name = (item.get("name") or "").split(".")[0] or item.get("ip") or "?"
            clients.append((name, int(item.get("count", 0))))
        top_clients = clients
        top_blocked = [(item.get("domain", "?"), int(item.get("count", 0)))
                       for item in d.get("domains", [])]
        top_updated = time.time()
    except Exception as e:
        log_error(f"fetch_pihole_top: {e}")


def fetch_pihole_top_async():
    global loading_top, last_update_top
    if loading_top:
        return
    def worker():
        global loading_top, last_update_top
        loading_top = True
        try:
            fetch_pihole_top()
            last_update_top = time.time()
        finally:
            loading_top = False
    threading.Thread(target=worker, daemon=True, name="fetch-top").start()


def fetch_pihole():
    global pihole_blocked, pihole_pct, pihole_ok, pihole_total
    try:
        data = _pihole_get("/stats/summary")
        pihole_total   = data["queries"]["total"]
        pihole_blocked = data["queries"]["blocked"]
        pihole_pct     = data["queries"]["percent_blocked"]
        pihole_ok      = True
    except Exception as e:
        log_error(f"fetch_pihole: {e}")
        pihole_ok = False


def fetch_pihole_async():
    global loading_pihole, last_update_pihole
    if loading_pihole:
        return
    def worker():
        global loading_pihole, last_update_pihole
        loading_pihole = True
        try:
            fetch_pihole()
            last_update_pihole = time.time()
        finally:
            loading_pihole = False
    threading.Thread(target=worker, daemon=True, name="fetch-pihole").start()


def fetch_clima():
    global clima_temp, clima_desc
    if not OWM_API_KEY:
        return
    try:
        url = (
            f"http://api.openweathermap.org/data/2.5/weather"
            f"?q={OWM_CITY}&appid={OWM_API_KEY}&units=metric&lang={tr('owm_lang')}"
        )
        with urllib.request.urlopen(url, timeout=3) as resp:
            data       = json.loads(resp.read().decode())
            clima_temp = data["main"]["temp"]
            clima_desc = data["weather"][0]["description"].capitalize()
    except Exception as e:
        log_error(f"fetch_clima: {e}")


def fetch_clima_async():
    global loading_clima, last_update_clima
    if loading_clima:
        return
    def worker():
        global loading_clima, last_update_clima
        loading_clima = True
        try:
            fetch_clima()
            last_update_clima = time.time()
        finally:
            loading_clima = False
    threading.Thread(target=worker, daemon=True, name="fetch-clima").start()


_SERVICE_UNITS = ("syncthing", "pihole-FTL", "tailscaled", "nanobot")


def fetch_services():
    """Estado de los servicios con UNA llamada a systemctl (antes: 3 procesos más dos
    recorridos completos de /proc con psutil cada 5 s)."""
    global svc_syncthing, svc_pihole_svc, svc_tailscale, svc_nanobot
    try:
        r = subprocess.run(["systemctl", "is-active", *_SERVICE_UNITS],
                           capture_output=True, text=True, timeout=4)
        states = dict(zip(_SERVICE_UNITS, r.stdout.split()))
    except Exception as e:
        log_error(f"fetch_services: {e}")
        return
    svc_syncthing  = states.get("syncthing") == "active"
    svc_pihole_svc = states.get("pihole-FTL") == "active"
    svc_tailscale  = states.get("tailscaled") == "active"
    svc_nanobot    = states.get("nanobot") == "active"


def fetch_hw():
    global hw_temp, hw_ram_used_mib, hw_ram_tot_mib, hw_uptime_s
    global ssd_used_gb, ssd_tot_gb, ssd_mounted
    global last_temp_sample

    try:
        hw_temp = int(open("/sys/class/thermal/thermal_zone0/temp").read().strip()) / 1000
        now_t = time.time()
        if not last_temp_sample or now_t - last_temp_sample >= 30:
            temp_history.append(hw_temp)
            last_temp_sample = now_t
    except Exception as e:
        log_error(f"hw_temp: {e}")

    try:
        m               = psutil.virtual_memory()
        hw_ram_used_mib = m.used  // (1024 * 1024)
        hw_ram_tot_mib  = m.total // (1024 * 1024)
    except Exception as e:
        log_error(f"hw_ram: {e}")

    try:
        hw_uptime_s = int(time.time() - psutil.boot_time())
    except Exception as e:
        log_error(f"hw_uptime: {e}")

    try:
        st          = os.statvfs("/mnt/ssd")
        ssd_tot_gb  = st.f_blocks * st.f_frsize / (1024 ** 3)
        ssd_used_gb = (st.f_blocks - st.f_bavail) * st.f_frsize / (1024 ** 3)
        ssd_mounted = True
    except Exception:
        ssd_mounted = False


def throttle_warning(raw) -> str:
    """Aviso corto según `vcgencmd get_throttled`, o "" si todo está bien. Bits 0/16:
    subtensión (ahora / desde el arranque); 2/18: CPU limitada. Los bits 1/17/3/19
    (frecuencia limitada por temperatura) son normales en una 3B+ a ~60 °C y no avisan."""
    if raw is None:
        return ""
    if raw & 0x10001:
        return tr("volt_low")
    if raw & 0x40004:
        return tr("cpu_lim")
    return ""


def backup_age_hours(names, now: datetime.datetime):
    """Horas desde el respaldo más reciente (carpetas AAAAMMDD-HHMMSS); None si no hay."""
    latest = None
    for name in names:
        try:
            when = datetime.datetime.strptime(name, "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if latest is None or when > latest:
            latest = when
    if latest is None:
        return None
    return max(0.0, (now - latest.replace(tzinfo=now.tzinfo)).total_seconds() / 3600)


def backup_status(age_h, failed: bool):
    """(texto, color) para la línea de respaldo de la página 2."""
    if failed:
        return tr("bk_err"), ROJO
    if age_h is None:
        return tr("bk_none"), ROJO
    text = tr("bk_h", n=int(age_h)) if age_h < 48 else tr("bk_d", n=int(age_h // 24))
    return text, (VERDE if age_h <= 30 else AMARILLO if age_h <= 50 else ROJO)


def fetch_health():
    """Subtensión (vcgencmd) y estado de los respaldos. Se ejecuta en un hilo."""
    global hw_throttled, backup_age_h, backup_failed
    try:
        r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3)
        hw_throttled = int(r.stdout.strip().split("=")[1], 16)
    except Exception:
        hw_throttled = None
    try:
        names = os.listdir(BACKUP_DIR)
        now = datetime.datetime.now(TZ) if TZ else datetime.datetime.now()
        backup_age_h = backup_age_hours(names, now)
        with open(os.path.join(BACKUP_DIR, "backup.log"), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 400))
            last = f.read().decode(errors="replace").strip().splitlines()[-1:]
        backup_failed = bool(last) and "ERROR" in last[0]
    except Exception:
        backup_age_h, backup_failed = None, False


def _load_state():
    global night_enabled
    try:
        with open(STATE_FILE) as f:
            night_enabled = bool(json.load(f).get("night_enabled", True))
    except FileNotFoundError:
        pass
    except Exception as e:
        log_error(f"load_state: {e}")


def _save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"night_enabled": night_enabled}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log_error(f"save_state: {e}")


def _in_night_window(now: datetime.datetime) -> bool:
    if NIGHT_START == NIGHT_END:
        return False
    if NIGHT_START < NIGHT_END:
        return NIGHT_START <= now.hour < NIGHT_END
    return now.hour >= NIGHT_START or now.hour < NIGHT_END


def _set_screen(fb: FBRenderer, on: bool):
    """El backlight de este módulo va cableado a 3.3 V y ningún GPIO lo corta: de noche la
    pantalla se deja en negro (un solo cuadro) y se deja de dibujar. Al despertar, el
    render normal repinta todo."""
    global backlight_on
    if backlight_on == on:
        return
    backlight_on = on
    if not on:
        fb.clear()
        fb.invalidate()
        fb.flush()


def fetch_reboots_24h():
    global reboots_24h
    count = 0
    cutoff = time.time() - 86400
    # Fuente principal: /var/lib/dashboard/boots.log (persistente; journalctl solo ve
    # el arranque actual porque /var/log es tmpfs en DietPi).
    try:
        with open(BOOTS_LOG) as f:
            for line in f:
                first = line.split(" ", 1)[0]
                if first.isdigit() and int(first) >= cutoff:
                    count += 1
        if count:
            reboots_24h = count
            return
    except FileNotFoundError:
        pass
    except Exception as e:
        log_error(f"boots_log: {e}")
    try:
        r = subprocess.run(
            ["journalctl", "--list-boots", "--no-pager"],
            capture_output=True, text=True, timeout=3
        )
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 6 or not parts[0].lstrip("-").isdigit():
                continue
            first_entry = " ".join(parts[2:5])
            try:
                dt = datetime.datetime.strptime(first_entry, "%a %Y-%m-%d %H:%M:%S")
                if TZ:
                    dt = dt.replace(tzinfo=TZ)
                if dt.timestamp() >= cutoff:
                    count += 1
            except Exception:
                continue
        # Si journald no conserva boots viejos, al menos cuenta el arranque actual.
        if count == 0 and time.time() - psutil.boot_time() <= 86400:
            count = 1
    except Exception as e:
        log_error(f"fetch_reboots_24h: {e}")
    reboots_24h = count


def _button_at(x, y):
    for button in ACTION_BUTTONS:
        if _rect_contains(button["rect"], x, y):
            return button
    return None


def _run_pihole_restart():
    global action_message, action_message_until
    _action_log("pihole-FTL restart", "requested")
    try:
        subprocess.run(["systemctl", "restart", "pihole-FTL"], timeout=15, check=True)
        _action_log("pihole-FTL restart", "ok")
        action_message = tr("ph_restarted")
    except Exception as e:
        _action_log("pihole-FTL restart", f"error: {e}")
        log_error(f"restart pihole-FTL: {e}")
        action_message = tr("ph_err")
    action_message_until = time.time() + 3


def _handle_action_touch(now_t: float) -> bool:
    global active_action, active_action_started, pending_reboot_at

    touch = _touch_snapshot()
    if pending_reboot_at:
        if now_t >= pending_reboot_at:
            subprocess.Popen(["systemctl", "reboot"])
            pending_reboot_at = 0.0
        return True

    if not active_action:
        return False

    if current_page != 1 or not touch["down"] or not _rect_contains(active_action["rect"], touch["x"], touch["y"]):
        active_action = None
        return True

    if now_t - active_action_started >= active_action["hold"]:
        completed = active_action
        active_action = None
        if completed["id"] == "pihole":
            _run_pihole_restart()
        elif completed["id"] == "reboot":
            _action_log("raspberry reboot", "requested")
            pending_reboot_at = time.time() + 3
        return True

    return True


def _start_action(button: dict, now_t: float):
    global active_action, active_action_started
    active_action = button
    active_action_started = now_t


# =============================================================================
#  Tablas de localización
# =============================================================================

DIAS  = STRINGS[LANG]["dias"]
MESES = STRINGS[LANG]["meses"]


# =============================================================================
#  Página 1 — Red, Clima y Estado
# =============================================================================

def render_page1(fb: FBRenderer):
    fb.clear()
    now = datetime.datetime.now(TZ) if TZ else datetime.datetime.now()
    anim_frame = int(time.time() * 2)

    # ── Sección 1: Reloj y Clima ──────────────────────────────────────────────
    y = 4

    _text_center(fb, y, now.strftime("%H:%M:%S"), FONT_HORA, BLANCO)
    y += ROW_HORA

    fecha = f"{DIAS[now.weekday()]} {now.day} {MESES[now.month]} {now.year}"
    _text_center(fb, y, fecha, FONT_FECHA, GRIS)
    y += ROW_FECHA

    if clima_temp is not None:
        ciudad = OWM_CITY.split(",")[0]
        c_str  = f"{ciudad}  {clima_temp:.1f}°C  {clima_desc}"
        c_col  = BLANCO
    else:
        c_str = tr("weather_na")
        c_col = GRIS
    _draw_weather_icon(fb, 30, y - 8, anim_frame)
    _text_center(fb, y, c_str, FONT_FECHA, c_col)
    if last_update_clima:
        fb.draw_text(8, y + ROW_FECHA - 2, _age_text(last_update_clima), FONT_SMALL, *GRIS)
    if loading_clima:
        _draw_spinner(fb, 70, y + ROW_FECHA + 4, anim_frame % 6)
    y += ROW_FECHA + 14

    _hline(fb, y);  y += 5

    # ── Sección 2: Conectividad local ─────────────────────────────────────────
    ROW = ROW_DATO + 4

    if net_local_ip:
        badge = (net_local_iface, VERDE) if net_local_iface else None
        _label_row(fb, y, tr("lbl_lan"), net_local_ip, BLANCO, badge)
    else:
        _label_row(fb, y, tr("lbl_lan"), tr("getting"), AMARILLO)
    y += ROW

    if net_ts_ip:
        _label_row(fb, y, "TAILSCALE", net_ts_ip, BLANCO)
    else:
        _label_row(fb, y, "TAILSCALE", tr("ts_off"), AMARILLO)
    y += ROW

    _hline(fb, y);  y += 5

    # ── Sección 3: Diagnóstico de red ─────────────────────────────────────────
    if net_gw_ok:
        _label_row(fb, y, "GATEWAY",  f"✓ OK ({net_gw_ms:.0f}ms)",   VERDE)
    else:
        _label_row(fb, y, "GATEWAY",  tr("down"),                     ROJO)
    y += ROW

    if net_inet_ok:
        _label_row(fb, y, "INTERNET", f"✓ OK ({net_inet_ms:.0f}ms)", VERDE)
    else:
        _label_row(fb, y, "INTERNET", tr("down"),                     ROJO)
    y += ROW

    if pihole_ok:
        _label_row(fb, y, "PI-HOLE",
                   tr("ph_on", b=pihole_blocked, p=pihole_pct),     VERDE)
    else:
        _label_row(fb, y, "PI-HOLE",  tr("ph_down"),                  ROJO)
    if loading_pihole:
        _draw_spinner(fb, 440, y + 10, anim_frame % 6)

    _draw_pet(fb, 430, BODY_H - 48, anim_frame // 3)

    _bottom_bar(fb, 1)


# =============================================================================
#  Página 2 — Hardware y Servicios
# =============================================================================

def render_page2(fb: FBRenderer):
    fb.clear()
    now = datetime.datetime.now(TZ) if TZ else datetime.datetime.now()
    touch = _touch_snapshot()
    active_id = active_action["id"] if active_action else None
    held_for = max(0.0, time.time() - active_action_started) if active_action else 0.0

    # ── Sección 1: Hora pequeña (derecha) ─────────────────────────────────────
    _text_right(fb, 5, now.strftime("%H:%M:%S"), FONT_FECHA, GRIS)
    y = 5 + ROW_FECHA + 2
    _hline(fb, y);  y += 5

    ROW = ROW_DATO + 1

    # ── Sección 2: Hardware ────────────────────────────────────────────────────
    temp_col = VERDE if hw_temp < 60 else AMARILLO if hw_temp <= 70 else ROJO
    _label_row(fb, y, "TEMP", f"{hw_temp:.1f}°C", temp_col)
    _draw_temp_graph(fb, 248, y - 2, 220, 44)
    y += ROW

    ram_pct = hw_ram_used_mib / hw_ram_tot_mib if hw_ram_tot_mib > 0 else 0
    ram_col = (ROJO     if ram_pct > 0.85
               else AMARILLO if ram_pct > 0.70
               else VERDE)
    _label_row(fb, y, "RAM",
               f"{hw_ram_used_mib}MiB / {hw_ram_tot_mib}MiB", BLANCO)
    # Barra RAM (debajo del texto)
    bar_y   = y + _th(FONT_DATO, "0MiB") + 2
    bar_w   = 200
    bar_h   = 8
    fill_px = max(0, min(bar_w, int(bar_w * ram_pct)))
    fb.fill_rect(VAL_X,              bar_y, bar_w,    bar_h, *SEP_COL)
    fb.fill_rect(VAL_X,              bar_y, fill_px,  bar_h, *ram_col)
    y += ROW + bar_h + 2

    days  = hw_uptime_s // 86400
    hours = (hw_uptime_s % 86400) // 3600
    mins  = (hw_uptime_s % 3600)  // 60
    _label_row(fb, y, "UPTIME", f"{days}d {hours}h {mins}m", BLANCO)
    y += ROW

    rb_col = VERDE if reboots_24h <= 1 else AMARILLO if reboots_24h <= 3 else ROJO
    fb.draw_text(LBL_X, y + 4, tr("lbl_reboots"), FONT_LABEL, *GRIS)
    fb.draw_text(190, y, str(reboots_24h), FONT_DATO, *rb_col)
    y += ROW

    # ── Sección 3: Almacenamiento ──────────────────────────────────────────────
    if ssd_mounted and ssd_tot_gb > 0:
        pct = ssd_used_gb * 100 / ssd_tot_gb
        _label_row(fb, y, "SSD",
                   f"{ssd_used_gb:.1f} / {ssd_tot_gb:.1f}GB ({pct:.0f}%)", BLANCO)
    else:
        _label_row(fb, y, "SSD", tr("ssd_off"), AMARILLO)
    y += ROW

    _hline(fb, y);  y += 3

    # ── Sección 4: Servicios ───────────────────────────────────────────────────
    svc_items = [
        ("SYN", svc_syncthing, VERDE if svc_syncthing else ROJO, "OK" if svc_syncthing else "OFF"),
        ("PIH", svc_pihole_svc, VERDE if svc_pihole_svc else ROJO, "OK" if svc_pihole_svc else "OFF"),
        ("TS",  svc_tailscale, VERDE if svc_tailscale else ROJO, "OK" if svc_tailscale else "OFF"),
        ("NANO", svc_nanobot, VERDE if svc_nanobot else AMARILLO, "OK" if svc_nanobot else "WAIT"),
    ]
    x = 8
    for label, _active, col, state in svc_items:
        fb.draw_text(x, y, f"{label}:{state}", FONT_BAR, *col)
        x += 112

    # ── Respaldo y voltaje (libre mientras no haya mensaje de acción en y=205) ─
    btxt, bcol = backup_status(backup_age_h, backup_failed)
    fb.draw_text(8, 184, btxt, FONT_BAR, *bcol)
    warn = throttle_warning(hw_throttled)
    if warn:
        _text_right(fb, 184, warn, FONT_BAR, ROJO)

    if action_message and time.time() < action_message_until:
        _text_center(fb, 205, action_message, FONT_BAR, VERDE)
    elif pending_reboot_at:
        remaining = max(0, int(pending_reboot_at - time.time()) + 1)
        _text_center(fb, 205, tr("reboot_in", n=remaining), FONT_BAR, ROJO)
    elif active_action:
        remaining = max(0, int(active_action["hold"] - held_for) + 1)
        _text_center(fb, 205, f"{remaining}...", FONT_BAR, AMARILLO)

    for button in ACTION_BUTTONS:
        progress = 0.0
        is_active = active_id == button["id"]
        if is_active:
            progress = min(1.0, held_for / button["hold"])
        _draw_button(fb, button, progress, is_active)

    if touch["down"] and touch["x"] is not None and touch["y"] is not None:
        fb.fill_rect(touch["x"] - 2, touch["y"] - 2, 5, 5, *AMARILLO)

    _bottom_bar(fb, 2)


# =============================================================================
#  Página 3 — Pi-hole en detalle + interruptor de la pantalla negra nocturna
# =============================================================================

def _ellipsize(font, text: str, max_w: int) -> str:
    if _tw(font, text) <= max_w:
        return text
    while text and _tw(font, text + "…") > max_w:
        text = text[:-1]
    return text + "…"


def _draw_top_list(fb: FBRenderer, y: int, items: list, bar_col: tuple, text_col: tuple):
    """Filas con barra de fondo proporcional al valor más alto."""
    row_h = 20
    if not items:
        fb.draw_text(LBL_X + 2, y, tr("no_data"), FONT_BAR, *GRIS)
        return
    top = max(v for _n, v in items) or 1
    for i, (name, val) in enumerate(items):
        ry = y + i * row_h
        bw = max(2, int(468 * val / top))
        fb.fill_rect(6, ry, bw, row_h - 2, *bar_col)
        count = str(val)
        fb.draw_text(FB_W - _tw(FONT_BAR, count) - 10, ry, count, FONT_BAR, *text_col)
        fb.draw_text(10, ry, _ellipsize(FONT_BAR, name, 360), FONT_BAR, *BLANCO)


def _draw_night_toggle(fb: FBRenderer):
    x, y, w, h = NIGHT_TOGGLE_RECT
    fb.fill_rect(x, y, w, h, 34, 34, 34)
    border = VERDE if night_enabled else (84, 84, 84)
    for i in range(2):
        fb.draw_line(x + i, y + i, x + w - 1 - i, y + i, *border)
        fb.draw_line(x + i, y + h - 1 - i, x + w - 1 - i, y + h - 1 - i, *border)
        fb.draw_line(x + i, y + i, x + i, y + h - 1 - i, *border)
        fb.draw_line(x + w - 1 - i, y + i, x + w - 1 - i, y + h - 1 - i, *border)
    fb.draw_text(x + 12, y + 3, tr("night_title"), FONT_BAR, *BLANCO)
    fb.draw_text(x + 12, y + 19, tr("night_range", a=NIGHT_START, b=NIGHT_END), FONT_SMALL, *GRIS)
    # interruptor tipo píldora
    pw, ph = 58, 24
    px, py = x + w - pw - 14, y + (h - ph) // 2
    fb.draw.rounded_rectangle([px, py, px + pw, py + ph], radius=ph // 2,
                              fill=VERDE if night_enabled else (70, 70, 70))
    kx = px + pw - ph + 3 if night_enabled else px + 3
    fb.draw.ellipse([kx, py + 3, kx + ph - 6, py + ph - 3], fill=BLANCO)
    label = tr("yes") if night_enabled else tr("no")
    lx = px + 9 if night_enabled else px + pw - 9 - _tw(FONT_SMALL, label)
    fb.draw_text(lx, py + 5, label, FONT_SMALL, *(NEGRO if night_enabled else BLANCO))


def render_page3(fb: FBRenderer):
    fb.clear()

    # ── Encabezado: resumen 24 h ──────────────────────────────────────────────
    fb.draw_text(8, 4, "Pi-hole · 24 h", FONT_FECHA, *BLANCO)
    if pihole_ok:
        resumen = tr("ph_summary", n=pihole_total, p=pihole_pct)
        _text_right(fb, 7, resumen, FONT_BAR, VERDE)
    else:
        _text_right(fb, 7, tr("no_conn"), FONT_BAR, ROJO)
    y = 4 + ROW_FECHA + 3
    _hline(fb, y);  y += 4

    # ── Top clientes ──────────────────────────────────────────────────────────
    fb.draw_text(LBL_X, y, tr("top_clients"), FONT_LABEL, *GRIS)
    y += 21
    _draw_top_list(fb, y, top_clients, (22, 46, 36), VERDE)
    y += 4 * 20 + 3
    _hline(fb, y);  y += 4

    # ── Top dominios bloqueados ───────────────────────────────────────────────
    fb.draw_text(LBL_X, y, tr("top_blocked"), FONT_LABEL, *GRIS)
    y += 21
    _draw_top_list(fb, y, top_blocked, (58, 28, 28), ROJO)

    _draw_night_toggle(fb)
    _bottom_bar(fb, 3)


PAGES = [render_page1, render_page2, render_page3]


# =============================================================================
#  Loop principal
# =============================================================================

_last_drawn_second = -1     # forzar primer render
_last_anim_frame = -1
_last_watchdog = 0.0
_recent_errors = deque(maxlen=20)


def _sd_notify(msg: str):
    """Avisa a systemd (WatchdogSec) sin dependencias externas."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError:
        pass


def _tick(fb):
    """Una iteración del bucle principal (aislada para poder capturar sus excepciones)."""
    global current_page, last_page_change
    global last_update_red, last_update_clima, last_update_pihole
    global last_pihole_auth, last_update_hw, last_update_reboots, last_update_services
    global last_update_health
    global night_enabled, wake_until, last_update_top
    global _last_drawn_second, _last_anim_frame, _last_watchdog
    now_t = time.time()

    # ── Refrescos periódicos ──────────────────────────────────────────────
    if now_t - last_update_red >= 5:
        run_async("fetch-network", fetch_network)
        last_update_red = now_t

    if now_t - last_update_clima >= 600:
        fetch_clima_async()

    if now_t - last_pihole_auth >= 1500:
        run_async("pihole-auth", pihole_auth)
        last_pihole_auth = now_t

    if now_t - last_update_pihole >= 30:
        fetch_pihole_async()

    if now_t - last_update_top >= 60:
        fetch_pihole_top_async()

    if now_t - last_update_hw >= 5:
        fetch_hw()
        last_update_hw = now_t

    if now_t - last_update_services >= 30:
        run_async("fetch-services", fetch_services)
        last_update_services = now_t

    if now_t - last_update_health >= 60:
        run_async("fetch-health", fetch_health)
        last_update_health = now_t

    if now_t - last_update_reboots >= 300:
        run_async("fetch-reboots", fetch_reboots_24h)
        last_update_reboots = now_t

    force_draw = _handle_action_touch(now_t)

    # ── Pantalla negra nocturna ──────────────────────────────────────────────────
    now_dt = datetime.datetime.now(TZ) if TZ else datetime.datetime.now()
    night_active = night_enabled and _in_night_window(now_dt)
    screen_on = (not night_active) or now_t < wake_until
    if not screen_on:
        _set_screen(fb, False)
    elif not backlight_on:
        force_draw = True   # al encender, dibujar primero y encender después
        fb.invalidate()

    # ── Toque manual ──────────────────────────────────────────────────────
    if TOUCH_EVENT.is_set():
        TOUCH_EVENT.clear()
        if not screen_on:
            # Pantalla apagada: el toque solo la despierta, no cambia de página
            wake_until       = now_t + NIGHT_WAKE_SECONDS
            last_page_change = now_t
            force_draw       = True
        else:
            if night_active:
                wake_until = now_t + NIGHT_WAKE_SECONDS   # prolongar mientras se usa
            press = _touch_press_snapshot()
            button = _button_at(press["x"], press["y"]) if current_page == 1 else None
            if button:
                _start_action(button, now_t)
                last_page_change = now_t
                force_draw = True
            elif current_page == 2 and _rect_contains(NIGHT_TOGGLE_RECT, press["x"], press["y"]):
                night_enabled = not night_enabled
                _save_state()
                last_page_change = now_t
                force_draw = True
            else:
                current_page      = (current_page + 1) % NUM_PAGES
                last_page_change  = now_t
                _last_drawn_second = -1   # forzar redibujado inmediato

    # ── Cambio automático de página cada 10 s ─────────────────────────────
    if now_t - last_page_change >= 10:
        current_page      = (current_page + 1) % NUM_PAGES
        last_page_change  = now_t
        _last_drawn_second = -1   # forzar redibujado inmediato al cambiar página

    # ── Render — solo con pantalla encendida ──────────────────────────────
    current_second = int(now_t)
    current_anim_frame = int(now_t * 2)
    if screen_on and (force_draw or current_second != _last_drawn_second or current_anim_frame != _last_anim_frame):
        PAGES[current_page](fb)
        fb.flush()
        _set_screen(fb, True)
        _last_drawn_second = current_second
        _last_anim_frame = current_anim_frame

    if now_t - _last_watchdog >= 10:
        _sd_notify("WATCHDOG=1")
        _last_watchdog = now_t



def main():
    global _renderer
    global current_page, last_page_change
    global last_update_red, last_update_clima, last_update_pihole
    global last_pihole_auth, last_update_hw, last_update_reboots
    global night_enabled, wake_until, last_update_top, last_update_services
    global last_update_health

    _load_state()
    fb = FBRenderer()
    _renderer = fb

    # Carga inicial de todos los datos
    pihole_auth()
    last_pihole_auth = time.time()

    fetch_network()
    last_update_red = time.time()

    fetch_clima()
    last_update_clima = time.time()

    fetch_pihole()
    last_update_pihole = time.time()

    fetch_pihole_top()
    last_update_top = time.time()

    fetch_hw()
    last_update_hw = time.time()

    fetch_services()
    last_update_services = time.time()

    fetch_reboots_24h()
    last_update_reboots = time.time()

    fetch_health()
    last_update_health = time.time()

    last_page_change  = time.time()
    # Thread de touch (daemon: muere solo cuando termina el proceso principal)
    threading.Thread(target=_touch_thread, daemon=True, name="touch").start()

    _sd_notify("READY=1")

    while True:
        time.sleep(0.1)
        try:
            _tick(fb)
        except Exception as e:
            now = time.time()
            log_error(f"loop: {e!r}\n{traceback.format_exc()}")
            _recent_errors.append(now)
            # Muchos errores seguidos: salir para que systemd reinicie el proceso limpio
            if len(_recent_errors) >= 20 and now - _recent_errors[0] < 60:
                log_error("demasiados errores en el bucle; saliendo para que systemd reinicie")
                sys.exit(1)
            time.sleep(0.5)

if __name__ == "__main__":
    main()


# =============================================================================
#  TROUBLESHOOTING
# =============================================================================

# PANTALLA NEGRA / PERMISO DENEGADO:
#   ls -la /dev/fb1                  → debe existir con permisos rw
#   groups root                      → root ya tiene acceso
#   # Si se ejecuta sin root:
#   sudo chmod 660 /dev/fb1
#   sudo usermod -aG video TU_USUARIO
#   # Verificar que el framebuffer funciona:
#   cat /dev/urandom > /dev/fb1      → debe aparecer ruido de color

# ERROR mmap.mmap() / 'invalid argument':
#   Verificar tamaño real del fb:
#   cat /sys/class/graphics/fb1/virtual_size  → debe ser 480,320
#   cat /sys/class/graphics/fb1/bits_per_pixel → debe ser 16

# TEXTO NO APARECE (fuente no encontrada):
#   ls /usr/share/fonts/truetype/dejavu/
#   sudo apt install -y fonts-dejavu-core
#   # Fuente bitmap de fallback activa si no hay TTF — texto muy pequeño pero visible

# PIL NO INSTALADO:
#   sudo apt install -y python3-pil
#   python3 -c "from PIL import Image; print('PIL OK')"

# NUMPY NO DISPONIBLE (flush lento ~150 ms):
#   sudo apt install -y python3-numpy   ← recomendado, mejora flush a ~2 ms
#   python3 -c "import numpy; print('numpy OK')"

# PI-HOLE CAÍDO:
#   curl -s -X POST http://localhost:8089/api/auth \
#     -H "Content-Type: application/json" -d '{"password":"TU_PASSWORD"}'
#   ss -tlnp | grep FTL              → debe escuchar en :8089

# CLIMA NO DISPONIBLE:
#   curl "http://api.openweathermap.org/data/2.5/weather?q=Lima,PE&appid=TU_KEY&units=metric"
#   Las keys nuevas de OWM tardan hasta 2 horas en activarse

# IMAGEN ROTADA:
#   sudo nano /boot/firmware/config.txt
#   Buscar dtoverlay=piscreen y cambiar rotate=270 por rotate=90, 180 o 0
#   sudo reboot -f

# SSD NO MONTADO:
#   lsblk                            → verificar dispositivo
#   mount | grep ssd                 → verificar punto de montaje /mnt/ssd
