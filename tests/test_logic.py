# -*- coding: utf-8 -*-
"""Business logic without FFmpeg: configuration, dates, file names, atmo selection, queue."""
import contextlib
import os
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from queue import Queue
from unittest import mock

from watchdog.events import DirCreatedEvent, FileCreatedEvent, FileModifiedEvent

from helpers import PATH_CONSTANTS, PRODUCTION_ATMO_SECONDS, MixerTestCase, start_thread, wait_until


class TestumgebungTest(MixerTestCase):
    def test_alle_pfade_zeigen_in_den_testordner(self):
        for name in PATH_CONSTANTS:
            if hasattr(self.mixer, name):
                path = getattr(self.mixer, name)
                self.assertTrue(path.is_relative_to(self.root), f"{name} zeigt auf {path}")


class KonfigurationTest(MixerTestCase):
    """Values that work according to the project description and must not change unintentionally."""

    def test_feste_werte(self):
        self.assertEqual(self.mixer.LEADIN_SECONDS, 5)
        self.assertEqual(self.mixer.BUFFER_SECONDS, 2)
        self.assertEqual(self.mixer.BITRATE, "320k")
        self.assertEqual(self.mixer.OUTPUT_PREFIX, "VAH ")

    def test_erlaubte_dateitypen(self):
        # .mp4 since v1.5.0
        self.assertEqual(self.mixer.ALLOWED_EXTS, {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".mp4"})

    def test_id3_tag_einstellungen(self):
        self.assertEqual(self.mixer.TAG_ENABLE_DATE, "Enable Date")
        self.assertEqual(self.mixer.TAG_EXPIRY_DATE, "Expiry Date")
        self.assertEqual(self.mixer.ENABLE_DAYS_BEFORE, 14)
        self.assertEqual(self.mixer.EXPIRY_DAYS_AFTER, 1)
        self.assertEqual(self.mixer.TAG_DATE_FORMAT, "%Y-%m-%d")

    def test_id3_text_einstellungen(self):
        # since v1.6.0: tagging of every generated file
        self.assertEqual(self.mixer.ID3_TEXT_FIELD, "Album")
        self.assertEqual(self.mixer.ID3_TEXT, "Veranstaltungshinweis")
        self.assertEqual(self.mixer.ID3_COMMENT_LANGUAGE, "deu")

    def test_id3_media_type_einstellung(self):
        # since v1.8.0: Media Type (TMED) of every generated file; v1.7.0 still wrote "News" to TCON
        self.assertEqual(self.mixer.ID3_MEDIA_TYPE, "News")
        self.assertFalse(hasattr(self.mixer, "ID3_CONTENT_TYPE"))


    def test_lautheit_einstellungen(self):
        # since v1.10.0: raise quiet voice tracks (variant 2 from the listening tests)
        self.assertEqual(self.mixer.LOUDNESS_TARGET_LUFS, -18.0)
        self.assertEqual(self.mixer.LOUDNESS_TOLERANCE_LU, 1.0)
        self.assertEqual(self.mixer.LOUDNESS_PEAK_LIMIT_DB, -2.0)
        self.assertEqual(self.mixer.LOUDNESS_HIGHPASS_HZ, 80)
        self.assertEqual(self.mixer.LOUDNESS_MAX_GAIN_DB, 20.0)


