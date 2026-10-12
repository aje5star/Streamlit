"""
LeadScraper Pro - Streamlit lead-discovery tool.

Searches the web for a niche/keyword, crawls the resulting business websites and
extracts public contact details (emails, phone numbers, social profiles).

Run with:  streamlit run app2.py
Needs:     streamlit, requests, beautifulsoup4, pandas   (fake-useragent is optional)

Please use responsibly: respect each site's terms and robots.txt, and follow the
privacy / anti-spam laws that apply to you when you contact the leads you collect.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:  # optional dependency - the app works fine without it
    from fake_useragent import UserAgent
except Exception:  # pragma: no cover
    UserAgent = None

# --------------------------------------------------------------------------- #
# Configuration & constants
# --------------------------------------------------------------------------- #
APP_TITLE = "LeadScraper Pro"
APP_ICON = "🚀"

FALLBACK_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
]

REQUEST_TIMEOUT = 10          # seconds
MAX_PAGE_BYTES = 2_000_000    # never download more than ~2 MB of a single page
POLITENESS_DELAY = 0.5        # seconds between requests to the same site
MAX_LOG_LINES = 500
DONATE_BTC_ADDRESS = "bc1qcvgn85qtg40uf7t88x32d2cfjwjrqhkaqgxkny"
MAX_EMAILS_PER_SITE = 10
MAX_PHONES_PER_SITE = 5

ENGINE_AUTO = "Auto (DuckDuckGo, then Google)"
ENGINE_DDG = "DuckDuckGo"
ENGINE_GOOGLE = "Google (often blocked)"
ENGINE_CHOICES = [ENGINE_AUTO, ENGINE_DDG, ENGINE_GOOGLE]

# Directories / social networks / reference sites: they are not the business's own site.
EXCLUDED_DOMAINS = {
    "google.com", "bing.com", "duckduckgo.com", "facebook.com", "instagram.com",
    "linkedin.com", "twitter.com", "x.com", "youtube.com", "tiktok.com",
    "pinterest.com", "yelp.com", "tripadvisor.com", "wikipedia.org", "reddit.com",
    "amazon.com", "mercadolibre.com.mx", "yellowpages.com", "mapquest.com",
    "foursquare.com", "glassdoor.com", "indeed.com",
}

SOCIAL_DOMAINS = {
    "facebook": "facebook.com",
    "instagram": "instagram.com",
    "linkedin": "linkedin.com",
    "twitter": "twitter.com",
    "x": "x.com",
    "youtube": "youtube.com",
    "tiktok": "tiktok.com",
}
SOCIAL_SHARE_HINTS = ("/sharer", "/share", "/intent", "/dialog", "/plugins")

SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".zip", ".rar",
    ".mp4", ".mp3", ".avi", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".css", ".js", ".xml", ".json", ".woff", ".woff2", ".ttf",
)
# Pages that usually contain contact details get crawled first.
PRIORITY_HINTS = (
    "contact", "contacto", "kontakt", "about", "nosotros", "acerca",
    "impressum", "team", "equipo", "location", "ubicacion", "sucursal",
)

# --- Extraction patterns -----------------------------------------------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@(?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,}")
JUNK_EMAIL_TLDS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "css", "js",
    "woff", "woff2", "ttf", "eot", "mp4", "pdf",
}
JUNK_EMAIL_DOMAINS = {
    "example.com", "domain.com", "yourdomain.com", "email.com",
    "sentry.io", "wixpress.com",
}
# Candidate phone numbers: optional +, then digits separated by spaces . - ( )
PHONE_CANDIDATE_RE = re.compile(r"(?<![\w.+])(\+?\(?\d[\d\s().\-]{7,20}\d)(?![\w])")

GENERIC_TITLE_WORDS = {"home", "homepage", "welcome", "inicio", "startseite", "index", "main"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _dedupe(items: Iterable[str]) -> List[str]:
    """Remove duplicates while keeping the original order."""
    return list(OrderedDict.fromkeys(items))


def site_key(url_or_host: str) -> str:
    """Normalised host used to compare / deduplicate sites ('www.' and port removed)."""
    host = urlparse(url_or_host if "//" in url_or_host else f"//{url_or_host}").hostname or ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def is_excluded_url(url: str) -> bool:
    host = site_key(url)
    return any(_host_matches(host, d) for d in EXCLUDED_DOMAINS)


def unwrap_redirect(href: str) -> str:
    """Turn search-engine redirect links (DuckDuckGo /l/?uddg=..., Google /url?q=...) into real URLs."""
    href = (href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    if "uddg" in qs:
        return qs["uddg"][0]
    if parsed.path == "/url":
        target = qs.get("q") or qs.get("url")
        if target:
            return target[0]
    return href


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
class ScraperConfig:
    """Wraps a requests.Session with retries, a sane User-Agent and safe page downloads."""

    def __init__(self, timeout: int = REQUEST_TIMEOUT):
        self.timeout = timeout
        self.session = requests.Session()

        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST", "HEAD"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.user_agent = self._pick_user_agent()
        self.headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.7,es;q=0.5",
        }

    @staticmethod
    def _pick_user_agent() -> str:
        if UserAgent is not None:
            try:
                return UserAgent().random
            except Exception as exc:  # fake_useragent can fail on first run / offline
                logger.warning("fake_useragent unavailable (%s); using built-in list", exc)
        return random.choice(FALLBACK_USER_AGENTS)

    def get_headers(self) -> Dict[str, str]:
        return dict(self.headers)

    def request(self, url: str, method: str = "GET", params: Optional[dict] = None,
                data: Optional[dict] = None) -> Optional[requests.Response]:
        """Generic request used for search engines. Returns None on any failure."""
        try:
            response = self.session.request(
                method, url, headers=self.get_headers(), params=params, data=data,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as exc:
            logger.error("Request failed for %s: %s", url, exc)
            return None

    def fetch_soup(self, url: str) -> Optional[BeautifulSoup]:
        """Download an HTML page (size-capped, HTML only) and parse it."""
        try:
            with self.session.get(url, headers=self.get_headers(), timeout=self.timeout,
                                  stream=True) as response:
                response.raise_for_status()
                ctype = response.headers.get("Content-Type", "").lower()
                if ctype and "html" not in ctype and "xml" not in ctype:
                    return None  # PDFs, images, downloads...
                chunks, size = [], 0
                for chunk in response.iter_content(chunk_size=65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_PAGE_BYTES:
                        break
            # Parse from bytes so BeautifulSoup detects the real encoding itself.
            return BeautifulSoup(b"".join(chunks), "html.parser")
        except requests.exceptions.RequestException as exc:
            logger.warning("Could not fetch %s: %s", url, exc)
            return None

    def fetch_robots(self, base_url: str) -> Tuple[Optional[int], str]:
        """Return (status_code, body) for <base_url>/robots.txt; (None, '') on network error."""
        try:
            response = self.session.get(base_url + "/robots.txt", headers=self.get_headers(),
                                        timeout=5)
            return response.status_code, response.text if response.status_code == 200 else ""
        except requests.exceptions.RequestException:
            return None, ""


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
class LeadExtractor:
    @staticmethod
    def _is_valid_email(email: str) -> bool:
        local, _, domain = email.rpartition("@")
        if not local or len(local) > 64 or len(email) > 254:
            return False
        if domain.rsplit(".", 1)[-1] in JUNK_EMAIL_TLDS:      # e.g. logo@2x.png
            return False
        if any(_host_matches(domain, d) for d in JUNK_EMAIL_DOMAINS):
            return False
        if re.fullmatch(r"[0-9a-f]{16,}", local):              # hash-like tracking ids
            return False
        return True

    @classmethod
    def extract_emails(cls, text: str, soup: Optional[BeautifulSoup] = None) -> List[str]:
        candidates: List[str] = []
        if soup is not None:
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.lower().startswith("mailto:"):
                    candidates.extend(EMAIL_RE.findall(unquote(href[7:]).split("?")[0]))
        candidates.extend(EMAIL_RE.findall(text or ""))
        cleaned = [c.strip(".").lower() for c in candidates]
        return _dedupe(e for e in cleaned if cls._is_valid_email(e))

    @staticmethod
    def _looks_like_phone(raw: str) -> bool:
        digits = re.sub(r"\D", "", raw)
        if not 9 <= len(digits) <= 15 or len(set(digits)) == 1:
            return False
        # Bare digit strings are usually ids / order numbers; real numbers have a + or separators.
        if not (raw.startswith("+") or re.search(r"[\s().\-]", raw)):
            return False
        if re.fullmatch(r"\d{4}[-/. ]\d{1,2}[-/. ]\d{1,2}", raw):        # dates
            return False
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", raw):                # IP addresses
            return False
        groups = re.findall(r"\d+", raw)
        if all(len(g) == 4 and g[:2] in ("19", "20") for g in groups):    # "2019 2023 2024"
            return False
        return True

    @staticmethod
    def _add_phone(found: "OrderedDict[str, str]", raw: str) -> None:
        digits = re.sub(r"\D", "", raw)
        if digits in found:  # same number again: keep the nicer (more formatted) spelling
            if len(raw) > len(found[digits]):
                found[digits] = raw
            return
        # Skip if the same number is already known with/without a country code.
        if any(d.endswith(digits) or digits.endswith(d) for d in found):
            return
        found[digits] = raw

    @classmethod
    def extract_phones(cls, text: str, soup: Optional[BeautifulSoup] = None) -> List[str]:
        found: "OrderedDict[str, str]" = OrderedDict()
        if soup is not None:  # tel: links are the most reliable source
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.lower().startswith("tel:"):
                    raw = unquote(href[4:]).split(";")[0].strip()
                    if 7 <= len(re.sub(r"\D", "", raw)) <= 15:
                        cls._add_phone(found, raw)
        for match in PHONE_CANDIDATE_RE.finditer(text or ""):
            raw = " ".join(match.group(1).split())
            if cls._looks_like_phone(raw):
                cls._add_phone(found, raw)
        return list(found.values())

    @staticmethod
    def _walk_json(node: Any) -> Iterator[dict]:
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from LeadExtractor._walk_json(value)
        elif isinstance(node, list):
            for item in node:
                yield from LeadExtractor._walk_json(item)

    @classmethod
    def extract_jsonld_contacts(cls, soup: BeautifulSoup) -> Tuple[List[str], List[str]]:
        """Emails / phones declared in schema.org JSON-LD blocks."""
        emails: List[str] = []
        phones: List[str] = []
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or tag.get_text() or "")
            except ValueError:
                continue
            for node in cls._walk_json(data):
                email, phone = node.get("email"), node.get("telephone")
                if isinstance(email, str):
                    emails.append(email.replace("mailto:", "").strip())
                if isinstance(phone, str):
                    phones.append(phone.strip())
        return emails, phones

    @staticmethod
    def _meta(soup: BeautifulSoup, key: str) -> str:
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        return (tag.get("content") or "").strip() if tag else ""

    @staticmethod
    def _clean_title(title: str) -> str:
        parts = [p.strip() for p in re.split(r"\s*[|–—·•]\s*|\s+-\s+", title) if p.strip()]
        for part in parts:
            if part.lower() not in GENERIC_TITLE_WORDS:
                return part
        return title.strip()

    @classmethod
    def extract_business_info(cls, soup: BeautifulSoup) -> Dict[str, str]:
        title_text = soup.title.get_text(strip=True) if soup.title else ""
        h1 = soup.find("h1")
        name = (
            cls._meta(soup, "og:site_name")
            or (cls._clean_title(title_text) if title_text else "")
            or cls._meta(soup, "og:title")
            or (h1.get_text(strip=True) if h1 else "")
        )
        description = cls._meta(soup, "og:description") or cls._meta(soup, "description")
        return {"name": name[:150], "description": description[:300]}

    @staticmethod
    def extract_social_links(soup: BeautifulSoup, base_url: str) -> Dict[str, str]:
        socials: Dict[str, str] = {}
        for a in soup.find_all("a", href=True):
            full = urljoin(base_url, a["href"].strip())
            parsed = urlparse(full)
            if parsed.scheme not in ("http", "https"):
                continue
            host = (parsed.hostname or "").lower()
            path = parsed.path.lower()
            if path in ("", "/") or any(h in path for h in SOCIAL_SHARE_HINTS):
                continue  # profile root / "share this page" buttons
            for name, domain in SOCIAL_DOMAINS.items():
                if _host_matches(host, domain):
                    socials.setdefault(name, full)
                    break
        return socials


# --------------------------------------------------------------------------- #
# Search engines
# --------------------------------------------------------------------------- #
class SearchError(Exception):
    """Raised when a search engine cannot return usable results."""


class DuckDuckGoSearch:
    URL = "https://html.duckduckgo.com/html/"

    @classmethod
    def search(cls, query: str, max_results: int, config: ScraperConfig) -> List[Dict[str, str]]:
        response = config.request(cls.URL, method="POST", data={"q": query})
        if response is None:
            raise SearchError("DuckDuckGo request failed.")
        if "anomaly" in response.text and "result__a" not in response.text:
            raise SearchError("DuckDuckGo asked for a CAPTCHA (too many requests). Try again later.")

        soup = BeautifulSoup(response.text, "html.parser")
        results: List[Dict[str, str]] = []
        for link in soup.select("a.result__a"):
            container = link.find_parent(class_="result")
            if container is not None and "result--ad" in (container.get("class") or []):
                continue  # skip sponsored results
            url = unwrap_redirect(link.get("href", ""))
            if not url.startswith(("http://", "https://")):
                continue
            snippet_tag = container.select_one(".result__snippet") if container is not None else None
            results.append({
                "title": link.get_text(strip=True),
                "url": url,
                "snippet": snippet_tag.get_text(" ", strip=True) if snippet_tag else "",
                "source": "DuckDuckGo",
            })
            if len(results) >= max_results:
                break
        return results


class GoogleSearch:
    """Best effort only: Google blocks most automated requests and changes its markup often."""
    URL = "https://www.google.com/search"

    @classmethod
    def search(cls, query: str, max_results: int, config: ScraperConfig) -> List[Dict[str, str]]:
        params = {"q": query, "num": min(max_results + 2, 20), "hl": "en"}
        response = config.request(cls.URL, params=params)
        if response is None:
            raise SearchError("Google request failed or was blocked.")
        text = response.text
        if "enablejs" in text or "unusual traffic" in text or "consent.google" in response.url:
            raise SearchError("Google blocked the request (JavaScript / consent / CAPTCHA page).")

        soup = BeautifulSoup(text, "html.parser")
        results: List[Dict[str, str]] = []
        for heading in soup.find_all("h3"):
            link = heading.find_parent("a", href=True)
            if link is None:
                continue
            url = unwrap_redirect(link["href"])
            if not url.startswith(("http://", "https://")):
                continue
            results.append({
                "title": heading.get_text(strip=True),
                "url": url,
                "snippet": "",
                "source": "Google",
            })
            if len(results) >= max_results:
                break
        return results


def web_search(query: str, max_results: int, engine: str, config: ScraperConfig) -> List[Dict[str, str]]:
    engines = {
        ENGINE_DDG: [DuckDuckGoSearch],
        ENGINE_GOOGLE: [GoogleSearch],
        ENGINE_AUTO: [DuckDuckGoSearch, GoogleSearch],
    }.get(engine, [DuckDuckGoSearch, GoogleSearch])

    errors: List[str] = []
    for backend in engines:
        try:
            results = backend.search(query, max_results, config)
            if results:
                return results
            errors.append(f"{backend.__name__}: no results")
        except SearchError as exc:
            errors.append(str(exc))
        except Exception as exc:  # parsing problems must never crash the app
            logger.exception("Unexpected search failure")
            errors.append(f"{backend.__name__}: {exc}")
    raise SearchError("No results. " + " | ".join(errors))


def filter_results(results: List[Dict[str, str]], known_domains: set,
                   skip_directories: bool) -> List[Dict[str, str]]:
    """Drop directories/social sites and sites that are already in the lead list or repeated."""
    seen = set(known_domains)
    kept = []
    for res in results:
        key = site_key(res["url"])
        if not key or key in seen:
            continue
        if skip_directories and is_excluded_url(res["url"]):
            continue
        seen.add(key)
        kept.append(res)
    return kept


# --------------------------------------------------------------------------- #
# Website crawler
# --------------------------------------------------------------------------- #
class WebsiteCrawler:
    """Breadth-first crawler restricted to one site, contact-like pages first."""

    def __init__(self, config: ScraperConfig, respect_robots: bool = True,
                 delay: float = POLITENESS_DELAY):
        self.config = config
        self.respect_robots = respect_robots
        self.delay = delay
        self.extractor = LeadExtractor()
        self._robots: Dict[str, RobotFileParser] = {}

    # -- robots.txt ---------------------------------------------------------
    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._robots.get(base)
        if parser is None:
            parser = RobotFileParser()
            status, body = self.config.fetch_robots(base)
            if status == 200:
                parser.parse(body.splitlines())
            elif status in (401, 403):
                parser.disallow_all = True
            else:
                parser.allow_all = True
            self._robots[base] = parser
        return parser.can_fetch("*", url)

    @staticmethod
    def _link_priority(url: str) -> int:
        path = urlparse(url).path.lower()
        return min((i for i, hint in enumerate(PRIORITY_HINTS) if hint in path),
                   default=len(PRIORITY_HINTS))

    # -- single page --------------------------------------------------------
    def _extract_page(self, url: str, soup: BeautifulSoup) -> Dict[str, Any]:
        ld_emails, ld_phones = self.extractor.extract_jsonld_contacts(soup)
        business_info = self.extractor.extract_business_info(soup)
        socials = self.extractor.extract_social_links(soup, url)

        for tag in soup(["script", "style", "noscript", "template"]):
            tag.decompose()  # keep code / CSS out of the visible-text searches
        text = soup.get_text(" ", strip=True)

        emails = self.extractor.extract_emails(text, soup) + self.extractor.extract_emails(" ".join(ld_emails))
        phones = self.extractor.extract_phones(text, soup)
        known = OrderedDict((re.sub(r"\D", "", p), p) for p in phones)
        for phone in ld_phones:
            if 7 <= len(re.sub(r"\D", "", phone)) <= 15:
                self.extractor._add_phone(known, phone)
        phones = list(known.values())

        links = []
        for a in soup.find_all("a", href=True):
            link, _ = urldefrag(urljoin(url, a["href"].strip()))
            parsed = urlparse(link)
            if parsed.scheme in ("http", "https") and not parsed.path.lower().endswith(SKIP_EXTENSIONS):
                links.append(link)

        return {
            "emails": emails, "phones": phones, "socials": socials,
            "business_info": business_info, "links": _dedupe(links),
        }

    # -- whole site ---------------------------------------------------------
    def crawl_site(self, start_url: str, max_depth: int = 1, max_pages: int = 5) -> Optional[Dict[str, Any]]:
        """
        Crawl `start_url` plus up to `max_depth` link levels (same site only), never more than
        `max_pages` pages. Returns merged results, or None if nothing could be fetched.
        """
        start_url, _ = urldefrag(start_url.strip())
        parsed = urlparse(start_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None

        domain = site_key(start_url)
        root = f"{parsed.scheme}://{parsed.netloc}/"
        frontier = [start_url]
        if parsed.path not in ("", "/"):
            frontier.append(root)  # search hits are often deep pages; the homepage is worth a look

        visited: set = set()
        merged: Dict[str, Any] = {"emails": [], "phones": [], "socials": {}, "business_info": {}}
        pages = 0
        depth = 0

        while frontier and pages < max_pages and depth <= max_depth:
            discovered: List[str] = []
            for url in frontier:
                if pages >= max_pages:
                    break
                if url in visited:
                    continue
                visited.add(url)
                if not self._allowed(url):
                    logger.info("robots.txt disallows %s", url)
                    continue
                if pages:
                    time.sleep(self.delay)
                soup = self.config.fetch_soup(url)
                if soup is None:
                    continue
                pages += 1
                page = self._extract_page(url, soup)

                merged["emails"].extend(page["emails"])
                merged["phones"].extend(page["phones"])
                for name, link in page["socials"].items():
                    merged["socials"].setdefault(name, link)
                for key, value in page["business_info"].items():
                    if value and not merged["business_info"].get(key):
                        merged["business_info"][key] = value

                if depth < max_depth:
                    discovered.extend(l for l in page["links"] if site_key(l) == domain)

            # Contact/about pages first, otherwise keep discovery order.
            unique = [u for u in _dedupe(discovered) if u not in visited]
            frontier = sorted(unique, key=self._link_priority)
            depth += 1

        if pages == 0:
            return None

        emails = _dedupe(merged["emails"])
        # Emails on the company's own domain are more valuable than gmail/third-party ones.
        emails.sort(key=lambda e: 0 if site_key(e.split("@", 1)[1]).endswith(domain) else 1)
        return {
            "url": start_url,
            "emails": emails[:MAX_EMAILS_PER_SITE],
            "phones": merged["phones"][:MAX_PHONES_PER_SITE],
            "social_links": merged["socials"],
            "business_info": merged["business_info"],
            "pages_crawled": pages,
        }


# --------------------------------------------------------------------------- #
# Data helpers (UI independent)
# --------------------------------------------------------------------------- #
def leads_to_df(leads: List[Dict[str, Any]]) -> pd.DataFrame:
    """Flatten list/dict fields so tables and CSV exports are readable."""
    rows = []
    for lead in leads:
        row = dict(lead)
        row["emails"] = ", ".join(lead.get("emails", []))
        row["phones"] = ", ".join(lead.get("phones", []))
        row["socials"] = "; ".join(f"{k}: {v}" for k, v in lead.get("socials", {}).items())
        rows.append(row)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Streamlit UI
# --------------------------------------------------------------------------- #
_ST_VERSION = tuple(int(x) for x in re.findall(r"\d+", getattr(st, "__version__", "0.0"))[:2] or [0, 0])
# `use_container_width` was replaced by `width="stretch"` in recent Streamlit releases.
STRETCH: Dict[str, Any] = {"width": "stretch"} if _ST_VERSION >= (1, 50) else {"use_container_width": True}


def init_session_state() -> None:
    defaults: Dict[str, Any] = {
        "max_leads": 10,
        "crawl_depth": 1,
        "max_pages": 5,
        "require_email": False,
        "require_phone": False,
        "respect_robots": True,
        "skip_directories": True,
        "engine": ENGINE_AUTO,
        "flash": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    if "leads" not in st.session_state:
        st.session_state.leads = []
    if "logs" not in st.session_state:
        st.session_state.logs = []
    if "scraper_config" not in st.session_state:
        st.session_state.scraper_config = ScraperConfig()


def add_log(msg: str, level: str = "INFO") -> None:
    entry = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}"
    st.session_state.logs.append(entry)
    del st.session_state.logs[:-MAX_LOG_LINES]
    getattr(logger, level.lower(), logger.info)(msg)


def render_sidebar() -> None:
    with st.sidebar:
        st.markdown(
            f"<h1 style='color:#4CAF50;margin-bottom:0'>{APP_TITLE}</h1>"
            "<p style='color:#aaa'>Lead discovery platform</p>",
            unsafe_allow_html=True,
        )
        st.divider()

        st.header("Configuration")
        st.number_input("Max leads per search", min_value=1, max_value=100, step=1, key="max_leads")
        st.slider("Crawl depth (link levels after the first page)", 0, 3, key="crawl_depth")
        st.slider("Max pages per site", 1, 15, key="max_pages")
        st.selectbox("Search engine", ENGINE_CHOICES, key="engine")
        st.checkbox("Require email", key="require_email")
        st.checkbox("Require phone", key="require_phone")
        st.checkbox("Skip directories & social sites", key="skip_directories")
        st.checkbox("Respect robots.txt", key="respect_robots")

        st.divider()
        st.header("Actions")
        if st.button("Clear all data"):
            st.session_state.leads = []
            st.session_state.logs = []
            st.rerun()

        leads = st.session_state.leads
        if leads:
            st.download_button(
                "⬇️ Download CSV",
                data=leads_to_df(leads).to_csv(index=False).encode("utf-8-sig"),  # BOM: Excel-friendly accents
                file_name="leads_scraper_pro.csv",
                mime="text/csv",
            )
            st.download_button(
                "⬇️ Download JSON",
                data=json.dumps(leads, indent=2, ensure_ascii=False),
                file_name="leads_scraper_pro.json",
                mime="application/json",
            )
        else:
            st.caption("Exports appear here once you have leads.")

        st.divider()
        st.header("Donate")
        st.caption("Enjoying the tool? Support it with Bitcoin (BTC):")
        st.code(DONATE_BTC_ADDRESS, language=None)  # built-in copy button


def render_dashboard() -> None:
    st.header("Dashboard overview")
    leads = st.session_state.leads

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total leads", len(leads))
    col2.metric("Unique domains", len({l.get("domain") or site_key(l.get("website", "")) for l in leads}))
    col3.metric("Emails found", sum(len(l.get("emails", [])) for l in leads))
    col4.metric("Phones found", sum(len(l.get("phones", [])) for l in leads))

    if leads:
        st.subheader("Latest leads")
        st.dataframe(leads_to_df(leads[-10:][::-1]), hide_index=True, **STRETCH)
    else:
        st.info("No leads discovered yet. Go to the **New Search** tab to start.")


def run_search(keyword: str, location: str) -> None:
    ss = st.session_state
    query = f"{keyword} in {location}" if location else keyword
    target = int(ss.max_leads)
    add_log(f"Starting search for: {query}")

    progress = st.progress(0.0, text="Searching the web...")
    try:
        results = web_search(query, min(target * 3, 30), ss.engine, ss.scraper_config)
    except SearchError as exc:
        progress.empty()
        add_log(str(exc), "ERROR")
        st.error(f"Search failed: {exc}")
        return

    known = {l.get("domain") or site_key(l.get("website", "")) for l in ss.leads}
    results = filter_results(results, known, bool(ss.skip_directories))
    if not results:
        progress.empty()
        st.warning("The search worked, but every result was a directory or an already-saved site.")
        return

    add_log(f"{len(results)} candidate sites to crawl")
    crawler = WebsiteCrawler(ss.scraper_config, respect_robots=bool(ss.respect_robots))
    new_leads: List[Dict[str, Any]] = []

    for i, res in enumerate(results, start=1):
        if len(new_leads) >= target:
            break
        domain = site_key(res["url"])
        progress.progress(min(0.1 + 0.9 * (i - 1) / len(results), 1.0),
                          text=f"Crawling {i}/{len(results)}: {domain}")

        data = crawler.crawl_site(res["url"], max_depth=int(ss.crawl_depth),
                                  max_pages=int(ss.max_pages))
        if not data:
            add_log(f"Skipped {domain} (unreachable, blocked or disallowed)", "WARNING")
            continue
        if ss.require_email and not data["emails"]:
            continue
        if ss.require_phone and not data["phones"]:
            continue

        info = data["business_info"]
        lead = {
            "id": uuid.uuid4().hex[:8],
            "business_name": info.get("name", ""),
            "title": res["title"],
            "website": res["url"],
            "domain": domain,
            "description": info.get("description") or res.get("snippet", ""),
            "emails": data["emails"],
            "phones": data["phones"],
            "socials": data["social_links"],
            "pages_crawled": data["pages_crawled"],
            "discovery_date": time.strftime("%Y-%m-%d %H:%M"),
            "status": "Scraped",  # contact data is extracted, not verified
        }
        new_leads.append(lead)
        add_log(f"Extracted lead: {lead['business_name'] or domain}")

    ss.leads.extend(new_leads)
    progress.progress(1.0, text="Done!")
    if new_leads:
        ss.flash = ("success", f"Added {len(new_leads)} new lead(s) for “{query}”.")
    else:
        ss.flash = ("warning", "No sites matched your filters. Try relaxing 'Require email/phone' "
                               "or increasing the crawl depth.")
    st.rerun()  # refresh the Dashboard / Leads tabs, which were drawn before the search ran


def render_search_tab() -> None:
    st.header("Lead discovery engine")
    with st.form("search_form"):
        col1, col2 = st.columns(2)
        keyword = col1.text_input("Business niche / keyword", placeholder="e.g., Dentists")
        location = col2.text_input("Location (optional)", placeholder="e.g., Amsterdam, Netherlands")
        submitted = st.form_submit_button("Start search", type="primary")

    if submitted:
        if not keyword.strip():
            st.warning("Enter a business niche or keyword first.")
        else:
            run_search(keyword.strip(), location.strip())


def render_leads_tab() -> None:
    st.header("Discovered leads")
    leads = st.session_state.leads
    if not leads:
        st.write("No leads collected yet.")
        return

    df = leads_to_df(leads)
    needle = st.text_input("Filter leads", placeholder="Search by name, domain, email...")
    if needle:
        mask = df.astype(str).apply(lambda col: col.str.contains(needle, case=False, regex=False)).any(axis=1)
        df = df[mask]
    st.caption(f"Showing {len(df)} of {len(leads)} leads")
    st.dataframe(
        df, hide_index=True,
        column_config={"website": st.column_config.LinkColumn("website")},
        **STRETCH,
    )


def render_logs_tab() -> None:
    st.header("Activity logs")
    if st.button("Clear logs"):
        st.session_state.logs = []
        st.rerun()
    logs = st.session_state.logs
    st.code("\n".join(reversed(logs)) if logs else "No activity yet.", language=None)  # newest first


CUSTOM_CSS = """
<style>
    .stApp { background-color: #121212; color: #E0E0E0; }
    [data-testid="stSidebar"] { background-color: #1E1E1E; }
    .stButton > button, .stDownloadButton > button {
        background-color: #4CAF50; color: white; border-radius: 5px; font-weight: bold; border: none;
    }
    [data-testid="stMetric"] {
        background-color: #1E1E1E; padding: 16px; border-radius: 10px; border: 1px solid #333;
    }
    h1, h2, h3, h4 { color: #FFFFFF !important; }
</style>
"""


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon=APP_ICON, layout="wide")
    init_session_state()
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    render_sidebar()

    flash = st.session_state.get("flash")
    if flash:
        kind, message = flash
        getattr(st, kind, st.info)(message)
        st.session_state.flash = None

    tab_dash, tab_search, tab_leads, tab_logs = st.tabs(
        ["Dashboard", "New Search", "Leads Database", "Logs"])
    with tab_dash:
        render_dashboard()
    with tab_search:
        render_search_tab()
    with tab_leads:
        render_leads_tab()
    with tab_logs:
        render_logs_tab()


if __name__ == "__main__":
    main()
