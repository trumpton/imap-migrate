# imap-migrate.sh User Manual

## Overview

`imap-migrate.sh` copies every email from a Gmail account to a Yahoo Mail account, and keeps going until the job is done. It is a wrapper around imapsync, a well-established free tool that copies mail between any two IMAP servers.

The script never deletes anything from either account. It only copies.

What it handles for you:

- **Resuming:** each pass compares message headers on both sides and skips mail already on Yahoo. Stop it at any point and run the same command again.
- **Retrying:** dropped connections and timeouts are retried automatically, waiting 30 seconds, then 60, 120 and so on up to 30 minutes.
- **Provider limits:** when Gmail's daily download allowance or Yahoo's throttling is hit, it pauses for an hour and carries on.
- **Passwords:** asked for once at the start, hidden as you type, never saved to disk permanently and never visible to other programs.
- **Safety:** a lock stops two copies running against the same pair of accounts.

Gmail and Yahoo are the defaults, but any IMAP server can be used on either side.

## Before you start

You need four things in place: imapsync installed, IMAP turned on in Gmail, an app password for each account, and enough space on Yahoo.

### 1. Install imapsync

| Computer | How to install |
| --- | --- |
| Mac | Install Homebrew, then run `brew install imapsync` |
| Debian / Ubuntu Linux | `sudo apt install imapsync` |
| Windows | Install WSL (Windows Subsystem for Linux) with Ubuntu, then use the Linux line above inside it |

Check it worked by running `imapsync --version`.

If you will only use `--mode labelheader`, you don't need imapsync. You need Python 3.9 or later instead, which most Macs and Linux systems already have. Check with `python3 --version`.

### 2. Turn on IMAP in Gmail

In Gmail, open Settings, then See all settings, then Forwarding and POP/IMAP, and make sure IMAP is enabled.

### 3. Create app passwords

Both Google and Yahoo normally refuse your ordinary password for this kind of access. Use an app password instead: a one-off password that only works for mail programs and can be revoked at any time.

1. **Google:** turn on 2-Step Verification, then go to your Google Account, Security, App passwords. Create one called "imapsync" and copy the 16-character code.
2. **Yahoo:** go to Account Info, Account Security, then Generate app password. Name it "imapsync" and copy the code.

Keep both codes handy; the script asks for them when it starts.

### 4. Check Yahoo has room

Look at how much storage your Gmail mail uses (shown at the bottom of the Gmail page or at one.google.com/storage) and compare it with the free space in your Yahoo account. If Yahoo fills up, the script stops and tells you.

### 5. Make the script runnable

Save `imap-migrate.sh` and `gmail-label-sync.py` together in one folder (or clone the repository), open a terminal in that folder and run `chmod +x imap-migrate.sh` once.

## Quick start

Do a dry run first to see what would be copied, then the real run with the same command minus `--dry-run`.

1. Open a terminal in the folder holding the script.
2. Start a dry run, using your own addresses:

```bash
./imap-migrate.sh --user1 you@gmail.com --user2 you@yahoo.com --dry-run
```

3. When asked, paste the Gmail app password, press Enter, then the Yahoo app password. Nothing appears on screen as you type; that is normal.
4. The script checks both logins, then lists the folders and messages it would copy. Nothing is changed.
5. If that looks right, start the real migration:

```bash
./imap-migrate.sh --user1 you@gmail.com --user2 you@yahoo.com
```

6. Leave it running. When it finishes you will see **Migration complete**.
7. Run the same command once more. A clean finish reports 0 messages transferred, which confirms everything is on Yahoo.

A large mailbox can take many hours or several days, mainly because of Gmail's daily download allowance. That is expected; the script waits and continues on its own.

## Options reference

Only `--user1` and `--user2` are required; every other option has a sensible default. Run `./imap-migrate.sh --help` to see this list in the terminal.

