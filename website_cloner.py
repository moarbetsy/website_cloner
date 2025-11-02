import argparse
import hashlib
import logging
import mimetypes
import os
import random
import re
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Lock
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
from urllib import robotparser
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------- Config ----------

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.7",
}

CSS_URL_RE = re.compile(r"url\(\s*([\"']?)([^)\"']+)\1\s*\)", re.IGNORECASE)
CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\()?\s*([\"']?)([^\)\"';]+)\1\s*\)?\s*;",
    re.IGNORECASE,
)
SRCSET_SPLIT_RE = re.compile(r"\s*,\s*")
WHITESPACE_RE = re.compile(r"\s+")

INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*]')

TRACKING_PARAM_PREFIXES = (
    "utm_",
    "gclid",
    "fbclid",
    "mc_",
    "yclid",
    "icid",
    "ref",
    "cmpid",
)

HTML_LIKE_EXTS = {".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".jspx", ".cfm"}

# ---------- Settings ----------

@dataclass
class Settings:
    timeout: float = 15.0
    workers: int = 4
    delay: float = 0.0  # base delay
    same_origin_only: bool = True
    respect_robots: bool = False
    max_bytes: int = 50_000_000
    skip_js: bool = False

    # Crawl options
    crawl: bool = False
    max_pages: int = 100
    max_depth: int = 1
    include: Optional[str] = None
    exclude: Optional[str] = None
    strip_params: bool = True
    allow_params: Set[str] = field(default_factory=set)
    sitemap_seed: bool = False

    # Rendering options
    render_js: bool = False
    render_timeout_ms: int = 10000
    wait_until: str = "networkidle"
    render_block_media: bool = True  # reduce requests during render

    # Throttle options
    global_rps: float = 2.0       # max requests per second across all hosts
    per_host_rps: float = 1.0     # max requests per second per host
    burst: int = 2                # token bucket capacity
    jitter: float = 0.3           # 0..1 fraction of added random delay
    max_backoff: float = 60.0     # seconds
    auto_throttle: bool = True    # honor Retry-After and back off on 429 or 503


# ---------- Throttling ----------

class TokenBucket:
    def __init__(self, rate: float, capacity: float):
        self.rate = max(rate, 0.001)
        self.capacity = max(capacity, 1.0)
        self.tokens = self.capacity
        self.ts = time.monotonic()
        self.lock = Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        delta = now - self.ts
        if delta > 0:
            self.tokens = min(self.capacity, self.tokens + delta * self.rate)
            self.ts = now

    def consume_wait(self, tokens: float = 1.0) -> float:
        with self.lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return 0.0
            need = tokens - self.tokens
            wait = need / self.rate
            self.tokens = 0.0
            self.ts = time.monotonic()
            return max(0.0, wait)


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    # seconds
    if value.isdigit():
        return float(value)
    # http date
    try:
        dt = parsedate_to_datetime(value)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        sec = (dt - now).total_seconds()
        return max(0.0, sec)
    except Exception:
        return None


class Throttle:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.global_bucket = TokenBucket(settings.global_rps, settings.burst)
        self.host_buckets: Dict[str, TokenBucket] = {}
        self.host_block_until: Dict[str, float] = {}
        self.host_429_count: Dict[str, int] = {}
        self.lock = Lock()

    def _host_bucket(self, host: str) -> TokenBucket:
        with self.lock:
            b = self.host_buckets.get(host)
            if b is None:
                b = TokenBucket(self.settings.per_host_rps, self.settings.burst)
                self.host_buckets[host] = b
            return b

    def acquire(self, url: str) -> None:
        host = urlparse(url).netloc
        base = self.settings.delay
        g_wait = self.global_bucket.consume_wait(1.0)
        h_wait = self._host_bucket(host).consume_wait(1.0)
        with self.lock:
            until = self.host_block_until.get(host, 0.0)
        now = time.monotonic()
        b_wait = max(0.0, until - now)
        wait = max(base, g_wait, h_wait, b_wait)
        if self.settings.jitter > 0:
            wait += random.uniform(0, self.settings.jitter * max(wait, base, 0.01))
        if wait > 0:
            time.sleep(wait)

    def on_result(self, url: str, status: int, headers: Dict[str, str]) -> None:
        if not self.settings.auto_throttle:
            return
        host = urlparse(url).netloc
        if status in (429, 503):
            ra = parse_retry_after(headers.get("Retry-After"))
            if ra is None:
                with self.lock:
                    n = self.host_429_count.get(host, 0) + 1
                    self.host_429_count[host] = n
                backoff = min(self.settings.max_backoff, 2 ** min(n, 6))
            else:
                backoff = min(self.settings.max_backoff, ra)
                with self.lock:
                    # do not increase streak if server gave an explicit window
                    self.host_429_count[host] = max(0, self.host_429_count.get(host, 0) - 1)
            jitter = 0.1 * backoff
            until = time.monotonic() + backoff + random.uniform(0, jitter)
            with self.lock:
                self.host_block_until[host] = max(self.host_block_until.get(host, 0.0), until)
        elif 200 <= status < 300:
            with self.lock:
                self.host_429_count[host] = 0


# ---------- Utilities ----------

def sanitize_filename(name: str) -> str:
    name = INVALID_FILENAME_CHARS_RE.sub("_", name)
    name = name or "file"
    if name.startswith("."):
        name = "_" + name[1:]
    return name[:200]


def can_fetch_url(url: str) -> bool:
    if not url:
        return False
    url = url.strip()
    if url.startswith(("#", "mailto:", "tel:", "javascript:", "data:", "blob:")):
        return False
    return True


def is_same_origin(base: str, other: str) -> bool:
    b, o = urlparse(base), urlparse(other)
    return (b.scheme, b.netloc) == (o.scheme, o.netloc)


def build_session(headers: Optional[Dict[str, str]] = None) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods={"GET", "HEAD"},
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=64, pool_maxsize=64)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update(DEFAULT_HEADERS if headers is None else headers)
    return s


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def filename_with_query_hash(path: str, query: str, content_type: Optional[str]) -> str:
    name = os.path.basename(path.rstrip("/")) or "index"
    root, ext = os.path.splitext(name)
    if not ext and content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            ext = guessed
    if query:
        qhash = hashlib.md5(query.encode("utf-8")).hexdigest()[:8]
        root = f"{root}_{qhash}"
    return sanitize_filename(root + ext)


