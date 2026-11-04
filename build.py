#!/usr/bin/env python3
"""Static site generator for ciainsures.com.

Usage:  python build.py [--out dist] [--base-url https://ciainsures.com]

Reads config.json, data/categories.json, content/pages/**.html, content/posts/*.html,
templates/*.html and static/**, and writes a complete static site to --out.
Each content file starts with a JSON front-matter block:  <!--meta {...} -->
"""
import argparse, hashlib, html, json, math, re, shutil, sys
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader, select_autoescape

try:
    from PIL import Image
except ImportError:  # image sizes are optional
    Image = None

ROOT = Path(__file__).resolve().parent
META_RE = re.compile(r"\A\s*<!--meta\s*(\{.*?\})\s*-->\s*", re.S)


# ---------------------------------------------------------------- loading
def load_content(path: Path):
    text = path.read_text(encoding="utf-8")
    m = META_RE.match(text)
    if not m:
        sys.exit(f"Missing <!--meta {{...}} --> header in {path}")
    meta = json.loads(m.group(1))
    meta["body"] = text[m.end():].strip()
    meta["source"] = str(path.relative_to(ROOT))
    return meta


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------- html helpers
_img_size_cache = {}


def optimize_images(markup: str, static_dir: Path) -> str:
    """Swap local PNG/JPG for pre-generated .webp siblings and add width/height when missing."""

    def repl(m):
        tag = m.group(0)
        src_m = re.search(r'src="(/wp-content/uploads/[^"]+)"', tag)
        if not src_m:
            return tag
        src = src_m.group(1)
        f = static_dir / src.lstrip("/")
        if f.suffix.lower() in (".png", ".jpg", ".jpeg") and Path(str(f) + ".webp").exists():
            tag = tag.replace(f'src="{src}"', f'src="{src}.webp"')
        if " width=" not in tag and Image is not None and f.exists():
            if src not in _img_size_cache:
                try:
                    with Image.open(f) as im:
                        _img_size_cache[src] = im.size
                except Exception:
                    _img_size_cache[src] = None
            size = _img_size_cache[src]
            if size:
                tag = tag.replace("<img ", f'<img width="{size[0]}" height="{size[1]}" ', 1)
        return tag

    return re.sub(r"<img\b[^>]*>", repl, markup)


def text_of(markup: str) -> str:
    return BeautifulSoup(markup, "lxml").get_text(" ", strip=True)


def extract_faqs(markup: str):
    """Find Q&A pairs: <details><summary> blocks and headings phrased as questions."""
    soup = BeautifulSoup(markup, "lxml")
    faqs = []
    for d in soup.find_all("details"):
        s = d.find("summary")
        if not s:
            continue
        q = s.get_text(" ", strip=True)
        s.extract()
        a = d.get_text(" ", strip=True)
        if q and a:
            faqs.append((q, a))
    for h in soup.find_all(["h2", "h3", "h4"]):
        q = h.get_text(" ", strip=True)
        if not q.endswith("?") or len(q) > 160:
            continue
        parts = []
        for sib in h.find_next_siblings():
            if sib.name in ("h1", "h2", "h3", "h4") or (sib.find(["h2", "h3", "h4"]) is not None):
                break
            t = sib.get_text(" ", strip=True)
            if t:
                parts.append(t)
            if sum(len(p) for p in parts) > 700:
                break
        a = " ".join(parts).strip()
        if len(a) > 40:
            faqs.append((q, a[:1000]))
    seen, out = set(), []
    for q, a in faqs:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append({"q": q, "a": a})
    return out if len(out) >= 2 else []


def reading_minutes(markup: str) -> int:
    return max(1, math.ceil(len(text_of(markup).split()) / 230))


def summary(markup: str, n=200):
    t = text_of(markup)
    return (t[: n].rsplit(" ", 1)[0] + "…") if len(t) > n else t


