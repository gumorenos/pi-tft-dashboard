"""Pruebas de la lógica pura de dashboard.py (no tocan el framebuffer ni la red).

Ejecutar desde la raíz del repo:  python3 -m unittest discover -s tests -v
Necesita lo mismo que el dashboard (psutil y Pillow), pero no /dev/fb1 ni el táctil.
"""
import datetime
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ["DASHBOARD_LANG"] = "es"   # las pruebas de textos asumen español salvo que parcheen d.LANG
import dashboard as d  # noqa: E402


def at(hour, minute=0):
    return datetime.datetime(2026, 10, 1, hour, minute)


class NightWindow(unittest.TestCase):
    def test_window_crossing_midnight(self):
        with mock.patch.object(d, "NIGHT_START", 23), mock.patch.object(d, "NIGHT_END", 6):
            self.assertTrue(d._in_night_window(at(23)))
            self.assertTrue(d._in_night_window(at(0, 29)))
            self.assertTrue(d._in_night_window(at(5, 59)))
            self.assertFalse(d._in_night_window(at(6)))
            self.assertFalse(d._in_night_window(at(12)))
            self.assertFalse(d._in_night_window(at(22, 59)))

    def test_window_same_day(self):
        with mock.patch.object(d, "NIGHT_START", 1), mock.patch.object(d, "NIGHT_END", 5):
            self.assertTrue(d._in_night_window(at(1)))
            self.assertFalse(d._in_night_window(at(5)))
            self.assertFalse(d._in_night_window(at(0)))

    def test_equal_hours_means_never(self):
        with mock.patch.object(d, "NIGHT_START", 6), mock.patch.object(d, "NIGHT_END", 6):
            self.assertFalse(any(d._in_night_window(at(h)) for h in range(24)))


class TouchScaling(unittest.TestCase):
    def test_scale_endpoints_and_clamp(self):
        self.assertEqual(d._scale_touch(0, 0, 4095, 479), 0)
        self.assertEqual(d._scale_touch(4095, 0, 4095, 479), 479)
        self.assertEqual(d._scale_touch(-50, 0, 4095, 479), 0)       # por debajo: se recorta
        self.assertEqual(d._scale_touch(9999, 0, 4095, 479), 479)    # por encima: se recorta
        self.assertIsNone(d._scale_touch(None, 0, 4095, 479))
        self.assertIsNone(d._scale_touch(10, 5, 5, 479))             # rango degenerado

    def test_to_screen_swap_invert_and_offset(self):
        cfg = dict(TOUCH_RAW_X_MIN=0, TOUCH_RAW_X_MAX=4095, TOUCH_RAW_Y_MIN=0, TOUCH_RAW_Y_MAX=4095,
                   TOUCH_SWAP_XY=False, TOUCH_INVERT_X=False, TOUCH_INVERT_Y=False,
                   TOUCH_X_OFFSET=0, TOUCH_Y_OFFSET=0)
        with mock.patch.multiple(d, **cfg):
            self.assertEqual(d._touch_to_screen(0, 0), (0, 0))
            self.assertEqual(d._touch_to_screen(4095, 4095), (d.FB_W - 1, d.FB_H - 1))
        with mock.patch.multiple(d, **{**cfg, "TOUCH_INVERT_X": True}):
            self.assertEqual(d._touch_to_screen(0, 0), (d.FB_W - 1, 0))
        with mock.patch.multiple(d, **{**cfg, "TOUCH_SWAP_XY": True}):
            self.assertEqual(d._touch_to_screen(0, 4095), (d.FB_W - 1, 0))
        with mock.patch.multiple(d, **{**cfg, "TOUCH_X_OFFSET": 5000}):
            self.assertEqual(d._touch_to_screen(0, 0)[0], d.FB_W - 1)   # el desplazamiento no se sale


class Throttle(unittest.TestCase):
    def test_normal_state_has_no_warning(self):
        self.assertEqual(d.throttle_warning(0), "")
        self.assertEqual(d.throttle_warning(None), "")
        # frecuencia limitada por temperatura (el estado real de este Pi): no es un fallo
        self.assertEqual(d.throttle_warning(0x20002), "")
        self.assertEqual(d.throttle_warning(0xA000A), "")

    def test_undervoltage(self):
        self.assertEqual(d.throttle_warning(0x1), "VOLTAJE BAJO")        # ahora
        self.assertEqual(d.throttle_warning(0x10000), "VOLTAJE BAJO")    # desde el arranque
        self.assertEqual(d.throttle_warning(0x50005), "VOLTAJE BAJO")    # gana a la limitación

    def test_cpu_throttled(self):
        self.assertEqual(d.throttle_warning(0x4), "CPU LIMITADA")
        self.assertEqual(d.throttle_warning(0x40000), "CPU LIMITADA")


