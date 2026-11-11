# -*- coding: utf-8 -*-
"""Configuration file overlay_mixer.ini (since v2.0.0): parsing, errors, use in main()."""
import configparser
import io
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from helpers import PROJECT_DIR, MixerTestCase

EXAMPLE_FILE = PROJECT_DIR / "overlay_mixer.example.ini"


class ConfigTestCase(MixerTestCase):
    def write_config(self, text, name="overlay_mixer.ini", encoding="utf-8"):
        path = self.root / name
        path.write_text(text, encoding=encoding)
        return path

    def load(self, text, **kwargs):
        return self.mixer.load_config(self.write_config(text, **kwargs))

    def keep_config_constants(self):
        """apply_config() changes module constants - restore them after the test."""
        names = {name for settings in self.mixer.CONFIG_SETTINGS.values() for name, _ in settings.values()}
        patcher = mock.patch.multiple(self.mixer, **{name: getattr(self.mixer, name) for name in names})
        patcher.start()
        self.addCleanup(patcher.stop)


class LoadConfigTest(ConfigTestCase):
    def test_missing_file_keeps_all_defaults(self):
        self.assertEqual(self.mixer.load_config(self.root / "missing.ini"), {})

    def test_paths(self):
        values = self.load(
            "[paths]\n"
            "input_dir = C:\\Radio\\Input\n"
            "atmo_dir = \\\\nas\\share\\Atmo\n"
            "output_dir = D:\\Radio\\Import\n"
            "error_dir = C:\\Radio\\Error\n"
            "archive_dir = C:\\Radio\\Archive\n"
            "log_file = C:\\Radio\\overlay_mixer.log\n")
        self.assertEqual(values, {
            "SCHNEIDEN_DIR": Path(r"C:\Radio\Input"), "ATMO_DIR": Path(r"\\nas\share\Atmo"),
            "FERTIG_DIR": Path(r"D:\Radio\Import"), "FEHLER_DIR": Path(r"C:\Radio\Error"),
            "ARCHIV_DIR": Path(r"C:\Radio\Archive"), "LOG_FILE": Path(r"C:\Radio\overlay_mixer.log")})

    def test_texts_numbers_and_quotes(self):
        values = self.load(
            "[programs]\nffmpeg = C:\\Tools\\ffmpeg.exe\nffprobe = ffprobe\n"
            "[mix]\nleadin_seconds = 5\nbuffer_seconds = 2.5\nbitrate = 256k\n"
            "[output]\nprefix = \"VAH \"\n"
            "[tags]\nenable_date_tag = Enable Date\nexpiry_date_tag = Expiry Date\n"
            "enable_days_before = 14\nexpiry_days_after = 1\ndate_format = %Y-%m-%d\n"
            "media_type = News\ntext_field = Album\ntext = Veranstaltungshinweis\ncomment_language = deu\n")
        self.assertEqual(values, {
            "FFMPEG_BIN": r"C:\Tools\ffmpeg.exe", "FFPROBE_BIN": "ffprobe",
            "LEADIN_SECONDS": 5, "BUFFER_SECONDS": 2.5, "BITRATE": "256k", "OUTPUT_PREFIX": "VAH ",
            "TAG_ENABLE_DATE": "Enable Date", "TAG_EXPIRY_DATE": "Expiry Date",
            "ENABLE_DAYS_BEFORE": 14, "EXPIRY_DAYS_AFTER": 1, "TAG_DATE_FORMAT": "%Y-%m-%d",
            "ID3_MEDIA_TYPE": "News", "ID3_TEXT_FIELD": "Album", "ID3_TEXT": "Veranstaltungshinweis",
            "ID3_COMMENT_LANGUAGE": "deu"})
        self.assertIsInstance(values["LEADIN_SECONDS"], int)   # keeps the log output "LeadIn=5s"

    def test_empty_texts_switch_tags_off(self):
        values = self.load('[tags]\nmedia_type =\ntext = ""\n')
        self.assertEqual(values, {"ID3_MEDIA_TYPE": "", "ID3_TEXT": ""})

    def test_loudness(self):
        values = self.load("[loudness]\ntarget_lufs = -16\ntolerance_lu = 0.5\npeak_limit_db = -1.5\n"
                           "highpass_hz = 100\nmax_gain_db = 12\n")
        self.assertEqual(values, {"LOUDNESS_TARGET_LUFS": -16.0, "LOUDNESS_TOLERANCE_LU": 0.5,
                                  "LOUDNESS_PEAK_LIMIT_DB": -1.5, "LOUDNESS_HIGHPASS_HZ": 100,
                                  "LOUDNESS_MAX_GAIN_DB": 12.0})

    def test_loudness_off(self):
        for raw in ("off", "OFF", "none", "", '""'):
            with self.subTest(raw=raw):
                self.assertEqual(self.load(f"[loudness]\ntarget_lufs = {raw}\n"), {"LOUDNESS_TARGET_LUFS": None})

    def test_sections_and_keys_are_case_insensitive(self):
        self.assertEqual(self.load("[Paths]\nINPUT_DIR = C:\\In\n"), {"SCHNEIDEN_DIR": Path(r"C:\In")})

    def test_comments_and_utf8_with_bom(self):
        values = self.load("; comment\n# another comment\n[paths]\n; input folder\ninput_dir = C:\\Ö Radio\\Eingang\n",
                           encoding="utf-8-sig")
        self.assertEqual(values, {"SCHNEIDEN_DIR": Path(r"C:\Ö Radio\Eingang")})

    def test_environment_variables_in_paths(self):
        with mock.patch.dict(os.environ, {"VAH_TEST_HOME": r"C:\Users\someone"}):
            values = self.load("[paths]\ninput_dir = %VAH_TEST_HOME%\\OneDrive\\Input\n")
        self.assertEqual(values, {"SCHNEIDEN_DIR": Path(r"C:\Users\someone\OneDrive\Input")})


