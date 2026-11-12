"""Modular, security-hardened web search & webpage retrieval service for Online Assisted Mode.
Supports DuckDuckGo (no key), Tavily, Brave Search, Serper, and HTTPS SearXNG via SearchProvider.
Enforces offline gating, host allowlists, rate limiting, TTL caching, robots.txt, timeouts, and size caps."""
from abc import ABC, abstractmethod
import collections
from dataclasses import asdict, dataclass
from html import unescape
from html.parser import HTMLParser
import json
import logging
import threading
import time
from urllib import robotparser
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from . import audit
from .config import InternetSpec, get_provider_api_key
from .netsec import NetworkSecurityError, safe_request

log = logging.getLogger("jarvis.search")


class SearchError(Exception):
    """Base error for web search failures (safe to display to the user)."""


class SearchOfflineError(SearchError):
    """Raised when search or fetch is attempted while Jarvis is in Local Mode."""


class SearchConfigError(SearchError):
    """Raised when provider config or API key is missing/invalid."""


class SearchRateLimitError(SearchError):
    """Raised when local RPM limit or upstream rate limit is hit."""


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    published_date: str | None = None
    provider: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class _DDGHTMLParser(HTMLParser):
    """Lightweight stdlib parser for DuckDuckGo HTML/Lite results."""

    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self._in_a = False
        self._in_snippet = False
        self._cur_href = ""
        self._cur_title: list[str] = []
        self._cur_snip: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        cls = attr.get("class", "")
        if tag == "a" and ("result__a" in cls or "result-link" in cls):
            self._in_a = True
            self._cur_href = attr.get("href", "")
            self._cur_title = []
        elif tag in ("a", "div", "td", "span") and ("result__snippet" in cls or "result-snippet" in cls):
            self._in_snippet = True
            self._cur_snip = []

    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            self._in_a = False
            title = " ".join("".join(self._cur_title).split())
            url = _unwrap_ddg_url(self._cur_href)
            if title and url.startswith("https://"):
                self.results.append({"title": title, "url": url, "snippet": ""})
        elif self._in_snippet and tag in ("a", "div", "td", "span"):
            self._in_snippet = False
            snip = " ".join("".join(self._cur_snip).split())
            if self.results and snip and not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = snip

    def handle_data(self, data):
        if self._in_a:
            self._cur_title.append(data)
        elif self._in_snippet:
            self._cur_snip.append(data)


class _TextExtractor(HTMLParser):
    """Extracts readable visible text and <title> from HTML while ignoring scripts/styles."""

    _SKIP = {"script", "style", "noscript", "svg", "iframe", "template"}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._in_title = False
        self.title: list[str] = []
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self._SKIP:
            self._skip_depth += 1
        elif t == "title" and self._skip_depth == 0:
            self._in_title = True

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif t == "title":
            self._in_title = False
        elif t in ("p", "div", "li", "h1", "h2", "h3", "h4", "br", "tr") and self._skip_depth == 0:
            self.chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title.append(data)
        else:
            self.chunks.append(data)


def _unwrap_ddg_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if parts.netloc.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        uddg = parse_qs(parts.query).get("uddg")
        if uddg:
            return unquote(uddg[0])
    return href


def extract_readable_text(html_bytes: bytes, charset: str = "utf-8", max_chars: int | None = 2500) -> tuple[str, str]:
    text_raw = html_bytes.decode(charset, errors="replace")
    parser = _TextExtractor()
    parser.feed(text_raw)
    parser.close()
    title = " ".join("".join(parser.title).split())
    body = unescape("".join(parser.chunks))
    lines = [" ".join(ln.split()) for ln in body.splitlines()]
    cleaned = "\n".join(ln for ln in lines if ln)
    if max_chars is not None and max_chars > 0 and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rsplit(" ", 1)[0] + "…"
    return title, cleaned