| Option | What it does | Default |
| --- | --- | --- |
| `--user1 ADDR` | Source login (your Gmail address) | Required |
| `--user2 ADDR` | Destination login (your Yahoo address) | Required |
| `--host1 HOST` | Source IMAP server | imap.gmail.com |
| `--port1 N` | Source port (SSL) | 993 |
| `--host2 HOST` | Destination IMAP server | imap.mail.yahoo.com |
| `--port2 N` | Destination port (SSL) | 993 |
| `--auth1 password\|oauth` | Source login method: app password or OAuth2 access token | password |
| `--auth2 password\|oauth` | Destination login method | password |
| `--mode labels\|allmail\|labelheader` | How Gmail labels are carried across (see next section) | labels |
| `--placement system\|archive\|firstlabel` | labelheader mode: which one folder each message goes in | system |
| `--archive-folder NAME` | Name of the archive folder (allmail and labelheader modes) | Gmail Archive |
| `--keep-label-duplicates` | Copy a message into every label folder it has | Off |
| `--include-spam-trash` | Also copy Spam and Trash | Off |
| `--max-retries N` | Stop after N failed passes; 0 means never give up | 0 |
| `--backoff-start SECS` | First wait before retrying; doubles each time | 30 |
| `--backoff-max SECS` | Longest normal wait between retries | 1800 |
| `--quota-wait SECS` | Wait after a provider limit is hit | 3600 |
| `--max-bytes-per-sec N` | Limit transfer speed, e.g. 500000 for about 0.5 MB/s | No limit |
| `--state-dir DIR` | Where logs and the lock are kept | `~/.imap-migrate/<user1>__<user2>` |
| `--dry-run` | Show what would be copied; change nothing | Off |
| `--just-folders` | Only create the folders on Yahoo, copy no mail | Off |
| `-- ARGS` | Pass anything after `--` straight to imapsync | None |

App passwords are recommended over OAuth. OAuth access tokens usually expire after about an hour, which is too short for most migrations.

## Choosing how folders and labels are copied

The default suits most people: each Gmail label becomes a Yahoo folder, and each email is copied once.

Gmail does not really have folders. It has labels, and one email can carry several. Over IMAP each label looks like a folder, and the special "All Mail" folder contains every email you have. Copying every folder blindly would put the same email on Yahoo several times.

| Choice | Command addition | What you get on Yahoo | Best for |
| --- | --- | --- | --- |
| Labels, one copy each | (none, the default) | A folder per label. An email with several labels lands only in the first folder it is found in, Inbox first. | Most people |
| Labels, full copies | `--keep-label-duplicates` | A folder per label, each complete. Emails with several labels are stored several times. | Keeping every label folder exactly as in Gmail, if Yahoo has the space |
| One archive folder | `--mode allmail` | All your mail in a single folder called "Gmail Archive" | Keeping one searchable archive with no duplicates |
| One copy, labels in a header | `--mode labelheader` | Each email once, in one folder, with all its labels listed in an `X-Gmail-Labels:` header | Keeping every label with no duplicates, to re-create as tags in Thunderbird |

Whatever you choose:

- Inbox, Sent, Drafts, Spam and Trash are mapped to Yahoo's own folders of the same purpose.
- "Important" and "Starred" are skipped in labels mode; those emails are still copied through their other folders.
- Spam and Trash are skipped unless you add `--include-spam-trash`.
- Read/unread status, flags and original dates are kept.
- Older UK accounts that show "[Google Mail]" instead of "[Gmail]" are handled automatically.

Pick one mode and stick with it for the whole migration. Switching mode partway through will copy mail again into the new layout.

### Keeping labels with `--mode labelheader`

This mode reads Gmail's "All Mail" folder, where every email appears exactly once, and copies each one to a single Yahoo folder. All its Gmail labels are written into the message itself:

```
X-Gmail-Labels: Inbox,Important,Family,Holidays 2024
X-GM-MSGID: 1780123456789012345
X-GM-THRID: 1780123456789000000
```

`X-Gmail-Labels` is the same header Google Takeout uses. `X-GM-MSGID` is Gmail's permanent ID for the message, which the script uses to avoid copying it twice. `X-GM-THRID` identifies the Gmail conversation. Labels with accented or non-Latin characters are stored in standard encoded form, which mail programs decode automatically.