class LautheitFilterTest(MixerTestCase):
    """loudness_filter(): whether and how the voice track is raised before mixing (since v1.10.0)."""

    LIMITER = "alimiter=limit=-2dB:level=disabled:attack=5:release=50:latency=1"

    def run_filter(self, *measurements, **settings):
        with mock.patch.object(self.mixer, "measure_loudness", side_effect=list(measurements)) as measure, \
             (mock.patch.multiple(self.mixer, **settings) if settings else contextlib.nullcontext()), \
             self.assertLogs("overlay_mixer", "INFO") as logs:
            result = self.mixer.loudness_filter(Path("Stadtfest.wav"))
        return result, logs.output, measure

    def test_leise_datei_wird_angehoben(self):
        result, logs, measure = self.run_filter((-26.2, -2.8), (-26.5, -3.0))
        self.assertEqual(result, "highpass=f=80,volume=8.50dB," + self.LIMITER)
        self.assertEqual(measure.call_args_list, [mock.call(Path("Stadtfest.wav")),
                                                  mock.call(Path("Stadtfest.wav"), "highpass=f=80")])
        self.assertEqual(logs, ["INFO:overlay_mixer:Loudness: -26.2 LUFS, peak -2.8 dBTP | "
                                "raised by +8.5 dB (target -18.0 LUFS, peaks limited to -2.0 dB)"])

    def test_laute_datei_bleibt_unveraendert(self):
        """Only raise, never lower."""
        result, logs, measure = self.run_filter((-12.7, 0.0))
        self.assertIsNone(result)
        self.assertEqual(measure.call_count, 1)
        self.assertEqual(logs, ["INFO:overlay_mixer:Loudness: -12.7 LUFS, peak 0.0 dBTP | "
                                "sufficient (target -18.0 LUFS) | unchanged"])

    def test_toleranz(self):
        for measured, changed in ((-18.0, False), (-18.9, False), (-19.0, False), (-19.1, True)):
            with self.subTest(measured=measured):
                result, _, _ = self.run_filter((measured, -3.0), (measured, -3.0))
                self.assertEqual(result is not None, changed)

    def test_anhebung_ist_begrenzt(self):
        result, logs, _ = self.run_filter((-50.0, -30.0), (-50.3, -30.0))
        self.assertEqual(result, "highpass=f=80,volume=20.00dB," + self.LIMITER)
        self.assertIn("WARNING:overlay_mixer:Loudness: gain capped at +20.0 dB (would need +32.3 dB)", logs)

    def test_ohne_tiefenfilter(self):
        """Without the high-pass filter, a single measurement is enough."""
        result, _, measure = self.run_filter((-26.2, -2.8), LOUDNESS_HIGHPASS_HZ=0)
        self.assertEqual(result, "volume=8.20dB," + self.LIMITER)
        self.assertEqual(measure.call_count, 1)

    def test_eigene_werte(self):
        result, logs, _ = self.run_filter((-30.0, -6.0), (-30.0, -6.0), LOUDNESS_TARGET_LUFS=-16.0,
                                          LOUDNESS_PEAK_LIMIT_DB=-1.5, LOUDNESS_HIGHPASS_HZ=100)
        self.assertEqual(result, "highpass=f=100,volume=14.00dB,"
                                 "alimiter=limit=-1.5dB:level=disabled:attack=5:release=50:latency=1")
        self.assertIn("target -16.0 LUFS, peaks limited to -1.5 dB", logs[0])

    def test_aus(self):
        with mock.patch.object(self.mixer, "measure_loudness") as measure, \
             mock.patch.object(self.mixer, "LOUDNESS_TARGET_LUFS", None):
            self.assertIsNone(self.mixer.loudness_filter(Path("Stadtfest.wav")))
        measure.assert_not_called()

    def test_messfehler_mischt_unveraendert(self):
        result, logs, _ = self.run_filter(RuntimeError("Loudness measurement failed: kaputt"))
        self.assertIsNone(result)
        self.assertEqual(logs, ["WARNING:overlay_mixer:Loudness not measurable (Stadtfest.wav): "
                                "Loudness measurement failed: kaputt | voice track unchanged"])

    def test_messfehler_im_zweiten_durchgang(self):
        result, logs, _ = self.run_filter((-26.2, -2.8), RuntimeError("kaputt"))
        self.assertIsNone(result)
        self.assertIn("WARNING:overlay_mixer:Loudness not measurable (Stadtfest.wav): kaputt | voice track unchanged",
                      logs)

    def test_stille_datei(self):
        for measured in (float("-inf"), -70.0):
            with self.subTest(measured=measured):
                result, logs, _ = self.run_filter((measured, float("-inf")))
                self.assertIsNone(result)
                self.assertEqual(logs, ["WARNING:overlay_mixer:Loudness not measurable (Stadtfest.wav): "
                                        "file is silent | voice track unchanged"])

    def test_startlog(self):
        for target, expected in ((-18.0, "INFO:overlay_mixer:Loudness: target -18.0 LUFS | raise only "
                                          "(tolerance 1.0 LU, at most +20.0 dB) | high-pass 80 Hz | "
                                          "peaks limited to -2.0 dB"),
                                 (None, "INFO:overlay_mixer:Loudness: off (no target configured)")):
            with self.subTest(target=target), mock.patch.object(self.mixer, "LOUDNESS_TARGET_LUFS", target), \
                 self.assertLogs("overlay_mixer", "INFO") as logs:
                self.mixer.log_loudness_setting()
            self.assertEqual(logs.output, [expected])


