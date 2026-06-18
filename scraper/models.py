from dataclasses import dataclass, field
from typing import Literal


GA4Mode = Literal["client-side", "server-side", "no"]
ConsentMode = Literal["yes", "partial", "no"]
AdPlatforms = Literal["Meta", "Google Ads", "Both", ""]


@dataclass
class ScrapeResult:
    domain: str
    gtm: bool = False
    gtm_id: str = ""
    ga4: GA4Mode = "no"
    ga4_id: str = ""
    cmp: bool = False
    cmp_vendor: str = ""
    consent_mode: ConsentMode = "no"
    meta_pixel: bool = False
    meta_pixel_id: str = ""
    google_ads: bool = False
    google_ads_id: str = ""
    ad_platforms: AdPlatforms = ""
    other_tools: list[str] = field(default_factory=list)
    consent_clicked: bool = False
    recon: dict = field(default_factory=dict)
    error: str = ""
