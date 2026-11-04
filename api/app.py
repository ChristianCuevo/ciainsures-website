#!/usr/bin/env python3
"""Small form/feedback backend for ciainsures.com (Python standard library only).

Runs on 127.0.0.1:8081 behind nginx (location /api/).

  POST /api/contact          quote / policy-review / audit-review / contact requests
  POST /api/feedback         customer satisfaction rating + optional comment (create or update)
  POST /api/feedback/click   records that a customer clicked through to Google reviews
  GET  /api/admin            password-protected dashboard (HTTP Basic auth, user "admin")
  GET  /api/admin/export.csv?t=leads|feedback
  GET  /api/health

Configuration (environment, normally /etc/ciainsures/api.env):
  ADMIN_PASSWORD   required for /api/admin
  DB_PATH          default /var/lib/ciainsures/data.db
  SITE_URL         default https://ciainsures.com
  SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASS, SMTP_FROM, NOTIFY_TO   optional e-mail alerts
"""
import base64, csv, hmac, html, io, json, os, re, smtplib, sqlite3, threading, time
from collections import defaultdict, deque
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DB_PATH = os.environ.get("DB_PATH", "/var/lib/ciainsures/data.db")
SITE_URL = os.environ.get("SITE_URL", "https://ciainsures.com").rstrip("/")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
MAX_BODY = 32 * 1024
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS leads(
          id INTEGER PRIMARY KEY, created TEXT, form_type TEXT, name TEXT, company TEXT, email TEXT,
          phone TEXT, industry TEXT, message TEXT, page TEXT, ip TEXT);
        CREATE TABLE IF NOT EXISTS feedback(
          id INTEGER PRIMARY KEY, created TEXT, updated TEXT, rating INTEGER, name TEXT, contact TEXT,
          comment TEXT, ref TEXT, google_clicked INTEGER DEFAULT 0, ip TEXT, token TEXT);
        """)


# ------------------------------------------------------------------ helpers
_hits = defaultdict(deque)


def rate_limited(ip, limit=20, window=600):
    q, now = _hits[ip], time.time()
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


def clip(v, n):
    return (str(v or "")).strip()[:n]


def send_mail(subject, body):
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("NOTIFY_TO")
    if not host or not to:
        return

    def run():
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER")
            msg["To"] = to
            msg.set_content(body)
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=20) as s:
                s.starttls()
                if os.environ.get("SMTP_USER"):
                    s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
                s.send_message(msg)
        except Exception as e:  # never break a form submission because mail failed
            print("mail error:", e, flush=True)

    threading.Thread(target=run, daemon=True).start()


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ handler
class H(BaseHTTPRequestHandler):
    server_version = "cia-api"

    def log_message(self, fmt, *args):
        print("%s %s" % (self.client_ip(), fmt % args), flush=True)

    def client_ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "charset" not in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def json(self, code, obj):
        self.send(code, json.dumps(obj))

    def wants_json(self):
        return "application/json" in (self.headers.get("Accept", "") + self.headers.get("Content-Type", ""))

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("Request too large")
        raw = self.rfile.read(n).decode("utf-8", "replace")
        if "application/json" in self.headers.get("Content-Type", ""):
            d = json.loads(raw or "{}")
            return d if isinstance(d, dict) else {}
        return {k: v[0] for k, v in parse_qs(raw).items()}

    # --- routes
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/health":
            return self.json(200, {"ok": True})
        if u.path.startswith("/api/admin"):
            if not self.authorized():
                return self.send(401, "Authentication required", "text/plain",
                                 {"WWW-Authenticate": 'Basic realm="CIA admin"'})
            if u.path == "/api/admin/export.csv":
                return self.export(parse_qs(u.query).get("t", ["leads"])[0])
            return self.admin()
        self.json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path in ("/api/contact", "/api/feedback") and rate_limited(self.client_ip()):
                return self.json(429, {"ok": False, "error": "Too many submissions. Please call 877-242-9600."})
            d = self.body()
            if path == "/api/contact":
                return self.contact(d)
            if path == "/api/feedback":
                return self.feedback(d)
            if path == "/api/feedback/click":
                return self.click(d)
        except (ValueError, json.JSONDecodeError) as e:
            return self.json(400, {"ok": False, "error": str(e) or "Bad request"})
        self.json(404, {"ok": False, "error": "Not found"})

    def contact(self, d):
        if d.get("website"):  # honeypot: silently accept bots
            return self.json(200, {"ok": True})
        name, email = clip(d.get("name"), 120), clip(d.get("email"), 160)
        if not name or not EMAIL_RE.match(email):
            return self.json(400, {"ok": False, "error": "Please enter your name and a valid email address."})
        row = (now(), clip(d.get("form_type"), 40) or "contact", name, clip(d.get("company"), 160), email,
               clip(d.get("phone"), 40), clip(d.get("industry"), 80), clip(d.get("message"), 4000),
               clip(d.get("page"), 200), self.client_ip())
        with _db_lock, db() as c:
            c.execute("INSERT INTO leads(created,form_type,name,company,email,phone,industry,message,page,ip) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)", row)
        send_mail(f"New website request ({row[1]}): {name}",
                  "\n".join(f"{k}: {v}" for k, v in zip(
                      ["When", "Form", "Name", "Company", "Email", "Phone", "Industry", "Message", "Page"], row[:9])))
        if self.wants_json():
            return self.json(200, {"ok": True})
        self.send(303, "", "text/plain", {"Location": "/thank-you/"})

    def feedback(self, d):
        try:
            rating = int(d.get("rating") or 0)
        except ValueError:
            rating = 0
        if not 1 <= rating <= 5:
            return self.json(400, {"ok": False, "error": "Please choose a rating."})
        fid, token = d.get("id"), clip(d.get("token"), 64)
        fields = dict(rating=rating, name=clip(d.get("name"), 120), contact=clip(d.get("contact"), 160),
                      comment=clip(d.get("comment"), 3000), ref=clip(d.get("ref"), 80))
        with _db_lock, db() as c:
            existing = None
            if fid:
                existing = c.execute("SELECT * FROM feedback WHERE id=? AND ip=?", (int(fid), self.client_ip())).fetchone()
            if existing:
                c.execute("UPDATE feedback SET updated=?,rating=?,name=COALESCE(NULLIF(?,''),name),"
                          "contact=COALESCE(NULLIF(?,''),contact),comment=COALESCE(NULLIF(?,''),comment) WHERE id=?",
                          (now(), rating, fields["name"], fields["contact"], fields["comment"], existing["id"]))
                fid = existing["id"]
            else:
                cur = c.execute("INSERT INTO feedback(created,updated,rating,name,contact,comment,ref,ip,token) "
                                "VALUES(?,?,?,?,?,?,?,?,?)", (now(), now(), rating, fields["name"], fields["contact"],
                                                              fields["comment"], fields["ref"], self.client_ip(), token))
                fid = cur.lastrowid
        if fields["comment"] or rating <= 3:
            send_mail(f"Customer feedback: {rating}★ from {fields['name'] or 'a customer'}",
                      f"Rating: {rating}/5\nName: {fields['name']}\nContact: {fields['contact']}\nRef: {fields['ref']}\n\n"
                      f"{fields['comment'] or '(no comment yet)'}\n\nDashboard: {SITE_URL}/api/admin")
        self.json(200, {"ok": True, "id": fid, "show_review": rating >= 4})

    def click(self, d):
        try:
            fid = int(d.get("id") or 0)
        except ValueError:
            fid = 0
        if fid:
            with _db_lock, db() as c:
                c.execute("UPDATE feedback SET google_clicked=1 WHERE id=? AND ip=?", (fid, self.client_ip()))
        self.json(200, {"ok": True})

    # --- admin
    def authorized(self):
        if not ADMIN_PASSWORD:
            return False
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:
            return False
        return user == "admin" and hmac.compare_digest(pw, ADMIN_PASSWORD)

    def export(self, table):
        table = "feedback" if table == "feedback" else "leads"
        with db() as c:
            rows = c.execute(f"SELECT * FROM {table} ORDER BY id DESC").fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        if rows:
            w.writerow(rows[0].keys())
            w.writerows([tuple(r) for r in rows])
        self.send(200, buf.getvalue(), "text/csv", {"Content-Disposition": f'attachment; filename="{table}.csv"'})

    def admin(self):
        e = html.escape
        with db() as c:
            leads = c.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 200").fetchall()
            fb = c.execute("SELECT * FROM feedback ORDER BY id DESC LIMIT 300").fetchall()
            st = c.execute("SELECT COUNT(*) n, AVG(rating) avg, SUM(rating>=4) happy, SUM(rating<=3) unhappy, "
                           "SUM(google_clicked) clicks FROM feedback").fetchone()
        stars = lambda n: "★" * n + "☆" * (5 - n)
        lead_rows = "".join(
            f"<tr><td>{e(r['created'])}</td><td>{e(r['form_type'])}</td><td>{e(r['name'])}<br><small>{e(r['company'])}</small></td>"
            f"<td><a href='mailto:{e(r['email'])}'>{e(r['email'])}</a><br>{e(r['phone'])}</td><td>{e(r['industry'])}</td>"
            f"<td class=msg>{e(r['message'])}</td></tr>" for r in leads) or "<tr><td colspan=6>No requests yet.</td></tr>"
        fb_rows = "".join(
            f"<tr class='{'bad' if r['rating'] <= 3 else ''}'><td>{e(r['created'])}</td><td class=st>{stars(r['rating'])}</td>"
            f"<td>{e(r['name'])}<br><small>{e(r['contact'])}</small></td><td>{e(r['ref'])}</td>"
            f"<td>{'✔' if r['google_clicked'] else ''}</td><td class=msg>{e(r['comment'])}</td></tr>" for r in fb
        ) or "<tr><td colspan=6>No feedback yet.</td></tr>"
        avg = f"{st['avg']:.2f}" if st["avg"] else "–"
        page = f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content=noindex><title>CIA Admin</title><style>
body{{font:15px/1.5 system-ui,sans-serif;margin:0;background:#f3f7f9;color:#14232b}}
header{{background:#00212d;color:#fff;padding:16px 24px}}h1{{margin:0;font-size:20px}}main{{padding:24px;max-width:1300px;margin:auto}}
.kpis{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}}.kpi{{background:#fff;border-radius:12px;padding:14px 18px;min-width:150px}}
.kpi b{{display:block;font-size:24px;color:#015275}}section{{background:#fff;border-radius:12px;padding:18px;margin-bottom:24px;overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border-bottom:1px solid #e3eaee;padding:8px;text-align:left;vertical-align:top}}
.msg{{max-width:420px;white-space:pre-wrap}}.st{{color:#f6be00;white-space:nowrap}}tr.bad{{background:#fff4f2}}
input{{font:inherit;padding:8px;border:1px solid #c9d6de;border-radius:8px;width:220px}}code{{background:#eef3f6;padding:3px 6px;border-radius:6px;word-break:break-all}}
textarea{{width:100%;font:inherit;padding:10px;border:1px solid #c9d6de;border-radius:8px}}button{{font:inherit;padding:8px 14px;border:0;border-radius:8px;background:#015275;color:#fff;cursor:pointer}}
</style></head><body><header><h1>Central Insurance Agency · Website admin</h1></header><main>
<div class=kpis><div class=kpi><b>{st['n'] or 0}</b>feedback responses</div><div class=kpi><b>{avg}</b>average rating</div>
<div class=kpi><b>{st['happy'] or 0}</b>happy (4–5★)</div><div class=kpi><b>{st['unhappy'] or 0}</b>need follow-up (1–3★)</div>
<div class=kpi><b>{st['clicks'] or 0}</b>clicked to Google</div><div class=kpi><b>{len(leads)}</b>recent quote requests</div></div>
<section><h2>Send a feedback link to a new customer</h2>
<p>Customer first name <input id=n placeholder="e.g. Maria"> &nbsp; Policy / account ref (optional) <input id=r placeholder="e.g. GL-2026-114"></p>
<p>Link: <code id=link>{SITE_URL}/feedback/</code></p>
<textarea id=msg rows=6 readonly></textarea><p><button id=copy type=button>Copy message</button></p></section>
<section><h2>Customer feedback</h2><p><a href="/api/admin/export.csv?t=feedback">Download CSV</a></p>
<table><tr><th>When</th><th>Rating</th><th>Customer</th><th>Ref</th><th>Google</th><th>Comment</th></tr>{fb_rows}</table></section>
<section><h2>Quote &amp; review requests</h2><p><a href="/api/admin/export.csv?t=leads">Download CSV</a></p>
<table><tr><th>When</th><th>Form</th><th>Name</th><th>Contact</th><th>Industry</th><th>Message</th></tr>{lead_rows}</table></section>
</main><script>
var base={json.dumps(SITE_URL + "/feedback/")};
function upd(){{var n=document.getElementById('n').value.trim(),r=document.getElementById('r').value.trim(),q=[];
if(n)q.push('n='+encodeURIComponent(n));if(r)q.push('ref='+encodeURIComponent(r));var l=base+(q.length?'?'+q.join('&'):'');
document.getElementById('link').textContent=l;
document.getElementById('msg').value='Hi '+(n||'there')+',\\n\\nThank you for choosing Central Insurance Agency for your new policy. We would love to hear how we did. It takes less than a minute:\\n\\n'+l+'\\n\\nThank you,\\nThe Central Insurance Agency team\\n877-242-9600';}}
document.getElementById('n').oninput=upd;document.getElementById('r').oninput=upd;upd();
document.getElementById('copy').onclick=function(){{var t=document.getElementById('msg');t.select();navigator.clipboard?navigator.clipboard.writeText(t.value):document.execCommand('copy');this.textContent='Copied!';}};
</script></body></html>"""
        self.send(200, page, "text/html")


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "8081"))
    print(f"cia-api listening on 127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
