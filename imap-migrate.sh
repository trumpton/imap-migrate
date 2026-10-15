#!/usr/bin/env bash
#
# Copyright (c) 2026 trumpton. Licensed under the MIT License (see LICENSE).
# https://github.com/trumpton/imap-migrate
#
# imap-migrate.sh — resumable Gmail -> Yahoo (or any IMAP -> IMAP) migration using imapsync.
#
# Resilience:
#   * imapsync compares messages by header (Message-ID etc.) on every pass, so a re-run
#     skips anything already copied. Interrupt it at any time and run the SAME command again.
#   * Transient failures (network drops, server throttling, timeouts) are retried
#     automatically with exponential backoff.
#   * Gmail's daily bandwidth limit is detected and waited out with a long pause.
#   * A lock file stops two copies running against the same account pair.
#   * Passwords are prompted for (hidden), held in temp files readable only by you,
#     passed to imapsync via --passfile (so they never appear in `ps`), and deleted on exit.
#
#   * --mode labelheader copies each Gmail message exactly once and records all its
#     labels in an "X-Gmail-Labels:" header (via gmail-label-sync.py, kept next to this
#     script), so a mail client such as Thunderbird can tag from it later.
#
# Requires: bash 3.2+ (macOS default is fine), imapsync 2.x
#           (labelheader mode needs python3 3.9+ instead of imapsync).
#
# Quick start:
#   ./imap-migrate.sh --user1 you@gmail.com --user2 you@yahoo.com
#
# Run ./imap-migrate.sh --help for all options.

set -uo pipefail

SCRIPT_NAME=$(basename "$0")

# ---------------------------------------------------------------- defaults --
HOST1="imap.gmail.com";        PORT1=993
HOST2="imap.mail.yahoo.com";   PORT2=993
USER1="";                      USER2=""
AUTH1="password";              AUTH2="password"   # password | oauth
MODE="labels"                  # labels | allmail | labelheader
PLACEMENT="system"             # labelheader: system | archive | firstlabel
ARCHIVE_FOLDER="Gmail Archive"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
WORKER="$SCRIPT_DIR/gmail-label-sync.py"
SKIP_CROSS_DUPS=1              # copy a multi-label message only once
INCLUDE_SPAM_TRASH=0
MAX_RETRIES=0                  # 0 = retry forever
BACKOFF_START=30               # seconds
BACKOFF_MAX=1800               # cap for normal backoff (30 min)
QUOTA_WAIT=3600                # wait when a provider bandwidth/rate limit is hit (1 h)
MAX_AUTH_FAILS=3               # consecutive auth failures before giving up
MAX_BYTES_PER_SEC=""           # optional throttle
STATE_DIR=""
DRY_RUN=0
JUST_FOLDERS=0
EXTRA_ARGS=()

# ------------------------------------------------------------------- help --
usage() {
  cat <<EOF
Usage: $SCRIPT_NAME --user1 SRC_ADDRESS --user2 DEST_ADDRESS [options] [-- extra imapsync args]

Required:
  --user1 ADDR            Source login (e.g. you@gmail.com)
  --user2 ADDR            Destination login (e.g. you@yahoo.com)

Servers (defaults: Gmail -> Yahoo):
  --host1 HOST            Source IMAP server        [${HOST1}]
  --port1 N               Source IMAP port (SSL)    [${PORT1}]
  --host2 HOST            Destination IMAP server   [${HOST2}]
  --port2 N               Destination IMAP port     [${PORT2}]

Authentication (you are always prompted; nothing is stored):
  --auth1 password|oauth  Source: app password, or an OAuth2 access token  [${AUTH1}]
  --auth2 password|oauth  Destination: same choices                          [${AUTH2}]
                          Note: OAuth access tokens usually expire after ~1 hour, so
                          app passwords are better for long migrations.

What to copy:
  --mode labels           Copy each Gmail label as a Yahoo folder (default). "All Mail",
                          "Important" and "Starred" are skipped (they duplicate other folders).
  --mode allmail          Copy only "All Mail" into a single Yahoo folder "Gmail Archive".
  --mode labelheader      Copy every message ONCE and add an "X-Gmail-Labels:" header
                          listing all its labels (needs python3; imapsync not used).
  --placement P           labelheader only: which ONE folder each message goes in:
                            system     Inbox/Sent/Drafts to Yahoo's own, rest to archive (default)
                            archive    everything to the archive folder
                            firstlabel like system, else the first user label's folder
  --archive-folder NAME   Archive folder name for allmail/labelheader   [${ARCHIVE_FOLDER}]
  --keep-label-duplicates In labels mode, copy a message into EVERY label folder it has
                          (default: copy it once, into the first folder it is found in).
  --include-spam-trash    Also copy Spam and Trash (skipped by default).

Behaviour:
  --max-retries N         Give up after N failed passes (0 = never give up)   [${MAX_RETRIES}]
  --backoff-start SECS    First retry delay, doubles each time               [${BACKOFF_START}]
  --backoff-max SECS      Longest normal retry delay                         [${BACKOFF_MAX}]
  --quota-wait SECS       Delay after a provider bandwidth/rate limit        [${QUOTA_WAIT}]
  --max-bytes-per-sec N   Throttle transfer speed (e.g. 500000)
  --state-dir DIR         Where logs and the lock live
                          [~/.imap-migrate/<user1>__<user2>]
  --dry-run               Show what would be copied; change nothing
  --just-folders          Only create the folder structure on the destination
  -h, --help              Show this help

Anything after "--" is passed straight to imapsync.

Interrupt with Ctrl-C at any time; run the same command again to continue.
EOF
}

