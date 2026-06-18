"""
detectors.py — Pure signal-detection functions.
Inputs: page JS content (str) and list of network request URLs (list[str]).
Outputs: typed values — no side effects, no Playwright calls here.
"""

import re
from typing import Literal

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def strip_html_comments(text: str) -> str:
    """Remove HTML comment blocks so commented-out (inactive) tags don't trigger
    false positives — e.g. a disabled gtag('consent','default') snippet."""
    return _HTML_COMMENT_RE.sub(" ", text)


# ── Known CMP signatures ──────────────────────────────────────────────────────
CMP_SIGNATURES: dict[str, str] = {
    "onetrust":     "OneTrust",
    "cookiebot":    "Cookiebot",
    "cookiepro":    "CookiePro",
    "usercentrics": "Usercentrics",
    "trustarc":     "TrustArc",
    "quantcast":    "Quantcast",
    "didomi":       "Didomi",
    "iubenda":      "iubenda",
    "axeptio":      "Axeptio",
    "consentmanager": "consentmanager.net",
    "sourcepoint":  "Sourcepoint",
    "sp-prod.net":  "Sourcepoint",
    "cmp.osano":    "Osano",
    "termly":       "Termly",
    "cookieyes":    "CookieYes",
    "complianz":    "Complianz",
    "borlabs":      "Borlabs Cookie",
    "cookiefirst":  "CookieFirst",
    "klaro":        "Klaro",
}

GA4_CLIENT_PATTERNS = [
    r"google-analytics\.com/g/collect",
    r"googletagmanager\.com/gtag/js\?id=G-",
    r"gtag\(['\"]config['\"],\s*['\"]G-",
]

GA4_SERVER_PATTERNS = [
    # 1st-party endpoints that proxy GA4 — look for /g/collect or /mp/collect on custom domains
    r"https?://(?!www\.google-analytics\.com|analytics\.google\.com)[^/]+/g/collect",
    r"https?://(?!www\.google-analytics\.com)[^/]+/mp/collect",
]

CONSENT_MODE_REQUIRED = ["analytics_storage", "ad_storage"]
CONSENT_MODE_OPTIONAL = ["ad_user_data", "ad_personalization", "functionality_storage", "security_storage"]

# ── Other analytics / tracking tools (substring → display name) ────────────────
OTHER_TOOL_SIGNATURES: dict[str, str] = {
    "matomo":                  "Matomo",
    "piwik":                   "Matomo",          # legacy Matomo name
    "cdn.drda.io":             "Dreamdata",
    "dreamdata":               "Dreamdata",
    "assets.apollo.io":        "Apollo.io",
    "apollo.io/micro":         "Apollo.io",
    "analytics.tiktok.com":    "TikTok Pixel",
    "ttq.load":                "TikTok Pixel",
    "snap.licdn.com":          "LinkedIn Insight",
    "_linkedin_partner_id":    "LinkedIn Insight",
    "static.hotjar.com":       "Hotjar",
    "clarity.ms":              "Microsoft Clarity",
    "cdn.segment.com":         "Segment",
    "js.hs-scripts.com":       "HubSpot",
    "js.hsforms.net":          "HubSpot",
    "cdn.shopify.com":         "Shopify",
    "plausible.io":            "Plausible",
}


# ── GTM ───────────────────────────────────────────────────────────────────────
def detect_gtm(js_content: str, network_urls: list[str]) -> bool:
    if any("googletagmanager.com/gtm.js" in u for u in network_urls):
        return True
    if re.search(r"gtm\.js\?id=GTM-", js_content):
        return True
    if re.search(r"GTM-[A-Z0-9]+", js_content):
        return True
    return False


# ── GA4 ───────────────────────────────────────────────────────────────────────
def detect_ga4(js_content: str, network_urls: list[str]) -> Literal["client-side", "server-side", "no"]:
    all_urls = " ".join(network_urls)

    # Server-side: 1st-party hit to /g/collect or /mp/collect
    for pattern in GA4_SERVER_PATTERNS:
        if re.search(pattern, all_urls):
            return "server-side"

    # Client-side: standard GA4 patterns
    combined = js_content + "\n" + all_urls
    for pattern in GA4_CLIENT_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            return "client-side"

    return "no"


# ── CMP ───────────────────────────────────────────────────────────────────────
def detect_cmp(js_content: str, network_urls: list[str]) -> tuple[bool, str]:
    all_text = js_content + "\n" + " ".join(network_urls)
    for key, name in CMP_SIGNATURES.items():
        if key in all_text.lower():
            return True, name
    return False, ""