def local_asset_path_for_url(absolute_url: str, output_root: Path, content_type: Optional[str]) -> Path:
    p = urlparse(absolute_url)
    host = sanitize_filename(p.netloc) or "host"
    segments = [seg for seg in p.path.split("/") if seg]
    segments = [sanitize_filename(seg) for seg in segments]
    if segments:
        filename = filename_with_query_hash("/" + segments[-1], p.query, content_type)
        segments[-1] = filename
    else:
        filename = filename_with_query_hash("/index", p.query, content_type)
        segments = [filename]
    return output_root.joinpath(host, *segments)


def local_html_path_for_url(absolute_url: str, output_root: Path) -> Path:
    p = urlparse(absolute_url)
    host = sanitize_filename(p.netloc) or "host"
    path = p.path or "/"
    segments = [seg for seg in path.split("/") if seg]
    if path.endswith("/"):
        fp = output_root.joinpath(host, *segments, "index.html")
    else:
        last = segments[-1] if segments else ""
        root, ext = os.path.splitext(last)
        if ext.lower() in HTML_LIKE_EXTS:
            fp = output_root.joinpath(host, *segments)
        elif ext:
            fp = output_root.joinpath(host, *segments, "index.html")
        else:
            fp = output_root.joinpath(host, *segments, "index.html")
    return fp


