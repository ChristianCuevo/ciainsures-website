# ciainsures.com: website source

The Central Insurance Agency website, rebuilt from the WordPress.com site as a fast static site, plus a small backend for quote requests and customer feedback.

## Publishing changes (automatic deploy)

1. Edit files in this folder (see "Where things live").
2. Double-click **`publish.cmd`**, or run `git add -A && git commit -m "..." && git push production main`.
3. The server rebuilds the site and switches it live in about 30 seconds. If the build fails, the live site is left unchanged and the error is shown in the publish window.

## Where things live

| What | Where |
|---|---|
| Phone, address, email, socials, menu, testimonials, **Google review link** | `config.json` |
| Page content (one file per page, same URL as before) | `content/pages/…html` |
| Blog posts (CIA Insider) | `content/posts/<slug>.html` |
| Category names and descriptions | `data/categories.json` |
| Home page FAQ (also published as FAQ structured data) | `data/home_faq.json` |
| Layout / design | `templates/`, `static/assets/site.css` |
| Images | `static/wp-content/uploads/…` (same paths as the old site) |
| Old URL → new URL redirects | `deploy/redirects.map` |

Each content file starts with a settings block:

```
<!--meta
{ "url": "/contact/", "seo_title": "Title shown in Google", "title": "Page heading",
  "description": "Meta description (~150 characters)", "image": "/wp-content/uploads/....png", ... }
-->
<p>Page HTML…</p>
```

To add a blog post, copy an existing file in `content/posts/`, change `url`, `title`, `seo_title`, `description`, `published`, `modified` and `categories` (category slugs from `data/categories.json`), then replace the HTML.

## Customer feedback → Google reviews

* Send new customers: `https://ciainsures.com/feedback/?n=FirstName&ref=PolicyNumber`
  (the admin page builds this link and a ready-to-send message for you).
* 4–5 stars → the customer is invited to leave a Google review (big button).
* 1–3 stars → a private "what could we do better?" form goes to your team. A small Google link remains visible,
  because Google's review policy and the FTC's 2024 rule on consumer reviews prohibit hiding the review option
  from unhappy customers ("review gating").
* Set the real review link in `config.json` → `google_review_url` (Google Business Profile → "Ask for reviews").
  Until then the button opens your Google Maps listing.

**Admin dashboard:** `https://ciainsures.com/api/admin` (user `admin`). It shows ratings, comments, Google click-throughs and quote requests, with CSV export.
Password and optional e-mail alerts are set in `/etc/ciainsures/api.env` on the server (`systemctl restart ciainsures-api` after editing).

## SEO / AEO features

* Every original URL is preserved; legacy URLs 301-redirect (`deploy/redirects.map`).
* Schema.org JSON-LD on every page: InsuranceAgency/Organization, WebSite, WebPage, BreadcrumbList, Service (coverage pages),
  BlogPosting (posts), CollectionPage (blog/categories), FAQPage (home and any page with question headings), VideoObject.
* `sitemap.xml` with last-modified dates, `robots.txt` that explicitly welcomes AI answer engines,
  `llms.txt` (plain-language site summary for AI assistants), RSS at `/feed/`.
* Canonical URLs, Open Graph/Twitter tags, a single H1 per page, WebP images with width/height, lazy loading, no render-blocking JS.
* The bare IP and non-canonical hostnames send `X-Robots-Tag: noindex` so they can't be indexed as duplicates.

## Server layout (Ubuntu 24.04)

* `/srv/ciainsures/repo.git`: bare repo; `hooks/post-receive` builds and deploys
* `/srv/ciainsures/releases/<timestamp>`: last 5 builds; `/srv/ciainsures/current` → live release (roll back by re-pointing this symlink)
* nginx site: `/etc/nginx/sites-available/ciainsures`; API: `ciainsures-api.service` (user `ciaapi`, SQLite at `/var/lib/ciainsures/data.db`)
* Automatic Ubuntu security updates are enabled.

## Going live on ciainsures.com (when ready)

The domain currently points to WordPress.com. To switch:
1. In your DNS provider, change the `A` records for `ciainsures.com` and `www` to `192.3.180.166` (leave the MX/Microsoft 365 records alone).
2. Once DNS has updated, on the server run:
   `certbot --nginx -d ciainsures.com -d www.ciainsures.com --redirect -m contactus@ciainsures.com --agree-tos -n`
3. Submit `https://ciainsures.com/sitemap.xml` in Google Search Console and Bing Webmaster Tools.
