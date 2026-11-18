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

# Page chrome that is never article text.
NON_CONTENT_TAGS = [
    "script",
    "style",
    "nav",
    "header",
    "footer",
    "noscript",
    "form",
    "svg",
]
HEADING_TAGS = ("h1", "h2", "h3", "h4")
TEXT_BLOCK_TAGS = ("p", "li", "td")

# An element whose text is more than this share links is a menu, not the article.
# Radiology Assistant repeats its whole site menu on every page.
MAX_LINK_TEXT_SHARE = 0.3

# Okapi BM25 tuning: how fast repeated terms stop adding score, and how much
# long pages are penalised. These are the usual defaults.
BM25_TERM_SATURATION = 1.4
BM25_LENGTH_NORMALISATION = 0.75


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
        url_words = urlparse(self.url).path.replace("-", " ").replace("/", " ")

        # The title is listed twice so a title match outranks a heading match.
        text = " ".join(
            [self.title, self.title, self.section, url_words, *self.headings]
        )
        return tokenize(text)


def load_sites(path: Path = CONFIG) -> list[Site]:
    """The configured reference sites."""
    if not path.exists():
        return []

    config = tomllib.loads(path.read_text())
    return [
        Site(
            name=site_config["name"],
            base_url=site_config["base_url"],
            sitemap=site_config.get("sitemap"),
            max_depth=int(site_config.get("max_depth", 0)),
        )
        for site_config in config.get("site", [])
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

        # Tool calls run in worker threads; the lock stops two of them building
        # the same index at once.
        self._index_lock = threading.Lock()
        self._indexes: dict[str, list[Page]] = {}

    # ------------------------------------------------------------------
    # Allowlist
    # ------------------------------------------------------------------

    def site_for(self, url: str) -> Site | None:
        """The allowlisted site that ``url`` belongs to, or None."""
        parsed_url = urlparse(url)
        if parsed_url.scheme not in ("http", "https"):
            return None

        host = parsed_url.netloc.lower()
        for site in self.sites:
            # Treat example.org and www.example.org as the same site.
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
        chosen_sites = [
            candidate for candidate in self.sites if site in (None, "", candidate.name)
        ]
        if not chosen_sites:
            site_names = (
                ", ".join(candidate.name for candidate in self.sites)
                or "none configured"
            )
            return {"error": f"Unknown site {site!r}. Sites: {site_names}"}

        pages: list[Page] = []
        for chosen_site in chosen_sites:
            pages.extend(self.index(chosen_site))

        result_limit = max(1, min(int(limit or 5), 20))
        ranked_pages = _bm25(pages, query, result_limit)

        return {
            "query": query,
            "results": [
                {
                    "url": page.url,
                    "title": page.title,
                    "section": page.section,
                    "score": score,
                }
                for page, score in ranked_pages
            ],
        }

    def index(self, site: Site) -> list[Page]:
        """Pages of ``site``, from the in-memory or disk cache or freshly built."""
        with self._index_lock:
            if site.name in self._indexes:
                return self._indexes[site.name]

            index_path = self.cache / f"{_slug(site.name)}.json"
            pages: list[Page] | None = None
            if _is_fresh(index_path):
                pages = [Page(**saved) for saved in json.loads(index_path.read_text())]

            if not pages:
                pages = self._build_index(site)
                index_path.parent.mkdir(parents=True, exist_ok=True)
                index_path.write_text(json.dumps([page.__dict__ for page in pages]))

            self._indexes[site.name] = pages
            return pages

    def _build_index(self, site: Site) -> list[Page]:
        pages_by_url: dict[str, Page] = {}

        if site.sitemap:
            for url in self._sitemap_urls(site.sitemap):
                if self.site_for(url) is site:
                    pages_by_url.setdefault(
                        url, Page(url=url, title=_title_from_url(url))
                    )

        # Fall back to crawling when there is no sitemap or it listed nothing.
        if not pages_by_url:
            self._crawl_links(site, pages_by_url)

        return list(pages_by_url.values())[:MAX_PAGES]

    def _sitemap_urls(self, sitemap_url: str) -> list[str]:
        try:
            response = self.client.get(sitemap_url)
            response.raise_for_status()
            sitemap = ElementTree.fromstring(response.content)
        except (httpx.HTTPError, ElementTree.ParseError):
            return []

        # Match on the tag suffix: sitemap tags carry an XML namespace prefix.
        return [
            element.text.strip()
            for element in sitemap.iter()
            if element.tag.endswith("loc") and element.text
        ]

    def _crawl_links(self, site: Site, pages_by_url: dict[str, Page]) -> None:
        """Breadth-first crawl from the home page, ``max_depth`` levels deep."""
        urls_to_visit = [site.base_url]
        visited: set[str] = set()

        for _depth in range(site.max_depth + 1):
            next_urls: list[str] = []

            for url in urls_to_visit:
                if url in visited or len(pages_by_url) >= MAX_PAGES:
                    continue
                visited.add(url)

                html = self._get(url)
                if html is None:
                    continue

                for link_url, link_text in _links(html, url):
                    if self.site_for(link_url) is not site or _is_asset(link_url):
                        continue
                    if link_url in pages_by_url:
                        continue

                    # The link text is usually the article title, which saves
                    # fetching every page just to name it.
                    pages_by_url[link_url] = Page(
                        url=link_url,
                        title=link_text or _title_from_url(link_url),
                        section=_section_from_url(link_url),
                    )
                    next_urls.append(link_url)

            urls_to_visit = next_urls

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
            allowed_urls = ", ".join(candidate.base_url for candidate in self.sites)
            return {
                "error": f"Not an allowlisted reference site. Allowed: {allowed_urls}"
            }

        html = self._get(url, cache=True)
        if html is None:
            return {"error": f"Could not fetch {url}"}

        title, headings, blocks = extract(html)
        self._remember_headings(site, url, title, headings)

        text = "\n".join(blocks)
        if section:
            wanted_heading = section.strip().lower()
            for block_index, block in enumerate(blocks):
                if block.startswith("#") and wanted_heading in block.lower():
                    text = "\n".join(blocks[block_index:])
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
        """Add a read page's headings to the index, so later searches match them.

        The crawl only sees link text; headings are learned as pages are read.
        """
        pages = self._indexes.get(site.name)
        if pages is None:
            return

        for page in pages:
            if page.url != url:
                continue

            page.headings = headings
            if title:
                page.title = title
            return

    def _get(self, url: str, cache: bool = False) -> str | None:
        """HTML of ``url``, or None if it fails or is not HTML."""
        cached_path = self.cache / "pages" / (_slug(url) + ".html")
        if cache and _is_fresh(cached_path):
            return cached_path.read_text(errors="replace")

        try:
            response = self.client.get(url)
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        if "html" not in response.headers.get("content-type", "html"):
            return None

        if cache:
            cached_path.parent.mkdir(parents=True, exist_ok=True)
            cached_path.write_text(response.text)
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
    for tag in soup(NON_CONTENT_TAGS):
        tag.decompose()

    content_root = _main_content(soup)

    headings: list[str] = []
    blocks: list[str] = []
    for element in content_root.find_all([*HEADING_TAGS, *TEXT_BLOCK_TAGS]):
        # A block inside another block would be emitted twice.
        if element.find_parent(list(TEXT_BLOCK_TAGS)) is not None:
            continue

        text = element.get_text(" ", strip=True)
        # Labels of the site's image-viewer buttons, not article text.
        if not text or text in ("Enable Scroll", "Disable Scroll"):
            continue

        if element.name in HEADING_TAGS:
            heading_level = int(element.name[1])
            headings.append(text)
            blocks.append(f"{'#' * heading_level} {text}")
        elif element.name == "li":
            blocks.append(f"- {text}")
        else:
            blocks.append(text)

    return title, headings, blocks


def _main_content(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    """The element with the most paragraph text that is not mostly links."""
    best_element: Tag | None = None
    best_paragraph_chars = 0

    for element in soup.find_all(["main", "article", "section", "div"]):
        total_chars = len(element.get_text(" ", strip=True))
        if not total_chars:
            continue

        link_chars = sum(
            len(anchor.get_text(" ", strip=True)) for anchor in element.find_all("a")
        )
        if link_chars / total_chars > MAX_LINK_TEXT_SHARE:
            continue

        paragraph_chars = sum(
            len(paragraph.get_text(" ", strip=True))
            for paragraph in element.find_all("p")
        )
        if paragraph_chars > best_paragraph_chars:
            best_element, best_paragraph_chars = element, paragraph_chars

    return best_element or soup.body or soup


def _links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(absolute URL, link text) for each link on the page."""
    soup = BeautifulSoup(html, "html.parser")
    links = []

    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if href.startswith(("mailto:", "javascript:", "tel:")):
            continue

        # Drop the fragment, query and trailing slash so one page has one URL.
        absolute_url = urldefrag(urljoin(base_url, href))[0].split("?")[0].rstrip("/")
        links.append((absolute_url, anchor.get_text(" ", strip=True)))

    return links


def _is_asset(url: str) -> bool:
    url_path = urlparse(url).path.lower()

    # /site/ holds Radiology Assistant's theme files.
    return "/site/" in url_path or bool(
        re.search(r"\.(png|jpe?g|gif|svg|ico|css|js|json|xml|pdf|mp4|zip)$", url_path)
    )


def _title_from_url(url: str) -> str:
    last_segment = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return last_segment.replace("-", " ").strip().capitalize() or url


def _section_from_url(url: str) -> str:
    """The folders above a page, e.g. ``head neck / cervical node mapping``."""
    path_segments = [segment for segment in urlparse(url).path.split("/") if segment]
    return " / ".join(segment.replace("-", " ") for segment in path_segments[:-1])


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:150]


def _is_fresh(path: Path) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < INDEX_TTL


def _bm25(pages: list[Page], query: str, limit: int) -> list[tuple[Page, float]]:
    """Rank ``pages`` against ``query`` with Okapi BM25; best ``limit`` first."""
    page_tokens = [page.tokens() for page in pages]
    if not page_tokens:
        return []

    query_terms = list(dict.fromkeys(tokenize(query)))  # unique, in order

    # How many pages contain each term: rare terms count for more.
    pages_containing: Counter[str] = Counter()
    for tokens in page_tokens:
        pages_containing.update(set(tokens))

    page_count = len(page_tokens)
    average_page_length = sum(len(tokens) for tokens in page_tokens) / page_count or 1.0

    ranked: list[tuple[Page, float]] = []
    for page, tokens in zip(pages, page_tokens, strict=True):
        term_counts = Counter(tokens)
        page_length = len(tokens) or 1
        length_factor = (
            1
            - BM25_LENGTH_NORMALISATION
            + BM25_LENGTH_NORMALISATION * page_length / average_page_length
        )

        score = 0.0
        for term in query_terms:
            term_count = term_counts.get(term, 0)
            if not term_count:
                continue

            containing = pages_containing[term]
            rarity = math.log(1 + (page_count - containing + 0.5) / (containing + 0.5))
            score += (
                rarity
                * term_count
                * (BM25_TERM_SATURATION + 1)
                / (term_count + BM25_TERM_SATURATION * length_factor)
            )

        if score > 0:
            ranked.append((page, round(score, 3)))

    ranked.sort(key=lambda ranked_page: -ranked_page[1])
    return ranked[:limit]
