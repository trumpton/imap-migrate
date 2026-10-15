#!/usr/bin/env python3
# Copyright (c) 2026 trumpton. Licensed under the MIT License (see LICENSE).
# https://github.com/trumpton/imap-migrate
"""
gmail-label-sync.py - copy every Gmail message ONCE to another IMAP server, recording
its Gmail labels in an "X-Gmail-Labels:" header (the same header Google Takeout writes).

Normally run for you by `imap-migrate.sh --mode labelheader`, which prompts for passwords,
retries after failures and prevents two runs at once. It can also be run on its own:
see --help.

How it avoids duplicates and survives interruption:
  * It reads only Gmail's "All Mail" folder (plus Spam/Trash if asked), where every
    message appears exactly once, however many labels it has.
  * Progress is stored in an SQLite file keyed by Gmail's permanent message ID
    (X-GM-MSGID). Re-running copies only what is not yet recorded as copied.
  * Each message is marked "pending" just before upload and "copied" just after. If the
    run dies in between, the next run searches the destination for that message's
    X-GM-MSGID header before deciding whether to upload it again.

Headers added to every copied message:
  X-Gmail-Labels: Inbox,Important,Family,Holidays 2024
  X-GM-MSGID: 1780123456789012345
  X-GM-THRID: 1780123456789000000    (Gmail conversation ID)

Exit codes: 0 done, 16 login failed, 64 fatal/config error, 75 temporary failure
(retry later), 130 interrupted.
"""

import argparse
import base64
import getpass
import imaplib
import os
import re
import signal
import sqlite3
import ssl
import sys
import time
from email.header import Header

EXIT_OK = 0
EXIT_AUTH = 16
EXIT_FATAL = 64
EXIT_TRANSIENT = 75
EXIT_INTERRUPTED = 130

LABEL_HEADER = "X-Gmail-Labels"
MSGID_HEADER = "X-GM-MSGID"
THRID_HEADER = "X-GM-THRID"

# Gmail system labels (as sent over IMAP) -> names used in the header, matching Takeout.
SYSTEM_LABELS = {
    "\\inbox": "Inbox",
    "\\sent": "Sent",
    "\\draft": "Drafts",
    "\\important": "Important",
    "\\starred": "Starred",
    "\\spam": "Spam",
    "\\trash": "Trash",
}

KEEP_FLAGS = {"\\seen", "\\answered", "\\flagged", "\\draft"}

# Server refusals that mean "stop this pass and retry/wait", not "skip this message".
RUN_LEVEL_REFUSAL = re.compile(
    r"quota|throttl|bandwidth|unavailable|try again|limit|inuse|server ?bug|too many",
    re.I,
)

MAX_FAILED_ATTEMPTS = 3


class AuthFailed(Exception):
    pass


class FatalError(Exception):
    pass


class Transient(Exception):
    pass


# --------------------------------------------------------------------- logging --
class Log:
    def __init__(self, path=None):
        self.f = open(path, "a", encoding="utf-8") if path else None

    def __call__(self, msg):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
        print(line, flush=True)
        if self.f:
            self.f.write(line + "\n")
            self.f.flush()


log = Log()


# ------------------------------------------------------------ modified UTF-7 --
def mutf7_decode(s):
    """Decode IMAP modified UTF-7 (RFC 3501 5.1.3). Returns s unchanged if invalid."""
    if "&" not in s:
        return s
    out, i = [], 0
    try:
        while i < len(s):
            c = s[i]
            if c != "&":
                out.append(c)
                i += 1
                continue
            j = s.index("-", i)
            chunk = s[i + 1:j]
            if chunk == "":
                out.append("&")
            else:
                b = chunk.replace(",", "/")
                b += "=" * (-len(b) % 4)
                out.append(base64.b64decode(b).decode("utf-16-be"))
            i = j + 1
    except (ValueError, UnicodeDecodeError):
        return s
    return "".join(out)


