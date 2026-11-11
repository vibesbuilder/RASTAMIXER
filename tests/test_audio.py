# -*- coding: utf-8 -*-
"""FFmpeg/ffprobe integration: determining durations and mixing (with generated test tones)."""
import os
import unittest

from helpers import (MixerTestCase, duration, explorer_properties, format_tags, id3_frames, make_silence, make_tone,
                     make_tone_with_spike, make_video_with_audio, max_volume_db, probe, requires_ffmpeg, silences,
                     title_tag)


@requires_ffmpeg
class DauerTest(MixerTestCase):
    def test_dauer_wird_ermittelt(self):
        for ext in (".wav", ".mp3"):
            with self.subTest(ext=ext):
                f = make_tone(self.eingang / f"ton{ext}", 3)
                self.assertAlmostEqual(self.mixer.get_duration_seconds(f), 3.0, delta=0.05)

    def test_kaputte_datei(self):
        f = self.eingang / "kaputt.mp3"
        f.write_bytes(b"das ist kein audio" * 50)
        with self.assertRaisesRegex(RuntimeError, "ffprobe failed"):
            self.mixer.get_duration_seconds(f)

    def test_fehlende_datei(self):
        with self.assertRaisesRegex(RuntimeError, "ffprobe failed"):
            self.mixer.get_duration_seconds(self.eingang / "fehlt.mp3")


