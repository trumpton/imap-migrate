# imap-migrate

A resumable wrapper around [imapsync](https://imapsync.lamiral.info/) for copying a whole mailbox from one IMAP server to another. It defaults to **Gmail → Yahoo Mail**, but works with any two IMAP servers.

Interrupt it at any time, run the same command again, and it carries on where it left off.

## Features

- **Resumable** – messages already on the destination are skipped, so re-runs never duplicate mail.
- **Self-healing** – network drops and timeouts are retried with exponential backoff (30 s → 30 min).
- **Provider-limit aware** – Gmail's daily IMAP download allowance and Yahoo throttling are detected and waited out.
- **Safe credentials** – passwords are prompted for (hidden), passed to imapsync via temporary files so they never appear in `ps`, and wiped on exit.
- **Labels preserved without duplicates** – `--mode labelheader` copies every message exactly once and writes all its Gmail labels into an `X-Gmail-Labels:` header (the same header Google Takeout uses), so Thunderbird or another client can re-create them as tags later.
- **Gmail-label aware** – in the other modes, skips "All Mail", "Important" and "Starred" so mail isn't copied several times; handles older UK `[Google Mail]` accounts.
- **Copy-only** – never deletes anything from either account.
- **Locking and logs** – prevents two runs against the same account pair; keeps a log per pass.

## Requirements

- bash 3.2 or later (macOS default is fine), on macOS, Linux or Windows via WSL
- imapsync 2.x (not needed for `--mode labelheader`, which uses Python 3.9+ instead)
- An app password for each account (recommended), with IMAP enabled in Gmail

## Install

```bash
# imapsync
brew install imapsync          # macOS
sudo apt install imapsync      # Debian / Ubuntu / WSL

# this script
git clone https://github.com/trumpton/imap-migrate.git
cd imap-migrate
chmod +x imap-migrate.sh
```

## Quick start

```bash
# See what would be copied, changing nothing
./imap-migrate.sh --user1 you@gmail.com --user2 you@yahoo.com --dry-run

# Do the migration
./imap-migrate.sh --user1 you@gmail.com --user2 you@yahoo.com
```

To keep every Gmail label without duplicating any message:

```bash
./imap-migrate.sh --user1 you@gmail.com --user2 you@yahoo.com --mode labelheader
```

Each message on Yahoo then carries headers like:

```
X-Gmail-Labels: Inbox,Important,Family,Holidays 2024
X-GM-MSGID: 1780123456789012345
X-GM-THRID: 1780123456789000000
```

You'll be asked for both passwords. For a large mailbox, run it inside `tmux` or `screen` so closing the terminal doesn't stop it. When it finishes, run the same command once more; it should report 0 messages transferred.

## Common options

| Option | Purpose |
| --- | --- |
| `--host1` / `--host2` | Use other IMAP servers (defaults: `imap.gmail.com`, `imap.mail.yahoo.com`) |
| `--mode labelheader` | Copy each message once, recording its labels in an `X-Gmail-Labels:` header |
| `--placement system\|archive\|firstlabel` | labelheader: which one folder each message goes in (default `system`) |
| `--mode allmail` | Copy everything into a single "Gmail Archive" folder instead of one folder per label |
| `--keep-label-duplicates` | Copy a message into every label folder it belongs to |
| `--include-spam-trash` | Also copy Spam and Trash |
| `--max-bytes-per-sec N` | Throttle transfer speed |
| `--dry-run` | Show what would happen without copying |
| `-- ARGS` | Pass extra arguments straight to imapsync |

Run `./imap-migrate.sh --help` for the full list.

## Documentation

See the [User Manual](USER_MANUAL.md) for setup, folder choices, resuming, logs, troubleshooting and post-migration steps.

## Files

| File | Purpose |
| --- | --- |
| `imap-migrate.sh` | The command you run: prompts, retries, locking, logs |
| `gmail-label-sync.py` | Worker used by `--mode labelheader` (can also be run on its own; see `--help`) |
| `tests/` | Tests using fake IMAP servers: `python3 -m unittest discover -s tests` |

## License

Released under the [MIT License](LICENSE). You may use, modify and redistribute it freely, provided the copyright notice and licence text are kept.

imapsync is a separate project by Gilles Lamiral, distributed under its own licence, and is not included here.