def mutf7_encode(s):
    out, buf = [], []

    def flush():
        if buf:
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode("ascii")
            out.append("&" + b.rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for c in s:
        if 0x20 <= ord(c) <= 0x7E:
            flush()
            out.append("&-" if c == "&" else c)
        else:
            buf.append(c)
    flush()
    return "".join(out)


def quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ------------------------------------------------------- IMAP response parsing --
LP, RP = object(), object()
_LIT_TAIL = re.compile(rb"\{(\d+)\+?\}\s*$")
_SPECIALS = b' \t\r\n()"'


def _lex(buf, out):
    i, n = 0, len(buf)
    while i < n:
        c = buf[i]
        if c in b" \t\r\n":
            i += 1
        elif c == 0x28:  # (
            out.append(LP)
            i += 1
        elif c == 0x29:  # )
            out.append(RP)
            i += 1
        elif c == 0x22:  # "
            j, s = i + 1, bytearray()
            while j < n:
                d = buf[j]
                if d == 0x5C and j + 1 < n:
                    s.append(buf[j + 1])
                    j += 2
                elif d == 0x22:
                    break
                else:
                    s.append(d)
                    j += 1
            out.append(s.decode("utf-8", "replace"))
            i = j + 1
        else:
            j = i
            while j < n and buf[j] not in _SPECIALS:
                j += 1
            out.append(buf[i:j].decode("utf-8", "replace"))
            i = j


def tokenize(data):
    """Turn imaplib's response list (bytes and (head, literal) tuples) into tokens.
    Literals become bytes tokens; atoms and quoted strings become str."""
    out = []
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, tuple):
            head = item[0]
            m = _LIT_TAIL.search(head)
            if m:
                head = head[:m.start()]
            _lex(head, out)
            out.append(bytes(item[1]))
        else:
            _lex(item, out)
    return out


def tree(tokens):
    stack = [[]]
    for t in tokens:
        if t is LP:
            stack.append([])
        elif t is RP:
            if len(stack) > 1:
                done = stack.pop()
                stack[-1].append(done)
        else:
            stack[-1].append(t)
    while len(stack) > 1:
        done = stack.pop()
        stack[-1].append(done)
    return stack[0]


def parse_fetch(data):
    """Return one dict per FETCH response, keys upper-cased (UID, FLAGS, BODY[] ...)."""
    items = []
    for x in tree(tokenize(data)):
        if isinstance(x, list):
            d = {}
            for k in range(0, len(x) - 1, 2):
                if isinstance(x[k], str):
                    d[x[k].upper()] = x[k + 1]
            items.append(d)
    return items


def parse_list(data):
    """Return [(attrs_lowercase_set, delimiter_or_None, decoded_name)] from LIST."""
    top = tree(tokenize(data))
    res, i = [], 0
    while i < len(top):
        if isinstance(top[i], list) and i + 2 < len(top):
            attrs = {a.lower() for a in top[i] if isinstance(a, str)}
            delim = top[i + 1]
            name = top[i + 2]
            if isinstance(delim, bytes):
                delim = delim.decode("utf-8", "replace")
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            if delim is not None and delim.upper() == "NIL":
                delim = None
            res.append((attrs, delim, mutf7_decode(str(name))))
            i += 3
        else:
            i += 1
    return res


def _text(data):
    parts = []
    for d in data or []:
        if isinstance(d, tuple):
            d = d[0]
        if isinstance(d, bytes):
            d = d.decode("utf-8", "replace")
        if d:
            parts.append(str(d))
    return " ".join(parts)


def seqset(uids):
    uids = sorted(uids)
    parts, start, prev = [], uids[0], uids[0]
    for u in uids[1:]:
        if u == prev + 1:
            prev = u
            continue
        parts.append(f"{start}:{prev}" if start != prev else str(start))
        start = prev = u
    parts.append(f"{start}:{prev}" if start != prev else str(start))
    return ",".join(parts)


# ------------------------------------------------------------ message building --
def gmail_labels(raw_labels, extra=()):
    """Gmail X-GM-LABELS values -> ordered list of readable label names."""
    out = []
    for lab in list(raw_labels or []) + list(extra):
        if isinstance(lab, bytes):
            lab = lab.decode("utf-8", "replace")
        lab = str(lab)
        if lab.startswith("\\"):
            name = SYSTEM_LABELS.get(lab.lower(), lab[1:])
        else:
            name = mutf7_decode(lab)
        if name and name not in out:
            out.append(name)
    return out


def header_line(name, value):
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        enc = Header(value, "utf-8", header_name=name).encode(linesep="\r\n")
        return (name + ": " + enc + "\r\n").encode("ascii")
    if len(name) + 2 + len(value) <= 900:
        return (name + ": " + value + "\r\n").encode("ascii")
    # Very long ASCII label list: fold after commas (lines must stay under 998 bytes).
    parts = value.split(",")
    lines, cur = [], name + ": "
    for i, p in enumerate(parts):
        tok = p + ("," if i < len(parts) - 1 else "")
        if len(cur) + len(tok) > 900 and cur.strip() != name + ":":
            lines.append(cur)
            cur = " "
        cur += tok
    lines.append(cur)
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