die()  { echo "ERROR: $*" >&2; exit 2; }

need_val() { [ $# -ge 2 ] && [ -n "$2" ] || die "option $1 needs a value"; }

# --------------------------------------------------------------- arg parse --
while [ $# -gt 0 ]; do
  case "$1" in
    --user1)  need_val "$@"; USER1=$2; shift 2 ;;
    --user2)  need_val "$@"; USER2=$2; shift 2 ;;
    --host1)  need_val "$@"; HOST1=$2; shift 2 ;;
    --host2)  need_val "$@"; HOST2=$2; shift 2 ;;
    --port1)  need_val "$@"; PORT1=$2; shift 2 ;;
    --port2)  need_val "$@"; PORT2=$2; shift 2 ;;
    --auth1)  need_val "$@"; AUTH1=$2; shift 2 ;;
    --auth2)  need_val "$@"; AUTH2=$2; shift 2 ;;
    --mode)   need_val "$@"; MODE=$2;  shift 2 ;;
    --placement)      need_val "$@"; PLACEMENT=$2;      shift 2 ;;
    --archive-folder) need_val "$@"; ARCHIVE_FOLDER=$2; shift 2 ;;
    --keep-label-duplicates) SKIP_CROSS_DUPS=0; shift ;;
    --include-spam-trash)    INCLUDE_SPAM_TRASH=1; shift ;;
    --max-retries)       need_val "$@"; MAX_RETRIES=$2;       shift 2 ;;
    --backoff-start)     need_val "$@"; BACKOFF_START=$2;     shift 2 ;;
    --backoff-max)       need_val "$@"; BACKOFF_MAX=$2;       shift 2 ;;
    --quota-wait)        need_val "$@"; QUOTA_WAIT=$2;        shift 2 ;;
    --max-bytes-per-sec) need_val "$@"; MAX_BYTES_PER_SEC=$2; shift 2 ;;
    --state-dir)         need_val "$@"; STATE_DIR=$2;         shift 2 ;;
    --dry-run)      DRY_RUN=1; shift ;;
    --just-folders) JUST_FOLDERS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; EXTRA_ARGS=("$@"); break ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

[ -n "$USER1" ] || { usage >&2; die "--user1 is required"; }
[ -n "$USER2" ] || { usage >&2; die "--user2 is required"; }

case "$AUTH1" in password|oauth) ;; *) die "--auth1 must be 'password' or 'oauth'";; esac
case "$AUTH2" in password|oauth) ;; *) die "--auth2 must be 'password' or 'oauth'";; esac
case "$MODE"  in labels|allmail|labelheader) ;;  *) die "--mode must be 'labels', 'allmail' or 'labelheader'";; esac
case "$PLACEMENT" in system|archive|firstlabel) ;; *) die "--placement must be 'system', 'archive' or 'firstlabel'";; esac

if [ "$MODE" = "labelheader" ]; then
  [ "$JUST_FOLDERS" -eq 1 ]    && die "--just-folders is not available with --mode labelheader"
  [ "$SKIP_CROSS_DUPS" -eq 0 ] && die "--keep-label-duplicates does not apply to --mode labelheader (it never duplicates)"
  [ -n "${EXTRA_ARGS[*]+set}" ] && die "extra imapsync arguments after '--' do not apply to --mode labelheader"