class Id3TextTagTest(MixerTestCase):
    """id3_text_tag(): which field is written with which text (since v1.6.0)."""

    def tag(self, field, text="Veranstaltungshinweis", media_type=None):
        if media_type is None:
            media_type = self.mixer.ID3_MEDIA_TYPE
        with mock.patch.object(self.mixer, "ID3_TEXT_FIELD", field), mock.patch.object(self.mixer, "ID3_TEXT", text), \
             mock.patch.object(self.mixer, "ID3_MEDIA_TYPE", media_type):
            return self.mixer.id3_text_tag()

    def test_standard_ist_album(self):
        self.assertEqual(self.mixer.id3_text_tag(), ("Album", "Veranstaltungshinweis"))

    def test_waehlbare_felder(self):
        for field in ("Album", "Genre", "Comment", "Kategorie", "Mein eigenes Feld"):
            with self.subTest(field=field):
                self.assertEqual(self.tag(field), (field, "Veranstaltungshinweis"))

    def test_tmed_nur_ohne_media_type(self):
        """Since v1.8.0 ID3_MEDIA_TYPE writes the TMED field; the ID3 text must then not overwrite it."""
        for field in ("TMED", "tmed", "Media Type"):
            with self.subTest(field=field):
                self.assertIsNone(self.tag(field))
                self.assertEqual(self.tag(field, media_type=""), (field, "Veranstaltungshinweis"))

    def test_feldname_wird_von_leerzeichen_befreit(self):
        self.assertEqual(self.tag("  Genre "), ("Genre", "Veranstaltungshinweis"))

    def test_eigener_text(self):
        self.assertEqual(self.tag("Album", "VAH – Community Radio"), ("Album", "VAH – Community Radio"))

    def test_leerer_text_schreibt_nichts(self):
        for text in ("", None):
            with self.subTest(text=text):
                self.assertIsNone(self.tag("Album", text))

    def test_leerer_feldname_schreibt_nichts(self):
        for field in ("", "   ", None):
            with self.subTest(field=field):
                self.assertIsNone(self.tag(field))

    def test_reservierte_feldnamen_werden_nicht_ueberschrieben(self):
        """The title and the date tags belong to the mixer; they must not be replaced by the text."""
        for field in ("title", "Title", "TITLE", "Enable Date", "expiry date"):
            with self.subTest(field=field):
                self.assertIsNone(self.tag(field))

    def test_ist_kommentar(self):
        for field, expected in (("Comment", True), ("comment", True), (" COMMENT ", True),
                                ("Album", False), ("Kommentar", False)):
            with self.subTest(field=field):
                self.assertEqual(self.mixer.is_comment_field(field), expected)