class ConfigErrorTest(ConfigTestCase):
    def assertConfigError(self, text, message):
        with self.assertRaises(self.mixer.ConfigError) as caught:
            self.load(text)
        self.assertIn(message, str(caught.exception))

    def test_unknown_section(self):
        self.assertConfigError("[pathes]\ninput_dir = C:\\In\n", "unknown section [pathes]")

    def test_unknown_setting(self):
        self.assertConfigError("[paths]\ninptu_dir = C:\\In\n", "unknown setting 'inptu_dir' in [paths]")

    def test_invalid_numbers(self):
        for text in ("[mix]\nleadin_seconds = five\n", "[tags]\nenable_days_before = 14.5\n",
                     "[loudness]\ntarget_lufs = loud\n"):
            with self.subTest(text=text):
                self.assertConfigError(text, "invalid value for")

    def test_empty_path(self):
        self.assertConfigError("[paths]\noutput_dir =\n", "invalid value for 'output_dir' in [paths]")

    def test_syntax_errors(self):
        for text in ("input_dir = C:\\In\n", "[paths]\ninput_dir\n", "[paths]\ninput_dir = a\ninput_dir = b\n"):
            with self.subTest(text=text):
                self.assertConfigError(text, "cannot read overlay_mixer.ini")

    def test_not_utf8(self):
        path = self.root / "overlay_mixer.ini"
        path.write_bytes("[paths]\ninput_dir = C:\\Ä\n".encode("cp1252"))
        with self.assertRaisesRegex(self.mixer.ConfigError, "cannot read overlay_mixer.ini"):
            self.mixer.load_config(path)


class ApplyConfigTest(ConfigTestCase):
    def test_apply_sets_module_constants(self):
        self.keep_config_constants()
        self.mixer.apply_config({"ID3_TEXT": "Test", "LEADIN_SECONDS": 3})
        self.assertEqual(self.mixer.ID3_TEXT, "Test")
        self.assertEqual(self.mixer.LEADIN_SECONDS, 3)
        self.assertEqual(self.mixer.id3_text_tag(), ("Album", "Test"))


