#!/usr/bin/env python3
"""Ticket Notebook - local web server. Python standard library only."""
import json, os, re, sqlite3, datetime, mimetypes, socket, urllib.request, urllib.error
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
        if not c.execute("SELECT 1 FROM sections").fetchone():
            c.executemany("INSERT INTO sections(name) VALUES(?)", [(n,) for n in SECTIONS])
    c.close()


def sections():
    c = db()
    names = [r["name"] for r in c.execute("SELECT name FROM sections ORDER BY id")]
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
            return self.send(200, rows)
        if self.path == "/api/sections":
            return self.send(200, sections())
        self.send(404, {"error": "not found"})

    do_HEAD = do_GET

    def ticket_id(self):
        head, _, tail = self.path.rpartition("/")
        return int(tail) if head == "/api/tickets" and tail.isdecimal() else None

    def write_ticket(self, tid=None):
        # JSON only, so a form posted from another site cannot write here
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send(415, {"error": "Send JSON"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if not 0 < n <= 200000:
                raise ValueError("Ticket is too long")
            t = clean(json.loads(self.rfile.read(n)))
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        now = datetime.datetime.now().isoformat(timespec="seconds")
        c = db()
        with c:
            if tid is None:
                cols = ",".join(f'"{f}"' for f in FIELDS)
                tid = c.execute(
                    f"INSERT INTO tickets({cols},created,updated) VALUES({','.join('?' * 9)})",
                    [*t.values(), now, now]).lastrowid
                ok = True
            else:
                sets = ",".join(f'"{f}"=?' for f in FIELDS)
                ok = c.execute(f"UPDATE tickets SET {sets},updated=? WHERE id=?",
                               [*t.values(), now, tid]).rowcount > 0
        c.close()
        self.send(200 if ok else 404, {"id": tid} if ok else {"error": "Ticket is gone"})

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
        if self.path == "/api/draft":
            return self.write_draft()
        if self.path == "/api/sections":
            return self.add_section()
        if self.path == "/api/tickets":
            return self.write_ticket()
        self.send(404, {"error": "not found"})

    def do_PUT(self):
        tid = self.ticket_id()
        if tid is None:
            return self.send(404, {"error": "not found"})
        self.write_ticket(tid)

    def do_DELETE(self):
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
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