class Id3TextStartLogTest(MixerTestCase):
    """The ID3 tagging is logged at startup so that configuration errors are noticed."""

    def log(self, field, text, media_type=None):
        if media_type is None:
            media_type = self.mixer.ID3_MEDIA_TYPE
        with mock.patch.object(self.mixer, "ID3_TEXT_FIELD", field), mock.patch.object(self.mixer, "ID3_TEXT", text), \
             mock.patch.object(self.mixer, "ID3_MEDIA_TYPE", media_type), \
             self.assertLogs("overlay_mixer", "INFO") as logs:
            self.mixer.log_id3_text_setting()
        return logs.output

    def test_standard(self):
        self.assertEqual(self.log("Album", "Veranstaltungshinweis"),
                         ["INFO:overlay_mixer:ID3 text: Album = Veranstaltungshinweis"])

    def test_kommentar(self):
        self.assertEqual(self.log("Comment", "Veranstaltungshinweis"),
                         ["INFO:overlay_mixer:ID3 text: Comment = Veranstaltungshinweis (comment frame COMM)"])

    def test_aus(self):
        self.assertEqual(self.log("Album", ""), ["INFO:overlay_mixer:ID3 text: off (no text configured)"])

    def test_reservierter_name_wird_gewarnt(self):
        self.assertEqual(self.log("Title", "Veranstaltungshinweis"),
                         ["WARNING:overlay_mixer:ID3 text: field name 'Title' is reserved | no text will be written"])

    def test_tmed_mit_media_type_wird_gewarnt(self):
        self.assertEqual(self.log("TMED", "Veranstaltungshinweis"),
                         ["WARNING:overlay_mixer:ID3 text: field name 'TMED' is reserved "
                          "(TMED is set via ID3_MEDIA_TYPE) | no text will be written"])

    def test_tmed_ohne_media_type(self):
        self.assertEqual(self.log("TMED", "Veranstaltungshinweis", media_type=""),
                         ["INFO:overlay_mixer:ID3 text: TMED = Veranstaltungshinweis"])

    def test_genre_ist_frei(self):
        self.assertEqual(self.log("Genre", "Veranstaltungshinweis"),
                         ["INFO:overlay_mixer:ID3 text: Genre = Veranstaltungshinweis"])


class Id3MediaTypeTest(MixerTestCase):
    """id3_media_type(): value for the Media Type field (TMED) of every file (since v1.8.0)."""

    def media_type(self, value):
        with mock.patch.object(self.mixer, "ID3_MEDIA_TYPE", value):
            return self.mixer.id3_media_type()

    def test_standard_ist_news(self):
        self.assertEqual(self.mixer.id3_media_type(), "News")

    def test_eigener_wert(self):
        self.assertEqual(self.media_type("Information"), "Information")

    def test_leerer_wert_schreibt_nichts(self):
        for value in ("", None):
            with self.subTest(value=value):
                self.assertIsNone(self.media_type(value))

    def test_startlog(self):
        for value, expected in (("News", "ID3 media type: TMED = News"),
                                ("", "ID3 media type: off (no value configured)")):
            with self.subTest(value=value), mock.patch.object(self.mixer, "ID3_MEDIA_TYPE", value), \
                 self.assertLogs("overlay_mixer", "INFO") as logs:
                self.mixer.log_id3_media_type_setting()
            self.assertEqual(logs.output, [f"INFO:overlay_mixer:{expected}"])


class AusgabenameTest(MixerTestCase):
    """output_stem(): OUTPUT_PREFIX + name, without a duplicate prefix at the start (since v1.9.0)."""

    def stem(self, name, prefix=None):
        with mock.patch.object(self.mixer, "OUTPUT_PREFIX", self.mixer.OUTPUT_PREFIX if prefix is None else prefix):
            return self.mixer.output_stem(name)

    def test_ohne_praefix_wird_es_vorangestellt(self):
        self.assertEqual(self.stem("Stadtfest"), "VAH Stadtfest")
        self.assertEqual(self.stem("20261116 Market"), "VAH 20261116 Market")

    def test_praefix_am_anfang_wird_nicht_verdoppelt(self):
        for name, expected in (
                ("VAH Market 20261116", "VAH Market 20261116"),
                ("vah open stage 20261108", "VAH open stage 20261108"),
                ("Vah Reading 20261111 ", "VAH Reading 20261111 "),
                ("VAh Choir", "VAH Choir"),
                ("VAH20251119 Film night", "VAH 20251119 Film night"),
                ("VAH_Market", "VAH Market"),
                ("VAH-Market", "VAH Market"),
                ("VAH - Market", "VAH Market"),
                ("VAH  Market", "VAH Market"),
                ("VAH vah jazz trio", "VAH jazz trio"),              # already duplicated in the source
                ("VAH VAH20251119 Dance hall", "VAH 20251119 Dance hall")):
            with self.subTest(name=name):
                self.assertEqual(self.stem(name), expected)

    def test_vah_weiter_hinten_bleibt_stehen(self):
        """Only the start of the name is handled (as specified)."""
        self.assertEqual(self.stem("20261114 VAH guest talk"), "VAH 20261114 VAH guest talk")
        self.assertEqual(self.stem("01 -  - 20261017 VAh Choir"), "VAH 01 -  - 20261017 VAh Choir")

    def test_wortanfang_vah_ist_kein_praefix(self):
        self.assertEqual(self.stem("VAHRaveNight"), "VAH VAHRaveNight")
        self.assertEqual(self.stem("Vahrenholz Konzert"), "VAH Vahrenholz Konzert")

    def test_name_nur_aus_praefix_bleibt_erhalten(self):
        for name in ("VAH", "VAH - ", "vah_"):
            with self.subTest(name=name):
                self.assertEqual(self.stem(name), f"VAH {name}")

    def test_anderes_praefix(self):
        self.assertEqual(self.stem("Konzert", prefix="Radio-"), "Radio-Konzert")
        self.assertEqual(self.stem("radio-Konzert", prefix="Radio-"), "Radio-Konzert")
        self.assertEqual(self.stem("Radio Konzert", prefix="Radio-"), "Radio-Konzert")

    def test_leeres_praefix_laesst_den_namen_unveraendert(self):
        self.assertEqual(self.stem("VAH Market", prefix=""), "VAH Market")
        self.assertEqual(self.stem("Stadtfest", prefix=""), "Stadtfest")