class SearchProvider(ABC):
    name: str
    requires_api_key: bool = False

    @abstractmethod
    async def search(self, query: str, max_results: int, spec: InternetSpec, transport_factory=None) -> list[SearchResult]:
        """Execute search query and return normalized SearchResult list."""


class DuckDuckGoProvider(SearchProvider):
    name = "duckduckgo"
    requires_api_key = False

    async def search(self, query: str, max_results: int, spec: InternetSpec, transport_factory=None) -> list[SearchResult]:
        # Try DuckDuckGo HTML endpoint first, then Instant Answer API as structured fallback (never solve CAPTCHAs)
        resp = await safe_request(
            "GET",
            "https://html.duckduckgo.com/html/",
            spec,
            allowed_hosts=spec.allowed_search_hosts,
            params={"q": query},
            headers={"Accept": "text/html,application/xhtml+xml"},
            transport_factory=transport_factory,
        )
        html = resp.body.decode("utf-8", errors="replace")
        if resp.status_code in (202, 403, 429) or "anomaly-modal" in html.lower() or "captcha" in html.lower():
            # Respect bot/rate gate immediately; try the official Instant Answer JSON API only
            ia = await safe_request(
                "GET",
                "https://api.duckduckgo.com/",
                spec,
                allowed_hosts=spec.allowed_search_hosts,
                params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                headers={"Accept": "application/json"},
                transport_factory=transport_factory,
            )
            if ia.status_code == 200:
                data = json.loads(ia.body.decode("utf-8", errors="replace"))
                out = []
                if data.get("AbstractText") and str(data.get("AbstractURL", "")).startswith("https://"):
                    out.append(SearchResult(
                        title=data.get("Heading") or query,
                        url=data["AbstractURL"],
                        snippet=data["AbstractText"],
                        provider=self.name,
                    ))
                for t in data.get("RelatedTopics", []):
                    if isinstance(t, dict) and t.get("Text") and str(t.get("FirstURL", "")).startswith("https://"):
                        out.append(SearchResult(
                            title=t["Text"].split(" - ", 1)[0][:90],
                            url=t["FirstURL"],
                            snippet=t["Text"],
                            provider=self.name,
                        ))
                if out:
                    return out[:max_results]
            raise SearchRateLimitError(
                "DuckDuckGo challenged or rate-limited automated search requests. "
                "Configure Tavily, Brave, Serper, or a SearXNG instance in System settings for reliable search."
            )
        if resp.status_code >= 400:
            raise SearchError(f"DuckDuckGo search returned HTTP {resp.status_code}")

        parser = _DDGHTMLParser()
        parser.feed(html)
        parser.close()
        return [
            SearchResult(title=r["title"], url=r["url"], snippet=r["snippet"], provider=self.name)
            for r in parser.results[:max_results]
        ]


class TavilyProvider(SearchProvider):
    name = "tavily"
    requires_api_key = True

    async def search(self, query: str, max_results: int, spec: InternetSpec, transport_factory=None) -> list[SearchResult]:
        key = get_provider_api_key("tavily")
        if not key:
            raise SearchConfigError("Tavily API key is not configured (set JARVIS_TAVILY_API_KEY or save it in System settings).")
        resp = await safe_request(
            "POST",
            "https://api.tavily.com/search",
            spec,
            allowed_hosts=spec.allowed_search_hosts,
            json_body={"api_key": key, "query": query, "max_results": max_results, "search_depth": "basic"},
            headers={"Accept": "application/json"},
            transport_factory=transport_factory,
        )
        if resp.status_code == 401:
            raise SearchConfigError("Tavily API key was rejected (HTTP 401).")
        if resp.status_code == 429:
            raise SearchRateLimitError("Tavily rate limit exceeded (HTTP 429).")
        if resp.status_code >= 400:
            raise SearchError(f"Tavily returned HTTP {resp.status_code}")
        data = json.loads(resp.body.decode("utf-8", errors="replace"))
        return [
            SearchResult(
                title=r.get("title") or r.get("url", ""),
                url=r.get("url", ""),
                snippet=r.get("content") or "",
                published_date=r.get("published_date"),
                provider=self.name,
            )
            for r in data.get("results", [])[:max_results]
            if str(r.get("url", "")).startswith("https://")
        ]


