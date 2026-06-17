"""
Tracking & Ad Platform Scanner
-------------------------------
Streamlit app that fetches a domain's public HTML and detects:
  - Google Tag Manager (GTM)
  - Google Analytics 4 (GA4)
  - Consent Management Platform (CMP)
  - Google Consent Mode v2
  - Meta Pixel
  - Google Ads (conversion tag / remarketing)
  - Bonus: TikTok Pixel, LinkedIn Insight Tag, Shopify

Run with:
    pip install -r requirements.txt
    streamlit run tracking_scanner_app.py
"""

import re
import socket
import ssl
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import requests
import subprocess
import sys
import time
import threading
import streamlit as st

# Optional/extra packages (wrapped imports handled below)
try:
    import httpx
except Exception:  # pragma: no cover - optional
    httpx = None
try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - optional
    BeautifulSoup = None
try:
    import dns.resolver
except Exception:  # pragma: no cover - optional
    dns = None
try:
    import whois as whois_lib
except Exception:  # pragma: no cover - optional
    whois_lib = None
try:
    import tldextract
except Exception:  # pragma: no cover - optional
    tldextract = None
try:
    from ipwhois import IPWhois
except Exception:  # pragma: no cover - optional
    IPWhois = None
try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional
    sync_playwright = None

st.set_page_config(page_title="Tracking & Ad Platform Scanner", layout="wide")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

OPTIONAL_NOTES = []
if httpx is None:
    OPTIONAL_NOTES.append("httpx missing: async/http2 features disabled")
if BeautifulSoup is None:
    OPTIONAL_NOTES.append("beautifulsoup4 missing: structured HTML parsing disabled")
if dns is None:
    OPTIONAL_NOTES.append("dnspython missing: DNS lookups disabled")
if whois_lib is None:
    OPTIONAL_NOTES.append("python-whois missing: WHOIS lookups disabled")
if tldextract is None:
    OPTIONAL_NOTES.append("tldextract missing: domain normalization degraded")
if IPWhois is None:
    OPTIONAL_NOTES.append("ipwhois missing: ASN lookup disabled")

# Playwright concurrency controls (updated from UI)
PLAYWRIGHT_ENABLED = False
PLAYWRIGHT_WORKERS = 1
PLAYWRIGHT_RETRIES = 2
PLAYWRIGHT_TIMEOUT = 30
PLAYWRIGHT_SEMAPHORE = threading.BoundedSemaphore(1)

CMP_VENDORS = {
    "Cookiebot": ["cookiebot.com", "consent.cookiebot.com"],
    "OneTrust": ["onetrust.com", "otsdkstub"],
    "Usercentrics": ["usercentrics.eu", "usercentrics.com"],
    "CookieYes": ["cookieyes.com"],
    "Complianz": ["complianz"],
    "Borlabs Cookie": ["borlabs-cookie", "borlabs.io"],
    "Consentmanager": ["consentmanager.net"],
    "Quantcast Choice": ["quantcast.com/choice", "quantcast.mgr.consensu.org"],
    "Didomi": ["didomi.io"],
    "TrustArc": ["trustarc.com"],
    "iubenda": ["iubenda.com"],
    "Klaro": ["klaro.kiprotect.com", "klaro-config"],
    "CookieFirst": ["cookiefirst.com"],
    "Sourcepoint": ["sourcepoint.mgr.consensu.org", "sp-prod"],
    "Termly": ["app.termly.io"],
    "Civic UK Cookie Control": ["civiccomputing.com"],
}


def fetch_html(domain: str, timeout: int = 12):
    """Try https / https-www / http for a bare domain, or use the URL as-is."""
    domain = domain.strip()
    if not domain:
        return None, "Empty domain"

    if domain.startswith("http://") or domain.startswith("https://"):
        urls_to_try = [domain]
    else:
        bare = domain.replace("www.", "")
        urls_to_try = [f"https://{bare}", f"https://www.{bare}", f"http://{bare}"]

    last_err = None
    for url in urls_to_try:
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
            if r.status_code < 400 and r.text:
                return r.text, None
            last_err = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    return None, last_err


