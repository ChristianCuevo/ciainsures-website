#!/usr/bin/env python3
"""Small form + client-satisfaction backend for ciainsures.com (Python standard library only).

Runs on 127.0.0.1:8081 behind nginx (location /api/).

  GET  /api/r?s=1..5&cid=<contact id>&e=<email>&n=<first name>   star link from the HubSpot "How did we do?" email:
                             records the rating, syncs it to HubSpot, redirects to /feedback/
  POST /api/feedback         rating / private comment from the feedback page (identified by token)
  POST /api/feedback/click   client clicked through to Google reviews
  POST /api/contact          quote / policy-review / audit-review / contact requests
  GET  /api/admin            password-protected dashboard (HTTP Basic auth, user "admin")
  GET  /api/admin/export.csv?t=leads|feedback
  GET  /api/health

Configuration (environment, normally /etc/ciainsures/api.env):
  ADMIN_PASSWORD     required for /api/admin
  HUBSPOT_TOKEN      HubSpot private-app token (see hubspot.py for scopes); enables CRM sync
  REVIEW_THRESHOLD   rating at or above which the Google review button is shown (default 5)
  DB_PATH            default /var/lib/ciainsures/data.db
  SITE_URL           default https://ciainsures.com
  SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASS, SMTP_FROM, NOTIFY_TO   optional e-mail alerts
"""
import base64, csv, hmac, html, io, json, os, re, secrets, smtplib, sqlite3, sys, threading, time
from collections import defaultdict, deque
from contextlib import closing
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hubspot  # noqa: E402

