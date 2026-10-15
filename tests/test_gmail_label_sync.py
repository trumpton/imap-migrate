"""Tests for gmail-label-sync.py, using fake IMAP servers (no network needed).

Run from the repository root:  python3 -m unittest discover -s tests -v
"""
import email
import email.header
import imaplib
import importlib.util
import os
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "gls", os.path.join(HERE, "..", "gmail-label-sync.py"))
gls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gls)
gls.log = lambda msg: None


def raw_msg(n, extra_headers=b""):
    return (b"From: a@example.com\r\nTo: b@example.com\r\n" + extra_headers +
            f"Subject: message {n}\r\nMessage-ID: <m{n}@example.com>\r\n\r\nBody {n}\r\n".encode())


class FakeGmail:
    """Stands in for gls.Conn on the Gmail side."""

    def __init__(self, messages, spam=(), trash=()):
        # messages: list of (msgid, labels, flags)
        self.folders = {
            "[Gmail]/All Mail": [(i + 1, m) for i, m in enumerate(messages)],
            "[Gmail]/Spam": [(i + 1, m) for i, m in enumerate(spam)],
            "[Gmail]/Trash": [(i + 1, m) for i, m in enumerate(trash)],
        }
        self.cur = None

    def connect(self):
        pass

    def capabilities(self):
        return {"IMAP4REV1", "X-GM-EXT-1"}

    def list_folders(self):
        return [({"\\noselect"}, "/", "[Gmail]"), (set(), "/", "INBOX"),
                ({"\\all"}, "/", "[Gmail]/All Mail"), ({"\\junk"}, "/", "[Gmail]/Spam"),
                ({"\\trash"}, "/", "[Gmail]/Trash"), (set(), "/", "Family")]

    def select(self, folder, readonly=False):
        self.cur = folder

    def uid_search_all(self):
        return [uid for uid, _ in self.folders[self.cur]]

    def fetch_meta(self, uids):
        out = []
        for uid, (msgid, labels, flags) in self.folders[self.cur]:
            if uid in uids:
                out.append({"UID": str(uid), "X-GM-MSGID": str(msgid), "X-GM-THRID": str(msgid + 7),
                            "X-GM-LABELS": list(labels), "FLAGS": list(flags),
                            "INTERNALDATE": "01-Jan-2020 10:00:00 +0000"})
        return out

    def fetch_body(self, uid):
        for u, (msgid, _l, _f) in self.folders[self.cur]:
            if u == uid:
                return raw_msg(msgid)
        return None

    def logout(self):
        pass


class FakeYahoo:
    def __init__(self, crash_after=None, crash_after_append=None, refuse=None, quota_after=None):
        self.boxes = {"INBOX": [], "Sent": [], "Draft": [], "Bulk": [], "Trash": []}
        self.crash_after = crash_after              # abort before the Nth append lands
        self.crash_after_append = crash_after_append  # abort right after the Nth lands
        self.refuse = refuse or set()               # msgids refused permanently
        self.quota_after = quota_after
        self.appends = 0
        self.cur = None

    def connect(self):
        pass

    def list_folders(self):
        special = {"Sent": "\\sent", "Draft": "\\drafts", "Bulk": "\\junk", "Trash": "\\trash"}
        return [({special[n]} if n in special else set(), "/", n) for n in self.boxes]

    def create(self, name):
        if name.startswith("BAD"):
            return False, "NO invalid name"
        self.boxes.setdefault(name, [])
        return True, ""

    def append(self, folder, flags, date, msg):
        if self.quota_after is not None and self.appends >= self.quota_after:
            return "NO", [b"[OVERQUOTA] Mailbox is full"]
        if self.crash_after is not None and self.appends >= self.crash_after:
            raise imaplib.IMAP4.abort("socket error: connection reset")
        parsed = email.message_from_bytes(msg)
        if int(parsed[gls.MSGID_HEADER]) in self.refuse:
            return "NO", [b"Message rejected: invalid content"]
        self.boxes[folder].append((flags, date, msg))
        self.appends += 1
        if self.crash_after_append is not None and self.appends >= self.crash_after_append:
            self.crash_after_append = None
            raise imaplib.IMAP4.abort("socket error: connection reset")
        return "OK", [b"APPEND completed"]

    def try_select(self, folder):
        self.cur = folder
        return folder in self.boxes

    def has_header(self, name, value):
        for _f, _d, msg in self.boxes.get(self.cur, []):
            if email.message_from_bytes(msg)[name] == value:
                return True
        return False

    def all_msgids(self):
        ids = []
        for box in self.boxes.values():
            for _f, _d, msg in box:
                ids.append(int(email.message_from_bytes(msg)[gls.MSGID_HEADER]))
        return ids

    def logout(self):
        pass