fi

for n in "$PORT1" "$PORT2" "$MAX_RETRIES" "$BACKOFF_START" "$BACKOFF_MAX" "$QUOTA_WAIT"; do
  case "$n" in ''|*[!0-9]*) die "expected a whole number, got '$n'";; esac
done
if [ -n "$MAX_BYTES_PER_SEC" ]; then
  case "$MAX_BYTES_PER_SEC" in *[!0-9]*) die "--max-bytes-per-sec must be a number";; esac
fi

if [ "$MODE" = "labelheader" ]; then
  command -v python3 >/dev/null 2>&1 || die "python3 not found on PATH (needed for --mode labelheader)."
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "python3 is too old; version 3.9 or later is needed."
  [ -f "$WORKER" ] || die "cannot find $WORKER (keep it in the same folder as $SCRIPT_NAME)."
else
  command -v imapsync >/dev/null 2>&1 || die "imapsync not found on PATH. Install it first (see https://imapsync.lamiral.info/)."
fi

# ------------------------------------------------------------- state / log --
sanitize() { printf '%s' "$1" | tr -c 'A-Za-z0-9._@-' '_'; }

if [ -z "$STATE_DIR" ]; then
  STATE_DIR="$HOME/.imap-migrate/$(sanitize "$USER1")__$(sanitize "$USER2")"
fi
LOG_DIR="$STATE_DIR/logs"
mkdir -p "$LOG_DIR" || die "cannot create $LOG_DIR"
chmod 700 "$STATE_DIR" 2>/dev/null || true
MAIN_LOG="$STATE_DIR/migrate.log"

log() {
  local line
  line="$(date '+%Y-%m-%d %H:%M:%S')  $*"
  echo "$line"
  echo "$line" >> "$MAIN_LOG"
}

# -------------------------------------------------------------------- lock --
LOCK_DIR="$STATE_DIR/lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  old_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    die "another migration for this account pair is already running (PID $old_pid)."
  fi
  echo "Removing stale lock from an earlier run that did not exit cleanly."
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR" || die "cannot create lock $LOCK_DIR"
fi
echo $$ > "$LOCK_DIR/pid"

# ----------------------------------------------------- secrets & cleanup --
SECRET_DIR=$(mktemp -d "${TMPDIR:-/tmp}/imap-migrate.XXXXXX") || die "mktemp failed"
chmod 700 "$SECRET_DIR"
SECRET1="$SECRET_DIR/s1"
SECRET2="$SECRET_DIR/s2"
CHILD_PID=""
INTERRUPTED=0

cleanup() {
  # overwrite then remove secrets
  for f in "$SECRET1" "$SECRET2"; do
    [ -f "$f" ] && { : > "$f"; rm -f "$f"; }
  done
  rm -rf "$SECRET_DIR" "$LOCK_DIR"
}
on_signal() {
  INTERRUPTED=1
  [ -n "$CHILD_PID" ] && kill -TERM "$CHILD_PID" 2>/dev/null
}
trap cleanup EXIT
trap on_signal INT TERM HUP

# Sleep that Ctrl-C can cut short.
nap() {
  sleep "$1" &
  CHILD_PID=$!
  wait "$CHILD_PID" 2>/dev/null
  CHILD_PID=""
}

prompt_secret() {   # $1 = label, $2 = auth type, $3 = file
  local label=$1 kind=$2 file=$3 value=""
  [ -t 0 ] || die "no terminal to prompt for the $label credential"
  while [ -z "$value" ]; do
    if [ "$kind" = "oauth" ]; then
      printf 'OAuth2 access token for %s: ' "$label" >&2
    else
      printf 'Password (app password) for %s: ' "$label" >&2
    fi
    IFS= read -r -s value
    echo >&2
    [ -n "$value" ] || echo "  (empty, try again)" >&2
  done
  ( umask 077; printf '%s\n' "$value" > "$file" )
  value=""
}

# ------------------------------------------------------ build imapsync cmd --
COMMON=(
  --host1 "$HOST1" --port1 "$PORT1" --ssl1 --user1 "$USER1"
  --host2 "$HOST2" --port2 "$PORT2" --ssl2 --user2 "$USER2"
  --timeout1 120 --timeout2 120
)
if [ "$AUTH1" = "oauth" ]; then COMMON+=(--oauthaccesstoken1 "$SECRET1"); else COMMON+=(--passfile1 "$SECRET1"); fi
if [ "$AUTH2" = "oauth" ]; then COMMON+=(--oauthaccesstoken2 "$SECRET2"); else COMMON+=(--passfile2 "$SECRET2"); fi