def normalize_domain(domain: str) -> str:
    d = domain.strip()
    if d.startswith("http://") or d.startswith("https://"):
        d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0]
    if tldextract:
        e = tldextract.extract(d)
        if e.registered_domain:
            return e.registered_domain
    return d


def resolve_dns(domain: str) -> dict:
    out = {"A": [], "AAAA": [], "MX": [], "TXT": []}
    if dns is None:
        return {"error": "dnspython not installed"}
    try:
        answers = dns.resolver.resolve(domain, "A", lifetime=5)
        out["A"] = [r.to_text() for r in answers]
    except Exception:
        pass
    try:
        answers = dns.resolver.resolve(domain, "AAAA", lifetime=5)
        out["AAAA"] = [r.to_text() for r in answers]
    except Exception:
        pass
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        out["MX"] = [r.to_text() for r in answers]
    except Exception:
        pass
    try:
        answers = dns.resolver.resolve(domain, "TXT", lifetime=5)
        out["TXT"] = [r.to_text().strip('"') for r in answers]
    except Exception:
        pass
    return out


def get_whois(domain: str) -> dict:
    if whois_lib is None:
        return {"error": "whois library not installed"}
    try:
        w = whois_lib.whois(domain)
        return {k: v for k, v in w.items()}
    except Exception as e:
        return {"error": str(e)}


def get_ip_asn(domain: str) -> dict:
    try:
        host = domain
        ips = []
        for res in socket.getaddrinfo(host, None):
            ips.append(res[4][0])
        ips = list(dict.fromkeys(ips))
        info = {"ips": ips}
        if IPWhois and ips:
            try:
                obj = IPWhois(ips[0])
                asn = obj.lookup_rdap(depth=1)
                info["asn"] = asn.get("asn")
                info["asn_country_code"] = asn.get("asn_country_code")
                info["asn_description"] = asn.get("asn_description")
            except Exception:
                info["asn_error"] = "lookup failed"
        return info
    except Exception as e:
        return {"error": str(e)}


def get_tls_info(domain: str) -> dict:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=5) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                cert = ssock.getpeercert()
                return cert
    except Exception as e:
        return {"error": str(e)}


def extract_meta(html: str) -> dict:
    if BeautifulSoup is None:
        return {"error": "beautifulsoup4 not installed"}
    soup = BeautifulSoup(html, "html.parser")
    meta = {}
    for tag in soup.find_all("meta"):
        if tag.get("name"):
            meta[tag.get("name")] = tag.get("content", "")
        if tag.get("property"):
            meta[tag.get("property")] = tag.get("content", "")
    title = soup.title.string if soup.title else ""
    return {"title": title, "meta": meta}


def extract_structured_data(html: str) -> list:
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            out.append(json.loads(s.string))
        except Exception:
            try:
                out.append(json.loads(s.get_text()))
            except Exception:
                out.append({"raw": s.string})
    return out


def find_contacts(html: str) -> dict:
    emails = set(re.findall(r"[\w\.-]+@[\w\.-]+\.[a-zA-Z]{2,}", html))
    phones = set(re.findall(r"\+?[0-9][0-9()\-\s]{6,}[0-9]", html))
    return {"emails": list(emails), "phones": list(phones)}


def detect_technologies(html: str) -> list:
    tech = []
    low = html.lower()
    if "wp-content" in low or "wordpress" in low:
        tech.append("WordPress")
    if "shopify" in low:
        tech.append("Shopify")
    if "woocommerce" in low:
        tech.append("WooCommerce")
    if "react" in low:
        tech.append("React")
    return tech