MESSAGES = [
    (101, ["\\Inbox", "\\Important", "Family"], ["\\Seen"]),
    (102, ["Family", "Holidays 2024"], []),
    (103, ["\\Sent", "Work/Clients"], ["\\Seen", "\\Answered"]),
    (104, ["\\Starred", "Recettes &AOk-t&AOk-"], ["\\Flagged", "$NotJunk"]),   # mUTF-7: "Recettes été"
    (105, [], ["\\Seen"]),
    (106, ["\\Draft"], ["\\Draft"]),
]


def args(tmp, **kw):
    a = types.SimpleNamespace(
        user1="me@gmail.com", host1="imap.gmail.com", user2="me@yahoo.com",
        host2="imap.mail.yahoo.com", justlogin=False, include_spam_trash=False,
        placement="system", archive_folder="Gmail Archive", dry_run=False,
        batch=2, max_bytes_per_sec=0)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def state(tmp):
    return gls.State(os.path.join(tmp, "s.sqlite"))


def header(msg, name):
    v = email.message_from_bytes(msg)[name]
    return str(email.header.make_header(email.header.decode_header(v))) if v else None


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def find(self, y, msgid):
        for folder, box in y.boxes.items():
            for flags, date, msg in box:
                if int(email.message_from_bytes(msg)[gls.MSGID_HEADER]) == msgid:
                    return folder, flags, date, msg
        return None

    def test_each_message_copied_once_with_labels(self):
        g, y = FakeGmail(MESSAGES), FakeYahoo()
        self.assertEqual(gls.run(args(self.tmp), g, y, state(self.tmp)), 0)
        self.assertEqual(sorted(y.all_msgids()), [101, 102, 103, 104, 105, 106])

        folder, flags, date, msg = self.find(y, 101)
        self.assertEqual(folder, "INBOX")
        self.assertEqual(header(msg, "X-Gmail-Labels"), "Inbox,Important,Family")
        self.assertEqual(flags, ["\\Seen"])
        self.assertEqual(date, "01-Jan-2020 10:00:00 +0000")
        self.assertEqual(header(msg, "X-GM-THRID"), "108")
        self.assertIn(b"Body 101", msg)

        self.assertEqual(self.find(y, 102)[0], "Gmail Archive")
        self.assertEqual(header(self.find(y, 102)[3], "X-Gmail-Labels"), "Family,Holidays 2024")
        self.assertEqual(self.find(y, 103)[0], "Sent")
        self.assertEqual(header(self.find(y, 103)[3], "X-Gmail-Labels"), "Sent,Work/Clients")
        f104 = self.find(y, 104)
        self.assertEqual(header(f104[3], "X-Gmail-Labels"), "Starred,Recettes été")
        self.assertEqual(f104[1], ["\\Flagged"])                    # $NotJunk dropped
        self.assertIsNone(email.message_from_bytes(self.find(y, 105)[3])["X-Gmail-Labels"])
        self.assertEqual(self.find(y, 106)[0], "Draft")

    def test_rerun_copies_nothing(self):
        g, y, s = FakeGmail(MESSAGES), FakeYahoo(), state(self.tmp)
        gls.run(args(self.tmp), g, y, s)
        gls.run(args(self.tmp), g, y, s)
        self.assertEqual(len(y.all_msgids()), 6)

    def test_new_mail_picked_up_on_rerun(self):
        g, y, s = FakeGmail(MESSAGES[:3]), FakeYahoo(), state(self.tmp)
        gls.run(args(self.tmp), g, y, s)
        g2 = FakeGmail(MESSAGES)   # three more arrived
        gls.run(args(self.tmp), g2, y, s)
        self.assertEqual(sorted(y.all_msgids()), [101, 102, 103, 104, 105, 106])

    def test_connection_drop_then_resume_no_duplicates(self):
        g, s = FakeGmail(MESSAGES), state(self.tmp)
        y = FakeYahoo(crash_after=3)
        with self.assertRaises(imaplib.IMAP4.abort):
            gls.run(args(self.tmp), g, y, s)
        self.assertEqual(len(y.all_msgids()), 3)
        y.crash_after = None
        gls.run(args(self.tmp), g, y, s)
        self.assertEqual(sorted(y.all_msgids()), [101, 102, 103, 104, 105, 106])

    def test_crash_after_upload_before_bookkeeping_no_duplicate(self):
        g, s = FakeGmail(MESSAGES), state(self.tmp)
        y = FakeYahoo(crash_after_append=2)   # 2nd message lands, then connection dies
        with self.assertRaises(imaplib.IMAP4.abort):
            gls.run(args(self.tmp), g, y, s)
        self.assertEqual(len(s.pending()), 1)
        gls.run(args(self.tmp), g, y, s)
        ids = y.all_msgids()
        self.assertEqual(sorted(ids), [101, 102, 103, 104, 105, 106])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(s.pending(), [])

    def test_permanently_refused_message_is_skipped_and_reported(self):
        g, s = FakeGmail(MESSAGES), state(self.tmp)
        y = FakeYahoo(refuse={104})
        for _ in range(4):
            self.assertEqual(gls.run(args(self.tmp), g, y, s), 0)
        self.assertEqual(sorted(y.all_msgids()), [101, 102, 103, 105, 106])
        rows = s.failed_rows()
        self.assertEqual(rows[0][0], 104)
        self.assertEqual(rows[0][1], gls.MAX_FAILED_ATTEMPTS)   # stops retrying after 3

    def test_destination_full_stops_the_pass(self):
        g, s = FakeGmail(MESSAGES), state(self.tmp)
        y = FakeYahoo(quota_after=2)
        with self.assertRaises(gls.Transient) as cm:
            gls.run(args(self.tmp), g, y, s)
        self.assertIn("OVERQUOTA", str(cm.exception))
        self.assertEqual(s.pending(), [])
        self.assertEqual(s.failed_rows(), [])

    def test_dry_run_changes_nothing(self):
        g, y, s = FakeGmail(MESSAGES), FakeYahoo(), state(self.tmp)
        gls.run(args(self.tmp, dry_run=True), g, y, s)
        self.assertEqual(y.all_msgids(), [])
        self.assertEqual(s.copied_set(), set())

    def test_archive_placement(self):
        g, y = FakeGmail(MESSAGES), FakeYahoo()
        gls.run(args(self.tmp, placement="archive"), g, y, state(self.tmp))
        self.assertEqual(len(y.boxes["Gmail Archive"]), 6)

    def test_firstlabel_placement(self):
        g, y = FakeGmail(MESSAGES), FakeYahoo()
        gls.run(args(self.tmp, placement="firstlabel"), g, y, state(self.tmp))
        self.assertEqual(self.find(y, 102)[0], "Family")
        self.assertEqual(self.find(y, 104)[0], "Recettes été")
        self.assertEqual(self.find(y, 105)[0], "Gmail Archive")

    def test_spam_and_trash_go_to_yahoo_equivalents(self):
        g = FakeGmail(MESSAGES, spam=[(201, ["\\Spam"], [])], trash=[(301, [], [])])
        y = FakeYahoo()
        gls.run(args(self.tmp, include_spam_trash=True), g, y, state(self.tmp))
        self.assertEqual(self.find(y, 201)[0], "Bulk")
        self.assertEqual(self.find(y, 301)[0], "Trash")
        self.assertEqual(header(self.find(y, 301)[3], "X-Gmail-Labels"), "Trash")

    def test_uncreatable_label_folder_falls_back_to_archive(self):
        msgs = [(401, ["BAD*name"], [])]
        g, y = FakeGmail(msgs), FakeYahoo()
        gls.run(args(self.tmp, placement="firstlabel"), g, y, state(self.tmp))
        self.assertEqual(self.find(y, 401)[0], "Gmail Archive")

    def test_non_gmail_source_rejected(self):
        g, y = FakeGmail(MESSAGES), FakeYahoo()
        g.capabilities = lambda: {"IMAP4REV1"}
        with self.assertRaises(gls.FatalError):
            gls.run(args(self.tmp), g, y, state(self.tmp))