class DatumserkennungTest(MixerTestCase):
    def extract(self, filename):
        return self.mixer.extract_date_from_filename(Path(filename))

    def test_gueltige_datumsangaben(self):
        cases = {
            "20260518 Radio Strassenfest.mp3": date(2026, 5, 18),
            "VAH Hiking 20261016.aac": date(2026, 10, 16),
            "01 -  - 20261017 VAh Choir.mp3": date(2026, 10, 17),
            "Konzert_20261231_final.wav": date(2026, 12, 31),
            "VAH20260518x.mp3": date(2026, 5, 18),
            "20240229 Schaltjahr.mp3": date(2024, 2, 29),
            "20260518.mp3": date(2026, 5, 18),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.extract(name), expected)

    def test_datum_im_format_yyyy_mm_dd(self):
        # since v1.5.0
        cases = {
            "2026-07-26 Band night Townhall.m4a": date(2026, 7, 26),
            "Konzert 2026-12-31.mp3": date(2026, 12, 31),
            "VAH_2028-02-29_Schaltjahr.wav": date(2028, 2, 29),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.extract(name), expected)

    def test_aufnahmedatum_in_handy_dateinamen_wird_als_datum_erkannt(self):
        """
        Deliberately decided this way (always accept YYYY-MM-DD). Note: for these names the
        detected date is the recording date, not the event date.
        """
        self.assertEqual(self.extract("WhatsApp Audio 2026-10-03 at 16.19.36.ogg"), date(2026, 10, 3))
        self.assertEqual(self.extract("AUDIO-2025-11-05-11-22-51.m4a"), date(2025, 11, 5))

    def test_yyyymmdd_hat_vorrang_vor_yyyy_mm_dd(self):
        self.assertEqual(self.extract("2026-12-10 Konzert 20261105.mp3"), date(2026, 11, 5))
        self.assertEqual(self.extract("20261105 Konzert 2026-12-10.mp3"), date(2026, 11, 5))
        self.assertEqual(self.extract("20261340 Konzert 2026-12-10.mp3"), date(2026, 12, 10))

    def test_kein_gueltiges_datum(self):
        names = [
            "Stadtfest.mp3",
            "20261340 Monat 13.mp3",
            "20250229 kein Schaltjahr.mp3",
            "202605181 neun Ziffern.mp3",
            "2026051 sieben Ziffern.mp3",
            "2026-13-01 Monat 13.mp3",
            "2026-02-30 kein Tag.mp3",
            "2026-7-26 einstelliger Monat.mp3",
            "12026-07-26 fuenfstelliges Jahr.mp3",
            "26.07.2026 deutsches Format.mp3",
        ]
        for name in names:
            with self.subTest(name=name):
                self.assertIsNone(self.extract(name))

    def test_ungueltiger_treffer_wird_uebersprungen(self):
        self.assertEqual(self.extract("20261340 Konzert 20261105.mp3"), date(2026, 11, 5))
        self.assertEqual(self.extract("2026-13-40 Konzert 2026-11-05.mp3"), date(2026, 11, 5))

    def test_erster_gueltiger_treffer_gewinnt(self):
        self.assertEqual(self.extract("20261105 Konzert 20261210.mp3"), date(2026, 11, 5))
        self.assertEqual(self.extract("2026-11-05 Konzert 2026-12-10.mp3"), date(2026, 11, 5))

    def test_nur_der_dateiname_zaehlt_nicht_der_ordner(self):
        self.assertIsNone(self.extract(r"C:\20260101 Ordner\Stadtfest.mp3"))


