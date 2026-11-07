"""Source extraction (Phase 3).

Turns a public Substack URL (including custom publication domains), user-supplied HTML, or
plain text into a source-agnostic :class:`Article`.

Design notes
------------
* Trafilatura is the primary body extractor; readability-lxml is the fallback. BeautifulSoup
  handles metadata, pre-cleaning and paywall detection.
* The extractor never authenticates, never sends cookies and never tries to reach content
  behind a paywall. If only a preview is available the article is flagged
  :attr:`Completeness.PARTIAL` and says why. It is never reported as complete in that case.
* :class:`ArticleExtractor` is the interface that future extractors (PDF, Medium, research
  papers, YouTube transcripts) will implement.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Iterable, Optional, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
import trafilatura
from bs4 import BeautifulSoup, NavigableString, Tag
from lxml import etree
from readability import Document

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "NoteForgeAI/0.1 (personal study-notes tool)"
MAX_RESPONSE_BYTES = 5_000_000
MAX_REDIRECTS = 5

#: Below this many words the article is reported as possibly truncated.
SHORT_ARTICLE_WORDS = 120
#: Below this many words an extraction is considered to have failed.
MIN_USABLE_WORDS = 40
#: If an extractor keeps less than this share of the body text, try the fallback.
MIN_RECALL_RATIO = 0.6


# --------------------------------------------------------------------------- exceptions
class ExtractionError(Exception):
    """Base class for all extraction failures."""


class InvalidSourceError(ExtractionError):
    """The supplied URL, HTML or text is invalid or empty."""


class FetchError(ExtractionError):
    """The page could not be downloaded (network error, 404, 5xx, rate limit...)."""


class ArticleInaccessibleError(ExtractionError):
    """The article requires authentication or is otherwise not publicly accessible."""


class PaywallError(ArticleInaccessibleError):
    """The article is paywalled and no public content could be extracted."""


class NoContentError(ExtractionError):
    """The page was fetched but no article content could be extracted."""


# --------------------------------------------------------------------------- data model
class Completeness(str, Enum):
    """How much of the article is known to have been captured."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"  # user-supplied text: completeness cannot be checked


