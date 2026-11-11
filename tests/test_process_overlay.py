# -*- coding: utf-8 -*-
"""End-to-end process_overlay: input -> import/archive or error folder (with real FFmpeg)."""
import os
import shutil
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from helpers import (MixerTestCase, duration, explorer_properties, format_tags, id3_frames, loudness, make_tone,
                     make_tone_with_spike, make_video_with_audio, max_volume_db, probe, requires_ffmpeg, silences,
                     title_tag)


@requires_ffmpeg
class ProcessOverlayTest(MixerTestCase):
    def setUp(self):
        super().setUp()
        self.install_test_atmos()
        # Shorten the stability check (tested separately in test_logic).
        original = self.mixer.wait_for_stable_file
        patcher = mock.patch.object(self.mixer, "wait_for_stable_file",
                                    side_effect=lambda p: original(p, stable_secs=0.2, timeout=5))
        patcher.start()
        self.addCleanup(patcher.stop)

    def process(self, overlay):
        with self.assertLogs("overlay_mixer", "INFO") as logs:
            self.mixer.process_overlay(overlay)
        return "\n".join(logs.output)

    def test_erfolg_mit_datum_im_namen(self):
        event = date.today() + timedelta(days=20)
        name = f"{event:%Y%m%d} Stadtfest"
        log = self.process(make_tone(self.eingang / f"{name}.wav", 10))

        self.assertEqual(self.names(self.fertig), [f"VAH {name}.mp3"])
        self.assertEqual(self.names(self.eingang), [])
        self.assertEqual(self.names(self.archiv), [f"{name}.wav"])
        self.assertEqual(self.names(self.fehler), [])

        out = self.fertig / f"VAH {name}.mp3"
        self.assertAlmostEqual(duration(out), 31.5, delta=0.1)  # 10 + 5 + 2 = 17 -> shortest atmo
        self.assertEqual(title_tag(out), f"VAH {name}")
        stat = out.stat()
        self.assertEqual(datetime.fromtimestamp(stat.st_mtime).date(), event)
        if os.name == "nt":
            self.assertEqual(datetime.fromtimestamp(stat.st_ctime).date(), event - timedelta(days=14))

        enable, expiry = event - timedelta(days=14), event + timedelta(days=1)
        self.assertEqual(self.txxx(out), {"Enable Date": f"{enable:%Y-%m-%d}", "Expiry Date": f"{expiry:%Y-%m-%d}"})

        self.assertIn(f"Date detected in file name: {event:%d.%m.%Y}", log)
        self.assertIn(f"ID3 tags set: Enable Date={enable:%Y-%m-%d} | Expiry Date={expiry:%Y-%m-%d}", log)
        self.assertIn(f"Archived: {name}.wav -> {name}.wav", log)
        self.assertIn(f"SUCCESS: VAH {name}.mp3 created | Overlay=10.00s | Atmo=31.50s", log)

    @staticmethod
    def txxx(path):
        return dict(content for frame_id, content in id3_frames(path)[1] if frame_id == "TXXX")

    def test_erfolg_ohne_datum_laesst_zeitstempel_unveraendert(self):
        log = self.process(make_tone(self.eingang / "Stadtfest.mp3", 10))
        out = self.fertig / "VAH Stadtfest.mp3"
        self.assertTrue(out.is_file())
        self.assertEqual(datetime.fromtimestamp(out.stat().st_mtime).date(), date.today())
        self.assertEqual(self.txxx(out), {})
        self.assertIn("No valid date detected in file name.", log)
        self.assertNotIn("Last Modified set", log)
        self.assertNotIn("ID3 tags set", log)

    def test_datum_im_format_yyyy_mm_dd(self):
        event = date.today() + timedelta(days=30)
        name = f"{event:%Y-%m-%d} Band night Townhall"
        log = self.process(make_tone(self.eingang / f"{name}.m4a", 8))
        out = self.fertig / f"VAH {name}.mp3"
        self.assertEqual(datetime.fromtimestamp(out.stat().st_mtime).date(), event)
        if os.name == "nt":
            self.assertEqual(datetime.fromtimestamp(out.stat().st_ctime).date(), event - timedelta(days=14))
        self.assertEqual(self.txxx(out)["Expiry Date"], f"{event + timedelta(days=1):%Y-%m-%d}")
        self.assertIn(f"Date detected in file name: {event:%d.%m.%Y}", log)

    def test_enable_date_weicht_bei_naher_veranstaltung_von_created_ab(self):
        """Created = tomorrow (rule < 14 days), Enable Date = date - 14 days (always, as specified)."""
        event = date.today() + timedelta(days=5)
        self.process(make_tone(self.eingang / f"{event:%Y%m%d} Bald.wav", 5))
        out = self.fertig / f"VAH {event:%Y%m%d} Bald.mp3"
        self.assertEqual(self.txxx(out)["Enable Date"], f"{event - timedelta(days=14):%Y-%m-%d}")
        if os.name == "nt":
            self.assertEqual(datetime.fromtimestamp(out.stat().st_ctime).date(), date.today() + timedelta(days=1))

    # --- ID3 tagging of every file (since v1.6.0) ---

    def frames(self, path):
        return id3_frames(path)[1]

    def process_with(self, field, text="Veranstaltungshinweis", name="Stadtfest.wav", media_type=None):
        if media_type is None:
            media_type = self.mixer.ID3_MEDIA_TYPE
        with mock.patch.object(self.mixer, "ID3_TEXT_FIELD", field), mock.patch.object(self.mixer, "ID3_TEXT", text), \
             mock.patch.object(self.mixer, "ID3_MEDIA_TYPE", media_type):
            log = self.process(make_tone(self.eingang / name, 5))
        return self.fertig / f"VAH {Path(name).stem}.mp3", log

    def test_album_wird_ohne_datum_gesetzt(self):
        log = self.process(make_tone(self.eingang / "Stadtfest.wav", 5))
        out = self.fertig / "VAH Stadtfest.mp3"
        self.assertIn(("TALB", "Veranstaltungshinweis"), self.frames(out))
        self.assertEqual(format_tags(out).get("album"), "Veranstaltungshinweis")
        self.assertIn("ID3 text set: Album=Veranstaltungshinweis", log)

    def test_album_wird_mit_datum_zusaetzlich_zu_den_datums_tags_gesetzt(self):
        event = date.today() + timedelta(days=20)
        log = self.process(make_tone(self.eingang / f"{event:%Y%m%d} Fest.wav", 5))
        out = self.fertig / f"VAH {event:%Y%m%d} Fest.mp3"
        frames = self.frames(out)
        self.assertIn(("TALB", "Veranstaltungshinweis"), frames)
        self.assertIn(("TIT2", f"VAH {event:%Y%m%d} Fest"), frames)
        self.assertEqual(self.txxx(out), {"Enable Date": f"{event - timedelta(days=14):%Y-%m-%d}",
                                          "Expiry Date": f"{event + timedelta(days=1):%Y-%m-%d}"})
        self.assertIn("ID3 tags set: Enable Date=", log)
        self.assertIn("ID3 text set: Album=Veranstaltungshinweis", log)

    def test_genre(self):
        out, log = self.process_with("Genre")
        frame_ids = [f[0] for f in self.frames(out)]
        self.assertIn(("TCON", "Veranstaltungshinweis"), self.frames(out))
        self.assertNotIn("TALB", frame_ids)
        self.assertIn(("TMED", "News"), self.frames(out))
        self.assertIn("ID3 text set: Genre=Veranstaltungshinweis", log)

    def test_tmed_als_textfeld_ueberschreibt_den_media_type_nicht(self):
        out, log = self.process_with("TMED")
        self.assertEqual([f for f in self.frames(out) if f[0] == "TMED"], [("TMED", "News")])
        self.assertIn("ID3 media type set: TMED=News", log)
        self.assertNotIn("ID3 text set", log)
        self.assertIn("SUCCESS", log)

    def test_comment_wird_echtes_kommentarfeld(self):
        out, log = self.process_with("Comment")
        self.assertIn(("COMM", ("deu", "", "Veranstaltungshinweis")), self.frames(out))
        self.assertEqual(self.txxx(out), {})
        self.assertEqual(format_tags(out).get("comment"), "Veranstaltungshinweis")
        self.assertIn("ID3 text set: Comment=Veranstaltungshinweis", log)

    def test_comment_mit_datum_zeitstempel_bleiben_korrekt(self):
        """The comment is inserted before Last Modified/Created are set."""
        event = date.today() + timedelta(days=20)
        out, _ = self.process_with("Comment", name=f"{event:%Y%m%d} Fest.wav")
        self.assertIn(("COMM", ("deu", "", "Veranstaltungshinweis")), self.frames(out))
        self.assertEqual(datetime.fromtimestamp(out.stat().st_mtime).date(), event)
        if os.name == "nt":
            self.assertEqual(datetime.fromtimestamp(out.stat().st_ctime).date(), event - timedelta(days=14))
        self.assertEqual(self.names(self.fertig), [out.name])

    def test_freier_feldname_wird_txxx(self):
        out, log = self.process_with("Kategorie")
        self.assertEqual(self.txxx(out), {"Kategorie": "Veranstaltungshinweis"})
        self.assertIn("ID3 text set: Kategorie=Veranstaltungshinweis", log)

    def test_leerer_text_schreibt_nichts(self):
        out, log = self.process_with("Album", text="")
        self.assertEqual(sorted(f[0] for f in self.frames(out)), ["TIT2", "TMED", "TSSE"])  # Media Type remains
        self.assertNotIn("ID3 text set", log)

    def test_ohne_text_und_media_type_nur_titel(self):
        out, log = self.process_with("Album", text="", media_type="")
        self.assertEqual([f[0] for f in self.frames(out)], ["TIT2", "TSSE"])
        self.assertNotIn("ID3 text set", log)
        self.assertNotIn("ID3 media type set", log)

    # --- Media Type (TMED) of every file (since v1.8.0; v1.7.0 still wrote "News" to TCON) ---

    def test_media_type_news_ohne_datum(self):
        log = self.process(make_tone(self.eingang / "Stadtfest.wav", 5))
        out = self.fertig / "VAH Stadtfest.mp3"
        frames = self.frames(out)
        self.assertEqual([f for f in frames if f[0] == "TMED"], [("TMED", "News")])
        self.assertNotIn("TCON", [f[0] for f in frames])
        self.assertIn(("TALB", "Veranstaltungshinweis"), frames)
        self.assertEqual(format_tags(out).get("tmed"), "News")
        self.assertEqual(self.txxx(out), {})
        self.assertIn("ID3 media type set: TMED=News", log)

    def test_media_type_mit_datum_zusaetzlich_zu_den_anderen_tags(self):
        event = date.today() + timedelta(days=20)
        log = self.process(make_tone(self.eingang / f"{event:%Y%m%d} Fest.wav", 5))
        out = self.fertig / f"VAH {event:%Y%m%d} Fest.mp3"
        frames = self.frames(out)
        self.assertIn(("TMED", "News"), frames)
        self.assertIn(("TALB", "Veranstaltungshinweis"), frames)
        self.assertIn(("TIT2", f"VAH {event:%Y%m%d} Fest"), frames)
        self.assertEqual(self.txxx(out), {"Enable Date": f"{event - timedelta(days=14):%Y-%m-%d}",
                                          "Expiry Date": f"{event + timedelta(days=1):%Y-%m-%d}"})
        self.assertEqual(datetime.fromtimestamp(out.stat().st_mtime).date(), event)
        self.assertIn("ID3 media type set: TMED=News", log)

    def test_eigener_media_type(self):
        out, log = self.process_with("Album", media_type="Information")
        self.assertEqual([f for f in self.frames(out) if f[0] == "TMED"], [("TMED", "Information")])
        self.assertIn("ID3 media type set: TMED=Information", log)

    def test_leerer_media_type_schreibt_nichts(self):
        out, log = self.process_with("Album", media_type="")
        self.assertNotIn("TMED", [f[0] for f in self.frames(out)])
        self.assertIn(("TALB", "Veranstaltungshinweis"), self.frames(out))
        self.assertNotIn("ID3 media type set", log)

    def test_media_type_zusammen_mit_kommentar(self):
        out, _ = self.process_with("Comment")
        frames = self.frames(out)
        self.assertIn(("TMED", "News"), frames)
        self.assertIn(("COMM", ("deu", "", "Veranstaltungshinweis")), frames)

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_windows_explorer_zeigt_album_und_kein_genre(self):
        """Windows Explorer does not display TMED; "News" must no longer appear in the genre (TCON)."""
        self.process(make_tone(self.eingang / "Stadtfest.wav", 5))
        self.assertEqual(explorer_properties(self.fertig, ["VAH Stadtfest.mp3"],
                                             ["System.Music.Genre", "System.Music.AlbumTitle"]),
                         [["", "Veranstaltungshinweis"]])

    def test_reservierter_feldname_ueberschreibt_den_titel_nicht(self):
        out, log = self.process_with("title")
        self.assertEqual(title_tag(out), "VAH Stadtfest")
        self.assertNotIn("ID3 text set", log)

    def test_fehler_beim_kommentar_bricht_nicht_ab(self):
        with mock.patch.object(self.mixer, "write_id3_comment", side_effect=OSError("gesperrt")):
            out, log = self.process_with("Comment")
        self.assertIn("Could not write ID3 comment (VAH Stadtfest.mp3): gesperrt", log)
        self.assertNotIn("ID3 text set", log)
        self.assertIn("SUCCESS", log)
        self.assertTrue(out.is_file())
        self.assertEqual(self.names(self.archiv), ["Stadtfest.wav"])

    def test_mp4_nur_audio(self):
        self.process(make_tone(self.eingang / "Farm open day.mp4", 5))
        self.assertEqual(self.names(self.fertig), ["VAH Farm open day.mp3"])
        self.assertEqual(self.names(self.archiv), ["Farm open day.mp4"])

    def test_mp4_mit_videospur(self):
        self.process(make_video_with_audio(self.eingang / "Handyvideo.mp4", 5))
        out = self.fertig / "VAH Handyvideo.mp3"
        self.assertEqual([s["codec_type"] for s in probe(out)["streams"]], ["audio"])
        self.assertEqual(self.names(self.archiv), ["Handyvideo.mp4"])

    def test_laengeres_overlay_bekommt_laengere_atmo(self):
        self.process(make_tone(self.eingang / "Lang.wav", 30))  # 30 + 7 = 37 -> 42 s atmo
        self.assertAlmostEqual(duration(self.fertig / "VAH Lang.mp3"), 42.0, delta=0.1)

    def test_zu_langes_overlay_landet_im_fehlerordner(self):
        log = self.process(make_tone(self.eingang / "Zu lang.wav", 57))  # 57 + 7 = 64 > 63
        self.assertEqual(self.names(self.fehler), ["Zu lang.wav"])
        self.assertEqual(self.names(self.fertig), [])
        self.assertEqual(self.names(self.archiv), [])
        self.assertIn("No suitable atmo available (overlay 57.00s", log)

    def test_defekte_datei_landet_im_fehlerordner(self):
        broken = self.eingang / "kaputt.mp3"
        broken.write_bytes(b"kein audio" * 100)
        log = self.process(broken)
        self.assertEqual(self.names(self.fehler), ["kaputt.mp3"])
        self.assertEqual(self.names(self.fertig), [])
        self.assertIn("ffprobe failed", log)

    def test_nicht_stabile_datei_landet_im_fehlerordner(self):
        overlay = make_tone(self.eingang / "Upload.wav", 5)
        with mock.patch.object(self.mixer, "wait_for_stable_file", return_value=False):
            log = self.process(overlay)
        self.assertEqual(self.names(self.fehler), ["Upload.wav"])
        self.assertIn("File did not become stable (timeout)", log)

    def test_nicht_erlaubter_dateityp_bleibt_liegen(self):
        note = self.eingang / "notiz.txt"
        note.write_text("hallo", encoding="utf-8")
        log = self.process(note)
        self.assertEqual(self.names(self.eingang), ["notiz.txt"])
        self.assertIn("Ignored (file type not allowed): notiz.txt", log)

    def test_namenskollision_in_import(self):
        (self.fertig / "VAH Stadtfest.mp3").write_bytes(b"alt")
        self.process(make_tone(self.eingang / "Stadtfest.wav", 5))
        self.assertEqual((self.fertig / "VAH Stadtfest.mp3").read_bytes(), b"alt")
        self.assertEqual(title_tag(self.fertig / "VAH Stadtfest (1).mp3"), "VAH Stadtfest (1)")

    # --- Loudness of the voice track (since v1.10.0; test atmos are silent) ---

    def test_leises_overlay_wird_auf_ziel_angehoben(self):
        log = self.process(make_tone(self.eingang / "Leise.wav", 8, volume_db=-10))       # -31.4 LUFS
        out = self.fertig / "VAH Leise.mp3"
        self.assertAlmostEqual(loudness(out), -18.0 - 6.0, delta=1.0)    # amix lowers the voice track by 6 dB
        self.assertAlmostEqual(silences(out)[0][1], 5.0, delta=0.05, msg="overlay no longer starts at 5 s")
        self.assertRegex(log, r"Loudness: -31\.\d LUFS, peak -?[\d.]+ dBTP \| raised by \+13\.\d dB "
                              r"\(target -18\.0 LUFS, peaks limited to -2\.0 dB\)")
        self.assertIn("SUCCESS: VAH Leise.mp3 created", log)

    def test_lautes_overlay_bleibt_unveraendert(self):
        log = self.process(make_tone(self.eingang / "Laut.wav", 8, volume_db=6))          # -15.4 LUFS
        self.assertAlmostEqual(loudness(self.fertig / "VAH Laut.mp3"), -15.35 - 6.0, delta=0.5)
        self.assertIn("sufficient (target -18.0 LUFS) | unchanged", log)
        self.assertNotIn("raised by", log)

    def test_spitzen_werden_beim_anheben_begrenzt(self):
        """
        Without a limiter the peak would be at +7 dBFS (clipping) after a gain of about +10 dB.
        The peak counts in the measurement and is limited afterwards – so the loudness
        ends up slightly below the target.
        """
        log = self.process(make_tone_with_spike(self.eingang / "Plopp.wav", 8))
        out = self.fertig / "VAH Plopp.mp3"
        self.assertLessEqual(max_volume_db(out), -2.0 - 6.0 + 1.0)
        self.assertAlmostEqual(loudness(out), -24.0, delta=1.5)
        self.assertIn("raised by", log)

    def test_leise_whatsapp_sprachnachricht(self):
        overlay = make_tone(self.eingang / "WhatsApp Audio.ogg", 6, channels=1, sample_rate=48000,
                            codec="libopus", volume_db=-10)
        log = self.process(overlay)
        self.assertAlmostEqual(loudness(self.fertig / "VAH WhatsApp Audio.mp3"), -24.0, delta=1.5)
        self.assertIn("raised by", log)

    def test_messfehler_mischt_unveraendert(self):
        with mock.patch.object(self.mixer, "measure_loudness", side_effect=RuntimeError("kaputt")):
            log = self.process(make_tone(self.eingang / "Stadtfest.wav", 5))
        self.assertIn("Loudness not measurable (Stadtfest.wav): kaputt | voice track unchanged", log)
        self.assertIn("SUCCESS: VAH Stadtfest.mp3 created", log)
        self.assertAlmostEqual(loudness(self.fertig / "VAH Stadtfest.mp3"), -21.35 - 6.0, delta=0.5)

    def test_lautheit_aus(self):
        with mock.patch.object(self.mixer, "LOUDNESS_TARGET_LUFS", None):
            log = self.process(make_tone(self.eingang / "Leise.wav", 5, volume_db=-10))
        self.assertNotIn("Loudness", log)
        self.assertAlmostEqual(loudness(self.fertig / "VAH Leise.mp3"), -31.35 - 6.0, delta=0.5)

    # --- No duplicate prefix (since v1.9.0) ---

    def test_praefix_im_namen_wird_nicht_verdoppelt(self):
        event = date.today() + timedelta(days=20)
        name = f"VAH Market {event:%Y%m%d}"
        log = self.process(make_tone(self.eingang / f"{name}.mp3", 5))
        out = self.fertig / f"{name}.mp3"
        self.assertEqual(self.names(self.fertig), [f"{name}.mp3"])
        self.assertEqual(title_tag(out), name)
        self.assertEqual(self.names(self.archiv), [f"{name}.mp3"])          # archive: original name
        self.assertEqual(datetime.fromtimestamp(out.stat().st_mtime).date(), event)
        self.assertIn(f"SUCCESS: {name}.mp3 created", log)

    def test_praefix_in_kleinbuchstaben_wird_vereinheitlicht(self):
        self.process(make_tone(self.eingang / "vah open stage 20261108.ogg", 5))
        self.assertEqual(self.names(self.fertig), ["VAH open stage 20261108.mp3"])
        self.assertEqual(title_tag(self.fertig / "VAH open stage 20261108.mp3"), "VAH open stage 20261108")
        self.assertEqual(self.names(self.archiv), ["vah open stage 20261108.ogg"])

    def test_namenskollision_ohne_doppeltes_praefix(self):
        (self.fertig / "VAH Market.mp3").write_bytes(b"alt")
        self.process(make_tone(self.eingang / "VAH Market.wav", 5))
        self.assertEqual(self.names(self.fertig), ["VAH Market (1).mp3", "VAH Market.mp3"])
        self.assertEqual(title_tag(self.fertig / "VAH Market (1).mp3"), "VAH Market (1)")

    def test_namenskollision_im_archiv(self):
        (self.archiv / "Stadtfest.wav").write_bytes(b"alt")
        self.process(make_tone(self.eingang / "Stadtfest.wav", 5))
        self.assertEqual(self.names(self.archiv), ["Stadtfest (1).wav", "Stadtfest.wav"])

    def test_namenskollision_im_fehlerordner(self):
        (self.fehler / "kaputt.mp3").write_bytes(b"alt")
        broken = self.eingang / "kaputt.mp3"
        broken.write_bytes(b"kein audio" * 100)
        self.process(broken)
        self.assertEqual(self.names(self.fehler), ["kaputt (1).mp3", "kaputt.mp3"])

    def test_fehler_beim_datum_setzen_bricht_nicht_ab(self):
        event = date.today() + timedelta(days=20)
        overlay = make_tone(self.eingang / f"{event:%Y%m%d} Fest.wav", 5)
        with mock.patch.object(self.mixer, "set_output_dates", side_effect=OSError("Zugriff verweigert")):
            log = self.process(overlay)
        self.assertIn("Could not set file dates", log)
        self.assertIn("SUCCESS", log)
        self.assertEqual(self.names(self.archiv), [f"{event:%Y%m%d} Fest.wav"])

    def test_fehler_beim_archivieren_laesst_overlay_im_eingang(self):
        overlay = make_tone(self.eingang / "Stadtfest.wav", 5)
        with mock.patch.object(shutil, "move", side_effect=PermissionError("gesperrt")):
            log = self.process(overlay)
        self.assertIn("Could not archive overlay (Stadtfest.wav)", log)
        self.assertIn("SUCCESS", log)
        self.assertEqual(self.names(self.fertig), ["VAH Stadtfest.mp3"])
        self.assertEqual(self.names(self.eingang), ["Stadtfest.wav"])


if __name__ == "__main__":
    unittest.main()