_STRIP_HDR = re.compile(rb"^(x-gmail-labels|x-gm-msgid|x-gm-thrid)\s*:", re.I)


def build_message(raw, labels, msgid, thrid=None):
    """Prepend our headers, removing any stale copies of them from the original."""
    m = re.search(rb"\r?\n\r?\n", raw)
    head, rest = (raw[:m.start()], raw[m.start():]) if m else (raw, b"")
    kept, skipping = [], False
    for line in head.splitlines(keepends=True):
        if line[:1] in (b" ", b"\t"):
            if not skipping:
                kept.append(line)
            continue
        skipping = bool(_STRIP_HDR.match(line))
        if not skipping:
            kept.append(line)
    new = b""
    if labels:
        new += header_line(LABEL_HEADER, ",".join(labels))
    new += f"{MSGID_HEADER}: {msgid}\r\n".encode("ascii")
    if thrid:
        new += f"{THRID_HEADER}: {thrid}\r\n".encode("ascii")
    return new + b"".join(kept) + rest


def choose_folder(sys_labels, user_labels, source_kind, placement, special, archive, delim):
    """Pick the ONE destination folder for a message."""
    if source_kind == "spam":
        return special["junk"]
    if source_kind == "trash":
        return special["trash"]
    if placement == "archive":
        return archive
    if "\\inbox" in sys_labels:
        return "INBOX"
    if "\\sent" in sys_labels:
        return special["sent"]
    if "\\draft" in sys_labels:
        return special["drafts"]
    if placement == "firstlabel" and user_labels:
        first = sorted(user_labels, key=str.casefold)[0]
        if delim and delim != "/":
            first = first.replace("/", delim)
        return first
    return archive


# ------------------------------------------------------------------- IMAP conn --
class Conn:
    def __init__(self, label, host, port, user, auth, secret, timeout):
        self.label, self.host, self.port, self.user = label, host, port, user
        self.auth, self.secret, self.timeout = auth, secret, timeout
        self.m = None

    def connect(self):
        try:
            self.m = imaplib.IMAP4_SSL(
                self.host, self.port, ssl_context=ssl.create_default_context(),
                timeout=self.timeout,
            )
        except OSError as e:
            raise Transient(f"{self.label}: cannot connect to {self.host}:{self.port}: {e}")
        try:
            if self.auth == "oauth":
                tok = f"user={self.user}\x01auth=Bearer {self.secret}\x01\x01".encode()
                self.m.authenticate("XOAUTH2", lambda _: tok)
            else:
                self.m.login(self.user, self.secret)
        except imaplib.IMAP4.abort as e:
            raise Transient(f"{self.label}: connection lost during login: {e}")
        except imaplib.IMAP4.error as e:
            raise AuthFailed(f"{self.label} login failed for {self.user}: {e}")

    def capabilities(self):
        return {c.upper() for c in self.m.capabilities}

    def _ok(self, typ, data, what):
        if typ != "OK":
            raise Transient(f"{self.label} {what} refused: {_text(data)}")
        return data

    def list_folders(self):
        typ, data = self.m.list()
        return parse_list(self._ok(typ, data, "LIST"))

    def select(self, folder, readonly=False):
        typ, data = self.m.select(quote(mutf7_encode(folder)), readonly=readonly)
        if typ != "OK":
            raise FatalError(f"{self.label}: cannot open folder '{folder}': {_text(data)}")

    def try_select(self, folder):
        typ, _ = self.m.select(quote(mutf7_encode(folder)))
        return typ == "OK"

    def uid_search_all(self):
        typ, data = self.m.uid("SEARCH", None, "ALL")
        data = self._ok(typ, data, "SEARCH")
        return [int(x) for x in b" ".join(d for d in data if d).split()]

    def fetch_meta(self, uids):
        typ, data = self.m.uid(
            "FETCH", seqset(uids),
            "(UID X-GM-MSGID X-GM-THRID X-GM-LABELS FLAGS INTERNALDATE RFC822.SIZE)",
        )
        return [d for d in parse_fetch(self._ok(typ, data, "FETCH")) if "X-GM-MSGID" in d]

    def fetch_body(self, uid):
        typ, data = self.m.uid("FETCH", str(uid), "(UID BODY.PEEK[])")
        for d in parse_fetch(self._ok(typ, data, "FETCH")):
            if "BODY[]" in d and str(d.get("UID", uid)) == str(uid):
                body = d["BODY[]"]
                return body if isinstance(body, bytes) else str(body).encode()
        return None

    def append(self, folder, flags, internaldate, msg):
        flag_str = "(" + " ".join(flags) + ")" if flags else None
        date = '"' + internaldate + '"' if internaldate else None
        return self.m.append(quote(mutf7_encode(folder)), flag_str, date, msg)

    def create(self, folder):
        typ, data = self.m.create(quote(mutf7_encode(folder)))
        return typ == "OK", _text(data)

    def has_header(self, name, value):
        typ, data = self.m.uid("SEARCH", None, "HEADER", name, quote(value))
        return typ == "OK" and any(d and d.strip() for d in data)

    def logout(self):
        try:
            if self.m:
                self.m.logout()
        except Exception:
            pass