# ---------------------------------------------------------------- schema.org
def org_schema(cfg):
    base = cfg["site_url"]
    a = cfg["address"]
    org = {
        "@type": ["InsuranceAgency", "Organization"],
        "@id": f"{base}/#organization",
        "name": cfg["name"],
        "alternateName": [cfg["short_name"], "CIA West Insurance Services"],
        "url": base + "/",
        "logo": base + cfg["logo"],
        "image": base + cfg["default_image"],
        "telephone": "+1-" + cfg["phone"],
        "faxNumber": "+1-" + cfg["fax"],
        "email": cfg["email"],
        "address": {"@type": "PostalAddress", "streetAddress": a["street"], "addressLocality": a["city"],
                    "addressRegion": a["region"], "postalCode": a["postal"], "addressCountry": a["country"]},
        "areaServed": {"@type": "Country", "name": "United States"},
        "sameAs": list(cfg["social"].values()),
        "knowsAbout": ["Security guard insurance", "Alarm and security systems insurance", "Fire suppression contractor insurance",
                       "General contractor insurance", "Specialty trade contractor insurance", "Janitorial insurance",
                       "Home healthcare agency insurance", "Workers' compensation", "General liability",
                       "Insurance audit counseling", "Commercial insurance policy review"],
        "priceRange": "$$",
    }
    if cfg.get("hours"):
        org["openingHours"] = cfg["hours"]
    website = {"@type": "WebSite", "@id": f"{base}/#website", "url": base + "/", "name": cfg["name"],
               "publisher": {"@id": f"{base}/#organization"}, "inLanguage": "en-US"}
    return [org, website]


def breadcrumb_schema(base, crumbs):
    return {"@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": c["label"], "item": base + c["url"]}
        for i, c in enumerate(crumbs)]}


def faq_schema(faqs):
    return {"@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": f["q"], "acceptedAnswer": {"@type": "Answer", "text": f["a"]}} for f in faqs]}


def ld_json(graph):
    s = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/")


