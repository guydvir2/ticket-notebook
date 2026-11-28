#!/usr/bin/env python3
"""Ticket Notebook - local web server. Python standard library only."""
import json, os, re, sqlite3, datetime, mimetypes, socket, urllib.request, urllib.error
import base64, hashlib, hmac, secrets
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("TN_DB", os.path.join(BASE, "tickets.db"))
PORT = int(os.environ.get("TN_PORT", "8080"))
FIELDS = ["title", "section", "status", "problem", "cause", "fix", "open"]
SECTIONS = ["Networking", "HA", "Proxmox"]
STATUSES = ["red", "yellow", "green"]


# Windows Explorer will not name a file ".env", so ai.env (and ai.env.txt) work too
ENV_NAMES = [".env", "ai.env", "ai.env.txt"]
ENV_FILE = next((os.path.join(BASE, n) for n in ENV_NAMES
                 if os.path.exists(os.path.join(BASE, n))), os.path.join(BASE, "ai.env"))


def load_env():
    """Read KEY=value lines from .env next to app.py. Real env vars win."""
    try:
        with open(ENV_FILE, encoding="utf-8-sig") as f:  # -sig: Windows Notepad BOM
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                k, sep, v = line.partition("=")
                if sep and k and not k.startswith("#"):
                    os.environ.setdefault(k.strip(), v.strip().strip('"\''))
    except OSError:
        pass


load_env()
AI_URL = os.environ.get("TN_AI_URL", "").rstrip("/")
AI_KEY = os.environ.get("TN_AI_KEY", "")
AI_MODEL = os.environ.get("TN_AI_MODEL", "FASST")
KEY_FILE = os.environ.get("TN_KEY_FILE", os.path.join(BASE, "secret.key"))


def load_key():
    """Master key for encrypted notes. Made once, kept in secret.key (git ignores it)."""
    if os.environ.get("TN_SECRET_KEY"):
        return bytes.fromhex(os.environ["TN_SECRET_KEY"])
    try:
        with open(KEY_FILE) as f:
            return bytes.fromhex(f.read().strip())
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key.hex() + "\n")
        return key


KEY = load_key()
ENC_KEY = hmac.new(KEY, b"tn-enc", hashlib.sha256).digest()
MAC_KEY = hmac.new(KEY, b"tn-mac", hashlib.sha256).digest()


def _stream(nonce, n):
    out = b""
    for i in range((n + 31) // 32):
        out += hmac.new(ENC_KEY, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
    return out[:n]


def seal(text):
    """Encrypt text (HMAC-SHA256 stream + HMAC tag, standard library only)."""
    if not text:
        return ""
    data = text.encode()
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(data, _stream(nonce, len(data))))
    tag = hmac.new(MAC_KEY, nonce + ct, hashlib.sha256).digest()
    return "v1:" + base64.b64encode(nonce + ct + tag).decode()


def unseal(blob):
    if not blob:
        return ""
    raw = base64.b64decode(blob[3:])
    nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(MAC_KEY, nonce + ct, hashlib.sha256).digest()):
        raise ValueError("Wrong key for this note")
    return bytes(a ^ b for a, b in zip(ct, _stream(nonce, len(ct)))).decode()


STATIC = {"/": "index.html", "/manifest.webmanifest": "manifest.webmanifest",
          "/icon-180.png": "icon-180.png", "/icon-512.png": "icon-512.png"}


def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    c = db()
    with c:
        c.execute("CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY, "
                  + ", ".join(f'"{f}" TEXT NOT NULL DEFAULT \'\'' for f in FIELDS)
                  + ", created TEXT, updated TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS sections("
                  "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE)")
        if "secret" not in [r["name"] for r in c.execute("PRAGMA table_info(tickets)")]:
            c.execute("ALTER TABLE tickets ADD COLUMN secret TEXT NOT NULL DEFAULT ''")
        c.execute("CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, "
                  "title TEXT NOT NULL, body TEXT NOT NULL, created TEXT, updated TEXT)")
        if "notebook" not in [r["name"] for r in c.execute("PRAGMA table_info(notes)")]:
            c.execute("ALTER TABLE notes ADD COLUMN notebook TEXT NOT NULL DEFAULT 'General'")
        c.execute("CREATE TABLE IF NOT EXISTS notebooks("
                  "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE)")
        if not c.execute("SELECT 1 FROM notebooks").fetchone():
            c.execute("INSERT INTO notebooks(name) VALUES('General')")
        if not c.execute("SELECT 1 FROM sections").fetchone():
            c.executemany("INSERT INTO sections(name) VALUES(?)", [(n,) for n in SECTIONS])
    c.close()