class ScheduleTagsTest(MixerTestCase):
    """Enable Date = detected date - 14 days, Expiry Date = detected date + 1 day (since v1.5.0)."""

    def test_tagwerte(self):
        cases = {
            date(2026, 11, 22): ("2026-11-08", "2026-11-23"),
            date(2026, 12, 31): ("2026-12-17", "2027-01-01"),
            date(2027, 1, 5): ("2026-12-22", "2027-01-06"),
            date(2028, 3, 14): ("2028-02-29", "2028-03-15"),
            date(2026, 2, 28): ("2026-02-14", "2026-03-01"),
        }
        for event, (enable, expiry) in cases.items():
            with self.subTest(event=event):
                self.assertEqual(self.mixer.schedule_tags(event), {"Enable Date": enable, "Expiry Date": expiry})

    def test_enable_date_haengt_nicht_vom_heutigen_datum_ab(self):
        """Unlike Created: always date - 14 days, even if that lies in the past."""
        tags = self.mixer.schedule_tags(date.today() + timedelta(days=3))
        self.assertEqual(tags["Enable Date"], (date.today() - timedelta(days=11)).strftime("%Y-%m-%d"))


class UniquePathTest(MixerTestCase):
    def test_freier_name_bleibt_unveraendert(self):
        target = self.fertig / "VAH Stadtfest.mp3"
        self.assertEqual(self.mixer.unique_path(target), target)
        self.assertFalse(target.exists(), "unique_path must not create a file")

    def test_nummerierung(self):
        (self.fertig / "VAH Stadtfest.mp3").touch()
        self.assertEqual(self.mixer.unique_path(self.fertig / "VAH Stadtfest.mp3"),
                         self.fertig / "VAH Stadtfest (1).mp3")
        (self.fertig / "VAH Stadtfest (1).mp3").touch()
        (self.fertig / "VAH Stadtfest (2).mp3").touch()
        self.assertEqual(self.mixer.unique_path(self.fertig / "VAH Stadtfest.mp3"),
                         self.fertig / "VAH Stadtfest (3).mp3")

    def test_luecke_wird_gefuellt(self):
        (self.fertig / "VAH Stadtfest.mp3").touch()
        (self.fertig / "VAH Stadtfest (2).mp3").touch()
        self.assertEqual(self.mixer.unique_path(self.fertig / "VAH Stadtfest.mp3"),
                         self.fertig / "VAH Stadtfest (1).mp3")

    def test_punkte_im_namen(self):
        (self.fertig / "VAH a.b.mp3").touch()
        self.assertEqual(self.mixer.unique_path(self.fertig / "VAH a.b.mp3"),
                         self.fertig / "VAH a.b (1).mp3")


