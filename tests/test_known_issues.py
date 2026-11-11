# -*- coding: utf-8 -*-
"""
Known bugs and agreed changes that are not implemented yet.

Each test describes the DESIRED behavior and is marked with @unittest.expectedFailure
as long as the script does not meet it yet. Once an item is implemented, unittest
reports "unexpected success" – then remove the decorator and move the test to the
matching test file.

Currently no open items.

Done:
- v1.4.1: worker dies on an error in error handling, renamed files,
  duplicate events (-> test_robustness), ctypes error code (-> test_timestamps)
- v1.5.0: MP4 (-> test_process_overlay, test_audio), YYYY-MM-DD (-> test_logic)
- v1.6.0: ID3 tagging (-> test_logic, test_audio, test_process_overlay)
- v1.7.0: Content Type/TCON (replaced by Media Type/TMED in v1.8.0)
- v1.8.0: Media Type/TMED (-> test_logic, test_audio, test_process_overlay)
- v1.9.0: no duplicate prefix (-> test_logic, test_process_overlay)
- v1.10.0: raise loudness (-> test_logic, test_audio, test_process_overlay)
- v1.11.0: English log messages (expected messages in all test files)
- v2.0.0: configuration file (-> test_config)
- v2.0.1: startup line "Input: ..." (-> test_robustness.MainTest)

Not decided yet (so no test here): 14-day edge case of the Created rule,
see test_timestamps.test_grenzfall_genau_14_tage_ergibt_created_heute.
"""
