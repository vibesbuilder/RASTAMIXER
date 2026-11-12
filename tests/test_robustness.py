# -*- coding: utf-8 -*-
"""Robustness since v1.4.1: logging, lock, startup without the network drive, queue, worker, folder watching."""
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from queue import Queue
from unittest import mock

from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileMovedEvent

from helpers import (SCRIPT_PATH, MixerTestCase, make_tone, requires_ffmpeg, start_thread, wait_until)

LOG_LINE = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[INFO\] "


class ImportWithoutSideEffectsTest(unittest.TestCase):
    def test_import_creates_no_folders_and_opens_no_log(self):
        """Previously even the import failed if the network drive was not connected yet."""
        code = (
            "import importlib.util, json, logging, pathlib, sys\n"
            "calls = []\n"
            "pathlib.Path.mkdir = lambda self, *a, **k: calls.append(str(self))\n"
            "spec = importlib.util.spec_from_file_location('m', sys.argv[1])\n"
            "spec.loader.exec_module(importlib.util.module_from_spec(spec))\n"
            "print(json.dumps({'mkdir': calls, 'handlers': len(logging.getLogger('overlay_mixer').handlers)}))\n"
        )
        result = subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPT_PATH)],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"mkdir": [], "handlers": 0})


class LoggingTest(MixerTestCase):
    def setUp(self):
        super().setUp()
        saved = list(self.mixer.logger.handlers)

        def restore():
            for handler in self.mixer.logger.handlers:
                handler.close()
            self.mixer.logger.handlers[:] = saved

        self.addCleanup(restore)
        self.console = io.StringIO()
        patcher = mock.patch("sys.stdout", self.console)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_writes_local_log_nas_log_and_console(self):
        self.mixer.setup_logging()
        self.mixer.logger.info("test entry äöü")
        for text in (self.mixer.LOCAL_LOG_FILE.read_text(encoding="utf-8"),
                     self.mixer.LOG_FILE.read_text(encoding="utf-8"),
                     self.console.getvalue()):
            self.assertRegex(text, LOG_LINE + "test entry äöü")

    def test_nas_log_is_closed_after_every_entry(self):
        self.mixer.setup_logging()
        self.mixer.logger.info("eins")
        nas = [h for h in self.mixer.logger.handlers if isinstance(h, self.mixer.ShortLivedFileHandler)]
        self.assertEqual(len(nas), 1)
        self.assertIsNone(nas[0].stream)
        # On Windows an open file cannot be renamed.
        moved = self.mixer.LOG_FILE.with_suffix(".moved")
        os.rename(self.mixer.LOG_FILE, moved)
        self.mixer.logger.info("zwei")
        self.assertIn("zwei", self.mixer.LOG_FILE.read_text(encoding="utf-8"))

    def test_unreachable_nas_breaks_nothing(self):
        unreachable = self.root / "fehlt" / "overlay_mixer.log"
        stderr = io.StringIO()
        with mock.patch.object(self.mixer, "LOG_FILE", unreachable), mock.patch("sys.stderr", stderr):
            self.mixer.setup_logging()
            for i in range(3):
                self.mixer.logger.info(f"Eintrag {i}")
        self.assertIn("Eintrag 2", self.mixer.LOCAL_LOG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(stderr.getvalue().count("not writable"), 1, stderr.getvalue())

    def test_nas_log_rotates(self):
        path = self.root / "rotation.log"
        handler = self.mixer.ShortLivedFileHandler(path, maxBytes=200, backupCount=2, encoding="utf-8")
        handler.setFormatter(self.mixer.fmt)
        try:
            for i in range(30):
                handler.emit(logging.LogRecord("t", logging.INFO, __file__, 1, f"Zeile {i:02d} " + "x" * 40, None, None))
        finally:
            handler.close()
        self.assertEqual(sorted(p.name for p in self.root.glob("rotation.log*")),
                         ["rotation.log", "rotation.log.1", "rotation.log.2"])
        self.assertIn("Zeile 29", path.read_text(encoding="utf-8"))


class ThreadExcepthookTest(MixerTestCase):
    def test_unhandled_thread_errors_end_up_in_the_log(self):
        saved = threading.excepthook
        self.addCleanup(setattr, threading, "excepthook", saved)
        self.mixer.install_thread_excepthook()

        def crash():
            raise ValueError("kaputt")

        with self.assertLogs("overlay_mixer", "CRITICAL") as logs:
            t = threading.Thread(target=crash, name="testthread")
            t.start()
            t.join()
        self.assertIn("Unhandled error in thread testthread", logs.output[0])
        self.assertIs(logs.records[0].exc_info[0], ValueError)


class QuickEditTest(MixerTestCase):
    class FakeKernel32:
        def __init__(self, mode=0x1F7, console=True):
            self.mode, self.console, self.set_to = mode, console, None

        def GetStdHandle(self, which):
            return 123

        def GetConsoleMode(self, handle, mode_ref):
            mode_ref._obj.value = self.mode
            return 1 if self.console else 0

        def SetConsoleMode(self, handle, mode):
            self.set_to = mode
            return 1

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_quickedit_is_disabled(self):
        fake = self.FakeKernel32(mode=0x1F7)
        with mock.patch.object(self.mixer, "_kernel32", return_value=fake):
            self.assertTrue(self.mixer.disable_quick_edit())
        self.assertEqual(fake.set_to, (0x1F7 & ~0x40) | 0x80)

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_without_console_nothing_happens(self):
        fake = self.FakeKernel32(console=False)
        with mock.patch.object(self.mixer, "_kernel32", return_value=fake):
            self.assertFalse(self.mixer.disable_quick_edit())
        self.assertIsNone(fake.set_to)

    def test_errors_are_swallowed(self):
        with mock.patch.object(self.mixer, "_kernel32", side_effect=OSError("kaputt")):
            self.assertFalse(self.mixer.disable_quick_edit())

    def test_real_call_does_not_raise(self):
        self.assertIsInstance(self.mixer.disable_quick_edit(), bool)


class SingleInstanceLockTest(MixerTestCase):
    def test_second_lock_in_same_process_fails(self):
        first = self.mixer.acquire_single_instance_lock()
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(self.mixer.acquire_single_instance_lock())
        finally:
            self.mixer.release_single_instance_lock(first)
        again = self.mixer.acquire_single_instance_lock()
        self.assertIsNotNone(again)
        self.mixer.release_single_instance_lock(again)

    def test_lock_works_across_processes_and_ends_with_the_process(self):
        code = (
            "import importlib.util, sys, pathlib\n"
            "spec = importlib.util.spec_from_file_location('m', sys.argv[1])\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "h = m.acquire_single_instance_lock(pathlib.Path(sys.argv[2]))\n"
            "print('ok' if h else 'busy', flush=True)\n"
            "sys.stdin.readline()\n"
        )
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(SCRIPT_PATH), str(self.mixer.LOCK_FILE)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ok")
            self.assertIsNone(self.mixer.acquire_single_instance_lock())
        finally:
            child.communicate("\n", timeout=10)
        handle = self.mixer.acquire_single_instance_lock()
        self.assertIsNotNone(handle)
        self.mixer.release_single_instance_lock(handle)

    def test_main_exits_if_an_instance_is_already_running(self):
        held = self.mixer.acquire_single_instance_lock()
        self.addCleanup(self.mixer.release_single_instance_lock, held)
        with mock.patch.object(self.mixer, "setup_logging"), \
             mock.patch.object(self.mixer, "disable_quick_edit"), \
             mock.patch.object(self.mixer, "install_thread_excepthook"), \
             self.assertLogs("overlay_mixer", "ERROR") as logs:
            self.assertEqual(self.mixer.main(), 1)
        self.assertIn("is already running", logs.output[0])
        self.assertFalse(self.mixer.PID_FILE.exists())


class WaitForDirsTest(MixerTestCase):
    def test_waits_until_folders_are_reachable(self):
        errors = [OSError("network drive not connected"), OSError("network drive not connected"), None]
        with mock.patch.object(self.mixer, "ensure_dirs", side_effect=errors) as ensure, \
             self.assertLogs("overlay_mixer", "INFO") as logs:
            self.mixer.wait_for_dirs(retry_seconds=0.01)
        self.assertEqual(ensure.call_count, 3)
        warnings = [r for r in logs.records if r.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 1, "repetitions must not be logged every time")
        self.assertIn("network drive not connected", warnings[0].getMessage())
        self.assertIn("Folders reachable", logs.output[-1])

    def test_no_log_entry_without_waiting(self):
        with mock.patch.object(self.mixer, "ensure_dirs"), mock.patch.object(self.mixer.logger, "info") as info:
            self.mixer.wait_for_dirs(retry_seconds=0.01)
        info.assert_not_called()


class OverlayQueueTest(MixerTestCase):
    def setUp(self):
        super().setUp()
        self.queue = self.mixer.OverlayQueue()
        self.file = self.eingang / "Stadtfest.mp3"
        self.file.write_bytes(b"x" * 10)

    def test_duplicate_entries_are_dropped(self):
        self.assertTrue(self.queue.put(self.file))
        self.assertFalse(self.queue.put(self.file))
        self.assertFalse(self.queue.put(Path(str(self.file))))
        self.assertEqual(self.queue.qsize(), 1)

    @unittest.skipUnless(os.name == "nt", "paths are case-insensitive on Windows only")
    def test_different_case_is_the_same_path(self):
        self.queue.put(self.file)
        self.assertFalse(self.queue.put(self.eingang / "STADTFEST.MP3"))

    def test_can_be_queued_again_after_processing(self):
        self.queue.put(self.file)
        self.queue.get()
        self.file.unlink()
        self.assertFalse(self.queue.finish(self.file, success=True))
        self.file.write_bytes(b"neu")
        self.assertTrue(self.queue.put(self.file))

    def test_file_left_after_success_is_not_queued_again(self):
        self.queue.put(self.file)
        self.queue.get()
        self.assertTrue(self.queue.finish(self.file, success=True))
        self.assertFalse(self.queue.put(self.file))
        self.file.write_bytes(b"geaenderter Inhalt")  # new file with the same name
        self.assertTrue(self.queue.put(self.file))

    def test_file_left_after_error_only_after_retry_delay(self):
        self.queue.put(self.file)
        self.queue.get()
        self.assertTrue(self.queue.finish(self.file, success=False))
        self.assertFalse(self.queue.put(self.file))  # waiting period still running
        later = time.monotonic() + self.mixer.FAILED_RETRY_SECONDS + 1
        with mock.patch.object(self.mixer.time, "monotonic", return_value=later):
            self.assertTrue(self.queue.put(self.file))


class WorkerTest(MixerTestCase):
    def run_worker(self, queue, items, timeout=5):
        stop = [False]
        t = start_thread(self.mixer.worker_loop, queue, stop)
        for item in items:
            queue.put(item)
        finished = wait_until(lambda: queue.unfinished_tasks == 0 or not t.is_alive(), timeout)
        alive = t.is_alive()
        stop[0] = True
        t.join(timeout=3)
        return finished, alive

    def test_worker_survives_unexpected_errors(self):
        with mock.patch.object(self.mixer, "process_overlay", side_effect=[RuntimeError("Bug"), True]) as process, \
             self.assertLogs("overlay_mixer", "ERROR") as logs:
            finished, alive = self.run_worker(Queue(), [Path("a.mp3"), Path("b.mp3")])
        self.assertTrue(finished and alive)
        self.assertEqual(process.call_count, 2)
        self.assertIn("Unexpected error while processing a.mp3", logs.output[0])

    def test_worker_survives_errors_in_error_handling(self):
        """In v1.4.0 this ended the worker – the mixer kept running but no longer processed anything."""
        files = []
        for name in ("a.mp3", "b.mp3"):
            f = self.eingang / name
            f.write_bytes(b"x")
            files.append(f)
        with mock.patch.object(self.mixer, "wait_for_stable_file", return_value=True), \
             mock.patch.object(self.mixer, "get_duration_seconds", side_effect=RuntimeError("ffprobe kaputt")), \
             mock.patch.object(self.mixer, "move_to_error", side_effect=PermissionError("network drive gone")) as move, \
             mock.patch("threading.excepthook"), \
             self.assertLogs("overlay_mixer", "ERROR") as logs:
            finished, alive = self.run_worker(Queue(), files)
        self.assertTrue(finished and alive, "worker thread died")
        self.assertEqual(move.call_count, 2)
        self.assertIn("Could not move a.mp3 to error folder: network drive gone", "\n".join(logs.output))

    def test_left_over_file_is_reported(self):
        f = self.eingang / "Stadtfest.mp3"
        f.write_bytes(b"x")
        queue = self.mixer.OverlayQueue()
        with mock.patch.object(self.mixer, "process_overlay", return_value=True), \
             self.assertLogs("overlay_mixer", "WARNING") as logs:
            self.run_worker(queue, [f])
        self.assertIn("Still in input folder after successful processing: Stadtfest.mp3", logs.output[0])
        self.assertFalse(queue.put(f))


class OverlayHandlerRobustnessTest(MixerTestCase):
    def setUp(self):
        super().setUp()
        self.queue = self.mixer.OverlayQueue()
        self.handler = self.mixer.OverlayHandler(self.queue)

    def test_renamed_file_in_input_is_detected(self):
        self.handler.dispatch(FileMovedEvent(str(self.eingang / "upload.tmp"), str(self.eingang / "Stadtfest.mp3")))
        self.assertEqual(list(self.queue.queue), [self.eingang / "Stadtfest.mp3"])

    def test_moving_out_of_input_is_ignored(self):
        self.handler.dispatch(FileMovedEvent(str(self.eingang / "a.mp3"), str(self.root / "anderswo" / "a.mp3")))
        self.handler.dispatch(FileMovedEvent(str(self.eingang / "a.mp3"), str(self.eingang / "a.txt")))
        self.assertEqual(list(self.queue.queue), [])

    def test_duplicate_events_are_queued_only_once(self):
        path = str(self.eingang / "Stadtfest.mp3")
        self.handler.dispatch(FileCreatedEvent(path))
        self.handler.dispatch(FileModifiedEvent(path))
        self.handler.dispatch(FileCreatedEvent(path))
        self.handler.dispatch(FileMovedEvent(str(self.eingang / "x.tmp"), path))
        self.assertEqual(len(self.queue.queue), 1)

    def test_handler_error_does_not_stop_watching(self):
        with mock.patch.object(self.queue, "put", side_effect=RuntimeError("kaputt")), \
             self.assertLogs("overlay_mixer", "ERROR") as logs:
            self.handler.dispatch(FileCreatedEvent(str(self.eingang / "a.mp3")))
        self.assertIn("Error while handling a file event", logs.output[0])


class ProcessOverlayRobustnessTest(MixerTestCase):
    def setUp(self):
        super().setUp()
        self.overlay = self.eingang / "Stadtfest.wav"
        self.overlay.write_bytes(b"x" * 100)
        self.atmo_file = self.atmo / "Atmo 32.mp3"
        self.atmo_file.write_bytes(b"x")
        for name, kwargs in [("wait_for_stable_file", {"return_value": True}),
                             ("get_duration_seconds", {"return_value": 10.0}),
                             ("choose_best_atmo", {"return_value": self.atmo_file})]:
            patcher = mock.patch.object(self.mixer, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_success_returns_true(self):
        def fake_mix(atmo, overlay, out_path, title, *extra):
            out_path.write_bytes(b"mp3")

        with mock.patch.object(self.mixer, "mix_with_ffmpeg", side_effect=fake_mix), \
             self.assertLogs("overlay_mixer", "INFO"):
            self.assertTrue(self.mixer.process_overlay(self.overlay))
        self.assertEqual(self.names(self.fertig), ["VAH Stadtfest.mp3"])

    def test_vanished_file_is_skipped_immediately(self):
        self.overlay.unlink()
        self.mixer.wait_for_stable_file.side_effect = AssertionError("must not wait")
        with self.assertLogs("overlay_mixer", "INFO") as logs:
            self.assertFalse(self.mixer.process_overlay(self.overlay))
        self.assertEqual(logs.output, ["INFO:overlay_mixer:Skipped (no longer in input folder): Stadtfest.wav"])

    def test_ffmpeg_error_removes_partial_output_file(self):
        def broken_mix(atmo, overlay, out_path, title, *extra):
            out_path.write_bytes(b"halb")
            raise RuntimeError("ffmpeg failed: Test")

        with mock.patch.object(self.mixer, "mix_with_ffmpeg", side_effect=broken_mix), \
             self.assertLogs("overlay_mixer", "INFO") as logs:
            self.assertFalse(self.mixer.process_overlay(self.overlay))
        self.assertEqual(self.names(self.fertig), [])
        self.assertEqual(self.names(self.fehler), ["Stadtfest.wav"])
        self.assertIn("Removed incomplete output file: VAH Stadtfest.mp3", "\n".join(logs.output))

    def test_error_moving_to_error_folder_is_logged(self):
        with mock.patch.object(self.mixer, "mix_with_ffmpeg", side_effect=RuntimeError("ffmpeg kaputt")), \
             mock.patch.object(self.mixer, "move_to_error", side_effect=PermissionError("gesperrt")), \
             self.assertLogs("overlay_mixer", "ERROR") as logs:
            self.assertFalse(self.mixer.process_overlay(self.overlay))
        self.assertIn("Error: ffmpeg kaputt | Could not move Stadtfest.wav to error folder: gesperrt",
                      logs.output[-1])


class FakeDeadEmitter:
    def is_alive(self):
        return False


class MixerServiceTest(MixerTestCase):
    def setUp(self):
        super().setUp()
        self.service = self.mixer.MixerService()
        self.addCleanup(self.service.stop)

    def test_start_and_stop(self):
        self.service.start()
        self.assertTrue(self.mixer._observer_healthy(self.service.observer))
        self.assertTrue(self.service.worker.is_alive())
        self.service.stop()
        self.assertIsNone(self.service.observer)
        self.assertFalse(self.service.worker.is_alive())

    def test_dead_worker_is_restarted(self):
        self.service.start()
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        self.service.worker = dead
        with self.assertLogs("overlay_mixer", "ERROR") as logs:
            self.service.tick()
        self.assertIn("Worker thread had stopped", logs.output[0])
        self.assertTrue(self.service.worker.is_alive())

    def test_stopped_observer_is_restarted(self):
        self.service.start()
        self.service.observer.stop()
        self.service.observer.join()
        with self.assertLogs("overlay_mixer", "ERROR") as logs:
            self.service.tick()
        self.assertIn("Folder watcher had stopped", logs.output[0])
        self.assertTrue(self.mixer._observer_healthy(self.service.observer))

    def test_dead_emitter_is_detected(self):
        observer = mock.Mock(is_alive=mock.Mock(return_value=True), emitters={FakeDeadEmitter()})
        self.assertFalse(self.mixer._observer_healthy(observer))

    def test_missing_input_is_logged_only_rarely(self):
        with mock.patch.object(self.mixer, "SCHNEIDEN_DIR", self.root / "does not exist"), \
             self.assertLogs("overlay_mixer", "ERROR") as logs:
            for _ in range(3):
                self.service.ensure_observer()
        self.assertEqual(len(logs.output), 1)
        self.assertIsNone(self.service.observer)

    def test_rescan_finds_unreported_files(self):
        (self.eingang / "Stadtfest.mp3").write_bytes(b"x")
        with self.assertLogs("overlay_mixer", "WARNING") as logs:
            self.assertEqual(self.service.rescan(), 1)
        self.assertIn("Rescan: queued 1 file(s)", logs.output[0])
        with mock.patch.object(self.mixer.logger, "warning") as warning:
            self.assertEqual(self.service.rescan(), 0)  # already in the queue
        warning.assert_not_called()

    def test_tick_triggers_rescan_and_heartbeat(self):
        with mock.patch.object(self.service, "ensure_worker"), \
             mock.patch.object(self.service, "ensure_observer"), \
             mock.patch.object(self.service, "rescan") as rescan, \
             self.assertLogs("overlay_mixer", "INFO") as logs:
            start = self.service.last_rescan
            self.service.tick(now=start + 1)
            rescan.assert_not_called()
            self.service.tick(now=start + self.mixer.RESCAN_SECONDS)
            rescan.assert_called_once()
            self.service.tick(now=start + self.mixer.HEARTBEAT_SECONDS)
        self.assertIn("Heartbeat: running", logs.output[-1])

    @requires_ffmpeg
    def test_end_to_end_with_real_folder_watching(self):
        """A new file and a renamed file are processed via folder watching – each exactly once."""
        self.install_test_atmos()
        original = self.mixer.wait_for_stable_file
        with mock.patch.object(self.mixer, "wait_for_stable_file",
                               side_effect=lambda p: original(p, stable_secs=0.3, timeout=10)), \
             mock.patch.object(self.mixer, "RESCAN_SECONDS", 10_000), \
             self.assertLogs("overlay_mixer", "INFO") as logs:
            self.service.start()
            make_tone(self.eingang / "Neu.wav", 3)
            staging = make_tone(self.root / "staging.wav", 3)
            os.replace(staging, self.eingang / "upload.tmp")
            os.replace(self.eingang / "upload.tmp", self.eingang / "Umbenannt.wav")
            done = wait_until(lambda: len(self.names(self.archiv)) == 2, timeout=120)
        self.assertTrue(done, "\n".join(logs.output))
        self.assertEqual(self.names(self.fertig), ["VAH Neu.mp3", "VAH Umbenannt.mp3"])
        self.assertEqual(self.names(self.eingang), [])
        self.assertNotIn("Rescan", "\n".join(logs.output))


class MainTest(MixerTestCase):
    @requires_ffmpeg
    def test_main_startup_processing_and_clean_exit(self):
        """main() end to end: startup, processing a file from the input folder, Ctrl+C, cleanup."""
        import _thread

        self.install_test_atmos()
        make_tone(self.eingang / "Stadtfest.wav", 3)
        saved_handlers = list(self.mixer.logger.handlers)
        saved_hook = threading.excepthook

        def restore():
            for handler in self.mixer.logger.handlers:
                handler.close()
            self.mixer.logger.handlers[:] = saved_handlers
            threading.excepthook = saved_hook

        self.addCleanup(restore)

        def interrupt_when_done():
            wait_until(lambda: self.names(self.archiv) == ["Stadtfest.wav"], timeout=120)
            time.sleep(0.5)
            _thread.interrupt_main()  # like Ctrl+C in the console window

        with mock.patch("sys.stdout", io.StringIO()), \
             mock.patch.object(self.mixer, "disable_quick_edit", return_value=False):
            start_thread(interrupt_when_done)
            self.assertEqual(self.mixer.main(), 0)

        self.assertEqual(self.names(self.fertig), ["VAH Stadtfest.mp3"])
        self.assertFalse(self.mixer.PID_FILE.exists())
        for log_file in (self.mixer.LOCAL_LOG_FILE, self.mixer.LOG_FILE):
            text = log_file.read_text(encoding="utf-8")
            self.assertIn(f"=== Overlay Mixer Watchdog started | version {self.mixer.VERSION} ===", text)
            self.assertIn("Files in input folder at startup: 1", text)
            self.assertIn(f"Input: {self.eingang} | Atmo: {self.atmo} | Output: {self.fertig}", text)  # since v2.0.1
            self.assertIn("ID3 text: Album = Veranstaltungshinweis", text)  # since v1.6.0
            self.assertIn("ID3 media type: TMED = News", text)              # since v1.8.0
            self.assertIn("Loudness: target -18.0 LUFS | raise only", text)   # since v1.10.0
            self.assertIn("dBTP | raised by +", text)                         # test tone (-21.4 LUFS) is raised
            self.assertIn("SUCCESS: VAH Stadtfest.mp3 created", text)
            self.assertIn("=== Overlay Mixer Watchdog stopped ===", text)
        handle = self.mixer.acquire_single_instance_lock()
        self.assertIsNotNone(handle, "lock was not released")
        self.mixer.release_single_instance_lock(handle)


if __name__ == "__main__":
    unittest.main()