class AtmoAuswahlTest(MixerTestCase):
    """choose_best_atmo with simulated ffprobe durations (exactly controllable)."""

    def setUp(self):
        super().setUp()
        self.durations = dict(PRODUCTION_ATMO_SECONDS)
        patcher = mock.patch.object(self.mixer, "get_duration_seconds", side_effect=self.fake_duration)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_duration(self, path):
        try:
            return self.durations[Path(path).name]
        except KeyError:
            raise RuntimeError(f"ffprobe failed for {path}")

    def create_atmos(self):
        for name in self.durations:
            (self.atmo / name).touch()

    def choose(self, overlay_seconds):
        result = self.mixer.choose_best_atmo(overlay_seconds)
        return None if result is None else result.name

    def test_produktions_schwellen(self):
        self.create_atmos()
        cases = [
            (10.0, "Ambiente-Musik 32 Sekunden.mp3"),
            (24.5, "Ambiente-Musik 32 Sekunden.mp3"),
            (24.6, "Ambiente-Musik 42 Sekunden.mp3"),
            (31.0, "Ambiente-Musik 42 Sekunden.mp3"),  # example from the project description
            (35.0, "Ambiente-Musik 42 Sekunden.mp3"),
            (35.1, "Ambiente-Musik 53 Sekunden.mp3"),
            (45.6, "Ambiente-Musik 53 Sekunden.mp3"),
            (45.7, "Ambiente-Musik 63 Sekunden.mp3"),
            (56.1, "Ambiente-Musik 63 Sekunden.mp3"),
            (56.2, None),
            (120.0, None),
        ]
        for overlay_seconds, expected in cases:
            with self.subTest(overlay=overlay_seconds):
                self.assertEqual(self.choose(overlay_seconds), expected)

    def test_genau_passende_atmo_reicht(self):
        self.durations = {"genau.mp3": 32.0, "laenger.mp3": 40.0}
        self.create_atmos()
        self.assertEqual(self.choose(25.0), "genau.mp3")  # 25 + 5 + 2 = 32

    def test_kuerzeste_passende_gewinnt_unabhaengig_vom_namen(self):
        self.durations["Zzz kurz.mp3"] = 40.0
        self.create_atmos()
        self.assertEqual(self.choose(31.0), "Zzz kurz.mp3")

    def test_nur_mp3_atmos_zaehlen(self):
        self.durations["Riesig.wav"] = 600.0
        self.create_atmos()
        self.assertIsNone(self.choose(60.0))

    def test_unlesbare_atmo_wird_uebersprungen(self):
        self.create_atmos()
        (self.atmo / "kaputt.mp3").touch()
        with self.assertLogs("overlay_mixer", "WARNING") as logs:
            self.assertEqual(self.choose(31.0), "Ambiente-Musik 42 Sekunden.mp3")
        self.assertIn("Could not determine atmo duration", "\n".join(logs.output))

    def test_leerer_atmo_ordner(self):
        self.assertIsNone(self.choose(10.0))


class WaitForStableFileTest(MixerTestCase):
    def test_unveraenderte_datei_ist_stabil(self):
        f = self.eingang / "fertig.mp3"
        f.write_bytes(b"x" * 1000)
        start = time.monotonic()
        self.assertTrue(self.mixer.wait_for_stable_file(f, stable_secs=0.3, timeout=5))
        self.assertGreaterEqual(time.monotonic() - start, 0.3)

    def test_fehlende_datei_laeuft_in_timeout(self):
        self.assertFalse(self.mixer.wait_for_stable_file(self.eingang / "fehlt.mp3", stable_secs=0.2, timeout=1))

    def test_spaeter_erscheinende_datei(self):
        f = self.eingang / "spaet.mp3"
        t = start_thread(lambda: (time.sleep(0.6), f.write_bytes(b"x")))
        self.assertTrue(self.mixer.wait_for_stable_file(f, stable_secs=0.3, timeout=5))
        t.join()

    def test_wartet_bis_datei_nicht_mehr_waechst(self):
        f = self.eingang / "upload.mp3"
        f.write_bytes(b"")
        growth_seconds = 1.5

        def grow():
            end = time.monotonic() + growth_seconds
            while time.monotonic() < end:
                with f.open("ab") as fh:
                    fh.write(b"x" * 100)
                time.sleep(0.1)

        start = time.monotonic()
        t = start_thread(grow)
        self.assertTrue(self.mixer.wait_for_stable_file(f, stable_secs=0.5, timeout=10))
        self.assertGreaterEqual(time.monotonic() - start, growth_seconds)
        t.join()

    def test_dauernd_wachsende_datei_laeuft_in_timeout(self):
        f = self.eingang / "endlos.mp3"
        f.write_bytes(b"")
        stop = threading.Event()

        def grow():
            while not stop.is_set():
                with f.open("ab") as fh:
                    fh.write(b"x" * 100)
                time.sleep(0.1)

        t = start_thread(grow)
        try:
            self.assertFalse(self.mixer.wait_for_stable_file(f, stable_secs=0.5, timeout=1.5))
        finally:
            stop.set()
            t.join()