def sections():
    c = db()
    names = [r["name"] for r in c.execute("SELECT name FROM sections ORDER BY id")]
    c.close()
    return names


def notebooks():
    c = db()
    names = [r["name"] for r in c.execute("SELECT name FROM notebooks ORDER BY id")]
    c.close()
    return names


def clean(d):
    if not isinstance(d, dict):
        raise ValueError("Send a ticket object")
    t = {f: str(d.get(f, "")).strip() for f in FIELDS}
    if not t["title"]:
        raise ValueError("Give the ticket a title")
    if t["section"] not in sections():
        raise ValueError("Choose a section")
    if t["status"] not in STATUSES:
        raise ValueError("Choose a status")
    return t


PROMPT = """You turn raw notes about a home-lab or smart-home problem into one ticket.
The input may be an error message, a log, or a pasted chat. Reply with JSON only, no other text:
{"title": "...", "section": "...", "status": "red|yellow|green",
 "problem": "...", "cause": "...", "fix": "...", "open": "..."}
Rules:
- title: short, under 60 characters, says what is broken.
- section: exactly one of: %s. Pick the closest.
- status: red if nothing is fixed yet, yellow if partly fixed, green if fixed.
- problem: what was seen, in 1-3 plain sentences. Keep the key error line in `backticks`.
- cause: why it happened, or "" if not known yet.
- fix: the steps that worked or should be tried, one per line, commands in `backticks`.
- open: what is left to do, one item per line written as "[ ] item", or "".
- Never invent facts that are not in the input. Leave a field "" if unknown.
- Replace any password, token, API key or private key with [hidden].
Write in English."""


def draft(text):
    """Ask the OmniRoute gateway to write a ticket from raw text."""
    if not (AI_URL and AI_KEY):
        raise RuntimeError("AI is not set up. Add TN_AI_URL and TN_AI_KEY to .env")
    names = sections()
    req = urllib.request.Request(
        AI_URL + "/chat/completions",
        data=json.dumps({"model": AI_MODEL, "temperature": 0.2, "messages": [
            {"role": "system", "content": PROMPT % ", ".join(names)},
            {"role": "user", "content": text}]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + AI_KEY})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            reply = json.load(r)["choices"][0]["message"]["content"] or ""
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OmniRoute said {e.code}: {e.read(300).decode(errors='replace')}")
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"Cannot reach OmniRoute ({getattr(e, 'reason', e)})")
    except (KeyError, IndexError, ValueError):
        raise RuntimeError("OmniRoute sent an answer this app cannot read")
    m = re.search(r"\{.*\}", reply, re.S)
    try:
        d = json.loads(m.group(0)) if m else None
    except ValueError:
        d = None
    if not isinstance(d, dict):
        raise RuntimeError("The AI did not return a ticket. Try again")
    t = {f: d.get(f, "") for f in FIELDS}
    for f in FIELDS:
        v = t[f]
        t[f] = "\n".join(map(str, v)) if isinstance(v, list) else str(v or "").strip()
    t["section"] = next((n for n in names if n.lower() == t["section"].lower()), "")
    if t["status"] not in STATUSES:
        t["status"] = "red"
    return t