```bash
./imap-migrate.sh --user1 you@gmail.com --user2 you@yahoo.com --mode labelheader --dry-run
./imap-migrate.sh --user1 you@gmail.com --user2 you@yahoo.com --mode labelheader
```

The dry run shows how many emails would go into each Yahoo folder. Choose the folder layout with `--placement`:

| Placement | Where each email goes |
| --- | --- |
| `system` (default) | Inbox, Sent and Drafts go to Yahoo's own folders; everything else to "Gmail Archive" |
| `firstlabel` | As `system`, but other emails go to a folder named after their first label (alphabetically), or the archive if they have none |
| `archive` | Everything into "Gmail Archive" |

With `--include-spam-trash`, Gmail Spam goes to Yahoo's Spam (Bulk) folder and Trash to Yahoo's Trash.

Things to know:

- Progress is kept in `labelheader-state.sqlite` in the state folder. Delete it only if you want to start from scratch.
- If Yahoo refuses a particular email (for example, one that is too large), the script records it, tries again on the next two runs, then gives up on it. Refused emails are listed at the end of each run.
- Labels you change in Gmail after an email has been copied are not updated on Yahoo.
- In Gmail's settings, Labels tab, "All Mail" must have "Show in IMAP" ticked. That is the default.

### Turning the header into Thunderbird tags

You can do this whenever you like, and repeat it, because the header stays with each email.

1. In Thunderbird, add your Yahoo account and let it download the folders.
2. Create a tag for each label you want: Settings, General, Tags, Add.
3. Open Tools, Message Filters, choose the Yahoo account, then New.
4. In the first condition's drop-down choose Customize, add the header `X-Gmail-Labels`, then select it.
5. Set the condition to "X-Gmail-Labels contains Family" and the action to "Tag Message: Family". Repeat for each label, or add several conditions to one filter.
6. Select the filter, pick a folder in "Run selected filter(s) on", then Run Now. Repeat for each folder.

"Contains" also matches longer labels: "Family" matches "Family Photos" too. If that matters, filter on the longer label first, or use the FiltaQuilla add-on, which allows regular expressions such as `(^|,)Family(,|$)`.

Thunderbird saves tags to the server as IMAP keywords, and Yahoo may not keep custom keywords. If tags disappear, they only exist in Thunderbird. Nothing is lost, because you can run the filters again at any time.

## Interruptions, resuming and long runs

To resume after any interruption, run exactly the same command again. Mail already on Yahoo is skipped, so nothing is copied twice.

### What happens when something goes wrong

| Situation | What the script does |
| --- | --- |
| You press Ctrl-C | Stops cleanly, removes the stored passwords and says how to continue |
| Connection drops or times out | Waits 30 s, 60 s, 120 s… up to 30 min between attempts, then retries |
| Gmail's daily download limit or Yahoo throttling | Waits 1 hour, then retries |
| Computer sleeps, loses power or the terminal closes | The run ends; run the command again when you are back |
| Login fails at the start | Asks for the password again, up to 3 times |
| Login fails 3 times during the run | Stops; run again and re-enter the passwords |
| Yahoo mailbox full | Stops; free up space, then run again |

### Keeping a long run alive

If you close the terminal window, the script stops with it. For a big mailbox, run it inside `tmux` (or `screen`) so it survives the window closing.

1. Install tmux if needed: `brew install tmux` on a Mac, `sudo apt install tmux` on Linux.
2. Start a session: `tmux new -s mail`
3. Run the migration command as normal and enter the passwords.
4. Detach and leave it running: press Ctrl-B, then D.
5. Come back later with `tmux attach -t mail`.

Also stop the computer going to sleep while it runs. On a Mac you can start the script with `caffeinate -i ./imap-migrate.sh …` to do this.

### Stale lock message

If a previous run was killed abruptly you may see "Removing stale lock". That is harmless. If you see "another migration… is already running", a copy really is still going; find it (for example in tmux) rather than starting a second one.

## Logs and checking the result