class Parsing(unittest.TestCase):
    def test_fetch_with_literals_like_imaplib_returns(self):
        data = [
            (b'1 (UID 5 X-GM-MSGID 1780000000000000001 X-GM-THRID 17 '
             b'X-GM-LABELS ("\\\\Inbox" Family {11}', b'odd "label"'),
            (b') FLAGS (\\Seen) INTERNALDATE "14-Oct-2026 10:00:00 +0100" '
             b'RFC822.SIZE 9 BODY[] {9}', b'Hi\r\nthere'),
            b')',
            b'2 (UID 6 X-GM-MSGID 2 X-GM-LABELS () FLAGS ())',
        ]
        items = gls.parse_fetch(data)
        self.assertEqual(len(items), 2)
        a = items[0]
        self.assertEqual(a["UID"], "5")
        self.assertEqual(a["X-GM-MSGID"], "1780000000000000001")
        self.assertEqual(a["X-GM-LABELS"], ["\\Inbox", "Family", b'odd "label"'])
        self.assertEqual(a["FLAGS"], ["\\Seen"])
        self.assertEqual(a["INTERNALDATE"], "14-Oct-2026 10:00:00 +0100")
        self.assertEqual(a["BODY[]"], b"Hi\r\nthere")
        self.assertEqual(items[1]["X-GM-LABELS"], [])

    def test_list(self):
        data = [b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
                b'(\\HasNoChildren) "/" INBOX',
                (b'(\\HasNoChildren) "/" {16}', b'Recettes &AOk-t&AOk-'),
                b'(\\Noselect) NIL "Top"']
        res = gls.parse_list(data)
        self.assertEqual(res[0], ({"\\hasnochildren", "\\all"}, "/", "[Gmail]/All Mail"))
        self.assertEqual(res[1][2], "INBOX")
        self.assertEqual(res[2][2], "Recettes été")
        self.assertIsNone(res[3][1])

    def test_mutf7_roundtrip(self):
        for s in ["Inbox", "Recettes été", "日本語", "A&B", "Work/Clients", "mix €5 & ü"]:
            self.assertEqual(gls.mutf7_decode(gls.mutf7_encode(s)), s)
        self.assertEqual(gls.mutf7_encode("A&B"), "A&-B")
        self.assertEqual(gls.mutf7_decode("bad&xyz"), "bad&xyz")   # tolerant

    def test_seqset(self):
        self.assertEqual(gls.seqset([5, 1, 2, 3, 7, 8, 10]), "1:3,5,7:8,10")

    def test_labels(self):
        self.assertEqual(gls.gmail_labels(["\\Inbox", "\\Important", "Family", "Family"]),
                         ["Inbox", "Important", "Family"])
        self.assertEqual(gls.gmail_labels([], ["\\Spam"]), ["Spam"])