GM='\[(Gmail|Google Mail)\]'   # older UK accounts use "[Google Mail]"

SYNC=(
  --automap                    # map Sent/Drafts/Trash/Spam to the destination's own folders
  --addheader                  # give messages without a Message-ID one, so re-runs don't duplicate them
  --syncinternaldates
  --exclude "^${GM}\$"         # the non-selectable parent folder
  --nofoldersizesatend
)

if [ "$MODE" = "allmail" ]; then
  SYNC+=(
    --include "^${GM}/All Mail\$"
    --regextrans2 "s{^${GM}/All Mail\$}{${ARCHIVE_FOLDER}}"
  )
elif [ "$MODE" = "labelheader" ]; then
  : # options for the Python worker are built separately below
else
  SYNC+=(
    --folderfirst INBOX
    --exclude "^${GM}/All Mail\$"
    --exclude "^${GM}/Important\$"
    --exclude "^${GM}/Starred\$"
  )
  [ "$SKIP_CROSS_DUPS" -eq 1 ] && SYNC+=(--skipcrossduplicates)
fi

if [ "$INCLUDE_SPAM_TRASH" -eq 0 ]; then
  SYNC+=(--exclude "^${GM}/Spam\$" --exclude "^${GM}/Trash\$" --exclude "^${GM}/Bin\$")
fi

[ -n "$MAX_BYTES_PER_SEC" ] && SYNC+=(--maxbytespersecond "$MAX_BYTES_PER_SEC")
[ "$DRY_RUN" -eq 1 ]        && SYNC+=(--dry)
[ "$JUST_FOLDERS" -eq 1 ]   && SYNC+=(--justfolders)

# labelheader mode: arguments for gmail-label-sync.py
WORKER_COMMON=(
  --host1 "$HOST1" --port1 "$PORT1" --user1 "$USER1" --auth1 "$AUTH1" --secret1 "$SECRET1"
  --host2 "$HOST2" --port2 "$PORT2" --user2 "$USER2" --auth2 "$AUTH2" --secret2 "$SECRET2"
  --state-dir "$STATE_DIR"
)
if [ "$MODE" = "labelheader" ]; then
  SYNC=(--placement "$PLACEMENT" --archive-folder "$ARCHIVE_FOLDER")
  [ "$INCLUDE_SPAM_TRASH" -eq 1 ] && SYNC+=(--include-spam-trash)
  [ -n "$MAX_BYTES_PER_SEC" ]     && SYNC+=(--max-bytes-per-sec "$MAX_BYTES_PER_SEC")
  [ "$DRY_RUN" -eq 1 ]            && SYNC+=(--dry-run)
fi

# Exit codes shared by imapsync (see its docs, "EXIT STATUS") and gmail-label-sync.py
EXIT_AUTH_FAILURE=16
EXIT_USAGE=64

run_imapsync() {   # args: extra engine args; sets RC and RUN_LOG
  RUN_LOG="run-$(date '+%Y%m%d-%H%M%S').log"
  if [ "$MODE" = "labelheader" ]; then
    python3 "$WORKER" "${WORKER_COMMON[@]}" "$@" --logfile "$LOG_DIR/$RUN_LOG" &
  else
    imapsync "${COMMON[@]}" "$@" --logdir "$LOG_DIR" --logfile "$RUN_LOG" &
  fi
  CHILD_PID=$!
  wait "$CHILD_PID"
  RC=$?
  CHILD_PID=""
  RUN_LOG="$LOG_DIR/$RUN_LOG"
}

log_mentions() { [ -f "$RUN_LOG" ] && grep -Eqi "$1" "$RUN_LOG"; }

# ---------------------------------------------------------------- preflight --
log "=== Migration: $USER1 @ $HOST1  ->  $USER2 @ $HOST2  (mode: $MODE) ==="
log "State and logs: $STATE_DIR"

prompt_secret "$USER1 ($HOST1)" "$AUTH1" "$SECRET1"
prompt_secret "$USER2 ($HOST2)" "$AUTH2" "$SECRET2"

