# Security Policy

## Supported versions

Only the latest version on the `main` branch receives fixes.

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report them privately through GitHub instead:
[Security → Report a vulnerability](https://github.com/vibesbuilder/RASTAMIXER/security/advisories/new).
Please describe the problem and, if possible, how to reproduce it. You will get an
answer as soon as possible.

## Keeping your installation private

- `overlay_mixer.ini` contains your own folders and paths. It is listed in `.gitignore`;
  never commit or share it.
- Log files (`logs\`, `overlay_mixer.log`) contain file names of your recordings and
  your folder paths; they are excluded by `.gitignore` as well.