# ---------------------------------------------------------------- build
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--base-path", default="", help="serve from a sub-folder, e.g. /ciainsures-website (GitHub Pages preview)")
    ap.add_argument("--noindex", action="store_true", help="preview build: tell search engines not to index it")
    args = ap.parse_args()
    base_path = args.base_path.rstrip("/")

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    if args.base_url:
        cfg["site_url"] = args.base_url.rstrip("/")
    base = cfg["site_url"]
    out = Path(args.out)
    static = ROOT / "static"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(static, out)

    # asset fingerprint for cache busting
    h = hashlib.sha1()
    for f in sorted((static / "assets").rglob("*")):
        if f.is_file():
            h.update(f.read_bytes())
    asset_v = h.hexdigest()[:10]

    categories = json.loads((ROOT / "data" / "categories.json").read_text(encoding="utf-8"))
    cat_by_slug = {c["slug"]: c for c in categories}

    pages = {}
    for f in sorted((ROOT / "content" / "pages").rglob("*.html")):
        p = load_content(f)
        pages[p["url"]] = p
    posts = [load_content(f) for f in sorted((ROOT / "content" / "posts").glob("*.html"))]
    for p in posts:
        p["date"] = parse_date(p.get("published"))
        p["cats"] = [cat_by_slug[s] for s in p.get("categories", []) if s in cat_by_slug]
        p["minutes"] = reading_minutes(p["body"])
        p["excerpt"] = p.get("description") or summary(p["body"])
    posts.sort(key=lambda p: p["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for c in categories:
        c["url"] = f"/category/{c['slug']}/"
        c["posts"] = [p for p in posts if c in p["cats"]]

    env = Environment(loader=FileSystemLoader(str(ROOT / "templates")), autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters["date"] = lambda d, fmt="%B %-d, %Y": d.strftime(fmt.replace("%-d", str(d.day))) if d else ""
    env.filters["img"] = lambda src: (src + ".webp") if src and (static / (src.lstrip("/") + ".webp")).exists() else src

    page_title = {u: p["title"] for u, p in pages.items()}
    sitemap = []

    def write(url, markup):
        dest = out / url.strip("/") / "index.html" if url != "/" else out / "index.html"
        if url.endswith(".html"):
            dest = out / url.lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        markup = optimize_images(markup, static)
        if base_path:  # prefix site-relative links so the site works from a sub-folder
            markup = re.sub(r'\b(href|src|poster|action)="/(?!/)', rf'\1="{base_path}/', markup)
        dest.write_text(markup, encoding="utf-8")

    def crumbs_for(url, title):
        cr = [{"label": "Home", "url": "/"}]
        parts = [x for x in url.strip("/").split("/") if x]
        acc = ""
        for part in parts[:-1]:
            acc += "/" + part
            u = acc + "/"
            if u in page_title:
                cr.append({"label": page_title[u], "url": u})
        cr.append({"label": title, "url": url})
        return cr

    # fonts: the preview loads every look's fonts so the switcher works; the live site loads only the chosen look's
    looks = cfg.get("looks", [])
    chosen = [l for l in looks if l["id"] == cfg.get("look")] or looks[:1]
    families = []
    for l in (looks if args.noindex else chosen):
        for f in l["fonts"]:
            if f not in families:
                families.append(f)
    fonts_url = ("https://fonts.googleapis.com/css2?" + "&".join("family=" + f for f in families) + "&display=swap") if families else ""

    common = dict(cfg=cfg, base=base, asset_v=asset_v, year=datetime.now().year, nav=cfg["nav"],
                  categories=categories, latest=posts[:3], force_noindex=args.noindex,
                  preview=args.noindex, looks=looks, fonts_url=fonts_url)

    def render(tpl, **ctx):
        return env.get_template(tpl).render(**common, **ctx)

    # ---- pages
    for url, p in pages.items():
        crumbs = crumbs_for(url, p["title"])
        faqs = extract_faqs(p["body"])
        graph = org_schema(cfg) + [
            {"@type": "WebPage", "@id": base + url + "#webpage", "url": base + url, "name": p["seo_title"],
             "description": p.get("description", ""), "isPartOf": {"@id": base + "/#website"},
             "about": {"@id": base + "/#organization"}, "inLanguage": "en-US",
             **({"dateModified": p["modified"]} if p.get("modified") else {}),
             **({"primaryImageOfPage": base + p["image"]} if p.get("image") else {})}]
        if url != "/":
            graph.append(breadcrumb_schema(base, crumbs))
        if any(url.startswith(x) for x in ("/commercial-insurance-programs/", "/industry-insurance-specialties/",
                                           "/employee-benefits/", "/private-investigator-insurance/",
                                           "/insurance-audit-counseling/", "/request-a-policy-review/",
                                           "/cleaning-company-insurance/", "/personal-insurance/")):
            graph.append({"@type": "Service", "name": p["title"], "serviceType": p["title"],
                          "description": p.get("description", ""), "url": base + url,
                          "provider": {"@id": base + "/#organization"},
                          "areaServed": {"@type": "Country", "name": "United States"}})
        if url == "/":
            home_faqs = json.loads((ROOT / "data" / "home_faq.json").read_text(encoding="utf-8"))
            graph.append(faq_schema(home_faqs))
            graph.append({"@type": "VideoObject", "name": "Central Insurance Agency overview",
                          "description": cfg["tagline"],
                          "thumbnailUrl": base + "/wp-content/uploads/2023/07/CIA_Video_Screen_Contract_HiRes.jpg",
                          "uploadDate": "2023-08-24T14:50:46-04:00",
                          "contentUrl": "https://videos.files.wordpress.com/if25ejAm/cia-overview-video.mp4.mp4"})
            by_url = {**{q["url"]: q for q in posts}, **pages}  # some menu items (e.g. Private Investigator) are posts
            cards = {c["url"]: by_url.get(c["url"], {}) for item in cfg["nav"] for c in item.get("children", [])}
            markup = render("home.html", page=p, cards=cards, faqs=home_faqs, ld=ld_json(graph),
                            canonical=base + "/")
        else:
            if faqs:
                graph.append(faq_schema(faqs))
            markup = render("page.html", page=p, crumbs=crumbs, form=cfg["forms"].get(url),
                            ld=ld_json(graph), canonical=base + url)
        write(url, markup)
        sitemap.append((url, p.get("modified"), "1.0" if url == "/" else "0.8"))

    # ---- posts
    for p in posts:
        url = p["url"]
        crumbs = [{"label": "Home", "url": "/"}, {"label": "CIA Insider", "url": "/insurance-news-insights/"}]
        if p["cats"]:
            crumbs.append({"label": p["cats"][0]["name"], "url": p["cats"][0]["url"]})
        crumbs.append({"label": p["title"], "url": url})
        faqs = extract_faqs(p["body"])
        img = base + (p.get("image") or cfg["default_image"])
        graph = org_schema(cfg) + [
            {"@type": "BlogPosting", "@id": base + url + "#article", "headline": p["title"][:110],
             "description": p.get("description", ""), "image": img, "url": base + url,
             "mainEntityOfPage": base + url, "inLanguage": "en-US",
             "datePublished": p.get("published"), "dateModified": p.get("modified") or p.get("published"),
             "author": {"@id": base + "/#organization"}, "publisher": {"@id": base + "/#organization"},
             "articleSection": [c["name"] for c in p["cats"]], "wordCount": len(text_of(p["body"]).split())},
            breadcrumb_schema(base, crumbs)]
        if faqs:
            graph.append(faq_schema(faqs))
        related = [q for q in posts if q is not p and p["cats"] and p["cats"][0] in q["cats"]][:3]
        write(url, render("post.html", page=p, crumbs=crumbs, related=related, ld=ld_json(graph),
                          canonical=base + url))
        sitemap.append((url, p.get("modified"), "0.6"))

    # ---- blog index & categories
    def listing(url, title, seo_title, description, items, intro_html="", cat=None):
        crumbs = [{"label": "Home", "url": "/"}, {"label": "CIA Insider", "url": "/insurance-news-insights/"}]
        if cat:
            crumbs.append({"label": cat["name"], "url": url})
        graph = org_schema(cfg) + [
            {"@type": "CollectionPage", "@id": base + url, "url": base + url, "name": seo_title,
             "description": description, "isPartOf": {"@id": base + "/#website"},
             "mainEntity": {"@type": "ItemList", "itemListElement": [
                 {"@type": "ListItem", "position": i + 1, "url": base + q["url"]} for i, q in enumerate(items[:50])]}},
            breadcrumb_schema(base, crumbs)]
        write(url, render("listing.html", page={"title": title, "seo_title": seo_title, "description": description,
                                                "url": url}, items=items, intro=intro_html, cat=cat, crumbs=crumbs,
                          ld=ld_json(graph), canonical=base + url))
        newest = items[0].get("modified") if items else None
        sitemap.append((url, newest, "0.7"))

    listing("/insurance-news-insights/", "CIA Insider: Insurance News & Insights",
            "CIA Insider | Commercial Insurance News & Risk Insights | Central Insurance Agency",
            "Insurance news, lawsuits, risk-management tips and state coverage guides for security, alarm, fire "
            "suppression, contractor, janitorial and home healthcare businesses.", posts)
    for c in categories:
        if c["posts"]:
            listing(c["url"], c["name"], c.get("seo_title") or f"{c['name']} | CIA Insider",
                    c.get("summary") or f"{c['name']} insurance insights from Central Insurance Agency.",
                    c["posts"], c.get("description", ""), cat=c)

    # ---- utility pages (not indexed)
    write("/feedback/", render("feedback.html", page={"title": "How did we do?", "seo_title": "How Did We Do? | Central Insurance Agency",
                                                      "description": "Tell us about your experience with Central Insurance Agency.", "url": "/feedback/"},
                               noindex=True, canonical=base + "/feedback/", ld=""))
    write("/thank-you/", render("thanks.html", page={"title": "Thank you", "seo_title": "Thank You | Central Insurance Agency",
                                                     "description": "", "url": "/thank-you/"},
                                noindex=True, canonical=base + "/thank-you/", ld=""))
    write("/404.html", render("404.html", page={"title": "Page not found", "seo_title": "Page Not Found | Central Insurance Agency",
                                                "description": "", "url": "/404"}, noindex=True, canonical="", ld=""))

    # ---- sitemap.xml
    rows = []
    for url, mod, prio in sitemap:
        d = parse_date(mod)
        lm = f"<lastmod>{d.date().isoformat()}</lastmod>" if d else ""
        rows.append(f"<url><loc>{xml_escape(base + url)}</loc>{lm}<priority>{prio}</priority></url>")
    (out / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                                     '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                                     + "\n".join(rows) + "\n</urlset>\n", encoding="utf-8")

    # ---- robots.txt (search + AI answer engines explicitly welcome; preview builds block everything)
    if args.noindex:
        (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    else:
        (out / "robots.txt").write_text(
        "User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /feedback/\nDisallow: /thank-you/\n\n"
        "# AI answer engines are welcome to read and cite our public content\n"
        + "".join(f"User-agent: {b}\nAllow: /\nDisallow: /api/\nDisallow: /feedback/\n\n"
                  for b in ("GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot", "PerplexityBot",
                            "Google-Extended", "Applebot-Extended", "Bingbot"))
        + f"Sitemap: {base}/sitemap.xml\n", encoding="utf-8")

    # ---- llms.txt (answer-engine summary of the site)
    lines = [f"# {cfg['name']}", "", f"> {cfg['name']} ({cfg['short_name']}) is a nationwide commercial insurance broker "
             f"based at {cfg['address']['street']}, {cfg['address']['city']}, {cfg['address']['region']} "
             f"{cfg['address']['postal']}. It specializes in insurance for security guard companies, alarm and security "
             "system installers, fire suppression contractors, general and specialty trade contractors, janitorial "
             f"companies and home healthcare agencies. Phone {cfg['phone']}, email {cfg['email']}.", "",
             cfg["license_note"], ""]
    for item in cfg["nav"]:
        lines.append(f"## {item['label']}")
        for c in item.get("children") or [item]:
            p = pages.get(c["url"])
            desc = (p or {}).get("description", "")
            lines.append(f"- [{c['label']}]({base}{c['url']})" + (f": {desc}" if desc else ""))
        lines.append("")
    lines.append("## Insurance guides and news")
    for p in posts[:40]:
        lines.append(f"- [{p['title']}]({base}{p['url']})" + (f": {p['description']}" if p.get("description") else ""))
    lines += ["", "## Contact", f"- [Contact us]({base}/contact/)", f"- Phone: {cfg['phone']}", f"- Email: {cfg['email']}", ""]
    (out / "llms.txt").write_text("\n".join(lines), encoding="utf-8")

    # ---- RSS feed (served at /feed/ by nginx)
    items = []
    for p in posts[:30]:
        d = p["date"]
        items.append("<item><title>{}</title><link>{}</link><guid>{}</guid>{}<description>{}</description></item>".format(
            xml_escape(p["title"]), xml_escape(base + p["url"]), xml_escape(base + p["url"]),
            f"<pubDate>{d.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>" if d else "", xml_escape(p["excerpt"])))
    (out / "feed.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
        f"<title>{xml_escape(cfg['name'])} – CIA Insider</title><link>{base}/insurance-news-insights/</link>"
        f"<description>{xml_escape(cfg['tagline'])}</description><language>en-us</language>"
        + "".join(items) + "</channel></rss>\n", encoding="utf-8")

    print(f"Built {len(pages)} pages, {len(posts)} posts, {sum(1 for c in categories if c['posts'])} categories -> {out}")


if __name__ == "__main__":
    main()
