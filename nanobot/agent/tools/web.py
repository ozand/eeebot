"""Web tools: web_search and web_fetch."""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.web_transport import PinnedAddressTransport
from nanobot.security.network import resolve_public_addresses

if TYPE_CHECKING:
    from nanobot.config.schema import WebSearchConfig

# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5  # Limit redirects to prevent DoS attacks


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL scheme/domain. Does NOT check resolved IPs (use _validate_url_safe for that)."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        return True, ""
    except Exception as e:
        return False, str(e)


def _validate_url_safe(url: str) -> tuple[bool, str]:
    """Validate URL with SSRF protection: scheme, domain, and resolved IP check."""
    from nanobot.security.network import validate_url_target
    return validate_url_target(url)


def _format_results(query: str, items: list[dict[str, Any]], n: int) -> str:
    """Format provider results into shared plaintext output."""
    if not items:
        return f"No results for: {query}"
    lines = [f"Results for: {query}\n"]
    for i, item in enumerate(items[:n], 1):
        title = _normalize(_strip_tags(item.get("title", "")))
        snippet = _normalize(_strip_tags(item.get("content", "")))
        lines.append(f"{i}. {title}\n   {item.get('url', '')}")
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)


class WebSearchTool(Tool):
    """Search the web using configured provider."""

    name = "web_search"
    external_result = True

    def external_source(self, params: dict[str, Any], result: str) -> str:
        return f"web_search query: {str(params.get('query') or '')}"
    description = "Search the web. Returns titles, URLs, and snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }

    def __init__(self, config: WebSearchConfig | None = None, proxy: str | None = None):
        from nanobot.config.schema import WebSearchConfig

        self.config = config if config is not None else WebSearchConfig()
        self.proxy = proxy

    async def execute(self, query: str, count: int | None = None, **kwargs: Any) -> str:
        provider = self.config.provider.strip().lower() or "brave"
        n = min(max(count or self.config.max_results, 1), 10)

        if provider == "duckduckgo":
            return await self._search_duckduckgo(query, n)
        elif provider == "tavily":
            return await self._search_tavily(query, n)
        elif provider == "searxng":
            return await self._search_searxng(query, n)
        elif provider == "jina":
            return await self._search_jina(query, n)
        elif provider == "brave":
            return await self._search_brave(query, n)
        else:
            return f"Error: unknown search provider '{provider}'"

    async def _search_brave(self, query: str, n: int) -> str:
        api_key = self.config.api_key or os.environ.get("BRAVE_API_KEY", "")
        if not api_key:
            logger.warning("BRAVE_API_KEY not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        try:
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": n},
                    headers={"Accept": "application/json", "X-Subscription-Token": api_key},
                    timeout=10.0,
                )
                r.raise_for_status()
            items = [
                {"title": x.get("title", ""), "url": x.get("url", ""), "content": x.get("description", "")}
                for x in r.json().get("web", {}).get("results", [])
            ]
            return _format_results(query, items, n)
        except Exception as e:
            return f"Error: {e}"

    async def _search_tavily(self, query: str, n: int) -> str:
        api_key = self.config.api_key or os.environ.get("TAVILY_API_KEY", "")
        if not api_key:
            logger.warning("TAVILY_API_KEY not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        try:
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"query": query, "max_results": n},
                    timeout=15.0,
                )
                r.raise_for_status()
            return _format_results(query, r.json().get("results", []), n)
        except Exception as e:
            return f"Error: {e}"

    async def _search_searxng(self, query: str, n: int) -> str:
        base_url = (self.config.base_url or os.environ.get("SEARXNG_BASE_URL", "")).strip()
        if not base_url:
            logger.warning("SEARXNG_BASE_URL not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        endpoint = f"{base_url.rstrip('/')}/search"
        is_valid, error_msg = _validate_url(endpoint)
        if not is_valid:
            return f"Error: invalid SearXNG URL: {error_msg}"
        try:
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.get(
                    endpoint,
                    params={"q": query, "format": "json"},
                    headers={"User-Agent": USER_AGENT},
                    timeout=10.0,
                )
                r.raise_for_status()
            return _format_results(query, r.json().get("results", []), n)
        except Exception as e:
            return f"Error: {e}"

    async def _search_jina(self, query: str, n: int) -> str:
        api_key = self.config.api_key or os.environ.get("JINA_API_KEY", "")
        if not api_key:
            logger.warning("JINA_API_KEY not set, falling back to DuckDuckGo")
            return await self._search_duckduckgo(query, n)
        try:
            headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.get(
                    "https://s.jina.ai/",
                    params={"q": query},
                    headers=headers,
                    timeout=15.0,
                )
                r.raise_for_status()
            data = r.json().get("data", [])[:n]
            items = [
                {"title": d.get("title", ""), "url": d.get("url", ""), "content": d.get("content", "")[:500]}
                for d in data
            ]
            return _format_results(query, items, n)
        except Exception as e:
            return f"Error: {e}"

    async def _search_duckduckgo(self, query: str, n: int) -> str:
        try:
            from ddgs import DDGS

            ddgs = DDGS(timeout=10)
            raw = await asyncio.to_thread(ddgs.text, query, max_results=n)
            if not raw:
                return f"No results for: {query}"
            items = [
                {"title": r.get("title", ""), "url": r.get("href", ""), "content": r.get("body", "")}
                for r in raw
            ]
            return _format_results(query, items, n)
        except Exception as e:
            logger.warning("DuckDuckGo search failed: {}", e)
            return f"Error: DuckDuckGo search failed ({e})"


class WebFetchTool(Tool):
    """Fetch and extract content from a URL."""

    name = "web_fetch"
    external_result = True

    def external_source(self, params: dict[str, Any], result: str) -> str:
        try:
            payload = json.loads(result)
            if isinstance(payload, dict):
                return str(payload.get("finalUrl") or payload.get("url") or params.get("url") or self.name)
        except (TypeError, ValueError):
            pass
        return str(params.get("url") or self.name)
    description = "Fetch URL and extract readable content (HTML → markdown/text)."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {"type": "integer", "minimum": 100},
        },
        "required": ["url"],
    }

    def __init__(self, max_chars: int = 50000, proxy: str | None = None):
        self.max_chars = max_chars
        self.proxy = proxy

    async def execute(self, url: str, extractMode: str = "markdown", maxChars: int | None = None, **kwargs: Any) -> str:  # noqa: N803
        max_chars = maxChars or self.max_chars
        proxy_error = self._proxy_error()
        if proxy_error:
            return json.dumps({"error": proxy_error, "url": url}, ensure_ascii=False)
        try:
            return await self._fetch_readability(url, extractMode, max_chars)
        except Exception as exc:
            logger.error("WebFetch error for {}: {}", url, exc)
            message = str(exc)
            return json.dumps({"error": message, "url": url}, ensure_ascii=False)

    def _proxy_error(self) -> str | None:
        if self.proxy:
            return "Explicit proxy configuration is unsupported"
        names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
        if any(os.environ.get(name) for name in names):
            return "Environment proxy configuration is unsupported"
        return None

    async def _fetch_jina(self, url: str, max_chars: int) -> str | None:
        """Try fetching via Jina Reader API. Returns None on failure."""
        try:
            headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
            jina_key = os.environ.get("JINA_API_KEY", "")
            if jina_key:
                headers["Authorization"] = f"Bearer {jina_key}"
            async with httpx.AsyncClient(proxy=self.proxy, timeout=20.0) as client:
                r = await client.get(f"https://r.jina.ai/{url}", headers=headers)
                if r.status_code == 429:
                    logger.debug("Jina Reader rate limited, falling back to readability")
                    return None
                r.raise_for_status()

            data = r.json().get("data", {})
            title = data.get("title", "")
            text = data.get("content", "")
            if not text:
                return None

            if title:
                text = f"# {title}\n\n{text}"
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]
            return json.dumps({
                "url": url, "finalUrl": data.get("url", url), "status": r.status_code,
                "extractor": "jina", "truncated": truncated, "length": len(text),
                "untrusted": True, "text": text,
            }, ensure_ascii=False)
        except Exception as e:
            logger.debug("Jina Reader failed for {}, falling back to readability: {}", url, e)
            return None

    @staticmethod
    def _resolve_hop(url: str) -> tuple[tuple[str, ...], str]:
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return (), "Only absolute http/https URLs are allowed"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            return resolve_public_addresses(parsed.hostname, port)
        except (TypeError, ValueError) as exc:
            return (), str(exc)

    @staticmethod
    async def _close_response(response: httpx.Response) -> None:
        """Attempt response cleanup again if closing raises or is cancelled."""
        stream = getattr(response, "stream", None)
        try:
            await response.aclose()
        except BaseException:
            try:
                if stream is not None:
                    await stream.aclose()
                else:
                    await response.aclose()
            except BaseException:
                logger.warning("WebFetch response cleanup retry failed", exc_info=True)
            cleanup = getattr(stream, "cleanup_after_close_failure", None)
            if cleanup is not None:
                try:
                    await cleanup()
                except BaseException:
                    logger.warning("WebFetch pool cleanup failed", exc_info=True)
            raise

    async def _fetch_readability(self, url: str, extract_mode: str, max_chars: int) -> str:
        """Fetch manually validated redirects through a pinned, direct transport."""
        try:
            from readability import Document
        except ModuleNotFoundError:
            Document = None  # noqa: N806

        current = url
        cookies = httpx.Cookies()
        for hop in range(MAX_REDIRECTS + 1):
            addresses, error = self._resolve_hop(current)
            if error:
                prefix = "URL validation failed" if hop == 0 else "Redirect blocked"
                return json.dumps({"error": f"{prefix}: {error}", "url": url}, ensure_ascii=False)
            parsed = urlparse(current)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            transport = PinnedAddressTransport(addresses, parsed.hostname, port)
            try:
                async with httpx.AsyncClient(transport=transport, cookies=cookies, trust_env=False,
                                             follow_redirects=False, timeout=30.0) as client:
                    response = await client.get(current, headers={"User-Agent": USER_AGENT})
                    if hasattr(client, "cookies"):
                        cookies.jar = client.cookies.jar
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location") or response.headers.get("Location")
                        try:
                            await self._close_response(response)
                        except Exception as exc:
                            logger.error("WebFetch redirect response close failed for {}: {}", current, exc)
                            return json.dumps({"error": str(exc), "url": url}, ensure_ascii=False)
                        if hop >= MAX_REDIRECTS:
                            return json.dumps({"error": "Too many redirects", "url": url}, ensure_ascii=False)
                        if not location:
                            return json.dumps({"error": "Redirect response missing Location", "url": url}, ensure_ascii=False)
                        current = urljoin(current, location)
                        continue
                    try:
                        response.raise_for_status()
                        if hasattr(response, "aread"):
                            content = await response.aread()
                        else:
                            content = response.text.encode("utf-8")
                        status, headers = response.status_code, response.headers
                    except BaseException:
                        if hasattr(response, "aclose"):
                            try:
                                await self._close_response(response)
                            except BaseException:
                                logger.warning("WebFetch response cleanup failed", exc_info=True)
                        raise
                    else:
                        if hasattr(response, "aclose"):
                            try:
                                await self._close_response(response)
                            except Exception as exc:
                                logger.error("WebFetch response close failed for {}: {}", current, exc)
                                return json.dumps({"error": str(exc), "url": url}, ensure_ascii=False)
            except Exception as exc:
                logger.error("WebFetch error for {}: {}", current, exc)
                return json.dumps({"error": str(exc), "url": url}, ensure_ascii=False)
            break

        ctype = headers.get("content-type", "")
        decoded_response = httpx.Response(200, headers=headers, content=content)
        decoded = decoded_response.text
        if "application/json" in ctype:
            text, extractor = json.dumps(json.loads(decoded), indent=2, ensure_ascii=False), "json"
        elif "text/html" in ctype or decoded[:256].lower().startswith(("<!doctype", "<html")):
            if Document is not None:
                doc = Document(decoded)
                body = doc.summary()
                body = self._to_markdown(body) if extract_mode == "markdown" else _strip_tags(body)
                text = f"# {doc.title()}\n\n{body}" if doc.title() else body
                extractor = "readability"
            else:
                title_match = re.search(r'<title[^>]*>([\s\S]*?)</title>', decoded, flags=re.I)
                title = _normalize(_strip_tags(title_match.group(1))) if title_match else ""
                body = self._to_markdown(decoded) if extract_mode == "markdown" else _strip_tags(decoded)
                text = f"# {title}\n\n{body}" if title else body
                extractor = "html-fallback"
        else:
            text, extractor = decoded, "raw"
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
        return json.dumps({"url": url, "finalUrl": current, "status": status,
            "extractor": extractor, "truncated": truncated, "length": len(text),
            "untrusted": True, "text": text}, ensure_ascii=False)

    def _to_markdown(self, html_content: str) -> str:
        """Convert HTML to markdown."""
        text = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
                      lambda m: f'[{_strip_tags(m[2])}]({m[1]})', html_content, flags=re.I)
        text = re.sub(r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
                      lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n', text, flags=re.I)
        text = re.sub(r'<li[^>]*>([\s\S]*?)</li>', lambda m: f'\n- {_strip_tags(m[1])}', text, flags=re.I)
        text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
        text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
        return _normalize(_strip_tags(text))