def parse_srcset(srcset_value: str) -> List[str]:
    urls: List[str] = []
    if not srcset_value:
        return urls
    for candidate in SRCSET_SPLIT_RE.split(srcset_value.strip()):
        if not candidate:
            continue
        parts = WHITESPACE_RE.split(candidate.strip())
        if parts:
            urls.append(parts[0])
    return urls


def normalize_url(u: str, *, strip_params: bool, allow_params: Set[str]) -> str:
    p = urlparse(u)
    p = p._replace(fragment="")
    if not strip_params or not p.query:
        return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))
    qs = parse_qsl(p.query, keep_blank_values=True)
    keep = []
    for k, v in qs:
        kl = k.lower()
        if k in allow_params:
            keep.append((k, v))
            continue
        drop = any(kl.startswith(pref) for pref in TRACKING_PARAM_PREFIXES)
        if not drop:
            keep.append((k, v))
    new_q = urlencode(keep, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, ""))


def url_allowed(u: str, base_url: str, settings: Settings) -> bool:
    if settings.same_origin_only and not is_same_origin(base_url, u):
        return False
    if settings.include and not re.search(settings.include, u):
        return False
    if settings.exclude and re.search(settings.exclude, u):
        return False
    return True


# ---------- Extraction ----------

def extract_asset_urls(soup: BeautifulSoup, base_url: str, *, skip_js: bool) -> Set[str]:
    urls: Set[str] = set()

    for link in soup.select("link[href]"):
        rels = {r.lower() for r in (link.get("rel") or [])}
        if {"stylesheet", "icon", "apple-touch-icon", "shortcut icon", "manifest"} & rels or "preload" in rels:
            if "preload" in rels:
                as_type = (link.get("as") or "").lower()
                if as_type not in {"style", "image", "font"}:
                    continue
            href = link.get("href")
            if can_fetch_url(href):
                urls.add(urljoin(base_url, href))

    for tag in soup.select("img[src], source[src], video[src], audio[src], track[src]"):
        src = tag.get("src")
        if can_fetch_url(src):
            urls.add(urljoin(base_url, src))

    for tag in soup.select("video[poster]"):
        poster = tag.get("poster")
        if can_fetch_url(poster):
            urls.add(urljoin(base_url, poster))

    for tag in soup.select("img[srcset], source[srcset]"):
        for u in parse_srcset(tag.get("srcset", "")):
            if can_fetch_url(u):
                urls.add(urljoin(base_url, u))

    for tag in soup.select("[style]"):
        css = tag.get("style") or ""
        for u in parse_css_urls(css):
            urls.add(urljoin(base_url, u))

    for style in soup.find_all("style"):
        text = style.string or ""
        for u in parse_css_urls(text):
            urls.add(urljoin(base_url, u))

    if not skip_js:
        for tag in soup.select("script[src]"):
            src = tag.get("src")
            if can_fetch_url(src):
                urls.add(urljoin(base_url, src))

    return urls


def parse_css_urls(text: str) -> Set[str]:
    urls: Set[str] = set()
    for m in CSS_URL_RE.finditer(text):
        u = m.group(2).strip()
        if can_fetch_url(u):
            urls.add(u)
    for m in CSS_IMPORT_RE.finditer(text):
        u = m.group(2).strip()
        if can_fetch_url(u):
            urls.add(u)
    return urls


def extract_anchor_links(soup: BeautifulSoup, base_url: str) -> Set[str]:
    urls: Set[str] = set()
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not can_fetch_url(href):
            continue
        absu = urljoin(base_url, href)
        urls.add(absu)
    return urls


# ---------- HTTP helpers with throttle ----------

def throttled_get(
    session: requests.Session,
    throttle: Throttle,
    url: str,
    *,
    timeout: float,
    stream: bool = False,
) -> requests.Response:
    throttle.acquire(url)
    r = session.get(url, timeout=timeout, stream=stream)
    throttle.on_result(url, r.status_code, r.headers)
    return r


# ---------- Downloaders ----------