@requires_ffmpeg
class MischungTest(MixerTestCase):
    """Silent atmo (20 s) + tone overlay (8 s): start time and level are measurable."""

    ATMO_SECONDS = 20
    OVERLAY_SECONDS = 8

    def setUp(self):
        super().setUp()
        self.atmo_file = make_silence(self.atmo / "atmo.mp3", self.ATMO_SECONDS)
        self.overlay = make_tone(self.eingang / "overlay.wav", self.OVERLAY_SECONDS)

    def mix(self, overlay=None, out=None, title="VAH Test"):
        out = out or self.fertig / "VAH Test.mp3"
        self.mixer.mix_with_ffmpeg(self.atmo_file, overlay or self.overlay, out, title)
        return out

    def assertOverlayAt5Seconds(self, out, overlay_seconds):
        found = silences(out)
        self.assertGreaterEqual(len(found), 2, found)
        (s1, e1), (s2, _) = found[0], found[1]
        self.assertAlmostEqual(s1, 0.0, delta=0.05)
        self.assertAlmostEqual(e1, 5.0, delta=0.05, msg="overlay does not start after 5 s")
        self.assertAlmostEqual(s2, 5.0 + overlay_seconds, delta=0.05)

    def test_overlay_setzt_nach_genau_5_sekunden_ein(self):
        self.assertOverlayAt5Seconds(self.mix(), self.OVERLAY_SECONDS)

    def test_ausgabe_ist_so_lang_wie_die_atmo(self):
        self.assertAlmostEqual(duration(self.mix()), self.ATMO_SECONDS, delta=0.05)

    def test_ausgabe_ist_mp3_mit_320_kbit_ohne_weitere_spuren(self):
        streams = probe(self.mix())["streams"]
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["codec_type"], "audio")
        self.assertEqual(streams[0]["codec_name"], "mp3")
        self.assertEqual(int(streams[0]["bit_rate"]), 320000)

    def test_title_metadaten(self):
        self.assertEqual(title_tag(self.mix(title="VAH 20261017 Stadtfest (1)")), "VAH 20261017 Stadtfest (1)")

    def test_ohne_zusatz_tags_nur_title_und_encoder(self):
        version, frames = id3_frames(self.mix())
        self.assertEqual(version, 4)
        self.assertEqual([f[0] for f in frames], ["TIT2", "TSSE"])

    def test_zusatz_tags_werden_als_txxx_frames_geschrieben(self):
        out = self.fertig / "VAH Test.mp3"
        self.mixer.mix_with_ffmpeg(self.atmo_file, self.overlay, out, "VAH Test",
                                   {"Enable Date": "2026-11-08", "Expiry Date": "2026-11-23"})
        version, frames = id3_frames(out)
        self.assertEqual(version, 4)
        self.assertIn(("TXXX", ("Enable Date", "2026-11-08")), frames)
        self.assertIn(("TXXX", ("Expiry Date", "2026-11-23")), frames)
        self.assertIn(("TIT2", "VAH Test"), frames)
        self.assertOverlayAt5Seconds(out, self.OVERLAY_SECONDS)  # mix unchanged

    def test_overlay_wird_von_amix_um_6_db_abgesenkt(self):
        self.assertAlmostEqual(max_volume_db(self.mix()), max_volume_db(self.overlay) - 6.0, delta=1.0)

    def test_zielordner_wird_angelegt(self):
        out = self.mix(out=self.fertig / "neu" / "VAH Test.mp3")
        self.assertTrue(out.is_file())

    def test_ffmpeg_fehler_wird_gemeldet(self):
        broken = self.eingang / "kaputt.wav"
        broken.write_bytes(b"kein audio" * 100)
        with self.assertRaisesRegex(RuntimeError, "ffmpeg failed"):
            self.mix(overlay=broken)

    def test_alle_erlaubten_formate(self):
        for ext in sorted(self.mixer.ALLOWED_EXTS):
            with self.subTest(ext=ext):
                overlay = make_tone(self.eingang / f"overlay{ext}", 4)
                out = self.mix(overlay=overlay, out=self.fertig / f"VAH overlay{ext}.mp3")
                self.assertOverlayAt5Seconds(out, 4)

    def test_mp4_mit_videospur_liefert_nur_audio(self):
        overlay = make_video_with_audio(self.eingang / "Handyvideo.mp4", 4)
        out = self.mix(overlay=overlay)
        self.assertOverlayAt5Seconds(out, 4)
        self.assertEqual([s["codec_type"] for s in probe(out)["streams"]], ["audio"])

    def test_whatsapp_sprachnachricht_mono_opus_48khz(self):
        overlay = make_tone(self.eingang / "WhatsApp Audio.ogg", 4, channels=1, sample_rate=48000, codec="libopus")
        out = self.mix(overlay=overlay)
        self.assertOverlayAt5Seconds(out, 4)
        stream = probe(out)["streams"][0]
        self.assertEqual(int(stream["channels"]), 2)
        self.assertEqual(int(stream["sample_rate"]), 44100)

    def test_album_und_genre_als_standardfelder(self):
        """Basis for ID3_TEXT_FIELD (since v1.6.0): FFmpeg writes album -> TALB, genre -> TCON."""
        for key, frame_id in (("Album", "TALB"), ("Genre", "TCON")):
            with self.subTest(key=key):
                out = self.fertig / f"VAH {key}.mp3"
                self.mixer.mix_with_ffmpeg(self.atmo_file, self.overlay, out, "VAH Test", {key: "Veranstaltungshinweis"})
                self.assertIn((frame_id, "Veranstaltungshinweis"), id3_frames(out)[1])

    def test_tmed_als_frame_id(self):
        """Basis for ID3_MEDIA_TYPE (since v1.8.0): the key "TMED" (upper case) becomes the TMED frame directly,
        whereas "tmed" (lower case) becomes a TXXX field."""
        for key, expected in (("TMED", ("TMED", "News")), ("tmed", ("TXXX", ("tmed", "News")))):
            with self.subTest(key=key):
                out = self.fertig / f"VAH {'gross' if key.isupper() else 'klein'}.mp3"   # Windows: case-insensitive
                self.mixer.mix_with_ffmpeg(self.atmo_file, self.overlay, out, "VAH Test", {key: "News"})
                self.assertIn(expected, id3_frames(out)[1])