# ----------------------------------------------------------------------- state --
class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS copied (msgid INTEGER PRIMARY KEY, folder TEXT, ts INTEGER);
            CREATE TABLE IF NOT EXISTS pending (msgid INTEGER PRIMARY KEY, folder TEXT, ts INTEGER);
            CREATE TABLE IF NOT EXISTS failed (msgid INTEGER PRIMARY KEY, attempts INTEGER, reason TEXT, ts INTEGER);
            """
        )
        self.db.commit()

    def copied_set(self):
        return {r[0] for r in self.db.execute("SELECT msgid FROM copied")}

    def given_up(self):
        return {r[0] for r in self.db.execute(
            "SELECT msgid FROM failed WHERE attempts >= ?", (MAX_FAILED_ATTEMPTS,))}

    def pending(self):
        return list(self.db.execute("SELECT msgid, folder FROM pending"))

    def mark_pending(self, msgid, folder):
        self.db.execute("INSERT OR REPLACE INTO pending VALUES (?,?,?)",
                        (msgid, folder, int(time.time())))
        self.db.commit()

    def mark_copied(self, msgid, folder):
        self.db.execute("INSERT OR REPLACE INTO copied VALUES (?,?,?)",
                        (msgid, folder, int(time.time())))
        self.db.execute("DELETE FROM pending WHERE msgid=?", (msgid,))
        self.db.execute("DELETE FROM failed WHERE msgid=?", (msgid,))
        self.db.commit()

    def clear_pending(self, msgid):
        self.db.execute("DELETE FROM pending WHERE msgid=?", (msgid,))
        self.db.commit()

    def mark_failed(self, msgid, reason):
        self.db.execute(
            "INSERT INTO failed VALUES (?,1,?,?) ON CONFLICT(msgid) DO UPDATE SET "
            "attempts=attempts+1, reason=excluded.reason, ts=excluded.ts",
            (msgid, reason[:500], int(time.time())),
        )
        self.db.execute("DELETE FROM pending WHERE msgid=?", (msgid,))
        self.db.commit()

    def failed_rows(self):
        return list(self.db.execute("SELECT msgid, attempts, reason FROM failed ORDER BY ts"))


# ------------------------------------------------------------------- the work --
def find_special(folders, attr, fallbacks, default=None):
    for attrs, _d, name in folders:
        if attr in attrs:
            return name
    by_lower = {n.lower(): n for _a, _d, n in folders}
    for fb in fallbacks:
        if fb.lower() in by_lower:
            return by_lower[fb.lower()]
    return default


def resolve_pending(state, dst):
    """Decide whether uploads interrupted mid-way actually landed."""
    for msgid, folder in state.pending():
        found = False
        try:
            if dst.try_select(folder):
                found = dst.has_header(MSGID_HEADER, str(msgid))
        except imaplib.IMAP4.abort:
            raise
        except imaplib.IMAP4.error:
            found = False
        if found:
            log(f"Interrupted upload of message {msgid} had completed; recorded as copied.")
            state.mark_copied(msgid, folder)
        else:
            log(f"Interrupted upload of message {msgid} did not complete; it will be copied again.")
            state.clear_pending(msgid)


def run(args, src, dst, state):
    src.connect()
    log(f"Host1 login OK ({args.user1} @ {args.host1})")
    dst.connect()
    log(f"Host2 login OK ({args.user2} @ {args.host2})")
    if "X-GM-EXT-1" not in src.capabilities():
        raise FatalError("labelheader mode needs a Gmail source (server lacks X-GM-EXT-1).")
    if args.justlogin:
        return EXIT_OK

    src_folders = src.list_folders()
    dst_folders = dst.list_folders()
    delim = next((d for _a, d, _n in dst_folders if d), "/")

    all_mail = find_special(src_folders, "\\all",
                            ["[Gmail]/All Mail", "[Google Mail]/All Mail"])
    if not all_mail:
        raise FatalError("cannot find Gmail's 'All Mail' folder. In Gmail settings, "
                         "Labels tab, make sure 'All Mail' has 'Show in IMAP' ticked.")
    sources = [("all", all_mail)]
    if args.include_spam_trash:
        for kind, attr, fbs in (
            ("spam", "\\junk", ["[Gmail]/Spam", "[Google Mail]/Spam"]),
            ("trash", "\\trash", ["[Gmail]/Trash", "[Google Mail]/Bin", "[Google Mail]/Trash"]),
        ):
            f = find_special(src_folders, attr, fbs)
            if f:
                sources.append((kind, f))
            else:
                log(f"Note: no Gmail {kind} folder visible over IMAP; skipping it.")

    special = {
        "sent": find_special(dst_folders, "\\sent", ["Sent", "Sent Items", "Sent Messages"], "Sent"),
        "drafts": find_special(dst_folders, "\\drafts", ["Draft", "Drafts"], "Drafts"),
        "junk": find_special(dst_folders, "\\junk", ["Bulk", "Bulk Mail", "Spam", "Junk"], "Spam"),
        "trash": find_special(dst_folders, "\\trash", ["Trash", "Deleted Items"], "Trash"),
    }
    existing = {n.lower() for _a, _d, n in dst_folders}
    existing.add("inbox")

    def ensure_folder(name):
        if name.lower() in existing:
            return name
        if args.dry_run:
            return name
        ok, why = dst.create(name)
        if ok or "exist" in why.lower():
            log(f"Created folder '{name}' on Host2")
            existing.add(name.lower())
            return name
        if name != args.archive_folder:
            log(f"WARNING: could not create folder '{name}' ({why}); using '{args.archive_folder}'")
            return ensure_folder(args.archive_folder)
        raise Transient(f"Host2 CREATE '{name}' refused: {why}")

    if not args.dry_run:
        resolve_pending(state, dst)
    copied = state.copied_set()
    given_up = state.given_up()
    log(f"Already copied in earlier passes: {len(copied)}")

    n_new = n_skip = n_fail = 0
    bytes_sent = 0
    t0 = time.monotonic()
    plan = {}

    for kind, folder in sources:
        src.select(folder, readonly=True)
        uids = src.uid_search_all()
        log(f"Gmail '{folder}': {len(uids)} messages")
        for i in range(0, len(uids), args.batch):
            for meta in src.fetch_meta(uids[i:i + args.batch]):
                msgid = int(meta["X-GM-MSGID"])
                if msgid in copied:
                    n_skip += 1
                    continue
                if msgid in given_up:
                    continue
                raw_labels = meta.get("X-GM-LABELS") or []
                if not isinstance(raw_labels, list):
                    raw_labels = [raw_labels]
                sys_labels = {str(l).lower() for l in raw_labels if str(l).startswith("\\")}
                extra = {"spam": ["\\Spam"], "trash": ["\\Trash"]}.get(kind, [])
                labels = gmail_labels(raw_labels, extra)
                user_labels = [mutf7_decode(str(l if not isinstance(l, bytes) else l.decode()))
                               for l in raw_labels if not str(l).startswith("\\")]
                target = choose_folder(sys_labels, user_labels, kind, args.placement,
                                       special, args.archive_folder, delim)

                if args.dry_run:
                    plan[target] = plan.get(target, 0) + 1
                    n_new += 1
                    continue

                uid = int(meta["UID"])
                body = src.fetch_body(uid)
                if body is None:
                    log(f"Message {msgid} disappeared from Gmail during the run; skipped.")
                    continue
                msg = build_message(body, labels, msgid, meta.get("X-GM-THRID"))
                flags = [f for f in (meta.get("FLAGS") or []) if str(f).lower() in KEEP_FLAGS]
                target = ensure_folder(target)

                state.mark_pending(msgid, target)
                typ, data = dst.append(target, flags, meta.get("INTERNALDATE"), msg)
                if typ == "OK":
                    state.mark_copied(msgid, target)
                    copied.add(msgid)
                    n_new += 1
                    bytes_sent += len(msg)
                else:
                    why = _text(data)
                    if RUN_LEVEL_REFUSAL.search(why):
                        state.clear_pending(msgid)
                        raise Transient(f"Host2 APPEND refused: {why}")
                    state.mark_failed(msgid, why)
                    n_fail += 1
                    log(f"WARNING: message {msgid} could not be copied: {why}")

                if n_new and n_new % 100 == 0:
                    log(f"Progress: {n_new} copied this pass ({bytes_sent / 1e6:.1f} MB)")
                if args.max_bytes_per_sec:
                    ahead = bytes_sent / args.max_bytes_per_sec - (time.monotonic() - t0)
                    if ahead > 0:
                        time.sleep(ahead)

    if args.dry_run:
        log(f"DRY RUN: would copy {n_new} messages; {n_skip} already copied.")
        for f, c in sorted(plan.items(), key=lambda kv: -kv[1]):
            log(f"  {c:>8}  ->  {f}")
        return EXIT_OK

    log(f"Pass finished. Transferred: {n_new}  Already there: {n_skip}  Failed: {n_fail}")
    rows = [r for r in state.failed_rows()]
    if rows:
        log(f"WARNING: {len(rows)} messages could not be copied "
            f"(given up after {MAX_FAILED_ATTEMPTS} attempts each):")
        for msgid, attempts, reason in rows[:20]:
            log(f"  {msgid}  attempts={attempts}  {reason}")
    return EXIT_OK


def read_secret(path, prompt):
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read().rstrip("\r\n")
    return getpass.getpass(prompt)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Copy each Gmail message once to another IMAP server, "
                    "adding an X-Gmail-Labels header. Resumable.")
    p.add_argument("--host1", default="imap.gmail.com")
    p.add_argument("--port1", type=int, default=993)
    p.add_argument("--user1", required=True)
    p.add_argument("--auth1", choices=["password", "oauth"], default="password")
    p.add_argument("--secret1", help="file holding the password/token (prompted if omitted)")
    p.add_argument("--host2", default="imap.mail.yahoo.com")
    p.add_argument("--port2", type=int, default=993)
    p.add_argument("--user2", required=True)
    p.add_argument("--auth2", choices=["password", "oauth"], default="password")
    p.add_argument("--secret2")
    p.add_argument("--placement", choices=["system", "archive", "firstlabel"], default="system")
    p.add_argument("--archive-folder", default="Gmail Archive")
    p.add_argument("--include-spam-trash", action="store_true")
    p.add_argument("--state-dir", default=".")
    p.add_argument("--logfile")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--justlogin", action="store_true")
    p.add_argument("--max-bytes-per-sec", type=int, default=0)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--batch", type=int, default=500)
    return p.parse_args(argv)


def main(argv=None):
    global log
    if sys.version_info < (3, 9):
        print("ERROR: Python 3.9 or later is required.", file=sys.stderr)
        return EXIT_FATAL
    args = parse_args(argv)
    log = Log(args.logfile)

    def on_term(_sig, _frm):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)

    src = dst = None
    try:
        os.makedirs(args.state_dir, exist_ok=True)
        state = State(os.path.join(args.state_dir, "labelheader-state.sqlite"))
        src = Conn("Host1", args.host1, args.port1, args.user1, args.auth1,
                   read_secret(args.secret1, f"Password for {args.user1}: "), args.timeout)
        dst = Conn("Host2", args.host2, args.port2, args.user2, args.auth2,
                   read_secret(args.secret2, f"Password for {args.user2}: "), args.timeout)
        return run(args, src, dst, state)
    except AuthFailed as e:
        log(f"ERROR: {e}")
        return EXIT_AUTH
    except FatalError as e:
        log(f"ERROR: {e}")
        return EXIT_FATAL
    except sqlite3.Error as e:
        log(f"ERROR: progress database problem: {e}")
        return EXIT_FATAL
    except KeyboardInterrupt:
        log("Interrupted. Run again to continue.")
        return EXIT_INTERRUPTED
    except (Transient, imaplib.IMAP4.error, OSError) as e:
        log(f"ERROR (temporary): {e}")
        return EXIT_TRANSIENT
    finally:
        for c in (src, dst):
            if c:
                c.logout()


if __name__ == "__main__":
    sys.exit(main())
