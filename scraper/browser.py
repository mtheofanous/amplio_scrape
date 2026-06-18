"""
browser.py — Playwright orchestration.
Fetches pages, intercepts network, extracts JS content, then calls detectors.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Literal

from playwright.async_api import (
    async_playwright,
    Page,
    Request,
    TimeoutError as PlaywrightTimeoutError,
)

from scraper.detectors import (
    detect_gtm,
    detect_gtm_id,
    detect_ga4,
    detect_ga4_id,
    detect_cmp,
    detect_consent_mode,
    detect_ad_platforms,
    detect_meta_pixel,
    detect_google_ads,
    detect_other_tools,
    classify_consent_mode,
    strip_html_comments,
)
from scraper.models import ScrapeResult
from scraper.recon import gather_recon

logger = logging.getLogger(__name__)

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# "Accept all" buttons for the CMPs we recognise (CSS pierces open shadow DOM).
CONSENT_ACCEPT_SELECTORS: list[str] = [
    "#onetrust-accept-btn-handler",                                # OneTrust / CookiePro
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",      # Cookiebot
    "#CybotCookiebotDialogBodyButtonAccept",                       # Cookiebot (alt)
    "button[data-testid='uc-accept-all-button']",                  # Usercentrics
    "#didomi-notice-agree-button",                                 # Didomi
    ".qc-cmp2-summary-buttons button[mode='primary']",             # Quantcast
    "#truste-consent-button",                                      # TrustArc
    ".iubenda-cs-accept-btn",                                      # iubenda
    "button[aria-label='Accept all'], button#axeptio_btn_acceptAll",  # Axeptio
    "#cmpwelcomebtnyes a, .cmpboxbtnyes",                          # consentmanager
    "button.sp_choice_type_11, button[title='Accept all']",        # Sourcepoint (in iframe)
]

# Generic fallback: short button text matching "accept" intent across languages.
CONSENT_ACCEPT_TEXTS: tuple[str, ...] = (
    "accept all", "accept cookies", "accept", "agree", "i agree", "allow all",
    "got it", "ok", "yes, i'm happy", "yes, i’m happy", "yes i'm happy",  # Sourcepoint/Guardian
    "alle akzeptieren", "akzeptieren", "zustimmen", "einverstanden",
    "aceptar todo", "aceptar todas", "aceptar", "accepter tout", "tout accepter", "accepter",
    "accetta tutto", "accetta", "aceitar tudo", "aceitar", "godkänn alla",
)


async def _click_consent_in(frame) -> bool:
    """Try known selectors then a text scan within a single frame. True if clicked."""
    for sel in CONSENT_ACCEPT_SELECTORS:
        try:
            el = frame.locator(sel).first
            if await el.is_visible(timeout=400):
                await el.click(timeout=2000)
                logger.info("Accepted consent via selector %s", sel)
                return True
        except Exception:
            continue

    # Generic text-based fallback: scan visible buttons for an accept-style label.
    try:
        buttons = frame.get_by_role("button")
        count = min(await buttons.count(), 40)
        for i in range(count):
            btn = buttons.nth(i)
            try:
                if not await btn.is_visible(timeout=200):
                    continue
                label = (await btn.inner_text(timeout=200)).strip().lower()
            except Exception:
                continue
            if len(label) <= 40 and any(t == label or t in label for t in CONSENT_ACCEPT_TEXTS):
                try:
                    await btn.click(timeout=2000)
                    logger.info("Accepted consent via button text %r", label)
                    return True
                except Exception:
                    continue
    except Exception:
        pass

    return False


async def _read_live_consent(page: Page) -> list[str]:
    """Read the live Google Consent Mode state out of the running page. Google stores
    it in window.google_tag_data.ics.entries; consent commands also land in dataLayer.
    Returns the consent-type keys (ad_storage, analytics_storage, ad_user_data, …).
    This catches Consent Mode configured *inside* the GTM container."""
    try:
        return await page.evaluate(
            """() => {
                const keys = new Set();
                try {
                    const ics = window.google_tag_data && window.google_tag_data.ics;
                    if (ics && ics.entries) Object.keys(ics.entries).forEach(k => keys.add(k));
                } catch (e) {}
                try {
                    for (const e of (window.dataLayer || [])) {
                        if (e && e[0] === 'consent' && e[2] && typeof e[2] === 'object')
                            Object.keys(e[2]).forEach(k => keys.add(k));
                    }
                } catch (e) {}
                return [...keys];
            }"""
        )
    except Exception:
        return []


async def _simulate_user_activity(page: Page) -> None:
    """Generate *trusted* user events (mouse move/scroll/key) to trip interaction-gated
    lazy loaders such as WP Rocket's "delay JavaScript until user interaction", which
    otherwise never fetch the CMP/analytics scripts in an automated session.
    Synthetic dispatched events won't do — these must come from the input driver."""
    try:
        await page.mouse.move(120, 160)
        await page.mouse.move(440, 360)          # 2nd move (loaders ignore the 1st)
        await page.mouse.wheel(0, 800)
        await page.keyboard.press("PageDown")
        await asyncio.sleep(0.4)
        await page.mouse.wheel(0, 1600)
        await page.mouse.move(700, 500)
    except Exception as exc:
        logger.debug("user-activity simulation skipped: %s", exc)