@requires_ffmpeg
class LautheitMessungTest(MixerTestCase):
    """measure_loudness() (since v1.10.0): EBU R128 loudness and true peak via FFmpeg loudnorm."""

    def test_testton(self):
        value, peak = self.mixer.measure_loudness(make_tone(self.eingang / "ton.wav", 5))
        self.assertAlmostEqual(value, -21.35, delta=0.3)
        self.assertLess(peak, -15)

    def test_leiser_ton_und_vorfilter(self):
        tone = make_tone(self.eingang / "leise.wav", 5, volume_db=-10)
        self.assertAlmostEqual(self.mixer.measure_loudness(tone)[0], -31.35, delta=0.3)
        self.assertAlmostEqual(self.mixer.measure_loudness(tone, "volume=6dB")[0], -25.35, delta=0.3)

    def test_spitze_wird_erkannt(self):
        value, peak = self.mixer.measure_loudness(make_tone_with_spike(self.eingang / "plopp.wav", 6))
        self.assertAlmostEqual(peak, -3.0, delta=0.5)
        self.assertLess(value, -26)

    def test_mono_opus_und_mp4_mit_video(self):
        for overlay in (make_tone(self.eingang / "WhatsApp.ogg", 4, channels=1, sample_rate=48000, codec="libopus"),
                        make_video_with_audio(self.eingang / "Handyvideo.mp4", 4)):
            with self.subTest(overlay=overlay.name):
                value, _ = self.mixer.measure_loudness(overlay)
                self.assertTrue(-40 < value < -10, value)

    def test_stille_liefert_minus_unendlich(self):
        value, _ = self.mixer.measure_loudness(make_silence(self.eingang / "still.wav", 3))
        self.assertEqual(value, float("-inf"))

    def test_kaputte_datei(self):
        broken = self.eingang / "kaputt.mp3"
        broken.write_bytes(b"kein audio" * 100)
        with self.assertRaisesRegex(RuntimeError, "Loudness measurement failed"):
            self.mixer.measure_loudness(broken)

    def test_mischung_mit_filter_fuer_die_sprachspur(self):
        """The filter only affects the voice track, before the 5 s delay; the overlay still starts at 5 s."""
        atmo = make_silence(self.atmo / "atmo.mp3", 20)
        overlay = make_tone(self.eingang / "overlay.wav", 8)
        out = self.fertig / "VAH Test.mp3"
        self.mixer.mix_with_ffmpeg(atmo, overlay, out, "VAH Test", None,
                                   "highpass=f=80,volume=6.00dB,alimiter=limit=-2dB:level=disabled:"
                                   "attack=5:release=50:latency=1")
        found = silences(out)
        self.assertAlmostEqual(found[0][1], 5.0, delta=0.05, msg="overlay does not start after 5 s")
        self.assertAlmostEqual(found[1][0], 13.0, delta=0.05)
        self.assertAlmostEqual(max_volume_db(out), max_volume_db(overlay) + 6.0 - 6.0, delta=1.0)
        self.assertAlmostEqual(duration(out), 20.0, delta=0.05)