class OverlayHandlerTest(MixerTestCase):
    def setUp(self):
        super().setUp()
        self.queue = Queue()
        self.handler = self.mixer.OverlayHandler(self.queue)

    def queued(self):
        return list(self.queue.queue)

    def test_erlaubte_dateitypen_werden_eingereiht(self):
        names = ["a.mp3", "b.wav", "c.m4a", "d.aac", "e.flac", "f.ogg", "g.mp4", "H.MP3", "I.Wav"]
        for name in names:
            self.handler.dispatch(FileCreatedEvent(str(self.eingang / name)))
        self.assertEqual(self.queued(), [self.eingang / n for n in names])

    def test_andere_dateien_werden_ignoriert(self):
        for name in ["notiz.txt", "upload.tmp", "bild.jpg", "ohne_endung"]:
            self.handler.dispatch(FileCreatedEvent(str(self.eingang / name)))
        self.assertEqual(self.queued(), [])

    def test_ordner_werden_ignoriert(self):
        self.handler.dispatch(DirCreatedEvent(str(self.eingang / "ordner.mp3")))
        self.assertEqual(self.queued(), [])

    def test_aenderungen_loesen_nichts_aus(self):
        self.handler.dispatch(FileModifiedEvent(str(self.eingang / "a.mp3")))
        self.assertEqual(self.queued(), [])


class ProcessExistingTest(MixerTestCase):
    def test_vorhandene_dateien_aelteste_zuerst(self):
        now = time.time()
        for name, age in [("neu.mp3", 10), ("alt.wav", 300), ("mittel.m4a", 100)]:
            f = self.eingang / name
            f.write_bytes(b"x")
            os.utime(f, (now - age, now - age))
        (self.eingang / "notiz.txt").write_bytes(b"x")
        (self.eingang / "unterordner.mp3").mkdir()

        q = Queue()
        self.mixer.process_existing(q)
        self.assertEqual(list(q.queue), [self.eingang / "alt.wav", self.eingang / "mittel.m4a",
                                         self.eingang / "neu.mp3"])


class WorkerLoopTest(MixerTestCase):
    def test_verarbeitet_nacheinander_und_endet_mit_stop_flag(self):
        q = Queue()
        stop = [False]
        processed = []
        with mock.patch.object(self.mixer, "process_overlay", side_effect=processed.append):
            t = start_thread(self.mixer.worker_loop, q, stop)
            items = [Path("a.mp3"), Path("b.mp3"), Path("c.mp3")]
            for item in items:
                q.put(item)
            self.assertTrue(wait_until(lambda: q.unfinished_tasks == 0))
            stop[0] = True
            t.join(timeout=3)
        self.assertEqual(processed, items)
        self.assertFalse(t.is_alive())


class OrdnerUndFehlerTest(MixerTestCase):
    def test_ensure_dirs_legt_ordner_an(self):
        for d in (self.eingang, self.atmo, self.fertig, self.fehler, self.archiv):
            d.rmdir()
        self.mixer.ensure_dirs()
        for d in (self.eingang, self.atmo, self.fertig, self.fehler, self.archiv):
            self.assertTrue(d.is_dir(), d)

    def test_move_to_error_verschiebt_und_protokolliert(self):
        (self.fehler / "kaputt.mp3").write_bytes(b"alt")
        src = self.eingang / "kaputt.mp3"
        src.write_bytes(b"neu")
        with self.assertLogs("overlay_mixer", "ERROR") as logs:
            self.mixer.move_to_error(src, "Testgrund")
        self.assertFalse(src.exists())
        self.assertEqual((self.fehler / "kaputt (1).mp3").read_bytes(), b"neu")
        self.assertEqual((self.fehler / "kaputt.mp3").read_bytes(), b"alt")
        self.assertIn("Reason: Testgrund", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
