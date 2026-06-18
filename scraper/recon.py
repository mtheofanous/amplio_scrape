"""
recon.py — Domain reconnaissance (no browser, no page HTML).

Gathers facts about the *domain itself* from external sources: DNS records,
WHOIS registration, hosting IP/ASN, and the TLS certificate. These complement the
tag detection in detectors.py — e.g. a `facebook-domain-verification` TXT record is
a strong Meta signal even when the Pixel is hidden behind a consent banner.

All lookups degrade gracefully: missing optional libraries or failed queries return
a partial dict rather than raising, so a recon failure never breaks a scrape.
"""

import logging
import re
import socket
import ssl
from datetime import datetime
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Optional dependencies — recon works with whatever is installed.
try:
    import dns.resolver  # type: ignore
except Exception:
    dns = None  # type: ignore

try:
    import whois as whois_lib  # type: ignore
except Exception:
    whois_lib = None  # type: ignore

try:
    from ipwhois import IPWhois  # type: ignore
except Exception:
    IPWhois = None  # type: ignore


def normalize_domain(domain: str) -> str:
    """Reduce a URL or host to a registrable-ish hostname (drops scheme/path/`www.`)."""
    d = domain.strip()
    if "://" not in d:
        d = "//" + d
    host = urlparse(d).hostname or domain.strip()
    if host.startswith("www."):
        host = host[4:]
    return host


def resolve_dns(host: str) -> dict:
    out: dict = {"A": [], "AAAA": [], "MX": [], "TXT": []}
    if dns is None:
        out["error"] = "dnspython not installed"
        return out
    for record in ("A", "AAAA", "MX", "TXT"):
        try:
            answers = dns.resolver.resolve(host, record, lifetime=5)
            out[record] = [r.to_text().strip('"') for r in answers]
        except Exception:
            pass
    return out


def get_whois(host: str) -> dict:
    if whois_lib is None:
        return {"error": "python-whois not installed"}
    try:
        w = whois_lib.whois(host)

        def _first(v):
            return v[0] if isinstance(v, (list, tuple)) and v else v

        return {
            "registrar": w.registrar,
            "org": w.org,
            "created": str(_first(w.creation_date)) if w.creation_date else None,
            "expires": str(_first(w.expiration_date)) if w.expiration_date else None,
            "country": w.country,
        }
    except Exception as e:
        return {"error": str(e)}


def get_ip_asn(host: str) -> dict:
    try:
        ips = list(dict.fromkeys(r[4][0] for r in socket.getaddrinfo(host, None)))
        info: dict = {"ips": ips, "ip": ips[0] if ips else ""}
        if IPWhois and ips:
            try:
                rdap = IPWhois(ips[0]).lookup_rdap(depth=1)
                info["asn"] = rdap.get("asn")
                info["asn_country"] = rdap.get("asn_country_code")
                info["asn_org"] = rdap.get("asn_description")
            except Exception:
                info["asn_error"] = "lookup failed"
        return info
    except Exception as e:
        return {"error": str(e)}


def get_tls_info(host: str) -> dict:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=6) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert() or {}
        issuer = {k: v for t in cert.get("issuer", []) for k, v in t}
        return {
            "issuer": issuer.get("organizationName") or issuer.get("commonName", ""),
            "not_after": cert.get("notAfter", ""),
            "subject_alt_names": [v for t, v in cert.get("subjectAltName", [])],
        }
    except Exception as e:
        return {"error": str(e)}


def _verification_signals(txt_records: list[str]) -> dict:
    """Pull marketing-relevant signals out of TXT records (verification tokens)."""
    blob = " ".join(txt_records).lower()
    return {
        "facebook_domain_verification": "facebook-domain-verification" in blob,
        "google_site_verification": "google-site-verification" in blob,
        "hubspot": "hubspotemail" in blob or "hs-mail" in blob,
    }


def gather_recon(domain: str) -> dict:
    """Run all recon lookups for a domain. Blocking — call via asyncio.to_thread.
    Returns a flat-ish dict suitable for display/export; never raises."""
    host = normalize_domain(domain)
    recon: dict = {"host": host}
    try:
        recon["dns"] = resolve_dns(host)
        recon["verification"] = _verification_signals(recon["dns"].get("TXT", []))
        recon["whois"] = get_whois(host)
        recon["ip_asn"] = get_ip_asn(host)
        recon["tls"] = get_tls_info(host)
    except Exception as e:  # belt-and-suspenders: recon must never break a scrape
        recon["error"] = str(e)
    return recon