def download_one(
    session: requests.Session,
    throttle: Throttle,
    absolute_url: str,
    output_root: Path,
    settings: Settings,
    robots: Optional[robotparser.RobotFileParser] = None,
) -> Optional[Path]:
    if settings.respect_robots and robots is not None:
        if not robots.can_fetch(session.headers.get("User-Agent", "*"), absolute_url):
            logging.debug("robots disallow: %s", absolute_url)
            return None

    try:
        resp = throttled_get(session, throttle, absolute_url, timeout=settings.timeout, stream=True)
        status = resp.status_code
        if status >= 400:
            logging.warning("failed %s -> HTTP %s", absolute_url, status)
            return None

        cl = resp.headers.get("Content-Length")
        if cl is not None:
            try:
                if int(cl) > settings.max_bytes:
                    logging.warning("skip large file %s (%s bytes)", absolute_url, cl)
                    return None
            except ValueError:
                pass

        content_type = resp.headers.get("Content-Type")
        local_path = local_asset_path_for_url(absolute_url, output_root, content_type)
        ensure_parent_dir(local_path)

        written = 0
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            written += len(chunk)
            if written > settings.max_bytes:
                logging.warning("truncated large file %s at %d bytes", absolute_url, written)
                break
            with open(local_path, "ab") as f:
                f.write(chunk)

        if written == 0:
            logging.warning("empty response %s", absolute_url)
            return None

        logging.info("downloaded asset: %s -> %s", absolute_url, local_path)
        return local_path

    except requests.RequestException as e:
        logging.warning("error downloading %s: %s", absolute_url, e)
        return None


def download_all(
    session: requests.Session,
    throttle: Throttle,
    urls: Iterable[str],
    output_root: Path,
    settings: Settings,
    robots: Optional[robotparser.RobotFileParser] = None,
) -> Dict[str, Path]:
    url_set: Set[str] = set(urls)
    result: Dict[str, Path] = {}
    if not url_set:
        return result

    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        future_map = {
            pool.submit(download_one, session, throttle, u, output_root, settings, robots): u
            for u in url_set
        }
        for fut in as_completed(future_map):
            u = future_map[fut]
            p = fut.result()
            if p is not None:
                result[u] = p
    return result


# ---------- Rewriters ----------

def rewrite_html_assets_and_srcset(
    soup: BeautifulSoup,
    base_url: str,
    mapping: Dict[str, Path],
    html_dir: Path,
) -> None:
    def to_rel(p: Path) -> str:
        return Path(os.path.relpath(p, html_dir)).as_posix()

    attr_map = {
        "img": ["src"],
        "source": ["src"],
        "video": ["src", "poster"],
        "audio": ["src"],
        "track": ["src"],
        "script": ["src"],
        "link": ["href"],
    }

    for tag_name, attrs in attr_map.items():
        for tag in soup.find_all(tag_name):
            for a in attrs:
                val = tag.get(a)
                if not val or not can_fetch_url(val):
                    continue
                absu = urljoin(base_url, val)
                if absu in mapping:
                    tag[a] = to_rel(mapping[absu])
                    for rm in ("integrity", "crossorigin", "referrerpolicy"):
                        if rm in tag.attrs:
                            del tag.attrs[rm]

    for tag in soup.select("img[srcset], source[srcset]"):
        srcset_val = tag.get("srcset", "")
        parts = []
        for candidate in SRCSET_SPLIT_RE.split(srcset_val.strip()):
            if not candidate:
                continue
            comp = WHITESPACE_RE.split(candidate.strip())
            if not comp:
                continue
            url_part = comp[0]
            desc = " ".join(comp[1:])
            absu = urljoin(base_url, url_part)
            url_out = to_rel(mapping[absu]) if absu in mapping else url_part
            parts.append((url_out, desc))
        tag["srcset"] = ", ".join([f"{u} {d}".strip() for u, d in parts if u])


