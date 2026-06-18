# Domain Tag Scraper — Project Context

## What this project does
A Streamlit app that scrapes one or multiple domains to detect marketing/analytics stack:
- **GTM** — Google Tag Manager presence (yes/no)
- **GA4** — Google Analytics 4 (client-side / server-side / no)
- **CMP** — Consent Management Platform (yes/no + vendor name)
- **Consent Mode** — Google Consent Mode v2 (yes / no / partial)
- **Ad Platforms** — Meta Pixel, Google Ads, or both

## Stack
- **Language**: Python 3.11+
- **UI**: Streamlit
- **Scraping**: Playwright (headless Chromium) — NOT requests/BeautifulSoup
- **Data**: pandas for tabular output, openpyxl for Excel export
- **Env vars**: python-dotenv

## Project structure
```
domain-scraper/
├── CLAUDE.md
├── .env                   # never commit
├── .gitignore
├── requirements.txt
├── app.py                 # Streamlit entry point
├── scraper/
│   ├── __init__.py
│   ├── browser.py         # Playwright setup & page fetch
│   ├── detectors.py       # All signal detection logic
│   ├── recon.py           # DNS / WHOIS / IP-ASN / TLS lookups (no browser)
│   └── models.py          # ScrapeResult dataclass
└── utils/
    ├── __init__.py
    └── export.py          # CSV / Excel export helpers
```

## Key commands
```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Run the app
streamlit run app.py

# Run a quick scrape test (CLI)
python -m scraper.browser https://example.com
```

## Detection logic — never change without updating tests
| Signal | Detection method |
|---|---|
| GTM | Network request to `googletagmanager.com/gtm.js` OR `gtm.js?id=GTM-` in DOM |
| GA4 client-side | `gtag('config', 'G-` call in JS or network hit to `google-analytics.com/g/collect` |
| GA4 server-side | Custom 1st-party endpoint proxying to GA4 (heuristic: path `/collect` on non-google domain) |
| CMP | Script src matching known CMP domains (OneTrust, Cookiebot, Usercentrics, TrustArc, Quantcast, Didomi, iubenda) |
| Consent Mode | Preferred: LIVE state read from the running page via `window.google_tag_data.ics` / dataLayer (`_read_live_consent` + `classify_consent_mode`) — this DOES see config inside the GTM container. Falls back to the static `gtag('consent','default'` regex if the live state is empty. v2 = `ad_user_data`/`ad_personalization` present |
| Meta Pixel | `connect.facebook.net/fbevents.js` / `facebook.com/tr?` network request OR `fbq(` in JS; ID from `fbq('init','…')` |
| Google Ads | `googleadservices.com`, `doubleclick.net/(pagead|ads)`, `AW-…` ID, OR `gtag('config','AW-` in JS |
| IDs | `gtm_id` (GTM-…), `ga4_id` (G-…), `google_ads_id` (AW-…), `meta_pixel_id` extracted alongside the booleans |
| Other tools | `detect_other_tools` matches Matomo, Dreamdata, Apollo.io, TikTok, LinkedIn, Hotjar, Clarity, Segment, HubSpot, Shopify, Plausible |

Detection inputs are **comment-stripped** (`strip_html_comments`) before matching, so commented-out (inactive) tags — a disabled `gtag('consent','default')` block, a second GTM snippet — don't create false positives.

**Recon** (`scraper/recon.py`, `do_recon=True`): DNS (A/AAAA/MX/TXT), WHOIS, IP/ASN, TLS cert — attached to `ScrapeResult.recon`. TXT records yield extra marketing signals (`facebook-domain-verification` ⇒ Meta, `google-site-verification`, HubSpot SPF). Runs via `asyncio.to_thread` (blocking I/O). Optional libs (`dnspython`, `python-whois`, `ipwhois`) degrade gracefully if absent.

## Scraping rules
- Always use Playwright with `headless=True`
- Wait for `networkidle` before extracting signals (captures lazy-loaded tags).
  A `networkidle` timeout is non-fatal — extract from the loaded page anyway, since
  heavy sites never go idle (see `_scrape_single`).
- Intercept network requests AND inspect DOM/JS — both are required
- Default timeout: 15 seconds per domain
- User-agent: realistic Chrome UA string (defined in `browser.py`)
- **Auto-accept consent banners** (default on, `accept_consent=True`) so post-consent
  tags fire and can be detected — most GTM/GA4/Pixel/Ads tags do not load until consent
  is granted. Selectors for known CMPs + a multilingual text fallback live in `browser.py`
  (`CONSENT_ACCEPT_SELECTORS` / `CONSENT_ACCEPT_TEXTS`). `network_urls` accumulates across
  the click, so one pass captures both pre- and post-consent state. `ScrapeResult.consent_clicked`
  records whether a banner was clicked. Pass `accept_consent=False` for a pre-consent-only scan.
- **Simulate user activity** (default on, `simulate_activity=True`) — mouse move / scroll /
  keypress via the input driver (NOT synthetic events) before consent, to trip
  interaction-gated lazy loaders such as WP Rocket's "delay JS until user interaction".
  Without it, those sites never fetch their CMP/analytics scripts in an automated session
  (verified on more-fire.com: 34 requests → 151, Usercentrics loader fires only with activity).
  See `_simulate_user_activity`.
- Never submit real forms (search, login, checkout) — mouse/scroll/keys for lazy-load and the
  consent "Accept all" click are the only permitted interactions.

## Code style
- Type hints everywhere (use `dataclasses` for `ScrapeResult`)
- All detector functions return `bool` or `Literal` types — no raw strings
- Async Playwright (`async_playwright`) for concurrency on batch scraping
- Keep `detectors.py` pure functions: input = page content/requests, output = signal value
- No print statements in library code — use Python `logging`

## Do not
- Do not use `requests` or `httpx` for scraping (JS won't execute)
- Do not hardcode domains in source code
- Do not commit `.env` or any credentials
- Do not block the Streamlit main thread — use `asyncio.run()` or `st.spinner`
