import asyncio
import subprocess
import sys

import pandas as pd
import streamlit as st
from scraper.browser import scrape_domains
from scraper.models import ScrapeResult
from utils.export import to_excel_bytes, to_csv_string

st.set_page_config(
    page_title="Domain Tag Scraper",
    page_icon="🔍",
    layout="wide",
)


@st.cache_resource(show_spinner="Setting up the headless browser…")
def _ensure_chromium() -> bool:
    """Streamlit Cloud builds a fresh container that never runs `playwright install`,
    so the Chromium binary is missing. Download it once per app boot (idempotent —
    a no-op locally where it's already installed). System libs come from packages.txt."""
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True, capture_output=True, text=True, timeout=300,
        )
        return True
    except Exception as exc:  # surface but don't crash the whole app on import
        st.warning(f"Could not auto-install Chromium: {exc}")
        return False


_ensure_chromium()

st.title("🔍 Domain Tag Scraper")
st.caption("Detects GTM · GA4 · CMP · Consent Mode · Ad Platforms")

# ── Input ────────────────────────────────────────────────────────────────────
col1, col2 = st.columns([3, 1])

with col1:
    raw_input = st.text_area(
        "Enter one domain per line (or paste a comma-separated list)",
        placeholder="https://example.com\nhttps://anothersite.com",
        height=150,
    )

with col2:
    st.markdown("**Options**")
    timeout = st.slider("Timeout per domain (s)", 5, 30, 15)
    wait_for = st.selectbox("Wait strategy", ["networkidle", "domcontentloaded", "load"])
    accept_consent = st.checkbox(
        "Accept consent banners",
        value=True,
        help="Clicks 'Accept all' so post-consent tags (GTM/GA4/Pixel/Ads) fire and get detected. "
             "Uncheck for a pre-consent-only scan.",
    )
    do_recon = st.checkbox(
        "Domain recon (DNS/WHOIS/IP/TLS)",
        value=True,
        help="Looks up DNS records, WHOIS, hosting ASN and TLS cert. Surfaces signals like a "
             "facebook-domain-verification TXT record even when the Pixel is hidden.",
    )
    concurrency = st.slider(
        "Parallel browsers", 1, 4, 2,
        help="How many sites to scan at once. Each one runs a headless Chromium tab — "
             "keep this low (1–2) on memory-limited hosts like Streamlit Cloud to avoid crashes.",
    )

run = st.button("🚀 Start Scraping", type="primary", use_container_width=True)

# Cap per run so a huge paste can't exhaust memory (esp. on Streamlit Cloud's ~1 GB).
MAX_DOMAINS = 25

# ── Parse domains ─────────────────────────────────────────────────────────────
def parse_domains(raw: str) -> list[str]:
    domains = []
    for part in raw.replace(",", "\n").splitlines():
        part = part.strip()
        if not part:
            continue
        if not part.startswith("http"):
            part = "https://" + part
        domains.append(part)
    return list(dict.fromkeys(domains))  # deduplicate, preserve order


# ── Results table ─────────────────────────────────────────────────────────────
def results_to_df(results: list[ScrapeResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        rows.append({
            "Domain": r.domain,
            "GTM": f"✅ {r.gtm_id}" if r.gtm and r.gtm_id else ("✅ Yes" if r.gtm else "❌ No"),
            "GA4": f"{r.ga4} ({r.ga4_id})" if r.ga4_id else r.ga4,
            "CMP": f"✅ {r.cmp_vendor}" if r.cmp else "❌ No",
            "Consent Mode": r.consent_mode,
            "Meta Pixel": f"✅ {r.meta_pixel_id}" if r.meta_pixel and r.meta_pixel_id else ("✅ Yes" if r.meta_pixel else "❌ No"),
            "Google Ads": f"✅ {r.google_ads_id}" if r.google_ads and r.google_ads_id else ("✅ Yes" if r.google_ads else "❌ No"),
            "Ad Platforms": r.ad_platforms or "—",
            "Other Tools": ", ".join(r.other_tools) or "—",
            "Consent Accepted": "✅" if r.consent_clicked else "—",
            **_recon_columns(r.recon),
            "Error": r.error or "",
        })
    return pd.DataFrame(rows)


def _recon_columns(recon: dict) -> dict:
    """Flatten the most useful recon fields into table columns."""
    if not recon:
        return {}
    verification = recon.get("verification", {})
    ip_asn = recon.get("ip_asn", {})
    whois = recon.get("whois", {})
    tls = recon.get("tls", {})
    return {
        "FB Domain Verify": "✅" if verification.get("facebook_domain_verification") else "—",
        "IP": ip_asn.get("ip", ""),
        "ASN Org": ip_asn.get("asn_org", ""),
        "Registrar": whois.get("registrar", "") or "",
        "TLS Issuer": tls.get("issuer", ""),
    }


# ── Main execution ────────────────────────────────────────────────────────────
if run:
    domains = parse_domains(raw_input)
    if len(domains) > MAX_DOMAINS:
        st.warning(
            f"You entered {len(domains)} domains — scanning only the first {MAX_DOMAINS} "
            f"to stay within memory limits. Run the rest in another batch."
        )
        domains = domains[:MAX_DOMAINS]
    if not domains:
        st.warning("Please enter at least one domain.")
    else:
        st.info(f"Scraping **{len(domains)}** domain(s)…")
        progress = st.progress(0)
        status = st.empty()
        results_placeholder = st.empty()
        results: list[ScrapeResult] = []

        async def run_with_progress():
            done = 0
            async for i, result in scrape_domains(domains, timeout=timeout, wait_for=wait_for, accept_consent=accept_consent, do_recon=do_recon, concurrency=concurrency):
                results.append(result)
                done += 1
                progress.progress(done / len(domains))
                status.text(f"✔ {result.domain}")
                results_placeholder.dataframe(results_to_df(results), use_container_width=True)

        asyncio.run(run_with_progress())

        status.success("Done!")
        df = results_to_df(results)

        st.subheader("Results")
        st.dataframe(df, use_container_width=True)

        # ── Export ────────────────────────────────────────────────────────────
        st.markdown("**Export**")
        ec1, ec2 = st.columns(2)
        with ec1:
            st.download_button(
                "⬇️ Download CSV",
                data=to_csv_string(df),
                file_name="domain_scrape_results.csv",
                mime="text/csv",
            )
        with ec2:
            st.download_button(
                "⬇️ Download Excel",
                data=to_excel_bytes(df),
                file_name="domain_scrape_results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
