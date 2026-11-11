# Changelog

## 2.0.1
- Startup log line `Input:` has its space again.

## 2.0.0
- Settings moved to a local configuration file `overlay_mixer.ini`
  (template: `overlay_mixer.example.ini`). Built-in defaults use folders next to the script.
- Missing configuration file: warning and defaults. Invalid configuration: the mixer does not start.

## 1.11.0
- Code comments, docstrings and all log messages in English.

## 1.10.0
- Quiet voice recordings are raised to −18 LUFS (EBU R128) before mixing:
  high-pass 80 Hz, gain (at most +20 dB), peak limiter at −2 dB. Never lowered.

## 1.9.0
- The output prefix is never doubled: `VAH Market 20261116` stays `VAH Market 20261116`,
  `vah concert` becomes `VAH concert`.

## 1.8.0
- ID3 media type (TMED, default `News`) in every file.

## 1.6.0
- Fixed ID3 text in every file in a selectable frame: Album, Genre, Comment (real COMM
  frame, Latin-1/UTF-16 so Windows Explorer shows it) or a custom TXXX frame.

## 1.5.0
- MP4 overlays (audio track only).
- Dates in the format `YYYY-MM-DD` in addition to `YYYYMMDD`.
- ID3 TXXX tags `Enable Date` and `Expiry Date` for scheduling in the playout software.

## 1.4.1
- Robustness: health checks with restart of worker and folder watcher, periodic rescan,
  renamed files are detected, duplicate protection, single-instance lock, local log,
  hourly heartbeat, waiting for network drives, QuickEdit disabled, partial output files
  removed after FFmpeg errors.

## 1.4.0
- Created date: tomorrow if the event is less than 14 days away.

## 1.3.0
- Check before setting the Created date.

## 1.2.0
- Created date set to 14 days before the date in the file name.

## 1.1.0
- Date detection in the file name; Last Modified set to that date.

## 1.0.0
- Folder watching, automatic atmo selection, mix with 5 s lead-in, archive and error folders, logging.