class H(BaseHTTPRequestHandler):
    server_version = "TicketNotebook"

    def log_message(self, fmt, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        name = STATIC.get(self.path)
        if name:
            try:
                with open(os.path.join(BASE, name), "rb") as f:
                    body = f.read()
            except OSError:
                return self.send(404, {"error": "missing file " + name})
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or "json" in ctype:
                ctype += "; charset=utf-8"
            return self.send(200, body, ctype)
        if self.path == "/api/tickets":
            c = db()
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM tickets ORDER BY CASE status WHEN 'red' THEN 0 "
                "WHEN 'yellow' THEN 1 ELSE 2 END, updated DESC, id DESC")]
            c.close()
            for r in rows:  # the secret stays on the server until asked for
                r["has_secret"] = bool(r.pop("secret", ""))
            return self.send(200, rows)
        m = re.fullmatch(r"/api/tickets/(\d+)/secret", self.path)
        if m:
            c = db()
            r = c.execute("SELECT secret FROM tickets WHERE id=?", (int(m[1]),)).fetchone()
            c.close()
            if not r:
                return self.send(404, {"error": "Ticket is gone"})
            try:
                return self.send(200, {"secret": unseal(r["secret"])})
            except ValueError as e:
                return self.send(500, {"error": str(e)})
        if self.path == "/api/notes":
            c = db()
            rows = c.execute("SELECT id, title, notebook, updated FROM notes "
                             "ORDER BY updated DESC").fetchall()
            c.close()
            try:
                return self.send(200, [{"id": r["id"], "title": unseal(r["title"]),
                                        "notebook": r["notebook"],
                                        "updated": r["updated"]} for r in rows])
            except ValueError as e:
                return self.send(500, {"error": str(e)})
        nid = self.note_id()
        if nid is not None:
            c = db()
            r = c.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
            c.close()
            if not r:
                return self.send(404, {"error": "Note is gone"})
            try:
                return self.send(200, {**dict(r), "title": unseal(r["title"]),
                                       "body": unseal(r["body"])})
            except ValueError as e:
                return self.send(500, {"error": str(e)})
        if self.path == "/api/sections":
            return self.send(200, sections())
        if self.path == "/api/notebooks":
            return self.send(200, notebooks())
        self.send(404, {"error": "not found"})

    do_HEAD = do_GET

    def ticket_id(self):
        head, _, tail = self.path.rpartition("/")
        return int(tail) if head == "/api/tickets" and tail.isdecimal() else None

    def note_id(self):
        head, _, tail = self.path.rpartition("/")
        return int(tail) if head == "/api/notes" and tail.isdecimal() else None

    def read_json(self, limit):
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ValueError("Send JSON")
        n = int(self.headers.get("Content-Length", 0))
        if not 0 < n <= limit:
            raise ValueError("Too long")
        d = json.loads(self.rfile.read(n))
        if not isinstance(d, dict):
            raise ValueError("Send an object")
        return d

    def write_note(self, nid=None):
        try:
            d = self.read_json(200000)
            title = str(d.get("title", "")).strip()
            body = str(d.get("body", "")).strip()
            book = str(d.get("notebook", "General")).strip()
            if not title:
                raise ValueError("Give the note a title")
            if book not in notebooks():
                raise ValueError("Choose a notebook")
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        now = datetime.datetime.now().isoformat(timespec="seconds")
        c = db()
        with c:
            if nid is None:
                nid = c.execute("INSERT INTO notes(title,body,notebook,created,updated) "
                                "VALUES(?,?,?,?,?)",
                                (seal(title), seal(body), book, now, now)).lastrowid
                ok = True
            else:
                ok = c.execute("UPDATE notes SET title=?,body=?,notebook=?,updated=? WHERE id=?",
                               (seal(title), seal(body), book, now, nid)).rowcount > 0
        c.close()
        self.send(200 if ok else 404, {"id": nid} if ok else {"error": "Note is gone"})

    def write_ticket(self, tid=None):
        # JSON only, so a form posted from another site cannot write here
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send(415, {"error": "Send JSON"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if not 0 < n <= 200000:
                raise ValueError("Ticket is too long")
            raw = json.loads(self.rfile.read(n))
            t = clean(raw)
            # "secret" sent: set it (empty clears). Not sent: keep the stored one.
            secret = str(raw["secret"]).strip() if "secret" in raw else None
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        now = datetime.datetime.now().isoformat(timespec="seconds")
        c = db()
        with c:
            if tid is None:
                cols = ",".join(f'"{f}"' for f in FIELDS)
                tid = c.execute(
                    f"INSERT INTO tickets({cols},secret,created,updated) "
                    f"VALUES({','.join('?' * 10)})",
                    [*t.values(), seal(secret or ""), now, now]).lastrowid
                ok = True
            else:
                sets = ",".join(f'"{f}"=?' for f in FIELDS)
                ok = c.execute(f"UPDATE tickets SET {sets},updated=? WHERE id=?",
                               [*t.values(), now, tid]).rowcount > 0
                if ok and secret is not None:
                    c.execute("UPDATE tickets SET secret=? WHERE id=?", (seal(secret), tid))
        c.close()
        self.send(200 if ok else 404, {"id": tid} if ok else {"error": "Ticket is gone"})

    def add_notebook(self):
        try:
            d = self.read_json(1000)
            name = " ".join(str(d.get("name", "")).split())
            if not 0 < len(name) <= 30:
                raise ValueError("Use 1 to 30 characters")
            if name.lower() == "all":
                raise ValueError("Choose another name")
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        c = db()
        try:
            with c:
                c.execute("INSERT INTO notebooks(name) VALUES(?)", (name,))
        except sqlite3.IntegrityError:
            return self.send(400, {"error": "That notebook already exists"})
        finally:
            c.close()
        self.send(200, {"name": name})

    def add_section(self):
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send(415, {"error": "Send JSON"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if not 0 < n <= 1000:
                raise ValueError("Name is too long")
            d = json.loads(self.rfile.read(n))
            name = " ".join(str(d.get("name", "") if isinstance(d, dict) else "").split())
            if not 0 < len(name) <= 30:
                raise ValueError("Use 1 to 30 characters")
            if name.lower() == "all":
                raise ValueError("Choose another name")
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        c = db()
        try:
            with c:
                c.execute("INSERT INTO sections(name) VALUES(?)", (name,))
        except sqlite3.IntegrityError:
            return self.send(400, {"error": "That section already exists"})
        finally:
            c.close()
        self.send(200, {"name": name})

    def write_draft(self):
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send(415, {"error": "Send JSON"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if not 0 < n <= 60000:
                raise ValueError("Text is too long, paste the important part")
            d = json.loads(self.rfile.read(n))
            text = str(d.get("text", "") if isinstance(d, dict) else "").strip()
            if not text:
                raise ValueError("Paste something first")
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        try:
            self.send(200, draft(text))
        except RuntimeError as e:
            self.send(502, {"error": str(e)})

    def do_POST(self):
        if self.path == "/api/notes":
            return self.write_note()
        if self.path == "/api/notebooks":
            return self.add_notebook()
        if self.path == "/api/draft":
            return self.write_draft()
        if self.path == "/api/sections":
            return self.add_section()
        if self.path == "/api/tickets":
            return self.write_ticket()
        self.send(404, {"error": "not found"})

    def do_PUT(self):
        nid = self.note_id()
        if nid is not None:
            return self.write_note(nid)
        tid = self.ticket_id()
        if tid is None:
            return self.send(404, {"error": "not found"})
        self.write_ticket(tid)

    def do_DELETE(self):
        nid = self.note_id()
        if nid is not None:
            c = db()
            with c:
                ok = c.execute("DELETE FROM notes WHERE id=?", (nid,)).rowcount > 0
            c.close()
            return self.send(200 if ok else 404, {"deleted": ok})
        tid = self.ticket_id()
        if tid is None:
            return self.send(404, {"error": "not found"})
        c = db()
        with c:
            ok = c.execute("DELETE FROM tickets WHERE id=?", (tid,)).rowcount > 0
        c.close()
        self.send(200 if ok else 404, {"deleted": ok} if ok else {"error": "Ticket is gone"})


def lan_ip():
    """Address other devices on the home network can use, or None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # picks a route, sends nothing
            return s.getsockname()[0]
    except OSError:
        return None


if __name__ == "__main__":
    init()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print("Ticket Notebook is running.")
    print(f"  This computer:  http://localhost:{PORT}")
    ip = lan_ip()
    if ip:
        print(f"  iPhone (Wi-Fi): http://{ip}:{PORT}")
    if AI_URL and AI_KEY:
        print(f"  AI writer:      on, model {AI_MODEL}, {AI_URL}")
    elif not os.path.exists(ENV_FILE):
        print(f"  AI writer:      off, no file {ENV_FILE}")
        near = [n for n in os.listdir(BASE) if ("env" in n.lower() and n not in ENV_NAMES)]
        if near:
            print(f"                  found {', '.join(near)} - rename the right one to ai.env")
    else:
        miss = [k for k, v in (("TN_AI_URL", AI_URL), ("TN_AI_KEY", AI_KEY)) if not v]
        print(f"  AI writer:      off, {' and '.join(miss)} empty in {ENV_FILE}")
    print(f"  Secret notes:   encrypted with {KEY_FILE} - back it up with tickets.db")
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