tries=0
while :; do
  log "Checking logins on both servers..."
  run_imapsync --justlogin
  [ "$INTERRUPTED" -eq 1 ] && { log "Interrupted."; exit 130; }
  [ "$RC" -eq 0 ] && { log "Both logins OK."; break; }
  [ "$RC" -eq "$EXIT_USAGE" ] && die "stopped with a fatal error (exit $RC). See $RUN_LOG"

  tries=$((tries + 1))
  if [ "$RC" -eq "$EXIT_AUTH_FAILURE" ] || log_mentions 'authenticat|LOGIN failed|invalid credentials'; then
    log "Login failed (see $RUN_LOG)."
    [ "$tries" -ge "$MAX_AUTH_FAILS" ] && die "giving up after $tries failed logins. Check app passwords and that IMAP is enabled."
    if log_mentions "Host1.*(fail|NO|BAD)"; then
      prompt_secret "$USER1 ($HOST1)" "$AUTH1" "$SECRET1"
    elif log_mentions "Host2.*(fail|NO|BAD)"; then
      prompt_secret "$USER2 ($HOST2)" "$AUTH2" "$SECRET2"
    else
      echo "Could not tell which login failed; please re-enter both." >&2
      prompt_secret "$USER1 ($HOST1)" "$AUTH1" "$SECRET1"
      prompt_secret "$USER2 ($HOST2)" "$AUTH2" "$SECRET2"
    fi
  else
    [ "$tries" -ge 5 ] && die "cannot connect (exit $RC). See $RUN_LOG"
    log "Connection check failed (exit $RC); retrying in ${BACKOFF_START}s..."
    nap "$BACKOFF_START"
    [ "$INTERRUPTED" -eq 1 ] && { log "Interrupted."; exit 130; }
  fi
done

# ---------------------------------------------------------------- main loop --
attempt=0
auth_fails=0
delay=$BACKOFF_START

while :; do
  attempt=$((attempt + 1))
  log "--- Pass $attempt starting ---"
  run_imapsync "${SYNC[@]}" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}

  if [ "$INTERRUPTED" -eq 1 ]; then
    log "Interrupted by you. Nothing is lost: run the same command again to continue."
    exit 130
  fi

  if [ "$RC" -eq 0 ]; then
    log "Pass $attempt finished with no errors."
    if log_mentions 'could not be copied'; then
      log "WARNING: some messages were refused by the destination; they are listed in $RUN_LOG"
    fi
    log "Migration complete. Details: $RUN_LOG"
    log "Tip: run the same command once more; it should report 0 messages transferred."
    exit 0
  fi

  log "Pass $attempt ended with exit code $RC (details: $RUN_LOG)."

  if [ "$RC" -eq "$EXIT_USAGE" ]; then
    die "stopped with a fatal error (exit $RC); this will not fix itself by retrying. See $RUN_LOG"
  fi

  if log_mentions 'over ?quota|mailbox is full|storage limit|exceeded.*(storage|quota)|quota.*exceeded'; then
    if ! log_mentions 'bandwidth'; then
      die "the destination mailbox looks full. Free up space on $HOST2, then re-run to continue."
    fi
  fi

  if [ "$RC" -eq "$EXIT_AUTH_FAILURE" ]; then
    auth_fails=$((auth_fails + 1))
    if [ "$auth_fails" -ge "$MAX_AUTH_FAILS" ]; then
      die "login keeps failing mid-run. If you used OAuth tokens they may have expired; re-run (you will be prompted again)."
    fi
  else
    auth_fails=0
  fi

  if [ "$MAX_RETRIES" -gt 0 ] && [ "$attempt" -ge "$MAX_RETRIES" ]; then
    die "stopping after $attempt passes (--max-retries). Re-run to continue from where it stopped."
  fi

  if log_mentions 'bandwidth|too many (simultaneous )?connections|rate limit|throttl|try again later|\[THROTTLED\]|\[UNAVAILABLE\]'; then
    log "Provider limit hit (Gmail allows a limited amount of IMAP download per day). Waiting $((QUOTA_WAIT / 60)) min..."
    nap "$QUOTA_WAIT"
    delay=$BACKOFF_START
  else
    log "Retrying in ${delay}s..."
    nap "$delay"
    delay=$((delay * 2))
    [ "$delay" -gt "$BACKOFF_MAX" ] && delay=$BACKOFF_MAX
  fi

  if [ "$INTERRUPTED" -eq 1 ]; then
    log "Interrupted by you. Run the same command again to continue."
    exit 130
  fi
done