class Backups(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 1, 12, 0)

    def test_age_uses_newest_valid_folder(self):
        names = ["20260929-033001", "20261001-033001", "basura", ".repo", "backup.log"]
        self.assertAlmostEqual(d.backup_age_hours(names, self.NOW), 8.5, places=2)

    def test_age_none_without_backups(self):
        self.assertIsNone(d.backup_age_hours([], self.NOW))
        self.assertIsNone(d.backup_age_hours(["backup.log", ".repo"], self.NOW))

    def test_age_never_negative(self):
        self.assertEqual(d.backup_age_hours(["20261002-000000"], self.NOW), 0.0)

    def test_status_text_and_color(self):
        self.assertEqual(d.backup_status(8.9, False), ("RESPALDO hace 8h", d.VERDE))
        self.assertEqual(d.backup_status(40, False)[1], d.AMARILLO)
        self.assertEqual(d.backup_status(60, False), ("RESPALDO hace 2d", d.ROJO))
        self.assertEqual(d.backup_status(None, False), ("RESPALDO: ninguno", d.ROJO))
        self.assertEqual(d.backup_status(1, True), ("RESPALDO: error", d.ROJO))   # el error manda


class Reboots(unittest.TestCase):
    def test_counts_only_last_24h(self):
        now = int(time.time())
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write(f"{now - 100000} viejo wdt=0 throttled=0x0\n")
            f.write(f"{now - 3600} reciente wdt=0 throttled=0x0\n")
            f.write(f"{now - 60} ahora wdt=0 throttled=0x0\n")
            path = f.name
        try:
            with mock.patch.object(d, "BOOTS_LOG", path):
                d.fetch_reboots_24h()
            self.assertEqual(d.reboots_24h, 2)
        finally:
            os.unlink(path)


class Language(unittest.TestCase):
    def test_all_languages_have_the_same_keys(self):
        self.assertEqual(set(d.STRINGS["es"]), set(d.STRINGS["en"]))
        for key in d.STRINGS["es"]:
            self.assertEqual(type(d.STRINGS["es"][key]), type(d.STRINGS["en"][key]), key)
        self.assertEqual(len(d.STRINGS["es"]["dias"]), 7)
        self.assertEqual(len(d.STRINGS["en"]["meses"]), 13)

    def test_every_text_formats_in_both_languages(self):
        values = dict(n=3, p=16.9, a=23, b=6)
        for lang in ("es", "en"):
            with mock.patch.object(d, "LANG", lang):
                for key, text in d.STRINGS[lang].items():
                    if isinstance(text, str):
                        self.assertTrue(d.tr(key, **values), (lang, key))

    def test_english_texts_are_used(self):
        with mock.patch.object(d, "LANG", "en"):
            self.assertEqual(d.backup_status(8.9, False), ("BACKUP 8h ago", d.VERDE))
            self.assertEqual(d.throttle_warning(0x1), "LOW VOLTAGE")
            self.assertEqual(d._age_text(0), "not updated")
            self.assertEqual(d.tr("night_range", a=23, b=6), "23:00 - 06:00")


class Pets(unittest.TestCase):
    PETS = ("beagle", "shepherd", "tabby", "white-cat")

    def _blank(self):
        from PIL import Image, ImageDraw
        fb = d.FBRenderer.__new__(d.FBRenderer)     # sin abrir /dev/fb1
        fb.img = Image.new("RGB", (480, 320))
        fb.draw = ImageDraw.Draw(fb.img)
        return fb

    def test_every_pet_draws_something_in_every_frame(self):
        for name in self.PETS:
            with mock.patch.object(d, "PET", name):
                for frame in range(10):
                    fb = self._blank()
                    d._draw_pet(fb, 430, 248, frame)
                    self.assertIsNotNone(fb.img.getbbox(), (name, frame))

    def test_pets_stay_inside_their_corner(self):
        for name in self.PETS:
            with mock.patch.object(d, "PET", name):
                fb = self._blank()
                d._draw_pet(fb, 430, d.BODY_H - 48, 1)
                x0, y0, x1, y1 = fb.img.getbbox()
                self.assertGreaterEqual(x0, 420, name)
                self.assertLessEqual(x1, d.FB_W, name)
                self.assertLessEqual(y1, d.BODY_H, name)    # no pisa la barra inferior

    def test_none_draws_nothing(self):
        with mock.patch.object(d, "PET", "none"):
            fb = self._blank()
            d._draw_pet(fb, 430, 248, 1)
            self.assertIsNone(fb.img.getbbox())

    def test_configured_pet_is_always_valid(self):
        self.assertIn(d.PET, self.PETS + ("none",))


class Misc(unittest.TestCase):
    def test_age_text(self):
        now = time.time()
        self.assertEqual(d._age_text(0), "sin actualizar")
        self.assertEqual(d._age_text(now - 5), "hace <1 min")
        self.assertEqual(d._age_text(now - 5 * 60), "hace 5 min")
        self.assertEqual(d._age_text(now - 3 * 3600), "hace 3 h")

    def test_rect_contains(self):
        rect = (20, 254, 440, 36)
        self.assertTrue(d._rect_contains(rect, 20, 254))
        self.assertTrue(d._rect_contains(rect, 459, 289))
        self.assertFalse(d._rect_contains(rect, 460, 254))
        self.assertFalse(d._rect_contains(rect, 100, 300))
        self.assertFalse(d._rect_contains(rect, None, 260))


if __name__ == "__main__":
    unittest.main()