class Headers(unittest.TestCase):
    def test_non_ascii_label_header_decodes(self):
        line = gls.header_line("X-Gmail-Labels", "Inbox,Recettes été,日本語")
        line.decode("ascii")
        msg = email.message_from_bytes(line + b"\r\nbody")
        self.assertEqual(header(line + b"\r\n", "X-Gmail-Labels"), "Inbox,Recettes été,日本語")
        self.assertTrue(msg["X-Gmail-Labels"])

    def test_long_header_is_folded_under_998(self):
        labels = ",".join(f"Label number {i}" for i in range(200))
        line = gls.header_line("X-Gmail-Labels", labels)
        self.assertTrue(all(len(l) < 998 for l in line.split(b"\r\n")))
        unfolded = header(line + b"\r\n", "X-Gmail-Labels").replace("\r\n", "").replace(", ", ",")
        self.assertEqual(unfolded, labels)

    def test_stale_headers_replaced(self):
        raw = raw_msg(1, b"X-Gmail-Labels: Old,\r\n Stale\r\nX-GM-MSGID: 999\r\n")
        out = gls.build_message(raw, ["New"], 1, 2)
        m = email.message_from_bytes(out)
        self.assertEqual(m.get_all("X-Gmail-Labels"), ["New"])
        self.assertEqual(m.get_all("X-GM-MSGID"), ["1"])
        self.assertNotIn(b"Stale", out)
        self.assertTrue(out.endswith(b"\r\n\r\nBody 1\r\n"))
        self.assertIn(b"Subject: message 1", out)


class ConnWire(unittest.TestCase):
    """Check the real Conn class sends sensible commands and parses imaplib output."""

    def setUp(self):
        self.calls = []
        test = self

        class M:
            capabilities = ("IMAP4REV1", "X-GM-EXT-1")

            def uid(self, cmd, *a):
                test.calls.append((cmd,) + a)
                if cmd == "FETCH" and "BODY.PEEK" in a[1]:
                    return "OK", [(b"1 (UID 9 BODY[] {5}", b"hello"), b")"]
                if cmd == "FETCH":
                    return "OK", [b'1 (UID 9 X-GM-MSGID 77 X-GM-LABELS ("\\\\Inbox") FLAGS ())',
                                  b'1 (FLAGS (\\Seen))']   # unsolicited update, ignored
                if cmd == "SEARCH":
                    return "OK", [b"3 9 12"]

            def append(self, *a):
                test.calls.append(("APPEND",) + a)
                return "OK", [b"done"]

            def select(self, *a, **k):
                test.calls.append(("SELECT",) + a)
                return "OK", [b"3"]

        self.c = gls.Conn("Host1", "h", 993, "u", "password", "s", 10)
        self.c.m = M()

    def test_wire(self):
        self.assertEqual(self.c.uid_search_all(), [3, 9, 12])
        meta = self.c.fetch_meta([3, 9, 12])
        self.assertEqual(len(meta), 1)
        self.assertEqual(self.calls[-1][1], "3,9,12")
        self.assertEqual(self.c.fetch_body(9), b"hello")
        self.c.append("Recettes été", ["\\Seen"], "01-Jan-2020 10:00:00 +0000", b"x")
        self.assertEqual(self.calls[-1][1:4],
                         ('"Recettes &AOk-t&AOk-"', "(\\Seen)", '"01-Jan-2020 10:00:00 +0000"'))
        self.c.select('Odd "name"', readonly=True)
        self.assertEqual(self.calls[-1][1], '"Odd \\"name\\""')


if __name__ == "__main__":
    unittest.main()