# ── Consent Mode ──────────────────────────────────────────────────────────────
def detect_consent_mode(js_content: str) -> Literal["yes", "partial", "no"]:
    # Look for gtag('consent', 'default', {...})
    match = re.search(
        r"gtag\s*\(\s*['\"]consent['\"]\s*,\s*['\"]default['\"]\s*,\s*(\{[^}]+\})",
        js_content,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return "no"

    consent_block = match.group(1).lower()
    has_required = all(k in consent_block for k in CONSENT_MODE_REQUIRED)
    has_all = has_required and all(k in consent_block for k in CONSENT_MODE_OPTIONAL)

    if has_all:
        return "yes"
    elif has_required:
        return "partial"
    else:
        return "partial"  # block found but incomplete


# ── Consent Mode (runtime) ─────────────────────────────────────────────────────
def classify_consent_mode(keys: list[str]) -> Literal["yes", "partial", "no"]:
    """Classify Consent Mode from the live consent keys read out of the running page
    (window.google_tag_data.ics / dataLayer). This sees config done *inside* GTM,
    which the static-HTML detector cannot. v2 = ad_user_data/ad_personalization present."""
    keyset = {k.lower() for k in keys}
    if not keyset:
        return "no"
    if {"ad_user_data", "ad_personalization"} & keyset:
        return "yes"
    if {"ad_storage", "analytics_storage"} & keyset:
        return "partial"
    return "partial"


# ── Ad Platforms ──────────────────────────────────────────────────────────────
def detect_ad_platforms(js_content: str, network_urls: list[str]) -> Literal["Meta", "Google Ads", "Both", ""]:
    all_text = js_content + "\n" + " ".join(network_urls)

    has_meta = bool(
        re.search(r"connect\.facebook\.net/[^/]+/fbevents\.js", all_text) or
        re.search(r"fbq\s*\(", js_content)
    )

    has_google_ads = bool(
        re.search(r"googleadservices\.com", all_text) or
        re.search(r"gtag\s*\(\s*['\"]config['\"]\s*,\s*['\"]AW-", js_content)
    )

    if has_meta and has_google_ads:
        return "Both"
    elif has_meta:
        return "Meta"
    elif has_google_ads:
        return "Google Ads"
    return ""


# ── ID extraction ──────────────────────────────────────────────────────────────
def detect_gtm_id(content: str) -> str:
    m = re.search(r"GTM-[A-Z0-9]{4,}", content)
    return m.group(0) if m else ""


def detect_ga4_id(content: str) -> str:
    m = re.search(r"\bG-[A-Z0-9]{6,12}\b", content)
    return m.group(0) if m else ""


# ── Meta Pixel ──────────────────────────────────────────────────────────────────
def detect_meta_pixel(content: str, network_urls: list[str]) -> tuple[bool, str]:
    all_text = content + "\n" + " ".join(network_urls)
    present = bool(
        re.search(r"connect\.facebook\.net/[^/]+/fbevents\.js", all_text)
        or re.search(r"fbq\s*\(", content)
        or re.search(r"facebook\.com/tr\?", all_text)
    )
    m = re.search(r"fbq\s*\(\s*['\"]init['\"]\s*,\s*['\"](\d{6,})['\"]", content)
    return present, (m.group(1) if m else "")


# ── Google Ads ───────────────────────────────────────────────────────────────────
def detect_google_ads(content: str, network_urls: list[str]) -> tuple[bool, str]:
    all_text = content + "\n" + " ".join(network_urls)
    m = re.search(r"AW-\d{6,}", content)
    present = bool(
        m
        or "googleadservices.com" in all_text.lower()
        or re.search(r"doubleclick\.net/(pagead|ads)", all_text)
        or re.search(r"gtag\s*\(\s*['\"]config['\"]\s*,\s*['\"]AW-", content)
    )
    return present, (m.group(0) if m else "")


# ── Other analytics / tracking tools ──────────────────────────────────────────────
def detect_other_tools(content: str, network_urls: list[str]) -> list[str]:
    all_text = (content + "\n" + " ".join(network_urls)).lower()
    found: list[str] = []
    for key, name in OTHER_TOOL_SIGNATURES.items():
        if key in all_text and name not in found:
            found.append(name)
    return found