def rewrite_css_text_in_context(css_text: str, css_base_url: str, mapping: Dict[str, Path], rel_base_dir: Path) -> str:
    def map_url(u: str) -> str:
        absu = urljoin(css_base_url, u)
        p = mapping.get(absu)
        if p is None:
            return u
        return Path(os.path.relpath(p, rel_base_dir)).as_posix()

    def repl_url(match: re.Match) -> str:
        quote = match.group(1) or ""
        u = match.group(2).strip()
        new_u = map_url(u)
        return f"url({quote}{new_u}{quote})"

    def repl_import(match: re.Match) -> str:
        quote = match.group(1) or ""
        u = match.group(2).strip()
        new_u = map_url(u)
        if match.group(0).lower().startswith("@import url"):
            return f"@import url({quote}{new_u}{quote});"
        else:
            return f"@import {quote}{new_u}{quote};"

    new_text = CSS_URL_RE.sub(repl_url, css_text)
    new_text = CSS_IMPORT_RE.sub(repl_import, new_text)
    return new_text


def rewrite_inline_css_in_html(
    soup: BeautifulSoup,
    page_url: str,
    mapping: Dict[str, Path],
    html_dir: Path,
) -> None:
    for tag in soup.select("[style]"):
        css = tag.get("style")
        if not css:
            continue
        new_css = rewrite_css_text_in_context(css, page_url, mapping, html_dir)
        if new_css != css:
            tag["style"] = new_css

    for style in soup.find_all("style"):
        if style.string:
            new_text = rewrite_css_text_in_context(style.string, page_url, mapping, html_dir)
            if new_text != style.string:
                style.string.replace_with(new_text)


def rewrite_links_in_place(
    soup: BeautifulSoup,
    base_url: str,
    expected_pages: Dict[str, Path],
    html_dir: Path,
    *, strip_params: bool, allow_params: Set[str]
) -> None:
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href or not can_fetch_url(href):
            continue
        absu = urljoin(base_url, href)
        parsed = urlparse(absu)
        frag = parsed.fragment
        norm = normalize_url(absu, strip_params=strip_params, allow_params=allow_params)
        target_path = expected_pages.get(norm)
        if target_path:
            rel = Path(os.path.relpath(target_path, html_dir)).as_posix()
            if frag:
                rel = f"{rel}#{frag}"
            a["href"] = rel


# ---------- Robots and sitemaps ----------

def fetch_robots(base_url: str, session: requests.Session, throttle: Throttle) -> Optional[robotparser.RobotFileParser]:
    try:
        rp = robotparser.RobotFileParser()
        robots_url = urljoin(base_url, "/robots.txt")
        r = throttled_get(session, throttle, robots_url, timeout=10)
        if r.status_code >= 400 or not r.text:
            return None
        rp.parse(r.text.splitlines())
        return rp
    except Exception:
        return None