class BraveProvider(SearchProvider):
    name = "brave"
    requires_api_key = True

    async def search(self, query: str, max_results: int, spec: InternetSpec, transport_factory=None) -> list[SearchResult]:
        key = get_provider_api_key("brave")
        if not key:
            raise SearchConfigError("Brave Search API key is not configured (set JARVIS_BRAVE_API_KEY or save it in System settings).")
        resp = await safe_request(
            "GET",
            "https://api.search.brave.com/res/v1/web/search",
            spec,
            allowed_hosts=spec.allowed_search_hosts,
            params={"q": query, "count": max_results},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            transport_factory=transport_factory,
        )
        if resp.status_code == 401:
            raise SearchConfigError("Brave Search API key was rejected (HTTP 401).")
        if resp.status_code == 429:
            raise SearchRateLimitError("Brave Search rate limit exceeded (HTTP 429).")
        if resp.status_code >= 400:
            raise SearchError(f"Brave Search returned HTTP {resp.status_code}")
        data = json.loads(resp.body.decode("utf-8", errors="replace"))
        items = data.get("web", {}).get("results", [])
        return [
            SearchResult(
                title=r.get("title") or r.get("url", ""),
                url=r.get("url", ""),
                snippet=r.get("description") or "",
                published_date=r.get("page_age") or r.get("age"),
                provider=self.name,
            )
            for r in items[:max_results]
            if str(r.get("url", "")).startswith("https://")
        ]


class SerperProvider(SearchProvider):
    name = "serper"
    requires_api_key = True

    async def search(self, query: str, max_results: int, spec: InternetSpec, transport_factory=None) -> list[SearchResult]:
        key = get_provider_api_key("serper")
        if not key:
            raise SearchConfigError("Serper API key is not configured (set JARVIS_SERPER_API_KEY or save it in System settings).")
        resp = await safe_request(
            "POST",
            "https://google.serper.dev/search",
            spec,
            allowed_hosts=spec.allowed_search_hosts,
            json_body={"q": query, "num": max_results},
            headers={"X-API-KEY": key, "Accept": "application/json"},
            transport_factory=transport_factory,
        )
        if resp.status_code in (401, 403):
            raise SearchConfigError(f"Serper API key was rejected (HTTP {resp.status_code}).")
        if resp.status_code == 429:
            raise SearchRateLimitError("Serper rate limit exceeded (HTTP 429).")
        if resp.status_code >= 400:
            raise SearchError(f"Serper returned HTTP {resp.status_code}")
        data = json.loads(resp.body.decode("utf-8", errors="replace"))
        return [
            SearchResult(
                title=r.get("title") or r.get("link", ""),
                url=r.get("link", ""),
                snippet=r.get("snippet") or "",
                published_date=r.get("date"),
                provider=self.name,
            )
            for r in data.get("organic", [])[:max_results]
            if str(r.get("link", "")).startswith("https://")
        ]


class SearXNGProvider(SearchProvider):
    name = "searxng"
    requires_api_key = False

    async def search(self, query: str, max_results: int, spec: InternetSpec, transport_factory=None) -> list[SearchResult]:
        base = (spec.searxng_url or "").strip().rstrip("/")
        if not base:
            raise SearchConfigError("SearXNG HTTPS URL is not configured in System settings.")
        parts = urlsplit(base)
        if parts.scheme != "https" or not parts.hostname:
            raise SearchConfigError("SearXNG URL must use https://")
        allow = [*spec.allowed_search_hosts, parts.hostname.lower()]
        headers = {"Accept": "application/json"}
        key = get_provider_api_key("searxng")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        resp = await safe_request(
            "GET",
            f"{base}/search",
            spec,
            allowed_hosts=allow,
            params={"q": query, "format": "json"},
            headers=headers,
            transport_factory=transport_factory,
        )
        if resp.status_code >= 400:
            raise SearchError(f"SearXNG returned HTTP {resp.status_code}")
        data = json.loads(resp.body.decode("utf-8", errors="replace"))
        return [
            SearchResult(
                title=r.get("title") or r.get("url", ""),
                url=r.get("url", ""),
                snippet=r.get("content") or "",
                published_date=r.get("publishedDate"),
                provider=self.name,
            )
            for r in data.get("results", [])[:max_results]
            if str(r.get("url", "")).startswith("https://")
        ]