def check_meta_ad_library(domain: str, token: str = None) -> dict:
    # Requires a Meta Graph API token with ads_read permissions.
    if not token:
        return {"status": "not_configured", "note": "Provide META_ACCESS_TOKEN env var or token"}
    url = "https://graph.facebook.com/v16.0/ads_archive"
    params = {"access_token": token, "search_terms": domain}
    try:
        r = requests.get(url, params=params, timeout=20)
        return {"status": "ok", "data": r.json()}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def check_google_ads_transparency(domain: str) -> dict:
    if sync_playwright is None:
        return {"status": "not_configured", "note": "playwright not installed"}

    # Use semaphore so we don't spawn too many browsers in parallel
    acquired = PLAYWRIGHT_SEMAPHORE.acquire(timeout=PLAYWRIGHT_TIMEOUT)
    if not acquired:
        return {"status": "error", "error": "could not acquire Playwright semaphore"}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            url = "https://transparencyreport.google.com/political-ads/advertiser"
            page.goto(url, timeout=int(PLAYWRIGHT_TIMEOUT * 1000))

            # robust search input detection
            input_selectors = [
                'input[type="search"]',
                'input[aria-label*="Search"]',
                'input[placeholder*="Search"]',
                'input[aria-label*="Advertiser"]',
                'input[name*="q"]',
            ]
            searched = False
            for sel in input_selectors:
                try:
                    el = page.query_selector(sel)
                    if el:
                        el.fill(domain)
                        el.press("Enter")
                        searched = True
                        break
                except Exception:
                    continue

            # try click search button as fallback
            if not searched:
                try:
                    btn = page.query_selector('button[aria-label*="Search"]') or page.query_selector('button[type="submit"]')
                    if btn:
                        btn.click()
                        searched = True
                except Exception:
                    searched = False

            # wait for result cards or for some visible change
            try:
                page.wait_for_selector("div[role='article']", timeout=int(PLAYWRIGHT_TIMEOUT * 1000))
            except Exception:
                # fallback wait a short while
                page.wait_for_timeout(3000)

            content = page.content()
            found = domain.lower() in content.lower()

            snippets = []
            try:
                cards = page.query_selector_all("div[role='article']")
                if not cards:
                    # alternative card selectors
                    cards = page.query_selector_all("div.card, div.result, div.c-card")
                for c in cards[:8]:
                    try:
                        txt = c.inner_text()
                        snippets.append(txt[:2000])
                    except Exception:
                        try:
                            snippets.append(c.text_content()[:2000])
                        except Exception:
                            snippets.append("")
            except Exception:
                pass

            browser.close()
            return {"status": "ok", "found": found, "snippets": snippets, "raw_excerpt": content[:8000]}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        try:
            PLAYWRIGHT_SEMAPHORE.release()
        except Exception:
            pass


def retry_playwright_check(domain: str, attempts: int = 2, delay: int = 2) -> dict:
    last = None
    for i in range(attempts):
        res = check_google_ads_transparency(domain)
        if res.get("status") == "ok" and (res.get("found") or res.get("snippets")):
            return res
        last = res
        time.sleep(delay)
    return last or {"status": "error", "error": "no result"}