class BlockKind(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    CODE = "code"
    QUOTE = "quote"


@dataclass(frozen=True)
class ListItem:
    text: str
    depth: int = 0
    #: Set only when this item's own list type differs from the top-level list's (a numbered sub-list inside a
    #: bullet list, or the reverse). ``None`` means "same kind as the block".
    ordered: Optional[bool] = None


@dataclass(frozen=True)
class Block:
    """One structural element of the article, in reading order."""

    kind: BlockKind
    text: str = ""
    level: int = 0  # heading level 1-6
    items: tuple[ListItem, ...] = ()
    ordered: bool = False
    rows: tuple[tuple[str, ...], ...] = ()
    has_header: bool = False
    language: Optional[str] = None  # code blocks

    @property
    def plain_text(self) -> str:
        if self.kind is BlockKind.LIST:
            return "\n".join(i.text for i in self.items)
        if self.kind is BlockKind.TABLE:
            return "\n".join(" | ".join(r) for r in self.rows)
        return self.text

    @property
    def word_count(self) -> int:
        return len(self.plain_text.split())


@dataclass
class Article:
    title: str
    blocks: list[Block]
    completeness: Completeness
    author: Optional[str] = None
    published: Optional[date] = None
    url: Optional[str] = None
    site_name: Optional[str] = None
    source_kind: str = "url"  # url | user_html | user_text
    extractor: str = ""  # trafilatura | readability | text
    completeness_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return sum(b.word_count for b in self.blocks)

    @property
    def is_complete(self) -> bool:
        return self.completeness is Completeness.COMPLETE

    @property
    def text(self) -> str:
        return "\n\n".join(b.plain_text for b in self.blocks if b.plain_text)


@dataclass(frozen=True)
class Metadata:
    title: Optional[str] = None
    author: Optional[str] = None
    published: Optional[date] = None
    site_name: Optional[str] = None
    accessible_for_free: Optional[bool] = None


@dataclass(frozen=True)
class FetchedPage:
    url: str  # final URL after redirects
    html: str
    status: int = 200


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchedPage: ...


# --------------------------------------------------------------------------- URL handling
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost"}
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home.arpa")
_POST_PATH = re.compile(r"(^|/)p/[^/]+|/home/post/p-\d+", re.IGNORECASE)
_LOGIN_PATH = re.compile(r"/(sign-?in|log-?in|account/login)\b", re.IGNORECASE)


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def validate_url(url: str, *, allow_private_hosts: bool = False) -> str:
    """Validate and normalize a URL. Returns the https URL without fragment.

    Raises :class:`InvalidSourceError` for non-http(s) schemes, embedded credentials,
    missing hosts, and localhost / private-network targets.
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidSourceError("URL is empty.")
    raw = url.strip()
    if any(c.isspace() for c in raw):
        raise InvalidSourceError("URL must not contain whitespace.")
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        raise InvalidSourceError(f"Unsupported URL scheme {parts.scheme!r}; use https.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise InvalidSourceError("URL has no host name.")
    if parts.username or parts.password:
        raise InvalidSourceError("URLs with embedded credentials are not accepted.")
    if not allow_private_hosts:
        if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
            raise InvalidSourceError(f"Host {host!r} is not a public site.")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host:
                raise InvalidSourceError(f"Host {host!r} is not a public site.") from None
        else:
            if not _is_public_ip(ip):
                raise InvalidSourceError(f"Address {host!r} is not a public address.")
    scheme = "http" if (allow_private_hosts and parts.scheme.lower() == "http") else "https"
    return urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))


def is_post_url(url: str) -> bool:
    """True if the URL path looks like a Substack post (``/p/<slug>``)."""
    return bool(_POST_PATH.search(urlsplit(url).path))


def _ensure_public_host(host: str) -> None:
    """Resolve ``host`` and refuse private targets (DNS-rebinding / redirect safety)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchError(f"Could not resolve host {host!r}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not _is_public_ip(ip):
            raise InvalidSourceError(f"Host {host!r} resolves to a non-public address.")


class HttpFetcher:
    """Plain, unauthenticated HTTP GET with redirect validation and a size cap.

    No cookies, no login, no custom headers beyond a polite User-Agent.
    """

    def __init__(
        self,
        timeout: float = 20.0,
        user_agent: str = DEFAULT_USER_AGENT,
        max_bytes: int = MAX_RESPONSE_BYTES,
        max_redirects: int = MAX_REDIRECTS,
        allow_private_hosts: bool = False,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.allow_private_hosts = allow_private_hosts
        self._session = session or requests.Session()

    def fetch(self, url: str) -> FetchedPage:
        current = validate_url(url, allow_private_hosts=self.allow_private_hosts)
        headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml"}
        for _ in range(self.max_redirects + 1):
            if not self.allow_private_hosts:
                _ensure_public_host(urlsplit(current).hostname or "")
            logger.info("Fetching %s", current)
            try:
                resp = self._session.get(
                    current, headers=headers, timeout=self.timeout,
                    allow_redirects=False, stream=True,
                )
            except requests.RequestException as exc:
                raise FetchError(f"Network error while fetching {current}: {exc}") from exc
            try:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                    current = validate_url(
                        urljoin(current, resp.headers["Location"]),
                        allow_private_hosts=self.allow_private_hosts,
                    )
                    if _LOGIN_PATH.search(urlsplit(current).path):
                        raise ArticleInaccessibleError(
                            "The site redirected to a sign-in page; the article requires login."
                        )
                    continue
                return self._read(resp, current)
            finally:
                resp.close()
        raise FetchError(f"Too many redirects (more than {self.max_redirects}).")

    def _read(self, resp: requests.Response, url: str) -> FetchedPage:
        status = resp.status_code
        if status in (401, 403):
            raise ArticleInaccessibleError(
                f"Access denied (HTTP {status}); the article is not publicly accessible."
            )
        if status in (404, 410):
            raise FetchError(f"Article not found (HTTP {status}).")
        if status == 429:
            raise FetchError("Rate limited by the site (HTTP 429); try again later.")
        if status >= 400:
            raise FetchError(f"Server returned HTTP {status}.")
        ctype = resp.headers.get("Content-Type", "")
        if ctype and "html" not in ctype.lower():
            raise FetchError(f"Expected an HTML page but received {ctype!r}.")
        chunks: list[bytes] = []
        size = 0
        for chunk in resp.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > self.max_bytes:
                raise FetchError(f"Response larger than {self.max_bytes} bytes; refusing to read.")
            chunks.append(chunk)
        match = re.search(r"charset=([\w-]+)", ctype, re.IGNORECASE)
        encoding = match.group(1) if match else "utf-8"
        try:
            html = b"".join(chunks).decode(encoding, errors="replace")
        except LookupError:
            html = b"".join(chunks).decode("utf-8", errors="replace")
        return FetchedPage(url=url, html=html, status=status)


# --------------------------------------------------------------------------- metadata
_ARTICLE_TYPES = {"article", "newsarticle", "blogposting", "socialmediaposting", "techarticle"}


def parse_date(value: Optional[str]) -> Optional[date]:
    """Parse ISO-8601 and a few common human formats. Returns None if unparseable."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        try:
            return date.fromisoformat(match.group(0))
        except ValueError:
            return None
    return None


def _json_ld_items(soup: BeautifulSoup) -> Iterable[dict[str, Any]]:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError):
            logger.debug("Ignoring malformed JSON-LD block")
            continue
        stack: list[Any] = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@graph" in node:
                    stack.append(node["@graph"])
                yield node


def _json_ld_article(soup: BeautifulSoup) -> Optional[dict[str, Any]]:
    for node in _json_ld_items(soup):
        types = node.get("@type")
        names = [types] if isinstance(types, str) else (types if isinstance(types, list) else [])
        if any(isinstance(t, str) and t.lower() in _ARTICLE_TYPES for t in names):
            return node
    return None


def _person_names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        return _person_names(value.get("name"))
    if isinstance(value, list):
        names: list[str] = []
        for v in value:
            names.extend(_person_names(v))
        return names
    return []


def _meta(soup: BeautifulSoup, *keys: str) -> Optional[str]:
    for key in keys:
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if isinstance(tag, Tag):
            content = tag.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return None


def _clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()


def _headline_is_site_tagline(headline: str, soup: BeautifulSoup) -> bool:
    """D-1: is a JSON-LD ``headline`` the site's generic tagline rather than this article's title?

    Some older pages put the publication tagline ("Subscription platform for online creators - Substack")
    in ``headline`` while ``og:title`` holds the real title. The headline is only distrusted when
    ``og:title`` exists, differs from it, and the headline either equals the page ``<title>`` (which
    on such pages is the same tagline), equals the site name, or carries the site name as a suffix
    that ``og:title`` does not.
    """
    og_title = _meta(soup, "og:title")
    if not og_title:
        return False
    head, og = _clean_title(headline).casefold(), _clean_title(og_title).casefold()
    if head == og:
        return False
    page_title = _clean_title(soup.title.string) if soup.title and soup.title.string else ""
    if page_title and page_title.casefold() == head:
        return True
    site = _meta(soup, "og:site_name")
    if site:
        site_key = _clean_title(site).casefold()
        if head == site_key:
            return True
        suffix = re.compile(rf"\s[|–—-]\s+{re.escape(site_key)}\s*$")
        if suffix.search(head) and not suffix.search(og):
            return True
    return False


def extract_metadata(soup: BeautifulSoup) -> Metadata:
    """Title, author, date and access flag from JSON-LD, meta tags and the DOM."""
    ld = _json_ld_article(soup) or {}

    headline = ld.get("headline") if isinstance(ld.get("headline"), str) and ld.get("headline").strip() else None
    if headline and _headline_is_site_tagline(headline, soup):
        headline = None  # D-1: fall back to og:title
    title = headline or _meta(soup, "og:title", "twitter:title")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else None
    if not title and soup.title and soup.title.string:
        title = re.split(r"\s+[|–—-]\s+", soup.title.string.strip())[0]

    names = _person_names(ld.get("author"))
    if not names:
        meta_author = _meta(soup, "author", "article:author")
        if meta_author:
            names = [meta_author]
    if not names:
        rel = soup.find("a", attrs={"rel": "author"})
        if rel and rel.get_text(strip=True):
            names = [rel.get_text(strip=True)]
    author = ", ".join(dict.fromkeys(names)) or None

    published = parse_date(ld.get("datePublished") if isinstance(ld.get("datePublished"), str) else None)
    if not published:
        published = parse_date(_meta(soup, "article:published_time", "date", "datePublished"))
    if not published:
        time_tag = soup.find("time", attrs={"datetime": True})
        if isinstance(time_tag, Tag):
            published = parse_date(str(time_tag.get("datetime")))

    free_raw = ld.get("isAccessibleForFree")
    free: Optional[bool] = None
    if isinstance(free_raw, bool):
        free = free_raw
    elif isinstance(free_raw, str) and free_raw.lower() in ("true", "false"):
        free = free_raw.lower() == "true"

    return Metadata(
        title=_clean_title(title) if title else None,
        author=author,
        published=published,
        site_name=_meta(soup, "og:site_name"),
        accessible_for_free=free,
    )


# --------------------------------------------------------------------------- paywall / page checks
_PAYWALL_CLASS = re.compile(r"paywall", re.IGNORECASE)
_PAYWALL_PHRASES = re.compile(
    r"(this post is for (paid )?subscribers|this post is for paying subscribers"
    r"|subscribe to continue reading|keep reading with a .{0,30}free trial"
    r"|upgrade to paid to (keep|continue) reading|read the rest of this post"
    r"|to read the rest of this post|unlock this post|become a paid subscriber to (read|continue))",
    re.IGNORECASE,
)


def detect_paywall(soup: BeautifulSoup, metadata: Optional[Metadata] = None) -> list[str]:
    """Return human-readable reasons the page is paywalled (empty list if none found).

    Structural markers and short call-to-action phrases are used; long prose paragraphs
    are ignored so an article that merely *discusses* paywalls is not misclassified.
    """
    reasons: list[str] = []
    if metadata is not None and metadata.accessible_for_free is False:
        reasons.append("Page metadata declares the article is not free to access.")
    if soup.find(attrs={"data-component-name": re.compile("paywall", re.IGNORECASE)}):
        reasons.append("Paywall component present in the page.")
    elif soup.find(class_=_PAYWALL_CLASS):
        reasons.append("Paywall element present in the page.")
    for tag in soup.find_all(["div", "p", "h2", "h3", "h4", "a", "button", "span"]):
        if not isinstance(tag, Tag):
            continue
        own = tag.get_text(" ", strip=True)
        if 0 < len(own) <= 200 and _PAYWALL_PHRASES.search(own):
            reasons.append(f"Subscriber call-to-action found: {own[:80]!r}.")
            break
    return reasons


def _looks_like_substack(soup: BeautifulSoup, html: str) -> bool:
    """Heuristic: Substack pages (including custom domains) reference its CDN or body markup."""
    if "substackcdn.com" in html or "substack.com" in html:
        return True
    return soup.select_one("div.available-content, div.body.markup") is not None


def _login_wall(soup: BeautifulSoup) -> bool:
    if soup.find("input", attrs={"type": "password"}):
        return True
    text = soup.get_text(" ", strip=True).lower()
    return len(text.split()) < 150 and bool(re.search(r"\b(sign in|log in)\b", text)) and not soup.find("article")


# --------------------------------------------------------------------------- pre-cleaning
_NOISE_TAGS = ("script", "style", "noscript", "nav", "footer", "aside", "form", "iframe",
               "svg", "button", "select", "template", "figure", "picture", "img", "video", "audio")
_NOISE_CLASS = re.compile(
    r"subscription-widget|subscribe-widget|subscribe-prompt|captioned-button|button-wrapper"
    r"|share-dialog|post-ufi|post-footer|comments-section|paywall|footnote-anchor|footnote-number"
    r"|header-anchor-widget|pencraft-hidden|recommendations|embedded-post|related-posts|cookie|advert|sponsor",
    re.IGNORECASE,
)


def _clean_soup(soup: BeautifulSoup) -> BeautifulSoup:
    """Remove navigation, widgets, ads and paywall/CTA elements (works on a copy)."""
    clean = BeautifulSoup(str(soup), "lxml")
    for tag in clean.find_all(_NOISE_TAGS):
        tag.decompose()
    doomed = [
        t for t in clean.find_all(True)
        if isinstance(t, Tag) and not t.decomposed
        and _NOISE_CLASS.search(" ".join(t.get("class") or []))
    ]
    for tag in doomed:
        if not tag.decomposed:
            tag.decompose()
    return clean


def _select_root(clean: BeautifulSoup) -> Optional[Tag]:
    """The element that holds the post body, if it can be identified."""
    for selector in ("div.available-content", "div.body.markup", "div.post-content",
                     "div.markup", "article"):
        found = clean.select_one(selector)
        if found and len(found.get_text(" ", strip=True).split()) >= MIN_USABLE_WORDS:
            return found
    return None


# --------------------------------------------------------------------------- block parsing
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _code_text(text: str) -> str:
    return text.replace("\r\n", "\n").strip("\n").rstrip()


def _code_languages(soup: BeautifulSoup) -> dict[str, str]:
    """Map normalized code text -> language, read from ``language-xxx`` classes."""
    langs: dict[str, str] = {}
    for pre in soup.find_all("pre"):
        holder = pre.find("code") or pre
        for cls in (holder.get("class") or []) + (pre.get("class") or []):
            m = re.match(r"(?:language|lang)-(\w[\w+#.-]*)", cls)
            if m:
                langs[_norm(pre.get_text())] = m.group(1).lower()
                break
    return langs


def _el_text(el: etree._Element) -> str:
    return _norm(" ".join(el.itertext()))


def _mixed_mark(own: bool, top: Optional[bool]) -> Optional[bool]:
    """``own`` only when it differs from the top-level list's type (see :class:`ListItem`)."""
    return None if top is None or own == top else own


def _xml_list_items(list_el: etree._Element, depth: int = 0, top: Optional[bool] = None) -> list[ListItem]:
    items: list[ListItem] = []
    own = list_el.get("rend") == "ol"
    top = own if top is None else top
    for item in list_el.iterchildren("item"):
        parts: list[str] = [item.text or ""]
        nested: list[etree._Element] = []
        for child in item:
            if child.tag == "list":
                nested.append(child)
            else:
                parts.append(" ".join(child.itertext()))
            parts.append(child.tail or "")
        text = _norm(" ".join(parts))
        if text:
            items.append(ListItem(text, depth, _mixed_mark(own, top)))
        for sub in nested:
            items.extend(_xml_list_items(sub, depth + 1, top))
    return items


def blocks_from_trafilatura_xml(xml: str, languages: Optional[dict[str, str]] = None) -> list[Block]:
    """Convert Trafilatura's XML output into :class:`Block` objects."""
    languages = languages or {}
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True)
    root = etree.fromstring(xml.encode("utf-8"), parser)
    main = root.find("main") if root is not None else None
    if main is None:
        return []
    blocks: list[Block] = []
    for el in main:
        tag = el.tag
        if tag == "head":
            text = _el_text(el)
            m = re.match(r"h([1-6])", el.get("rend") or "")
            if text:
                blocks.append(_heading_block(text, int(m.group(1)) if m else 2))
        elif tag == "p":
            text = _el_text(el)
            if text:
                blocks.append(Block(BlockKind.PARAGRAPH, text))
        elif tag == "list":
            items = _xml_list_items(el)
            if items:
                blocks.append(Block(BlockKind.LIST, items=tuple(items), ordered=el.get("rend") == "ol"))
        elif tag == "code":
            text = _code_text("".join(el.itertext()))
            if text:
                blocks.append(Block(BlockKind.CODE, text, language=languages.get(_norm(text))))
        elif tag == "table":
            rows: list[tuple[str, ...]] = []
            header = False
            for r_i, row in enumerate(el.iterchildren("row")):
                cells = tuple(_el_text(c) for c in row.iterchildren("cell"))
                if any(cells):
                    rows.append(cells)
                if r_i == 0 and any(c.get("role") == "head" for c in row.iterchildren("cell")):
                    header = True
            if rows:
                blocks.append(Block(BlockKind.TABLE, rows=tuple(rows), has_header=header))
        elif tag == "quote":
            text = _el_text(el)
            if text:
                blocks.append(Block(BlockKind.QUOTE, text))
        else:
            text = _el_text(el)
            if text:
                blocks.append(Block(BlockKind.PARAGRAPH, text))
    return blocks


def _html_list_items(list_tag: Tag, depth: int = 0, top: Optional[bool] = None) -> list[ListItem]:
    items: list[ListItem] = []
    own = list_tag.name == "ol"
    top = own if top is None else top
    for li in list_tag.find_all("li", recursive=False):
        parts: list[str] = []
        nested: list[Tag] = []
        for child in li.children:
            if isinstance(child, Tag) and child.name in ("ul", "ol"):
                nested.append(child)
            elif isinstance(child, Tag):
                parts.append(child.get_text(" "))
            elif isinstance(child, NavigableString):
                parts.append(str(child))
        text = _norm(" ".join(parts))
        if text:
            items.append(ListItem(text, depth, _mixed_mark(own, top)))
        for sub in nested:
            items.extend(_html_list_items(sub, depth + 1, top))
    return items


_MAX_HEADING_CHARS = 200  # D-9: a real heading is a short label, not a paragraph
_LONG_HEADING_CHARS = 120


def _heading_block(text: str, level: int) -> Block:
    """A heading block, or a paragraph when the "heading" is really body text (D-9).

    Extractors sometimes tag a bold or emphasised paragraph as a heading. Text longer than
    ``_MAX_HEADING_CHARS``, or longer than ``_LONG_HEADING_CHARS`` and made of several sentences,
    is kept word for word but becomes a paragraph, so it no longer appears in the table of contents.
    """
    sentences = len(re.findall(r"[.!?]['\")”]?\s+\S", text)) + 1
    if len(text) > _MAX_HEADING_CHARS or (len(text) > _LONG_HEADING_CHARS and sentences >= 2):
        return Block(BlockKind.PARAGRAPH, text)
    return Block(BlockKind.HEADING, text, level=level)


def blocks_from_html(html: str, languages: Optional[dict[str, str]] = None) -> list[Block]:
    """Convert an HTML fragment into :class:`Block` objects (used for the Readability fallback)."""
    soup = BeautifulSoup(html, "lxml")
    languages = languages or _code_languages(soup)
    blocks: list[Block] = []

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                text = _norm(str(child))
                if text and not child.parent.name in ("script", "style"):
                    blocks.append(Block(BlockKind.PARAGRAPH, text))
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name
            if re.fullmatch(r"h[1-6]", name):
                text = _norm(child.get_text(" "))
                if text:
                    blocks.append(_heading_block(text, int(name[1])))
            elif name == "p":
                text = _norm(child.get_text(" "))
                if text:
                    blocks.append(Block(BlockKind.PARAGRAPH, text))
            elif name in ("ul", "ol"):
                items = _html_list_items(child)
                if items:
                    blocks.append(Block(BlockKind.LIST, items=tuple(items), ordered=name == "ol"))
            elif name == "pre":
                text = _code_text(child.get_text())
                if text:
                    blocks.append(Block(BlockKind.CODE, text, language=languages.get(_norm(text))))
            elif name == "table":
                rows: list[tuple[str, ...]] = []
                for tr in child.find_all("tr"):
                    cells = tuple(_norm(c.get_text(" ")) for c in tr.find_all(["th", "td"]))
                    if any(cells):
                        rows.append(cells)
                first = child.find("tr")
                header = bool(first and (first.find("th") or first.find_parent("thead")))
                if rows:
                    blocks.append(Block(BlockKind.TABLE, rows=tuple(rows), has_header=header))
            elif name == "blockquote":
                text = _norm(child.get_text(" "))
                if text:
                    blocks.append(Block(BlockKind.QUOTE, text))
            elif name in ("script", "style", "hr", "br"):
                continue
            else:  # div, section, article, body, span...
                walk(child)

    body = soup.body or soup
    walk(body)
    return blocks


def _words(blocks: Iterable[Block]) -> int:
    return sum(b.word_count for b in blocks)


def _drop_title_heading(blocks: list[Block], title: str) -> list[Block]:
    """Remove a leading heading that merely repeats the article title."""
    if blocks and blocks[0].kind is BlockKind.HEADING and _norm(blocks[0].text).lower() == _norm(title).lower():
        return blocks[1:]
    return blocks


# --------------------------------------------------------------------------- plain text parsing
_BULLET = re.compile(r"^(\s*)([-*•])\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _split_row(line: str) -> tuple[str, ...]:
    return tuple(c.strip() for c in line.strip().strip("|").split("|"))


def blocks_from_text(text: str) -> list[Block]:
    """Parse plain / light-markdown text into blocks.

    Recognizes ``#`` headings, fenced code, bullet and numbered lists, ``>`` quotes,
    pipe tables, and paragraphs separated by blank lines.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    para: list[str] = []
    i = 0

    def flush_para() -> None:
        if para:
            blocks.append(Block(BlockKind.PARAGRAPH, _norm(" ".join(para))))
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush_para()
            i += 1
            continue
        fence = re.match(r"^\s*```\s*([\w+#.-]*)\s*$", line)
        if fence:
            flush_para()
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # closing fence (or end of input)
            body = _code_text("\n".join(code))
            if body:
                blocks.append(Block(BlockKind.CODE, body, language=(fence.group(1) or None)))
            continue
        heading = _MD_HEADING.match(stripped)
        if heading:
            flush_para()
            blocks.append(Block(BlockKind.HEADING, heading.group(2), level=len(heading.group(1))))
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush_para()
            rows = [_split_row(stripped)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            blocks.append(Block(BlockKind.TABLE, rows=tuple(rows), has_header=True))
            continue
        if _BULLET.match(line) or _NUMBERED.match(line):
            flush_para()
            ordered = bool(_NUMBERED.match(line))
            items: list[ListItem] = []
            while i < len(lines):
                m = _BULLET.match(lines[i]) or _NUMBERED.match(lines[i])
                if not m:
                    break
                items.append(ListItem(_norm(m.group(3)), depth=len(m.group(1).expandtabs(4)) // 2,
                                      ordered=_mixed_mark(bool(_NUMBERED.match(lines[i])), ordered)))
                i += 1
            blocks.append(Block(BlockKind.LIST, items=tuple(items), ordered=ordered))
            continue
        if stripped.startswith(">"):
            flush_para()
            quote: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(Block(BlockKind.QUOTE, _norm(" ".join(quote))))
            continue
        para.append(stripped)
        i += 1
    flush_para()
    return blocks


# --------------------------------------------------------------------------- extractors
class ArticleExtractor(ABC):
    """Interface for all source extractors (Substack now; PDF/Medium/YouTube later)."""

    @abstractmethod
    def extract(self, source: str) -> Article:
        """Extract an :class:`Article` from ``source`` (format is extractor-specific)."""


def _looks_like_url(source: str) -> bool:
    return bool(re.match(r"^\s*https?://\S+\s*$", source, re.IGNORECASE))


def _looks_like_html(source: str) -> bool:
    return bool(re.search(r"<\s*(html|body|article|p|div|h[1-6])[\s>]", source[:5000], re.IGNORECASE))


class SubstackExtractor(ArticleExtractor):
    """Extract articles from public Substack posts, user HTML or plain text."""

    def __init__(self, fetcher: Optional[Fetcher] = None) -> None:
        self._fetcher: Fetcher = fetcher or HttpFetcher()

    # ---- public API
    def extract(self, source: str) -> Article:
        """Dispatch on the shape of ``source``: URL, HTML markup, or plain text."""
        if not isinstance(source, str) or not source.strip():
            raise InvalidSourceError("Nothing to extract: input is empty.")
        if _looks_like_url(source):
            return self.extract_from_url(source.strip())
        if _looks_like_html(source):
            return self.extract_from_html(source)
        return self.extract_from_text(source)

    def extract_from_url(self, url: str) -> Article:
        normalized = validate_url(url)
        if not is_post_url(normalized):
            raise InvalidSourceError(
                "URL does not look like a Substack post (expected a path like /p/<article-slug>)."
            )
        page = self._fetcher.fetch(normalized)
        return self._from_html(page.html, url=page.url, source_kind="url")

    def extract_from_html(self, html: str, url: Optional[str] = None) -> Article:
        """Extract from HTML the user supplied (e.g. 'Save page as' or view-source)."""
        if not isinstance(html, str) or not html.strip():
            raise InvalidSourceError("HTML input is empty.")
        article = self._from_html(html, url=url, source_kind="user_html")
        article.warnings.append(
            "Extracted from user-supplied HTML; completeness reflects only the markup provided."
        )
        return article

    def extract_from_text(
        self,
        text: str,
        title: Optional[str] = None,
        author: Optional[str] = None,
        published: Optional[date] = None,
        url: Optional[str] = None,
    ) -> Article:
        """Build an article from plain text or light markdown the user pasted."""
        if not isinstance(text, str) or not text.strip():
            raise InvalidSourceError("Text input is empty.")
        blocks = blocks_from_text(text)
        if not blocks:
            raise NoContentError("No usable text found in the input.")
        warnings: list[str] = []
        if not title:
            first = blocks[0]
            if first.kind is BlockKind.HEADING:
                title = first.text
                blocks = blocks[1:]
            else:
                title = "Untitled Article"
                warnings.append("No title supplied or detected; using 'Untitled Article'.")
        if author is None:
            warnings.append("Author not supplied.")
        if published is None:
            warnings.append("Publication date not supplied.")
        if not blocks:
            raise NoContentError("Only a title was found; no article body.")
        return Article(
            title=title, blocks=blocks, author=author, published=published, url=url,
            completeness=Completeness.UNVERIFIED, source_kind="user_text", extractor="text",
            completeness_reasons=["Plain text supplied by the user; completeness cannot be verified."],
            warnings=warnings,
        )

    # ---- pipeline
    def _from_html(self, html: str, url: Optional[str], source_kind: str) -> Article:
        soup = BeautifulSoup(html, "lxml")
        meta = extract_metadata(soup)
        paywall = detect_paywall(soup, meta)
        languages = _code_languages(soup)
        clean = _clean_soup(soup)
        root = _select_root(clean)
        reference_words = len((root or clean.body or clean).get_text(" ", strip=True).split())

        # Trafilatura returns nothing for bare fragments and drops headings when the content
        # is not inside <article>, so the isolated body is wrapped in a minimal document.
        scoped = (
            f"<html><body><article>{root}</article></body></html>" if root is not None else str(clean)
        )
        blocks, used = self._extract_blocks(scoped, languages, reference_words)
        if root is not None and _words(blocks) < MIN_USABLE_WORDS:
            logger.info("Body container yielded little text; retrying on the whole page")
            full_ref = len((clean.body or clean).get_text(" ", strip=True).split())
            alt, alt_used = self._extract_blocks(str(clean), languages, full_ref)
            logger.debug("Whole-page retry kept %d words (was %d)", _words(alt), _words(blocks))
            if _words(alt) > _words(blocks):
                blocks, used = alt, alt_used

        title = meta.title or "Untitled Article"
        blocks = _drop_title_heading(blocks, title)
        words = _words(blocks)
        logger.info("Extracted %d words from %d blocks using %s", words, len(blocks), used)

        if words < MIN_USABLE_WORDS:
            if paywall:
                raise PaywallError(
                    "The article is paywalled and no public content could be extracted. "
                    "NoteForge AI does not bypass paywalls; provide the text yourself if you "
                    "have legitimate access."
                )
            if _login_wall(soup):
                raise ArticleInaccessibleError(
                    "The page appears to require signing in; no article content is accessible."
                )
            raise NoContentError("No article content could be extracted from the page.")

        reasons: list[str] = []
        warnings: list[str] = []
        if paywall:
            reasons.append(
                "Paywall detected: only the freely accessible preview was extracted; the "
                "rest of the article is not included."
            )
            reasons.extend(paywall)
        if words < SHORT_ARTICLE_WORDS and not paywall:
            reasons.append(
                f"Only {words} words were extracted; the article may be truncated or very short."
            )
        if reference_words and words < MIN_RECALL_RATIO * reference_words:
            reasons.append(
                f"Extraction kept {words} of roughly {reference_words} words on the page; "
                "some content may be missing."
            )
        if blocks and blocks[-1].kind is BlockKind.PARAGRAPH and not re.search(r"[.!?\"')”’:\]]\s*$", blocks[-1].text):
            warnings.append("The article's last paragraph does not end with punctuation (possible cut-off).")
        if not meta.author:
            warnings.append("Author could not be determined.")
        if not meta.published:
            warnings.append("Publication date could not be determined.")
        if not meta.title:
            warnings.append("Title could not be determined.")
        if source_kind == "url" and not _looks_like_substack(soup, html):
            warnings.append("The page does not look like a Substack publication; results may be less accurate.")

        return Article(
            title=title, blocks=blocks, author=meta.author, published=meta.published, url=url,
            site_name=meta.site_name, source_kind=source_kind, extractor=used,
            completeness=Completeness.PARTIAL if reasons else Completeness.COMPLETE,
            completeness_reasons=reasons, warnings=warnings,
        )

    def _extract_blocks(
        self, html: str, languages: dict[str, str], reference_words: int
    ) -> tuple[list[Block], str]:
        """Trafilatura first; Readability if it fails or keeps too little of the text."""
        blocks: list[Block] = []
        try:
            xml = trafilatura.extract(
                html, output_format="xml", include_tables=True, include_formatting=True,
                include_links=False, include_comments=False, favor_recall=True,
            )
            if xml:
                blocks = blocks_from_trafilatura_xml(xml, languages)
        except Exception as exc:  # trafilatura raises varied errors on malformed input
            logger.warning("Trafilatura failed: %s", exc)

        words = _words(blocks)
        needs_fallback = words < MIN_USABLE_WORDS or (
            reference_words and words < MIN_RECALL_RATIO * reference_words
        )
        if not needs_fallback:
            return blocks, "trafilatura"

        logger.info("Falling back to readability (trafilatura kept %d words)", words)
        try:
            summary = Document(html).summary(html_partial=True)
            fallback = blocks_from_html(summary, languages)
        except Exception as exc:  # readability raises Unparseable and lxml errors
            logger.warning("Readability failed: %s", exc)
            fallback = []
        if _words(fallback) > words:
            return fallback, "readability"
        return blocks, "trafilatura"
