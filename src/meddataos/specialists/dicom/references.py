"""Search and read an allowlist of radiology reference sites.

Sites come from ``configs/dicom_references.toml``. Each site's index is built from
its sitemap, or failing that from the links on its home page and section pages,
and cached on disk. Searches run on that local index, so queries never leave the
machine. Only pages on an allowlisted site are fetched.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup, Tag

from meddataos.datactrl.utils.notes_index import tokenize
from meddataos.specialists.common import CACHE_DIR, REPO

CONFIG = REPO / "configs" / "dicom_references.toml"
CACHE = CACHE_DIR / "dicom" / "references"
USER_AGENT = "MedDataOS-reference-reader/0.1"
INDEX_TTL = 7 * 24 * 3600
MAX_PAGES = 600
_DROP_TAGS = ["script", "style", "nav", "header", "footer", "noscript", "form", "svg"]


@dataclass
class Site:
    name: str
    base_url: str
    sitemap: str | None = None
    max_depth: int = 0

    @property
    def host(self) -> str:
        return urlparse(self.base_url).netloc.lower()


@dataclass
class Page:
    url: str
    title: str
    section: str = ""
    headings: list[str] = field(default_factory=list)

    def tokens(self) -> list[str]:
        slug = urlparse(self.url).path.replace("-", " ").replace("/", " ")
        text = " ".join([self.title, self.title, self.section, slug, *self.headings])
        return tokenize(text)


def load_sites(path: Path = CONFIG) -> list[Site]:
    """The configured reference sites."""
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text())
    return [
        Site(
            name=entry["name"],
            base_url=entry["base_url"],
            sitemap=entry.get("sitemap"),
            max_depth=int(entry.get("max_depth", 0)),
        )
        for entry in data.get("site", [])
    ]


class References:
    """Index, search and read the allowlisted sites."""

    def __init__(
        self,
        sites: list[Site] | None = None,
        cache: Path = CACHE,
        client: httpx.Client | None = None,
    ) -> None:
        self.sites = load_sites() if sites is None else sites
        self.cache = cache
        self.client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=20.0,
            follow_redirects=True,
        )
        self._lock = threading.Lock()
        self._indexes: dict[str, list[Page]] = {}

    # ------------------------------------------------------------------
    # Allowlist
    # ------------------------------------------------------------------

    def site_for(self, url: str) -> Site | None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        host = parsed.netloc.lower()
        for site in self.sites:
            if (
                host == site.host
                or host == f"www.{site.host}"
                or site.host == f"www.{host}"
            ):
                return site
        return None

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self, query: str, site: str | None = None, limit: int = 5
    ) -> dict[str, Any]:
        """BM25 over page titles, sections, URL slugs and known headings."""
        sites = [s for s in self.sites if site in (None, "", s.name)]
        if not sites:
            names = ", ".join(s.name for s in self.sites) or "none configured"
            return {"error": f"Unknown site {site!r}. Sites: {names}"}

        pages: list[Page] = []
        for entry in sites:
            pages.extend(self.index(entry))

        hits = _bm25(pages, query, max(1, min(int(limit or 5), 20)))
        return {
            "query": query,
            "results": [
                {
                    "url": page.url,
                    "title": page.title,
                    "section": page.section,
                    "score": score,
                }
                for page, score in hits
            ],
        }

    def index(self, site: Site) -> list[Page]:
        """Pages of ``site``, from the in-memory or disk cache or freshly built."""
        with self._lock:
            if site.name in self._indexes:
                return self._indexes[site.name]
            path = self.cache / f"{_slug(site.name)}.json"
            pages: list[Page] | None = None
            if path.exists() and time.time() - path.stat().st_mtime < INDEX_TTL:
                pages = [Page(**row) for row in json.loads(path.read_text())]
            if not pages:
                pages = self._build_index(site)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps([page.__dict__ for page in pages]))
            self._indexes[site.name] = pages
            return pages

    def _build_index(self, site: Site) -> list[Page]:
        found: dict[str, Page] = {}
        if site.sitemap:
            for url in self._sitemap_urls(site.sitemap):
                if self.site_for(url) is site:
                    found.setdefault(url, Page(url=url, title=_title_from_url(url)))
        if not found:
            self._crawl_links(site, found)
        return list(found.values())[:MAX_PAGES]

    def _sitemap_urls(self, url: str) -> list[str]:
        try:
            response = self.client.get(url)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
        except (httpx.HTTPError, ElementTree.ParseError):
            return []
        return [
            el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text
        ]

    def _crawl_links(self, site: Site, found: dict[str, Page]) -> None:
        frontier = [site.base_url]
        seen: set[str] = set()
        for _ in range(site.max_depth + 1):
            next_frontier: list[str] = []
            for url in frontier:
                if url in seen or len(found) >= MAX_PAGES:
                    continue
                seen.add(url)
                html = self._get(url)
                if html is None:
                    continue
                for link, text in _links(html, url):
                    if self.site_for(link) is not site or _is_asset(link):
                        continue
                    if link not in found:
                        found[link] = Page(
                            url=link,
                            title=text or _title_from_url(link),
                            section=_section_from_url(link),
                        )
                        next_frontier.append(link)
            frontier = next_frontier

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read(
        self, url: str, section: str | None = None, max_chars: int = 8000
    ) -> dict[str, Any]:
        """Main text of one allowlisted page, optionally from one heading on."""
        url = urldefrag(url.strip())[0]
        site = self.site_for(url)
        if site is None:
            allowed = ", ".join(s.base_url for s in self.sites)
            return {"error": f"Not an allowlisted reference site. Allowed: {allowed}"}

        html = self._get(url, cache=True)
        if html is None:
            return {"error": f"Could not fetch {url}"}

        title, headings, blocks = extract(html)
        self._remember_headings(site, url, title, headings)

        text = "\n".join(blocks)
        if section:
            wanted = section.strip().lower()
            for index, block in enumerate(blocks):
                if block.startswith("#") and wanted in block.lower():
                    text = "\n".join(blocks[index:])
                    break
        max_chars = max(500, min(int(max_chars or 8000), 30000))
        return {
            "url": url,
            "site": site.name,
            "title": title,
            "headings": headings,
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
        }

    def _remember_headings(
        self, site: Site, url: str, title: str, headings: list[str]
    ) -> None:
        pages = self._indexes.get(site.name)
        if pages is None:
            return
        for page in pages:
            if page.url == url:
                page.headings = headings
                if title:
                    page.title = title
                return

    def _get(self, url: str, cache: bool = False) -> str | None:
        path = self.cache / "pages" / (_slug(url) + ".html")
        if cache and path.exists() and time.time() - path.stat().st_mtime < INDEX_TTL:
            return path.read_text(errors="replace")
        try:
            response = self.client.get(url)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        if "html" not in response.headers.get("content-type", "html"):
            return None
        if cache:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(response.text)
        return response.text


# ----------------------------------------------------------------------
# HTML helpers
# ----------------------------------------------------------------------


def extract(html: str) -> tuple[str, list[str], list[str]]:
    """Title, headings and text blocks of a page's main content.

    The main content is the element with the most paragraph text whose text is
    mostly not links, which skips the repeated site navigation.
    """
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    title = re.sub(r"^The Radiology Assistant\s*:\s*", "", title)
    for tag in soup(_DROP_TAGS):
        tag.decompose()

    best: Tag | None = None
    best_score = 0
    for element in soup.find_all(["main", "article", "section", "div"]):
        total = len(element.get_text(" ", strip=True))
        if not total:
            continue
        link_chars = sum(
            len(a.get_text(" ", strip=True)) for a in element.find_all("a")
        )
        if link_chars / total > 0.3:
            continue
        score = sum(len(p.get_text(" ", strip=True)) for p in element.find_all("p"))
        if score > best_score:
            best, best_score = element, score
    root = best or soup.body or soup

    headings: list[str] = []
    blocks: list[str] = []
    for element in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "td"]):
        if element.find_parent(["p", "li", "td"]) is not None:
            continue
        text = element.get_text(" ", strip=True)
        if not text or text in ("Enable Scroll", "Disable Scroll"):
            continue
        if element.name in ("h1", "h2", "h3", "h4"):
            level = int(element.name[1])
            headings.append(text)
            blocks.append(f"{'#' * level} {text}")
        elif element.name == "li":
            blocks.append(f"- {text}")
        else:
            blocks.append(text)
    return title, headings, blocks


def _links(html: str, base: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        url = urldefrag(urljoin(base, href))[0].split("?")[0].rstrip("/")
        out.append((url, anchor.get_text(" ", strip=True)))
    return out


def _is_asset(url: str) -> bool:
    path = urlparse(url).path.lower()
    return "/site/" in path or bool(
        re.search(r"\.(png|jpe?g|gif|svg|ico|css|js|json|xml|pdf|mp4|zip)$", path)
    )


def _title_from_url(url: str) -> str:
    last = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return last.replace("-", " ").strip().capitalize() or url


def _section_from_url(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    return " / ".join(p.replace("-", " ") for p in parts[:-1])


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:150]


def _bm25(pages: list[Page], query: str, limit: int) -> list[tuple[Page, float]]:
    docs = [page.tokens() for page in pages]
    if not docs:
        return []
    terms = list(dict.fromkeys(tokenize(query)))
    df: Counter[str] = Counter()
    for tokens in docs:
        df.update(set(tokens))
    avgdl = sum(len(tokens) for tokens in docs) / len(docs) or 1.0
    k1, b = 1.4, 0.75
    scored = []
    for page, tokens in zip(pages, docs, strict=True):
        counts = Counter(tokens)
        dl = len(tokens) or 1
        score = 0.0
        for term in terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + (len(docs) - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))
        if score > 0:
            scored.append((page, round(score, 3)))
    scored.sort(key=lambda item: -item[1])
    return scored[:limit]