async def _accept_consent(page: Page, attempts: int = 4, delay: float = 1.5) -> bool:
    """Best-effort "Accept all" click. Searches the main document AND every iframe,
    since major CMPs (Sourcepoint, TrustArc, Quantcast) render the banner in a frame.
    Retries because lazy-loaded CMPs (e.g. Usercentrics behind WP Rocket) paint their
    UI a few seconds after the loader fetches."""
    for attempt in range(attempts):
        if await _click_consent_in(page.main_frame):
            return True
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            try:
                if await _click_consent_in(frame):
                    return True
            except Exception:
                continue
        if attempt < attempts - 1:
            await asyncio.sleep(delay)
    return False


async def _scrape_single(
    page: Page,
    domain: str,
    timeout: int,
    wait_for: str,
    accept_consent: bool = True,
    simulate_activity: bool = True,
) -> ScrapeResult:
    result = ScrapeResult(domain=domain)
    network_urls: list[str] = []

    def on_request(req: Request):
        network_urls.append(req.url)

    page.on("request", on_request)

    try:
        try:
            await page.goto(domain, timeout=timeout * 1000, wait_until=wait_for)  # type: ignore[arg-type]
        except PlaywrightTimeoutError:
            # Heavy commercial sites (constant beacons/polling) may never reach
            # "networkidle". The page has still navigated and tags have fired, so
            # don't discard the run — fall through and extract from what loaded.
            logger.info("'%s' wait timed out for %s; extracting from loaded page", wait_for, domain)

        # Trip interaction-gated lazy loaders (WP Rocket etc.) BEFORE consent, so the
        # CMP/analytics scripts actually fetch and the banner has something to render.
        if simulate_activity:
            await _simulate_user_activity(page)
            try:
                await page.wait_for_load_state("networkidle", timeout=timeout * 1000)
            except PlaywrightTimeoutError:
                pass

        # Accept the consent banner so post-consent tags (GTM/GA4/Pixel/Ads) fire.
        # network_urls keeps accumulating across the click, so we capture both
        # the pre- and post-consent state in one pass.
        if accept_consent:
            try:
                if await _accept_consent(page):
                    result.consent_clicked = True
                    try:
                        await page.wait_for_load_state("networkidle", timeout=timeout * 1000)
                    except PlaywrightTimeoutError:
                        pass
            except Exception as exc:
                logger.info("Consent acceptance skipped for %s: %s", domain, exc)

        # Small extra wait to capture any deferred tag loads
        await asyncio.sleep(2)

        # Collect all inline + external JS source text visible in DOM…
        js_content: str = await page.evaluate("""() => {
            const scripts = [...document.querySelectorAll('script')];
            return scripts.map(s => s.textContent || s.src || '').join('\\n');
        }""")
        # …plus the full serialised HTML. The HTML carries vendor references that
        # never execute in headless — e.g. a CMP loader tag (<script id="usercentrics-cmp">)
        # or an inline Consent Mode default call — which the script-text view misses.
        try:
            page_html: str = await page.content()
        except Exception:
            page_html = ""
        # Strip HTML comments so commented-out (inactive) tags don't false-positive,
        # e.g. a disabled gtag('consent','default') block or a second GTM snippet.
        content = strip_html_comments(js_content + "\n" + page_html)

        result.gtm          = detect_gtm(content, network_urls)
        result.gtm_id       = detect_gtm_id(content)
        result.ga4          = detect_ga4(content, network_urls)
        result.ga4_id       = detect_ga4_id(content)
        result.cmp, result.cmp_vendor = detect_cmp(content, network_urls)

        # Consent Mode: prefer the LIVE state from the running page (sees config done
        # inside the GTM container, invisible to static HTML); fall back to HTML regex.
        consent_keys = await _read_live_consent(page)
        result.consent_mode = (
            classify_consent_mode(consent_keys) if consent_keys
            else detect_consent_mode(content)
        )

        result.meta_pixel, result.meta_pixel_id = detect_meta_pixel(content, network_urls)
        result.google_ads, result.google_ads_id = detect_google_ads(content, network_urls)
        result.ad_platforms = detect_ad_platforms(content, network_urls)
        result.other_tools  = detect_other_tools(content, network_urls)

    except Exception as exc:
        logger.warning("Error scraping %s: %s", domain, exc)
        result.error = str(exc)

    return result