def discover_sitemaps(session: requests.Session, throttle: Throttle, base_url: str) -> List[str]:
    sites: List[str] = []
    try:
        robots_url = urljoin(base_url, "/robots.txt")
        r = throttled_get(session, throttle, robots_url, timeout=10)
        if r.status_code == 200 and r.text:
            for line in r.text.splitlines():
                if line.strip().lower().startswith("sitemap:"):
                    sites.append(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return sites


def parse_sitemap_urls(session: requests.Session, throttle: Throttle, sitemap_url: str) -> List[str]:
    urls: List[str] = []
    try:
        r = throttled_get(session, throttle, sitemap_url, timeout=15)
        if r.status_code >= 400:
            return urls
        root = ET.fromstring(r.text)
        if root.tag.endswith("sitemapindex"):
            for loc in root.findall(".//{*}loc"):
                child_url = loc.text.strip()
                urls.extend(parse_sitemap_urls(session, throttle, child_url))
        else:
            for loc in root.findall(".//{*}loc"):
                if loc.text:
                    urls.append(loc.text.strip())
    except Exception:
        pass
    return urls


# ---------- Rendering ----------

class HtmlRenderer:
    def fetch(self, session: requests.Session, throttle: Throttle, url: str, timeout: float) -> Tuple[Optional[str], Optional[str]]:
        raise NotImplementedError


class RequestsRenderer(HtmlRenderer):
    def fetch(self, session: requests.Session, throttle: Throttle, url: str, timeout: float) -> Tuple[Optional[str], Optional[str]]:
        r = throttled_get(session, throttle, url, timeout=timeout)
        if r.status_code >= 400:
            return None, None
        ct = (r.headers.get("Content-Type") or "").lower()
        if "text/html" not in ct and "application/xhtml+xml" not in ct:
            return None, None
        if not r.encoding:
            r.encoding = "utf-8"
        return r.text, r.encoding


class PlaywrightRenderer(HtmlRenderer):
    def __init__(self, wait_until: str = "networkidle", timeout_ms: int = 10000, block_media: bool = True):
        self.wait_until = wait_until
        self.timeout_ms = timeout_ms
        self.block_media = block_media

    def fetch(self, session: requests.Session, throttle: Throttle, url: str, timeout: float) -> Tuple[Optional[str], Optional[str]]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            logging.error("Playwright not installed. Install 'playwright' then 'playwright install'.")
            return None, None
        html: Optional[str] = None
        try:
            throttle.acquire(url)
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()
                if self.block_media:
                    def _route(route):
                        rt = route.request.resource_type
                        if rt in ("image", "media", "font"):
                            route.abort()
                        else:
                            route.continue_()
                    page.route("**/*", _route)
                page.goto(url, wait_until=self.wait_until, timeout=self.timeout_ms)
                html = page.content()
                browser.close()
            # Assume success
            throttle.on_result(url, 200, {})
        except Exception as e:
            logging.warning("Playwright render failed for %s: %s", url, e)
            html = None
        return html, "utf-8" if html is not None else (None, None)  # type: ignore


def get_renderer(settings: Settings) -> HtmlRenderer:
    if settings.render_js:
        return PlaywrightRenderer(wait_until=settings.wait_until, timeout_ms=settings.render_timeout_ms, block_media=settings.render_block_media)
    return RequestsRenderer()


# ---------- Main flows ----------

def clone_single_page(base_url: str, output_folder: str, settings: Settings) -> None:
    out_root = Path(output_folder).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    host = sanitize_filename(urlparse(base_url).netloc) or "host"
    html_out_dir = out_root.joinpath(host)

    session = build_session()
    throttle = Throttle(settings)
    robots = fetch_robots(base_url, session, throttle) if settings.respect_robots else None

    renderer = get_renderer(settings)
    logging.info("GET %s", base_url)
    try:
        html, _ = renderer.fetch(session, throttle, base_url, settings.timeout)
        if html is None:
            print(f"Critical error: failed to fetch HTML for {base_url}")
            return
    except requests.RequestException as e:
        print(f"Critical error: failed to fetch {base_url}: {e}")
        return

    soup = BeautifulSoup(html, "html.parser")

    asset_urls = extract_asset_urls(soup, base_url, skip_js=settings.skip_js)
    mapping = download_all(session, throttle, asset_urls, out_root, settings, robots)

    html_out_dir.mkdir(parents=True, exist_ok=True)
    rewrite_html_assets_and_srcset(soup, base_url, mapping, html_out_dir)
    rewrite_inline_css_in_html(soup, base_url, mapping, html_out_dir)

    html_out = html_out_dir.joinpath("index.html")
    html_out.write_text(soup.prettify(), encoding="utf-8")

    for asset_url, asset_path in list(mapping.items()):
        if asset_path.suffix.lower() == ".css":
            try:
                text = asset_path.read_text(encoding="utf-8", errors="ignore")
                new_text = rewrite_css_text_in_context(text, asset_url, mapping, asset_path.parent)
                if new_text != text:
                    asset_path.write_text(new_text, encoding="utf-8")
            except Exception:
                pass

    print("Cloning complete")
    print(f"Saved to: {html_out}")


def mirror_site(base_url: str, output_folder: str, settings: Settings) -> None:
    out_root = Path(output_folder).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    session = build_session()
    throttle = Throttle(settings)
    robots = fetch_robots(base_url, session, throttle) if settings.respect_robots else None
    if robots and settings.delay <= 0.0:
        try:
            d = robots.crawl_delay(DEFAULT_HEADERS.get("User-Agent", "*"))
            if d:
                settings.delay = max(settings.delay, float(d))
        except Exception:
            pass

    renderer = get_renderer(settings)

    start_norm = normalize_url(base_url, strip_params=settings.strip_params, allow_params=settings.allow_params)
    frontier: deque[Tuple[str, int]] = deque([(start_norm, 0)])
    seen: Set[str] = set()
    enqueued: Set[str] = {start_norm}

    if settings.sitemap_seed:
        for sm in discover_sitemaps(session, throttle, base_url):
            for u in parse_sitemap_urls(session, throttle, sm):
                if not can_fetch_url(u):
                    continue
                if not url_allowed(u, base_url, settings):
                    continue
                n = normalize_url(u, strip_params=settings.strip_params, allow_params=settings.allow_params)
                if n not in enqueued:
                    frontier.append((n, 0))
                    enqueued.add(n)

    asset_map: Dict[str, Path] = {}
    page_map: Dict[str, Path] = {}
    expected_page_map: Dict[str, Path] = {}

    def expected_path_for(u: str) -> Path:
        return local_html_path_for_url(u, out_root)

    expected_page_map[start_norm] = expected_path_for(start_norm)
    pages_done = 0

    while frontier and pages_done < settings.max_pages:
        url, depth = frontier.popleft()
        if url in seen:
            continue
        seen.add(url)

        if settings.respect_robots and robots is not None:
            if not robots.can_fetch(DEFAULT_HEADERS.get("User-Agent", "*"), url):
                logging.info("robots disallow page: %s", url)
                continue

        logging.info("Fetch page [%d/%d] depth=%d: %s", pages_done + 1, settings.max_pages, depth, url)

        html_text, _enc = renderer.fetch(session, throttle, url, settings.timeout)
        if html_text is None:
            ap = download_one(session, throttle, url, out_root, settings, robots)
            if ap:
                asset_map[url] = ap
            continue

        soup = BeautifulSoup(html_text, "html.parser")

        if depth < settings.max_depth:
            anchors = extract_anchor_links(soup, url)
            for link in anchors:
                if not url_allowed(link, base_url, settings):
                    continue
                n = normalize_url(link, strip_params=settings.strip_params, allow_params=settings.allow_params)
                if n not in enqueued:
                    frontier.append((n, depth + 1))
                    enqueued.add(n)
                    expected_page_map[n] = expected_path_for(n)

        asset_urls = extract_asset_urls(soup, url, skip_js=settings.skip_js)
        downloaded = download_all(session, throttle, asset_urls, out_root, settings, robots)
        asset_map.update(downloaded)

        html_path = expected_page_map.get(url) or expected_path_for(url)
        html_dir = html_path.parent
        html_dir.mkdir(parents=True, exist_ok=True)

        rewrite_html_assets_and_srcset(soup, url, asset_map, html_dir)
        rewrite_inline_css_in_html(soup, url, asset_map, html_dir)
        rewrite_links_in_place(soup, url, expected_page_map, html_dir, strip_params=settings.strip_params, allow_params=settings.allow_params)

        ensure_parent_dir(html_path)
        html_path.write_text(soup.prettify(), encoding="utf-8")
        page_map[url] = html_path
        pages_done += 1

    print("Mirroring complete")
    print(f"Pages saved: {pages_done}")
    print(f"Root: {out_root}")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="website_cloner.py",
        description="Clone a page or crawl a site with polite throttling.",
    )
    p.add_argument("url", help="http(s) URL to clone")
    p.add_argument("output_folder", help="output directory")
    p.add_argument("--external", action="store_true", help="include third party assets")
    p.add_argument("--respect-robots", action="store_true", help="respect robots.txt")
    p.add_argument("--delay", type=float, default=0.0, help="base delay per request seconds")
    p.add_argument("--timeout", type=float, default=15.0, help="request timeout seconds")
    p.add_argument("--workers", type=int, default=4, help="concurrent downloads")
    p.add_argument("--max-bytes", type=int, default=50_000_000, help="max bytes per file")
    p.add_argument("--skip-js", action="store_true", help="do not download script files")
    p.add_argument("--verbose", action="store_true", help="debug logging")

    # crawl
    p.add_argument("--crawl", action="store_true", help="crawl links and mirror multiple pages")
    p.add_argument("--max-pages", type=int, default=100, help="max HTML pages to fetch")
    p.add_argument("--max-depth", type=int, default=1, help="max link depth")
    p.add_argument("--include", type=str, default=None, help="only crawl URLs matching regex")
    p.add_argument("--exclude", type=str, default=None, help="skip URLs matching regex")
    p.add_argument("--no-strip-params", action="store_true", help="keep all query parameters")
    p.add_argument("--allow-param", action="append", default=[], help="query parameter name to keep")
    p.add_argument("--sitemap-seed", action="store_true", help="add URLs from sitemap")

    # render
    p.add_argument("--render-js", action="store_true", help="render with Playwright if installed")
    p.add_argument("--render-timeout-ms", type=int, default=10000, help="Playwright timeout ms")
    p.add_argument("--wait-until", type=str, default="networkidle", help="Playwright wait_until")
    p.add_argument("--no-render-block-media", action="store_true", help="do not block images and fonts during render")

    # throttle
    p.add_argument("--global-rps", type=float, default=2.0, help="global requests per second")
    p.add_argument("--per-host-rps", type=float, default=1.0, help="requests per second per host")
    p.add_argument("--burst", type=int, default=2, help="token bucket burst size")
    p.add_argument("--jitter", type=float, default=0.3, help="delay jitter fraction 0..1")
    p.add_argument("--max-backoff", type=float, default=60.0, help="max backoff seconds")
    p.add_argument("--no-auto-throttle", action="store_true", help="disable backoff on 429 or 503")

    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    if urlparse(args.url).scheme not in {"http", "https"}:
        print("Invalid URL. Use http:// or https://")
        sys.exit(1)

    settings = Settings(
        timeout=args.timeout,
        workers=max(1, args.workers),
        delay=max(0.0, args.delay),
        same_origin_only=not args.external,
        respect_robots=args.respect_robots,
        max_bytes=max(1024, args.max_bytes),
        skip_js=args.skip_js,
        crawl=args.crawl,
        max_pages=max(1, args.max_pages),
        max_depth=max(0, args.max_depth),
        include=args.include,
        exclude=args.exclude,
        strip_params=not args.no_strip_params,
        allow_params=set(args.allow_param or []),
        sitemap_seed=args.sitemap_seed,
        render_js=args.render_js,
        render_timeout_ms=args.render_timeout_ms,
        wait_until=args.wait_until,
        render_block_media=not args.no_render_block_media,
        global_rps=max(0.01, args.global_rps),
        per_host_rps=max(0.01, args.per_host_rps),
        burst=max(1, args.burst),
        jitter=max(0.0, min(1.0, args.jitter)),
        max_backoff=max(1.0, args.max_backoff),
        auto_throttle=not args.no_auto_throttle,
    )

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    print("Reminder: only clone content you own or have permission to copy.")

    if settings.crawl:
        mirror_site(args.url, args.output_folder, settings)
    else:
        clone_single_page(args.url, args.output_folder, settings)


if __name__ == "__main__":
    main()