DB_PATH = os.environ.get("DB_PATH", "/var/lib/ciainsures/data.db")
SITE_URL = os.environ.get("SITE_URL", "https://ciainsures.com").rstrip("/")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
THRESHOLD = int(os.environ.get("REVIEW_THRESHOLD", "5"))
MAX_BODY = 32 * 1024
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with closing(db()) as c, c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS leads(
          id INTEGER PRIMARY KEY, created TEXT, form_type TEXT, name TEXT, company TEXT, email TEXT,
          phone TEXT, industry TEXT, message TEXT, page TEXT, ip TEXT);
        CREATE TABLE IF NOT EXISTS feedback(
          id INTEGER PRIMARY KEY, created TEXT, updated TEXT, rating INTEGER, name TEXT, contact TEXT,
          comment TEXT, ref TEXT, google_clicked INTEGER DEFAULT 0, ip TEXT, token TEXT,
          email TEXT, hs_contact_id TEXT, hs_deal_id TEXT, hs_status TEXT);
        CREATE INDEX IF NOT EXISTS feedback_token ON feedback(token);
        """)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(feedback)")}
        for col in ("email", "hs_contact_id", "hs_deal_id", "hs_status"):
            if col not in cols:
                c.execute(f"ALTER TABLE feedback ADD COLUMN {col} TEXT")
    if hubspot.enabled():
        try:
            hubspot.ensure_properties()
        except Exception as e:
            print("hubspot: could not ensure properties:", e, flush=True)


# ------------------------------------------------------------------ helpers
_hits = defaultdict(deque)


def rate_limited(ip, limit=30, window=600):
    q, t = _hits[ip], time.time()
    while q and q[0] < t - window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(t)
    return False


def clip(v, n):
    return (str(v or "")).strip()[:n]


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def bg(fn, *a):
    threading.Thread(target=fn, args=a, daemon=True).start()


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
        except Exception as e:  # never break a submission because mail failed
            print("mail error:", e, flush=True)

    bg(run)


def get_row(token):
    if not token:
        return None
    with closing(db()) as c:
        return c.execute("SELECT * FROM feedback WHERE token=?", (token,)).fetchone()


def set_status(fid, status):
    with _db_lock, closing(db()) as c, c:
        c.execute("UPDATE feedback SET hs_status=? WHERE id=?", (status, fid))


# ------------------------------------------------------------------ HubSpot sync (runs in background threads)
def hs_rating(fid):
    """Verify the contact, store rating on the contact, log a note, and open a follow-up task below the threshold."""
    r = get_row_by_id(fid)
    if not r or not r["hs_contact_id"] or not hubspot.enabled():
        return
    try:
        c = hubspot.verify_contact(r["hs_contact_id"], r["email"])
        deal = None
        try:
            deal = hubspot.latest_deal(r["hs_contact_id"])
        except Exception as e:
            print("hubspot: deal lookup failed:", e, flush=True)
        deal_id = deal["id"] if deal else None
        owner = (deal or {}).get("properties", {}).get("hubspot_owner_id") or c["properties"].get("hubspot_owner_id")
        with _db_lock, closing(db()) as cx, cx:
            cx.execute("UPDATE feedback SET hs_deal_id=? WHERE id=?", (deal_id, fid))
        hubspot.update_contact(r["hs_contact_id"], {
            "cia_review_rating": r["rating"], "cia_review_date": time.strftime("%Y-%m-%d")})
        stars = "★" * r["rating"] + "☆" * (5 - r["rating"])
        hubspot.add_note(r["hs_contact_id"], deal_id, f"<p><strong>Client satisfaction: {stars} ({r['rating']}/5)</strong></p>"
                         f"<p>From the post-signing “How did we do?” email.</p>")
        if r["rating"] < THRESHOLD:
            name = " ".join(filter(None, [c["properties"].get("firstname"), c["properties"].get("lastname")])) or r["email"]
            hubspot.add_task(r["hs_contact_id"], deal_id, owner, f"Follow up: {name} rated us {r['rating']}/5",
                             "This client rated their experience below 5 stars after signing. "
                             "Call to understand what happened and how we can help. Any comment they leave will be added as a note.")
            send_mail(f"Client rated us {r['rating']}/5: {name}",
                      f"{name} ({r['email']}) rated their experience {r['rating']}/5.\n"
                      f"A follow-up task was created in HubSpot.\n\nDashboard: {SITE_URL}/api/admin")
        set_status(fid, "synced")
    except hubspot.HubSpotError as e:
        print("hubspot sync failed:", e, flush=True)
        set_status(fid, "unverified" if "mismatch" in str(e) else "error")


def hs_comment(fid):
    r = get_row_by_id(fid)
    if not r or not r["hs_contact_id"] or r["hs_status"] not in ("synced",) or not hubspot.enabled():
        return
    try:
        hubspot.update_contact(r["hs_contact_id"], {"cia_review_comment": r["comment"] or ""})
        body = html.escape(r["comment"] or "").replace("\n", "<br>")
        extra = "".join(f"<br>{html.escape(k)}: {html.escape(v)}" for k, v in (("Name", r["name"]), ("Contact", r["contact"])) if v)
        hubspot.add_note(r["hs_contact_id"], r["hs_deal_id"],
                         f"<p><strong>Client feedback ({r['rating']}/5):</strong></p><p>{body}{extra}</p>")
    except hubspot.HubSpotError as e:
        print("hubspot comment sync failed:", e, flush=True)


def hs_click(fid):
    r = get_row_by_id(fid)
    if r and r["hs_contact_id"] and r["hs_status"] == "synced" and hubspot.enabled():
        try:
            hubspot.update_contact(r["hs_contact_id"], {"cia_review_google_clicked": "true"})
        except hubspot.HubSpotError as e:
            print("hubspot click sync failed:", e, flush=True)


def get_row_by_id(fid):
    with closing(db()) as c:
        return c.execute("SELECT * FROM feedback WHERE id=?", (fid,)).fetchone()


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

    def redirect(self, url):
        self.send(303, "", "text/plain", {"Location": url})

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
            return self.json(200, {"ok": True, "hubspot": hubspot.enabled()})
        if u.path == "/api/r":
            if rate_limited(self.client_ip()):
                return self.redirect("/feedback/")
            return self.rate_link({k: v[0] for k, v in parse_qs(u.query).items()})
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

    # --- star link from the email
    def rate_link(self, q):
        try:
            rating = int(q.get("s", "0"))
        except ValueError:
            rating = 0
        cid = re.sub(r"\D", "", q.get("cid", ""))[:20]
        email = clip(q.get("e"), 160).lower()
        name = clip(q.get("n"), 60)
        if not 1 <= rating <= 5:
            return self.redirect("/feedback/")
        with _db_lock, closing(db()) as c, c:
            # one response per client per week: clicking a different star updates the same record
            row = None
            if cid:
                row = c.execute("SELECT * FROM feedback WHERE hs_contact_id=? AND created>=datetime('now','-7 days','localtime') "
                                "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
            changed = True
            if row:
                changed = row["rating"] != rating or row["hs_status"] == "error"
                c.execute("UPDATE feedback SET rating=?, updated=? WHERE id=?", (rating, now(), row["id"]))
                fid, token = row["id"], row["token"]
            else:
                token = secrets.token_urlsafe(16)
                cur = c.execute("INSERT INTO feedback(created,updated,rating,name,ref,ip,token,email,hs_contact_id,hs_status) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (now(), now(), rating, name, "email", self.client_ip(), token, email, cid or None,
                                 "pending" if cid else "no-contact"))
                fid = cur.lastrowid
        if cid and changed:  # re-clicking the same star doesn't create duplicate notes/tasks
            bg(hs_rating, fid)
        params = {"s": rating, "t": token}
        if name:
            params["n"] = name
        self.redirect("/feedback/?" + urlencode(params))

    # --- feedback page
    def feedback(self, d):
        row = get_row(clip(d.get("token"), 64))
        try:
            rating = int(d.get("rating") or (row["rating"] if row else 0))
        except ValueError:
            rating = 0
        if not 1 <= rating <= 5:
            return self.json(400, {"ok": False, "error": "Please choose a rating."})
        name, contact, comment = clip(d.get("name"), 120), clip(d.get("contact"), 160), clip(d.get("comment"), 3000)
        with _db_lock, closing(db()) as c, c:
            if row:
                c.execute("UPDATE feedback SET updated=?, rating=?, name=COALESCE(NULLIF(?,''),name), "
                          "contact=COALESCE(NULLIF(?,''),contact), comment=COALESCE(NULLIF(?,''),comment) WHERE id=?",
                          (now(), rating, name, contact, comment, row["id"]))
                fid, token = row["id"], row["token"]
            else:
                token = secrets.token_urlsafe(16)
                cur = c.execute("INSERT INTO feedback(created,updated,rating,name,contact,comment,ref,ip,token,hs_status) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?)", (now(), now(), rating, name, contact, comment,
                                                                clip(d.get("ref"), 80), self.client_ip(), token, "no-contact"))
                fid = cur.lastrowid
        if comment:
            bg(hs_comment, fid)
            send_mail(f"Client feedback: {rating}/5 from {name or (row['email'] if row else '') or 'a client'}",
                      f"Rating: {rating}/5\nName: {name}\nContact: {contact}\n\n{comment}\n\nDashboard: {SITE_URL}/api/admin")
        self.json(200, {"ok": True, "token": token, "show_review": rating >= THRESHOLD})

    def click(self, d):
        row = get_row(clip(d.get("token"), 64))
        if row:
            with _db_lock, closing(db()) as c, c:
                c.execute("UPDATE feedback SET google_clicked=1 WHERE id=?", (row["id"],))
            bg(hs_click, row["id"])
        self.json(200, {"ok": True})

    # --- contact / quote forms
    def contact(self, d):
        if d.get("website"):  # honeypot: silently accept bots
            return self.json(200, {"ok": True})
        name, email = clip(d.get("name"), 120), clip(d.get("email"), 160)
        if not name or not EMAIL_RE.match(email):
            return self.json(400, {"ok": False, "error": "Please enter your name and a valid email address."})
        row = (now(), clip(d.get("form_type"), 40) or "contact", name, clip(d.get("company"), 160), email,
               clip(d.get("phone"), 40), clip(d.get("industry"), 80), clip(d.get("message"), 4000),
               clip(d.get("page"), 200), self.client_ip())
        with _db_lock, closing(db()) as c, c:
            c.execute("INSERT INTO leads(created,form_type,name,company,email,phone,industry,message,page,ip) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)", row)
        send_mail(f"New website request ({row[1]}): {name}",
                  "\n".join(f"{k}: {v}" for k, v in zip(
                      ["When", "Form", "Name", "Company", "Email", "Phone", "Industry", "Message", "Page"], row[:9])))
        if self.wants_json():
            return self.json(200, {"ok": True})
        self.redirect("/thank-you/")

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
        with closing(db()) as c:
            rows = c.execute(f"SELECT * FROM {table} ORDER BY id DESC").fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        if rows:
            w.writerow([k for k in rows[0].keys() if k not in ("token", "ip")])
            w.writerows([[r[k] for k in r.keys() if k not in ("token", "ip")] for r in rows])
        self.send(200, buf.getvalue(), "text/csv", {"Content-Disposition": f'attachment; filename="{table}.csv"'})

    def admin(self):
        e = html.escape
        with closing(db()) as c:
            leads = c.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 200").fetchall()
            fb = c.execute("SELECT * FROM feedback ORDER BY id DESC LIMIT 300").fetchall()
            st = c.execute("SELECT COUNT(*) n, AVG(rating) avg, SUM(rating>=?) happy, SUM(rating<?) unhappy, "
                           "SUM(google_clicked) clicks FROM feedback", (THRESHOLD, THRESHOLD)).fetchone()
        stars = lambda n: "★" * n + "☆" * (5 - n)
        hs_label = {"synced": "✔ in HubSpot", "pending": "syncing…", "unverified": "⚠ email/ID mismatch",
                    "error": "⚠ sync error", "no-contact": "–"}
        lead_rows = "".join(
            f"<tr><td>{e(r['created'])}</td><td>{e(r['form_type'])}</td><td>{e(r['name'])}<br><small>{e(r['company'])}</small></td>"
            f"<td><a href='mailto:{e(r['email'])}'>{e(r['email'])}</a><br>{e(r['phone'])}</td><td>{e(r['industry'])}</td>"
            f"<td class=msg>{e(r['message'])}</td></tr>" for r in leads) or "<tr><td colspan=6>No requests yet.</td></tr>"
        fb_rows = "".join(
            f"<tr class='{'bad' if r['rating'] < THRESHOLD else ''}'><td>{e(r['created'])}</td><td class=st>{stars(r['rating'])}</td>"
            f"<td>{e(r['name'] or '')}<br><small>{e(r['email'] or r['contact'] or '')}</small></td>"
            f"<td>{e(hs_label.get(r['hs_status'] or '', r['hs_status'] or ''))}</td>"
            f"<td>{'✔' if r['google_clicked'] else ''}</td><td class=msg>{e(r['comment'] or '')}</td></tr>" for r in fb
        ) or "<tr><td colspan=6>No ratings yet. They appear here when clients click a star in the HubSpot email.</td></tr>"
        avg = f"{st['avg']:.2f}" if st["avg"] else "–"
        hs_state = "connected" if hubspot.enabled() else "not connected (add HUBSPOT_TOKEN)"
        page = f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content=noindex><title>CIA Admin</title><style>
body{{font:15px/1.5 system-ui,sans-serif;margin:0;background:#f3f7f9;color:#14232b}}
header{{background:#00212d;color:#fff;padding:16px 24px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}}h1{{margin:0;font-size:20px}}
main{{padding:24px;max-width:1300px;margin:auto}}.kpis{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}}
.kpi{{background:#fff;border-radius:12px;padding:14px 18px;min-width:150px}}.kpi b{{display:block;font-size:24px;color:#015275}}
section{{background:#fff;border-radius:12px;padding:18px;margin-bottom:24px;overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border-bottom:1px solid #e3eaee;padding:8px;text-align:left;vertical-align:top}}
.msg{{max-width:420px;white-space:pre-wrap}}.st{{color:#d9a600;white-space:nowrap}}tr.bad{{background:#fff4f2}}
</style></head><body><header><h1>Central Insurance Agency · Client satisfaction</h1><span>HubSpot: {e(hs_state)} · Google button at {THRESHOLD}★</span></header><main>
<div class=kpis><div class=kpi><b>{st['n'] or 0}</b>ratings</div><div class=kpi><b>{avg}</b>average</div>
<div class=kpi><b>{st['happy'] or 0}</b>asked for a Google review</div><div class=kpi><b>{st['unhappy'] or 0}</b>need follow-up</div>
<div class=kpi><b>{st['clicks'] or 0}</b>clicked to Google</div><div class=kpi><b>{len(leads)}</b>website requests</div></div>
<section><h2>Client ratings</h2><p><a href="/api/admin/export.csv?t=feedback">Download CSV</a></p>
<table><tr><th>When</th><th>Rating</th><th>Client</th><th>HubSpot</th><th>Google</th><th>Private comment</th></tr>{fb_rows}</table></section>
<section><h2>Website quote &amp; review requests</h2><p><a href="/api/admin/export.csv?t=leads">Download CSV</a></p>
<table><tr><th>When</th><th>Form</th><th>Name</th><th>Contact</th><th>Industry</th><th>Message</th></tr>{lead_rows}</table></section>
</main></body></html>"""
        self.send(200, page, "text/html")


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "8081"))
    print(f"cia-api listening on 127.0.0.1:{port} (hubspot sync: {hubspot.enabled()}, threshold {THRESHOLD})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