async def scrape_domains(
    domains: list[str],
    timeout: int = 15,
    wait_for: Literal["networkidle", "domcontentloaded", "load"] = "networkidle",
    concurrency: int = 3,
    accept_consent: bool = True,
    simulate_activity: bool = True,
    do_recon: bool = True,
) -> AsyncGenerator[tuple[int, ScrapeResult], None]:
    """
    Async generator yielding (index, ScrapeResult) for each domain.
    Uses a semaphore to cap concurrency so we don't hammer machines.

    accept_consent=True clicks the CMP "Accept all" control so post-consent tags
    fire and get detected; set False for a pre-consent-only scan.
    simulate_activity=True generates trusted user input to trip interaction-gated
    lazy loaders (e.g. WP Rocket) that otherwise never fetch the tracking scripts.
    do_recon=True attaches DNS/WHOIS/IP-ASN/TLS reconnaissance (see scraper/recon.py).
    """
    sem = asyncio.Semaphore(concurrency)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=CHROME_UA)

        async def bounded_scrape(i: int, domain: str):
            async with sem:
                page = await context.new_page()
                try:
                    result = await _scrape_single(
                        page, domain, timeout, wait_for, accept_consent, simulate_activity
                    )
                finally:
                    await page.close()
            # Recon is blocking network I/O (DNS/WHOIS/sockets) — run off the event loop.
            if do_recon:
                try:
                    result.recon = await asyncio.to_thread(gather_recon, domain)
                except Exception as exc:
                    result.recon = {"error": str(exc)}
            return i, result

        tasks = [bounded_scrape(i, d) for i, d in enumerate(domains)]

        # Yield results as they complete (not necessarily in order)
        for coro in asyncio.as_completed(tasks):
            yield await coro

        await browser.close()


def _cli() -> None:
    """CLI entry point: `python -m scraper.browser https://example.com [...]`."""
    import sys

    logging.basicConfig(level=logging.INFO)
    domains = sys.argv[1:]
    if not domains:
        print("usage: python -m scraper.browser <url> [<url> ...]")
        raise SystemExit(2)

    async def _run() -> None:
        async for _, result in scrape_domains(domains):
            print(result)

    asyncio.run(_run())


if __name__ == "__main__":
    _cli()
