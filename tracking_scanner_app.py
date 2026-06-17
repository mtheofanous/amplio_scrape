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
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Tracking & Ad Platform Scanner", layout="wide")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

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
    html, err = fetch_html(domain)
    row = {"Domain": domain}
    if err:
        row["Status"] = "Error"
        row["Error"] = err
        return row
    row["Status"] = "OK"
    row["Error"] = ""
    row.update(analyze_html(html))
    return row


# ============================== UI ==============================

st.title("🔍 Tracking & Ad Platform Scanner")
st.caption(
    "Detects GTM, GA4, CMP, Consent Mode v2, Meta Pixel and Google Ads tags directly from a "
    "site's public HTML — the same signals used in the Amplio Account Master."
)

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