@requires_ffmpeg
class Id3KommentarTest(MixerTestCase):
    """
    write_id3_comment() (since v1.6.0): FFmpeg writes "comment" only as TXXX, so the
    mixer inserts a real comment frame (COMM) into the ID3v2.4 tag itself.
    """

    def setUp(self):
        super().setUp()
        self.out = self.fertig / "VAH Test.mp3"
        self.mixer.mix_with_ffmpeg(make_silence(self.atmo / "atmo.mp3", 10), make_tone(self.eingang / "o.wav", 3),
                                   self.out, "VAH Test", {"Enable Date": "2026-11-08"})

    @staticmethod
    def split_tag(data):
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        return data[:10 + size], data[10 + size:]

    def test_kommentar_wird_als_comm_frame_eingefuegt(self):
        self.mixer.write_id3_comment(self.out, "Veranstaltungshinweis")
        version, frames = id3_frames(self.out)
        self.assertEqual(version, 4)
        self.assertIn(("COMM", ("deu", "", "Veranstaltungshinweis")), frames)
        self.assertNotIn("comment", [f[1][0].lower() for f in frames if f[0] == "TXXX"])

    def test_player_lesen_den_kommentar(self):
        self.mixer.write_id3_comment(self.out, "Veranstaltungshinweis")
        tags = format_tags(self.out)
        self.assertEqual(tags.get("comment"), "Veranstaltungshinweis")
        self.assertEqual(tags.get("title"), "VAH Test")
        self.assertEqual(tags.get("enable date"), "2026-11-08")

    def test_audio_und_uebrige_tags_bleiben_unveraendert(self):
        before = self.out.read_bytes()
        frames_before = id3_frames(self.out)[1]
        self.mixer.write_id3_comment(self.out, "Veranstaltungshinweis")
        after = self.out.read_bytes()
        self.assertEqual(self.split_tag(after)[1], self.split_tag(before)[1], "audio data changed")
        self.assertEqual([f for f in id3_frames(self.out)[1] if f[0] != "COMM"], frames_before)
        self.assertAlmostEqual(duration(self.out), 10.0, delta=0.05)

    def test_umlaute_und_sonderzeichen(self):
        text = "Veranstaltungshinweis – Österreich & Straße"
        self.mixer.write_id3_comment(self.out, text)
        self.assertIn(("COMM", ("deu", "", text)), id3_frames(self.out)[1])
        self.assertEqual(format_tags(self.out).get("comment"), text)

    @staticmethod
    def comm_encoding(path):
        data = path.read_bytes()
        return data[data.find(b"COMM") + 10]

    def test_kodierung_latin1_sonst_utf16_nie_utf8(self):
        """Windows Explorer does not display UTF-8 comments (encoding 3) – but it does display Latin-1 (0) and UTF-16 (1)."""
        other = self.fertig / "VAH Test2.mp3"
        other.write_bytes(self.out.read_bytes())
        self.mixer.write_id3_comment(self.out, "Veranstaltungshinweis Österreich")
        self.assertEqual(self.comm_encoding(self.out), 0)
        self.mixer.write_id3_comment(other, "Veranstaltungshinweis – €")
        self.assertEqual(self.comm_encoding(other), 1)
        self.assertIn(("COMM", ("deu", "", "Veranstaltungshinweis – €")), id3_frames(other)[1])

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_windows_explorer_zeigt_kommentar_und_album(self):
        self.mixer.write_id3_comment(self.out, "Veranstaltungshinweis – Österreich")
        album = self.fertig / "VAH Album.mp3"
        self.mixer.mix_with_ffmpeg(make_silence(self.atmo / "atmo2.mp3", 10), make_tone(self.eingang / "o2.wav", 3),
                                   album, "VAH Album", {"Album": "Veranstaltungshinweis"})
        self.assertEqual(explorer_properties(self.fertig, ["VAH Test.mp3", "VAH Album.mp3"],
                                             ["System.Comment", "System.Music.AlbumTitle"]),
                         [["Veranstaltungshinweis – Österreich", ""], ["", "Veranstaltungshinweis"]])

    def test_keine_temporaere_datei_bleibt_liegen(self):
        self.mixer.write_id3_comment(self.out, "Veranstaltungshinweis")
        self.assertEqual(self.names(self.fertig), ["VAH Test.mp3"])

    def test_datei_ohne_id3v24_tag_bleibt_unveraendert(self):
        wav = make_tone(self.fertig / "ton.wav", 1)
        before = wav.read_bytes()
        with self.assertRaises(ValueError):
            self.mixer.write_id3_comment(wav, "Veranstaltungshinweis")
        self.assertEqual(wav.read_bytes(), before)
        self.assertEqual(self.names(self.fertig), ["VAH Test.mp3", "ton.wav"])


if __name__ == "__main__":
    unittest.main()
