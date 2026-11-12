# -*- coding: utf-8 -*-
"""Last Modified / Created based on the date in the file name (rule since v1.4.0)."""
import os
import unittest
from datetime import date, datetime, timedelta

from helpers import MixerTestCase


def created_ts(stat) -> float:
    # Since Python 3.12 the Created date on Windows is called st_birthtime.
    return getattr(stat, "st_birthtime", stat.st_ctime)


@unittest.skipUnless(os.name == "nt", "Created can only be set on Windows")
class SetOutputDatesTest(MixerTestCase):
    def apply(self, event_date, name="VAH Test.mp3"):
        """Creates a fresh output file and applies set_output_dates."""
        path = self.fertig / name
        path.write_bytes(b"\0" * 128)
        before = path.stat()
        with self.assertLogs("overlay_mixer", "INFO") as logs:
            self.mixer.set_output_dates(path, event_date)
        return before, path.stat(), "\n".join(logs.output)

    def assertDateKeepsTime(self, actual_ts, original_ts, expected_date, what):
        """actual_ts has the date expected_date and the time of day of original_ts."""
        expected = datetime.fromtimestamp(original_ts).replace(
            year=expected_date.year, month=expected_date.month, day=expected_date.day)
        self.assertAlmostEqual(
            actual_ts, expected.timestamp(), delta=0.001,
            msg=f"{what}: {datetime.fromtimestamp(actual_ts)} instead of {expected}")

    def test_last_modified_gets_date_from_name_and_keeps_time(self):
        event = date.today() + timedelta(days=20)
        before, after, log = self.apply(event)
        self.assertDateKeepsTime(after.st_mtime, before.st_mtime, event, "Last Modified")
        self.assertIn(f"Last Modified set: VAH Test.mp3 -> {event:%d.%m.%Y}", log)

    def test_created_rule(self):
        today = date.today()
        tomorrow = today + timedelta(days=1)
        # Days from today to the event -> expected Created date, rule in the log
        cases = [
            (-30, today - timedelta(days=44), "standard"),
            (-14, today - timedelta(days=28), "standard"),
            (-13, tomorrow, "active"),
            (-3, tomorrow, "active"),
            (0, tomorrow, "active"),
            (3, tomorrow, "active"),
            (13, tomorrow, "active"),
            (14, today, "standard"),  # edge case, see separate test
            (15, tomorrow, "standard"),
            (20, today + timedelta(days=6), "standard"),
            (60, today + timedelta(days=46), "standard"),
        ]
        for offset, expected_created, rule in cases:
            with self.subTest(days=offset):
                event = today + timedelta(days=offset)
                before, after, log = self.apply(event, name=f"VAH Test {offset}.mp3")
                self.assertDateKeepsTime(created_ts(after), created_ts(before), expected_created, "Created")
                self.assertDateKeepsTime(after.st_mtime, before.st_mtime, event, "Last Modified")
                self.assertIn(f"Created rule {rule}", log)

    def test_edge_case_exactly_14_days_gives_created_today(self):
        """
        Current behavior (v1.4.0): day_diff == 14 falls into the "14 days before
        Last Modified" branch and therefore yields TODAY instead of TOMORROW. A downstream
        import that only picks up files with Created == today could miss such a file if it
        is processed late in the day. Whether this will change is still undecided.
        """
        today = date.today()
        before, after, log = self.apply(today + timedelta(days=14))
        self.assertDateKeepsTime(created_ts(after), created_ts(before), today, "Created")
        self.assertIn("are 14 days apart. Using 14 days before Last Modified", log)

    def test_set_windows_creation_time_sets_date_and_time(self):
        path = self.fertig / "VAH Test.mp3"
        path.write_bytes(b"x")
        target = datetime(2026, 5, 4, 13, 52, 7, 250000)
        self.mixer.set_windows_creation_time(path, target)
        self.assertAlmostEqual(created_ts(path.stat()), target.timestamp(), delta=0.001)

    def test_set_windows_creation_time_reports_the_real_cause(self):
        """Up to v1.4.0, missing ctypes prototypes always caused "The handle is invalid" (error 6)."""
        with self.assertRaises(OSError) as ctx:
            self.mixer.set_windows_creation_time(self.fertig / "fehlt.mp3", datetime.now())
        self.assertIn(ctx.exception.winerror, (2, 3))


if __name__ == "__main__":
    unittest.main()