PROVIDERS: dict[str, SearchProvider] = {
    "duckduckgo": DuckDuckGoProvider(),
    "tavily": TavilyProvider(),
    "brave": BraveProvider(),
    "serper": SerperProvider(),
    "searxng": SearXNGProvider(),
}


class WebSearchService:
    """Rate-limited, cached search & robots-aware page fetcher with offline guards."""

    def __init__(self, spec: InternetSpec, providers: dict[str, SearchProvider] | None = None, transport_factory=None):
        self.spec = spec
        self.providers = dict(PROVIDERS if providers is None else providers)
        self._transport_factory = transport_factory
        self._lock = threading.Lock()
        self._req_times: collections.deque[float] = collections.deque()
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._robots_cache: dict[str, tuple[float, robotparser.RobotFileParser]] = {}
        self.status_state = "offline" if not spec.enabled else "ready"
        self.last_error: str | None = None

    def _require_online(self):
        if not self.spec.enabled or not audit.is_online_enabled():
            self.status_state = "offline"
            raise SearchOfflineError(
                "Jarvis is in Local Mode (internet access is disabled). Enable Online Assisted Mode to run web searches."
            )

    def _check_rate_limit(self):
        now = time.time()
        with self._lock:
            while self._req_times and now - self._req_times[0] > 60.0:
                self._req_times.popleft()
            if len(self._req_times) >= self.spec.rate_limit_rpm:
                raise SearchRateLimitError(f"Rate limit reached ({self.spec.rate_limit_rpm} requests/minute). Please wait briefly.")
            self._req_times.append(now)

    def clear_cache(self):
        with self._lock:
            self._cache.clear()
            self._robots_cache.clear()

    def status(self) -> dict:
        provider = self.providers.get(self.spec.provider)
        has_key = True if not (provider and provider.requires_api_key) else bool(get_provider_api_key(self.spec.provider))
        return {
            "enabled": bool(self.spec.enabled and audit.is_online_enabled()),
            "mode": "online_assisted" if (self.spec.enabled and audit.is_online_enabled()) else "local",
            "provider": self.spec.provider,
            "provider_ready": has_key,
            "requires_api_key": bool(provider and provider.requires_api_key),
            "allow_page_fetch": bool(self.spec.allow_page_fetch),
            "state": "offline" if not self.spec.enabled else self.status_state,
            "last_error": self.last_error,
            "cache_entries": len(self._cache),
        }

    async def search(self, query: str, max_results: int | None = None) -> dict:
        self._require_online()
        q = " ".join((query or "").split())
        if not q:
            raise SearchError("Search query cannot be empty.")
        limit = max(1, min(int(max_results or self.spec.max_results), 10))
        prov = self.providers.get(self.spec.provider)
        if prov is None:
            raise SearchConfigError(f"Unknown search provider: {self.spec.provider!r}")

        cache_key = ("search", prov.name, q.lower(), limit)
        now = time.time()
        if self.spec.cache_ttl_s > 0:
            with self._lock:
                hit = self._cache.get(cache_key)
                if hit and now - hit[0] < self.spec.cache_ttl_s:
                    self.status_state = "cached"
                    return {"query": q, "provider": prov.name, "cached": True, "results": [r.to_dict() for r in hit[1]]}

        self._check_rate_limit()
        self.status_state = "searching"
        self.last_error = None
        try:
            results = await prov.search(q, limit, self.spec, transport_factory=self._transport_factory)
        except NetworkSecurityError as e:
            self.status_state = "blocked"
            self.last_error = str(e)
            raise SearchError(str(e)) from e
        except SearchError as e:
            self.status_state = "error"
            self.last_error = str(e)
            raise
        except Exception as e:
            self.status_state = "error"
            self.last_error = f"{type(e).__name__}: {e}"
            raise SearchError(f"Web search failed ({type(e).__name__}): {e}") from e

        if self.spec.cache_ttl_s > 0:
            with self._lock:
                self._cache[cache_key] = (time.time(), results)
        self.status_state = "ready"
        return {"query": q, "provider": prov.name, "cached": False, "results": [r.to_dict() for r in results]}

    async def _robots_allows(self, url: str) -> bool:
        if not self.spec.respect_robots:
            return True
        parts = urlsplit(url)
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        now = time.time()
        with self._lock:
            cached = self._robots_cache.get(origin)
        if cached and now - cached[0] < 600:
            rp = cached[1]
        else:
            rp = robotparser.RobotFileParser()
            robots_url = f"{origin}/robots.txt"
            try:
                resp = await safe_request(
                    "GET",
                    robots_url,
                    self.spec,
                    max_bytes=65_536,
                    transport_factory=self._transport_factory,
                )
                if resp.status_code == 200:
                    rp.parse(resp.body.decode("utf-8", errors="replace").splitlines())
                elif resp.status_code in (401, 403):
                    rp.disallow_all = True
                else:
                    rp.allow_all = True
            except NetworkSecurityError:
                raise
            except Exception:
                rp.allow_all = True
            with self._lock:
                self._robots_cache[origin] = (now, rp)
        return rp.can_fetch("JarvisLocalAssistant", url)

    async def fetch_page(self, url: str, *, max_chars: int | None = 2500) -> dict:
        self._require_online()
        if not self.spec.allow_page_fetch:
            raise SearchOfflineError("Website content retrieval is disabled in settings.")
        clean_url = (url or "").strip()
        cache_key = ("page", clean_url, max_chars)
        now = time.time()
        if self.spec.cache_ttl_s > 0:
            with self._lock:
                hit = self._cache.get(cache_key)
                if hit and now - hit[0] < self.spec.cache_ttl_s:
                    return dict(hit[1], cached=True)

        self._check_rate_limit()
        if not await self._robots_allows(clean_url):
            audit.record_external("robots_block", clean_url, urlsplit(clean_url).hostname or "", False, "disallowed by robots.txt")
            raise SearchError("Website retrieval blocked by the site's robots.txt policy.")

        try:
            resp = await safe_request(
                "GET",
                clean_url,
                self.spec,
                headers={"Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"},
                transport_factory=self._transport_factory,
            )
        except NetworkSecurityError as e:
            raise SearchError(str(e)) from e
        except Exception as e:
            raise SearchError(f"Page fetch failed ({type(e).__name__}): {e}") from e

        if resp.status_code >= 400:
            raise SearchError(f"Website returned HTTP {resp.status_code}")

        ctype = resp.headers.get("content-type", "").lower()
        if "html" in ctype or not ctype:
            title, content = extract_readable_text(resp.body, max_chars=max_chars)
        elif ctype.startswith("text/") or "json" in ctype:
            title = urlsplit(resp.url).hostname or resp.url
            decoded = resp.body.decode("utf-8", errors="replace")
            content = decoded[:max_chars] if (max_chars is not None and max_chars > 0) else decoded
        else:
            raise SearchError(f"Unsupported content type for retrieval: {ctype}")

        import hashlib

        digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
        result = {
            "url": resp.url,
            "title": title or resp.url,
            "content": content,
            "sha256": digest,
            "untrusted": True,
            "cached": False,
        }
        if self.spec.cache_ttl_s > 0:
            with self._lock:
                self._cache[cache_key] = (time.time(), result)
        return result