Everything the script does is logged in `~/.imap-migrate/<gmail address>__<yahoo address>/` in your home folder.

| File | Contents |
| --- | --- |
| `migrate.log` | One line per event: passes started, retries, waits, finish. Read this first. |
| `logs/run-YYYYMMDD-HHMMSS.log` | imapsync's full detail for one pass: every folder and message, and any errors |

To watch progress from another terminal:

```bash
tail -f ~/.imap-migrate/*/migrate.log
```

To confirm the migration is complete:

1. Run the same command one final time. The summary near the end of the newest run log should show 0 messages transferred.
2. In Yahoo Mail, open a few folders and compare the message counts with the matching Gmail labels. In the default mode, label folders after Inbox may hold fewer messages than in Gmail, because emails already copied to an earlier folder are not repeated.
3. Spot-check a few old emails with attachments, and search Yahoo for something you know is in your Gmail.

The logs contain folder names and email subjects but never your passwords. Delete the folder once you are happy with the result.

## Troubleshooting

Most problems are login-related and fixed by a fresh app password. For anything else, the newest file in the `logs` folder says what went wrong.

| Message or symptom | Likely cause | Fix |
| --- | --- | --- |
| imapsync not found on PATH | imapsync is not installed, or not visible in this terminal | Install it (see Before you start) and check `imapsync --version` works |
| Login failed / giving up after 3 failed logins | Wrong password, normal password used instead of an app password, or IMAP off in Gmail | Create a new app password; check IMAP is enabled in Gmail settings |
| no terminal to prompt for the credential | The script was started without a terminal, e.g. from a scheduler | Run it from an open terminal window, or inside tmux |
| Permission denied when starting | The script is not marked runnable | Run `chmod +x imap-migrate.sh` |
| Provider limit hit… Waiting 60 min | Gmail's daily download allowance used up, or Yahoo slowing you down | Nothing; it carries on by itself. Add `--max-bytes-per-sec 500000` to go gently |
| destination mailbox looks full | Yahoo storage used up | Free space on Yahoo or consider `--mode allmail` to avoid duplicates, then run again |
| imapsync rejected the options | A typo in extra arguments after `--` | Check or remove those arguments |
| login keeps failing mid-run | App password revoked, or OAuth token expired | Run again and re-enter the passwords; prefer app passwords |
| Duplicate emails on Yahoo | Mode changed between runs, or Gmail and Yahoo both received copies via forwarding | Keep one mode for the whole job; turn on forwarding only after migrating |
| python3 not found / too old | labelheader mode needs Python 3.9+ | Install Python 3 (`brew install python` or `sudo apt install python3`) |
| cannot find gmail-label-sync.py | The worker file was not kept next to the script | Keep both files in the same folder |
| cannot find Gmail's 'All Mail' folder | "Show in IMAP" is off for All Mail | In Gmail settings, Labels tab, tick "Show in IMAP" for All Mail |
| labelheader mode needs a Gmail source | `--host1` is not a Gmail server | labelheader only works when copying from Gmail |
| Very slow start | imapsync is reading message headers in every folder before copying | Normal for large mailboxes; it speeds up once copying begins |

If you get stuck, send the last 30 lines of `migrate.log` and of the newest run log to whoever is helping you. Check first that nothing private is in them.

## After the migration

Once Yahoo has everything, point new mail at Yahoo and tidy up the access you granted.

- [ ] Run the command one last time and confirm 0 messages transferred.
- [ ] In Gmail, Settings, Forwarding and POP/IMAP, add your Yahoo address as a forwarding address and confirm it from Yahoo. New mail sent to Gmail then reaches Yahoo too.
- [ ] Tell your contacts, and update the email address on important accounts (bank, utilities, shopping, social media).
- [ ] If anything arrives in Gmail before forwarding is on, run the script again; it copies only the new messages.
- [ ] Revoke the "imapsync" app password in both Google and Yahoo security settings.
- [ ] Delete the `~/.imap-migrate/` folder once you are satisfied.
- [ ] Keep the Gmail account open for a few months as a backup before deleting any mail from it.