def analyze_html(html: str) -> dict:
    text = html
    low = html.lower()
    result = {}

    # --- GTM ---
    gtm_match = re.search(r"GTM-[A-Z0-9]{4,}", text)
    gtm_present = "googletagmanager.com/gtm.js" in low or bool(gtm_match)
    result["GTM"] = "Yes" if gtm_present else "No"
    result["GTM Container ID"] = gtm_match.group(0) if gtm_match else ""

    # --- GA4 ---
    ga4_match = re.search(r"G-[A-Z0-9]{6,10}", text)
    ua_match = re.search(r"UA-\d{4,}-\d+", text)
    if ga4_match:
        result["GA4"] = "Yes"
    elif gtm_present:
        result["GA4"] = "Partial (likely inside GTM, unverified)"
    elif ua_match:
        result["GA4"] = "No (legacy Universal Analytics found)"
    else:
        result["GA4"] = "No"
    result["GA4 Measurement ID"] = ga4_match.group(0) if ga4_match else ""

    # --- CMP ---
    cmp_found = [vendor for vendor, sigs in CMP_VENDORS.items() if any(s in low for s in sigs)]
    result["CMP"] = "Yes" if cmp_found else "No"
    result["CMP Vendor"] = ", ".join(cmp_found)

    # --- Consent Mode ---
    consent_default = re.search(
        r"gtag\(\s*['\"]consent['\"]\s*,\s*['\"]default['\"][^)]*\)", text, re.S
    )
    if consent_default:
        block = consent_default.group(0)
        if "ad_user_data" in block and "ad_personalization" in block:
            result["Consent Mode"] = "Yes (v2)"
        else:
            result["Consent Mode"] = "Partial (v1 only)"
    else:
        result["Consent Mode"] = "No"

    # --- Meta Pixel ---
    fb_match = re.search(r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d+)['\"]", text)
    fb_present = ("connect.facebook.net" in low and "fbevents.js" in low) or bool(fb_match)
    result["Meta Pixel"] = "Yes" if fb_present else "No"
    result["Meta Pixel ID"] = fb_match.group(1) if fb_match else ""

    # --- Google Ads ---
    aw_match = re.search(r"AW-\d{6,}", text)
    gads_present = (
        bool(aw_match)
        or "googleadservices.com" in low
        or "doubleclick.net/pagead" in low
        or "doubleclick.net/ads" in low
    )
    result["Google Ads"] = "Yes" if gads_present else "No"
    result["Google Ads ID"] = aw_match.group(0) if aw_match else ""

    # --- Bonus signals ---
    result["TikTok Pixel"] = "Yes" if ("analytics.tiktok.com" in low or "ttq.load(" in low) else "No"
    result["LinkedIn Insight"] = (
        "Yes" if ("snap.licdn.com" in low or "_linkedin_partner_id" in low) else "No"
    )
    result["Shopify"] = "Yes" if ("cdn.shopify.com" in low or "shopify.shop" in low) else "No"

    # --- Combined "Ad Platforms" field (matches Account Master dropdown) ---
    if gads_present and fb_present:
        result["Ad Platforms"] = "Both"
    elif gads_present:
        result["Ad Platforms"] = "Google"
    elif fb_present:
        result["Ad Platforms"] = "Meta"
    else:
        result["Ad Platforms"] = "None"

    return result


def scan_domain(domain: str) -> dict:
    norm = normalize_domain(domain)
    html, err = fetch_html(domain)
    row = {"Domain": norm}
    if err:
        row["Status"] = "Error"
        row["Error"] = err
        return row
    row["Status"] = "OK"
    row["Error"] = ""

    # existing HTML analysis
    row.update(analyze_html(html))

    # expanded reconnaissance
    try:
        row["DNS"] = resolve_dns(norm)
    except Exception as e:
        row["DNS"] = {"error": str(e)}
    try:
        row["WHOIS"] = get_whois(norm)
    except Exception as e:
        row["WHOIS"] = {"error": str(e)}
    try:
        row["IP_ASN"] = get_ip_asn(norm)
    except Exception as e:
        row["IP_ASN"] = {"error": str(e)}
    try:
        row["TLS"] = get_tls_info(norm)
    except Exception as e:
        row["TLS"] = {"error": str(e)}

    # page parsing
    try:
        row["PageMeta"] = extract_meta(html)
        row["StructuredData"] = extract_structured_data(html)
        row["Contacts"] = find_contacts(html)
        row["Technologies"] = detect_technologies(html)
    except Exception as e:
        row["ParseError"] = str(e)

    # ad/transparency checks (optional tokens)
    meta_token = None
    try:
        meta_token = st.secrets.get("META_ACCESS_TOKEN") if hasattr(st, "secrets") else None
    except Exception:
        meta_token = None
    row["MetaAdLibrary"] = check_meta_ad_library(norm, token=meta_token)
    if PLAYWRIGHT_ENABLED:
        # use retry wrapper which itself calls the semaphore-protected check
        row["GoogleAdsTransparency"] = retry_playwright_check(norm, attempts=int(PLAYWRIGHT_RETRIES), delay=2)
    else:
        row["GoogleAdsTransparency"] = {"status": "disabled"}

    if OPTIONAL_NOTES:
        row["OptionalNotes"] = OPTIONAL_NOTES

    row["ScannedAt"] = datetime.utcnow().isoformat() + "Z"
    return row


# ============================== UI ==============================

st.title("🔍 Tracking & Ad Platform Scanner")
st.caption(
    "Detects GTM, GA4, CMP, Consent Mode v2, Meta Pixel and Google Ads tags directly from a "
    "site's public HTML — the same signals used in the Amplio Account Master."
)

# Sidebar controls for Playwright headless checks
with st.sidebar.expander("Headless checks (Playwright)", expanded=False):
    PLAYWRIGHT_ENABLED = st.checkbox("Enable Google Ads Transparency checks (Playwright)", value=False)
    PLAYWRIGHT_WORKERS = st.slider("Playwright workers", 1, 4, value=1)
    PLAYWRIGHT_RETRIES = st.number_input("Playwright retries", min_value=0, max_value=5, value=2)
    PLAYWRIGHT_TIMEOUT = st.number_input("Playwright timeout (s)", min_value=5, max_value=120, value=30)
    if st.button("Install Playwright browsers"):
        with st.spinner("Installing Playwright browsers (this may take a minute)..."):
            try:
                # run playwright install in the venv python
                subprocess.check_call([sys.executable, "-m", "playwright", "install"], shell=False)
                st.success("Playwright browsers installed")
            except Exception as e:
                st.error(f"Playwright install failed: {e}")

    # update semaphore size based on workers
    try:
        PLAYWRIGHT_SEMAPHORE = threading.BoundedSemaphore(int(PLAYWRIGHT_WORKERS))
    except Exception:
        PLAYWRIGHT_SEMAPHORE = threading.BoundedSemaphore(1)

with st.expander("⚠️ How this works / limitations", expanded=False):
    st.markdown(
        """
This tool fetches a domain's **raw HTML** (no JavaScript execution) — the same thing a browser
sees *before* you click "Accept" on a cookie banner. It reliably catches:

- GTM container snippets, direct `gtag()` calls (GA4 / Google Ads), Meta Pixel `fbq()` calls
- Named CMP vendor scripts (Cookiebot, OneTrust, Usercentrics, etc.)
- `Consent Mode` default calls, including whether the **v2-only** parameters (`ad_user_data`,
  `ad_personalization`) are present

**What it can miss:** tags that GTM injects dynamically *only after* a visitor accepts cookies
(very common for the Meta Pixel) won't appear in the raw HTML. For a 100% live confirmation of
active ad spend, cross-check the **Meta Ad Library** and **Google Ads Transparency Center**.
        """
    )

tab1, tab2 = st.tabs(["Single domain", "Batch scan"])

with tab1:
    domain_input = st.text_input("Domain", placeholder="e.g. mawave.com")
    if st.button("Scan", key="single_scan") and domain_input:
        with st.spinner(f"Scanning {domain_input}..."):
            row = scan_domain(domain_input)
        if row.get("Status") == "Error":
            st.error(f"Could not fetch {domain_input}: {row.get('Error')}")
        else:
            st.success(f"Scanned {domain_input}")
            st.dataframe(pd.DataFrame([row]), use_container_width=True)

with tab2:
    st.write("Paste one domain per line, or upload a CSV/Excel file with a `Domain` / `Website` column.")
    domains_text = st.text_area(
        "Domains (one per line)",
        height=180,
        placeholder="mawave.com\nmore-fire.com\ne-dialog.group",
    )
    uploaded = st.file_uploader("...or upload a file", type=["csv", "xlsx"])

    domains = []
    if domains_text.strip():
        domains = [d.strip() for d in domains_text.splitlines() if d.strip()]
    if uploaded is not None:
        up_df = pd.read_csv(uploaded) if uploaded.name.endswith(".csv") else pd.read_excel(uploaded)
        col = next(
            (c for c in up_df.columns if c.strip().lower() in ("domain", "website", "domains")), None
        )
        if col:
            domains += up_df[col].dropna().astype(str).tolist()
        else:
            st.warning("No 'Domain' / 'Website' column found in the uploaded file.")

    domains = list(dict.fromkeys(domains))  # dedupe, keep order

    max_workers = st.slider("Parallel requests", 1, 10, 5)

    if st.button("Run batch scan", key="batch_scan") and domains:
        progress = st.progress(0)
        status_box = st.empty()
        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(scan_domain, d): d for d in domains}
            done = 0
            for future in as_completed(futures):
                results.append(future.result())
                done += 1
                progress.progress(done / len(domains))
                status_box.text(f"Scanned {done}/{len(domains)}")

        order = {d: i for i, d in enumerate(domains)}
        results.sort(key=lambda r: order[r["Domain"]])
        df = pd.DataFrame(results)

        st.success(f"Done — scanned {len(df)} domains.")
        st.dataframe(df, use_container_width=True)

        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Download results as CSV", csv, "tracking_scan_results.csv", "text/csv")
