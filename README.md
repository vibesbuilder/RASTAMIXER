# VAH Overlay Mixer

A small Windows service-style tool that turns spoken event announcements into finished
radio spots. It watches a folder, mixes every new voice recording over a matching
background music bed with FFmpeg, tags the MP3 for the playout software and archives
the original.

"VAH" stands for *Veranstaltungshinweis*, German for "event announcement".

## Features

- **Folder watching** – new recordings in the input folder (e.g. a synced OneDrive
  folder) are picked up automatically and processed one after another.
  Supported: `.mp3`, `.wav`, `.m4a`, `.aac`, `.flac`, `.ogg`, `.mp4` (audio track only).
- **Automatic music bed** – the shortest atmo (background music, `*.mp3`) that is at
  least *overlay + 5 s lead-in + 2 s buffer* long is chosen. The voice starts after
  5 seconds; the result is as long as the atmo; output is MP3 at 320 kbit/s.
- **Loudness** – quiet voice recordings are raised to −18 LUFS (EBU R128) with a
  high-pass filter and a peak limiter before mixing. Recordings are never lowered.
- **Scheduling tags** – if the file name contains a date (`YYYYMMDD` or `YYYY-MM-DD`),
  the ID3 TXXX tags `Enable Date` (date − 14 days) and `Expiry Date` (date + 1 day)
  are written, and the file's *Last Modified*/*Created* timestamps are set from the date.
- **Labels** – every file gets an ID3 media type (TMED, default `News`) and a fixed
  text in a selectable frame (Album, Genre, Comment or a custom TXXX frame).
- **Safe file handling** – output name `VAH <original name>.mp3` (the prefix is never
  doubled), existing files are never overwritten (` (1)`, ` (2)` … is appended),
  processed originals go to an archive folder, problem files to an error folder.
- **Built for unattended operation** – periodic rescan of the input folder, health
  checks with automatic restart of the folder watcher and worker, single-instance
  lock, hourly heartbeat, local log plus an optional second log (e.g. on a network
  share), waits for network drives at startup.

## Requirements

- Windows 10 or 11 (setting the *Created* timestamp and the console handling are
  Windows specific)
- Python 3.11 or newer
- [watchdog](https://pypi.org/project/watchdog/) (`pip install -r requirements.txt`)
- [FFmpeg](https://ffmpeg.org/) with `ffmpeg` and `ffprobe` in `PATH`
  (or configured with full paths)

## Installation

1. Download or clone this repository into a folder of your choice.
2. Install the Python dependency:
   ```
   pip install -r requirements.txt
   ```
3. Install FFmpeg and make sure `ffmpeg -version` works in a console.
4. Copy `overlay_mixer.example.ini` to `overlay_mixer.ini` and adjust at least the
   folders in `[paths]` (see [Configuration](#configuration)).
5. Put your atmo files (`*.mp3` of different lengths) into the atmo folder.
6. Start `start_overlay_mixer.bat`. The console window shows the log; closing it
   stops the mixer.

To start the mixer automatically, put a shortcut to `start_overlay_mixer.bat` into
the startup folder (`shell:startup` for the current user, `shell:common startup` for
all users).

## Configuration

All settings live in `overlay_mixer.ini` next to the script. The file is local and not
part of the repository; `overlay_mixer.example.ini` documents every setting.
Settings you leave out keep their built-in default.

| Section | Settings |
|---|---|
| `[paths]` | `input_dir`, `atmo_dir`, `output_dir`, `error_dir`, `archive_dir`, `log_file` |
| `[programs]` | `ffmpeg`, `ffprobe` |
| `[mix]` | `leadin_seconds` (5), `buffer_seconds` (2), `bitrate` (320k) |
| `[output]` | `prefix` (`"VAH "`) |
| `[tags]` | `enable_date_tag`, `expiry_date_tag`, `enable_days_before` (14), `expiry_days_after` (1), `date_format` (`%Y-%m-%d`), `media_type` (News), `text_field` (Album), `text`, `comment_language` (deu) |
| `[loudness]` | `target_lufs` (−18, `off` disables), `tolerance_lu` (1), `peak_limit_db` (−2), `highpass_hz` (80), `max_gain_db` (20) |

Notes:

- Put a value in double quotes to keep spaces (`prefix = "VAH "`) or to set an empty
  text (`text = ""`).
- Environment variables such as `%USERPROFILE%` are expanded in paths.
- Comments go on their own line (`;` or `#`).
- Without `overlay_mixer.ini` the folders `input`, `atmo`, `output`, `error` and
  `archive` next to the script are used and a warning is logged.
- An unknown setting or an invalid value stops the mixer at startup with an error in
  the log – it never runs with a half-understood configuration.

Restart the mixer after changing the configuration.

## How a file is processed

1. A new file appears in the input folder (or is found by the rescan).
2. The mixer waits until the file size has been stable for 2 seconds (uploads).
3. `ffprobe` measures the duration; the shortest suitable atmo is selected.
   No suitable atmo → the file goes to the error folder.
4. The loudness of the voice track is measured; if it is quieter than the target,
   a high-pass filter, gain and a limiter are applied to the voice track.
5. FFmpeg mixes atmo and voice (voice delayed by 5 s, `amix`) into an MP3 with the
   title, scheduling tags (if a date was found), media type and text label.
6. If a date was found, *Last Modified* is set to that date and *Created* to 14 days
   before it (or to tomorrow if the event is less than 14 days away).
7. The original is moved to the archive folder.

Example: `20261116 Concert in the park.wav` →
`VAH 20261116 Concert in the park.mp3` with `Enable Date = 2026-11-02` and
`Expiry Date = 2026-11-17`.

## Logs

- `logs\overlay_mixer.log` next to the script (rotating, 7 × 1 MB)
- the second log file from `log_file` (opened and closed for every entry, so it also
  works on a network share)
- the console window

Example:

```
2026-11-09 11:07:52 [INFO] Detected: 20261116 Concert in the park.wav
2026-11-09 11:07:52 [INFO] Date detected in file name: 16.11.2026
2026-11-09 11:07:54 [INFO] Loudness: -26.2 LUFS, peak -2.8 dBTP | raised by +7.5 dB (target -18.0 LUFS, peaks limited to -2.0 dB)
2026-11-09 11:07:56 [INFO] ID3 tags set: Enable Date=2026-11-02 | Expiry Date=2026-11-17
2026-11-09 11:07:56 [INFO] ID3 media type set: TMED=News
2026-11-09 11:07:56 [INFO] ID3 text set: Album=Veranstaltungshinweis
2026-11-09 11:07:56 [INFO] Archived: 20261116 Concert in the park.wav -> 20261116 Concert in the park.wav
2026-11-09 11:07:56 [INFO] SUCCESS: VAH 20261116 Concert in the park.mp3 created | Overlay=46.89s | Atmo=63.13s | LeadIn=5s | Buffer=2s | Version=2.0.1
```

## Tests

The tests use only the Python standard library (`unittest`) and need FFmpeg. They run
in temporary folders and never touch the configured folders or `overlay_mixer.ini`.

```
run_tests.bat
```

or

```
python -B -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