class ExampleFileTest(ConfigTestCase):
    """overlay_mixer.example.ini is the documentation of all settings - it has to stay complete."""

    def test_example_file_is_valid_and_complete(self):
        values = self.mixer.load_config(EXAMPLE_FILE)
        expected = {name for settings in self.mixer.CONFIG_SETTINGS.values() for name, _ in settings.values()}
        self.assertEqual(set(values), expected)

    def test_example_file_uses_the_built_in_defaults_except_paths(self):
        defaults = load_defaults(self.mixer)
        values = self.mixer.load_config(EXAMPLE_FILE)
        for name, value in values.items():
            if name in ("SCHNEIDEN_DIR", "ATMO_DIR", "FERTIG_DIR", "FEHLER_DIR", "ARCHIV_DIR", "LOG_FILE"):
                continue
            with self.subTest(name=name):
                self.assertEqual(value, defaults[name])

    def test_example_file_contains_only_placeholder_paths(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(EXAMPLE_FILE, encoding="utf-8")
        for key, value in parser.items("paths"):
            with self.subTest(key=key):
                self.assertTrue(value.startswith(("%USERPROFILE%\\Announcements\\", "D:\\Audio\\")), value)


def load_defaults(mixer):
    """Built-in defaults as written in the script (independent of test patches)."""
    source = Path(mixer.__file__).read_text(encoding="utf-8")
    namespace = {}
    names = {name for settings in mixer.CONFIG_SETTINGS.values() for name, _ in settings.values()}
    for line in source.splitlines():
        name = line.split("=", 1)[0].strip()
        if name in names and line.startswith(name):
            exec(line, {"Path": Path, "BASE_DIR": Path(mixer.__file__).parent}, namespace)
    return namespace


class MainWithConfigTest(ConfigTestCase):
    """main() reads CONFIG_FILE first. MixerTestCase redirects CONFIG_FILE into the test folder."""

    def run_main_until_startup(self):
        saved_handlers = list(self.mixer.logger.handlers)
        saved_hook = threading.excepthook

        def restore():
            for handler in self.mixer.logger.handlers:
                handler.close()
            self.mixer.logger.handlers[:] = saved_handlers
            threading.excepthook = saved_hook

        self.addCleanup(restore)
        with mock.patch("sys.stdout", io.StringIO()), \
             mock.patch.object(self.mixer, "disable_quick_edit", return_value=False), \
             mock.patch.object(self.mixer, "wait_for_dirs", side_effect=KeyboardInterrupt):
            code = self.mixer.main()
        return code, self.mixer.LOCAL_LOG_FILE.read_text(encoding="utf-8")

    def test_main_uses_the_configuration_file(self):
        self.keep_config_constants()
        self.write_config(f"[paths]\ninput_dir = {self.eingang}\n[tags]\ntext = Test\n[loudness]\ntarget_lufs = off\n")
        code, log = self.run_main_until_startup()
        self.assertEqual(code, 0)
        self.assertIn(f"Configuration: {self.mixer.CONFIG_FILE} (3 settings)", log)
        self.assertIn("ID3 text: Album = Test", log)
        self.assertIn("Loudness: off (no target configured)", log)

    def test_main_without_configuration_file_warns_and_uses_defaults(self):
        code, log = self.run_main_until_startup()
        self.assertEqual(code, 0)
        self.assertIn(f"[WARNING] Configuration: no file found ({self.mixer.CONFIG_FILE}) | using built-in defaults",
                      log)

    def test_main_stops_on_configuration_error(self):
        self.keep_config_constants()
        self.write_config("[paths]\ninptu_dir = C:\\In\n")
        code, log = self.run_main_until_startup()
        self.assertEqual(code, 1)
        self.assertIn(f"[ERROR] Configuration error in {self.mixer.CONFIG_FILE}: unknown setting 'inptu_dir' "
                      f"in [paths] | exiting.", log)
        self.assertNotIn("started", log)
        self.assertFalse(self.mixer.PID_FILE.exists())


if __name__ == "__main__":
    unittest.main()
