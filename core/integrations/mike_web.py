# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

import gzip
import html
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import List, Optional

from mike_config import WEB_REQUEST_TIMEOUT_SECONDS

BRAVE_API_KEY: str = os.getenv("BRAVE_API_KEY", "")

try:
    from ddgs import DDGS
except Exception:  # pragma: no cover - optional dependency fallback
    DDGS = None

_log = logging.getLogger("mike")

# ---------------------------------------------------------------------------
# Palavras de conversa que NAO devem ir para o motor de busca
# ---------------------------------------------------------------------------
_FILLER_WORDS = re.compile(
    r"\b(cara|mano|irmao|irmão|por favor|pfv|pf|faz|faca|faça|faz uma|me (fala|diz|mostra)|"
    r"uma pesquisa (sobre|do|da|de)?|pesquisa (sobre|do|da|de)?|pesquise (sobre|o?|a?)|"
    r"pesquisar (sobre|o?|a?)|buscar (sobre|o?|a?)|"
    r"procura (sobre|o|a)|busca (sobre|o|a)|me informa|me informe|sabe(s)?|"
    r"pode(s)?|consegue|tente|tenta|veja|verifique|verifica|queria saber|"
    r"gostaria de saber|preciso saber|qual e o?|qual é o?|me conta|pela internet|"
    r"na internet|na web|no brave|no ddgs|no dds|no duckduckgo|qual (o|a)|quais (os|as)|"
    r"como (esta|está|fica|e|é))\b",
    re.IGNORECASE,
)

# Remove prefixos comuns do chip de pesquisa: "Pesquisa na web:", "Pesquise na web:", etc.
_CHIP_PREFIX = re.compile(
    r"^(pesquisa\s+(na\s+web|na\s+internet|no\s+brave|no\s+ddgs|no\s+duckduckgo)|"
    r"pesquise\s+(na\s+web|na\s+internet|no\s+brave|no\s+ddgs|no\s+duckduckgo))\s*:?\s*",
    re.IGNORECASE,
)

# Marcadores de tempo real — nao cachear estes
_REALTIME_TERMS = frozenset((
    "meteo", "meteorologia", "clima", "tempo", "previsao", "temperatura",
    "chuva", "neve", "forecast", "hoje", "agora", "ao vivo",
    "preco", "preço", "cotacao", "cotação", "bitcoin", "cripto",
    "dolar", "dólar", "euro", "bolsa", "cambio", "câmbio",
    "placar", "resultado", "score", "ao vivo",
))


def _urlopen_optional_timeout(request):
    """urlopen com timeout garantido — nunca bloqueia infinitamente."""
    timeout = max(WEB_REQUEST_TIMEOUT_SECONDS, 10.0)
    return urllib.request.urlopen(request, timeout=timeout)


def _normalize_provider(provider: Optional[str]) -> str:
    normalized = (provider or "auto").strip().lower()
    if normalized in {"dds", "ddgs"}:
        return "ddgs"
    if normalized == "brave":
        return "brave"
    if normalized == "duckduckgo":
        return "duckduckgo"
    return "auto"


def _clean_query(raw: str) -> str:
    """Remove filler conversacional e retorna query limpa para o motor de busca."""
    q = _CHIP_PREFIX.sub("", raw)
    q = _FILLER_WORDS.sub(" ", q)
    q = re.sub(r"\s*:\s*", " ", q)
    q = re.sub(r"\s{2,}", " ", q).strip(" ,!?.")
    return q if len(q) >= 3 else raw


def _is_realtime(query: str) -> bool:
    lower = query.lower()
    return any(term in lower for term in _REALTIME_TERMS)


@dataclass
class WebSearchResult:
    title: str
    url: str
    description: str
    age: str = ""


class MikeWebSearch:
    def __init__(self, provider: str = "auto") -> None:
        self.provider = _normalize_provider(provider)
        self.last_provider_used = "none"
        self.ddgs_available = DDGS is not None

    @property
    def active_provider(self) -> str:
        if self.provider == "brave":
            return "ddgs" if self.ddgs_available else "duckduckgo"
        if self.provider == "ddgs":
            return "ddgs"
        if self.provider == "duckduckgo":
            return "duckduckgo"
        # auto: prefer DDGS; Brave is kept only as a legacy alias/fallback knob.
        return "ddgs" if self.ddgs_available else "duckduckgo"

    @property
    def provider_ready(self) -> bool:
        p = self.active_provider
        if p == "brave":
            return bool(BRAVE_API_KEY)
        if p == "ddgs":
            return self.ddgs_available
        return True

    @property
    def ddgs_ready(self) -> bool:
        return self.ddgs_available

    @property
    def brave_ready(self) -> bool:
        return bool(BRAVE_API_KEY)

    def is_realtime_query(self, query: str) -> bool:
        return _is_realtime(query)

    def search(self, query: str, count: int = 5) -> List[dict]:
        count = max(1, min(int(count or 5), 10))
        clean = _clean_query(query)
        _log.debug("Web search: raw=%r -> clean=%r provider=%s", query, clean, self.active_provider)
        provider = self.active_provider

        if provider == "brave":
            try:
                results = self._dedupe_results(
                    self._search_brave(clean, original=query, count=count),
                    count=count,
                )
                if results:
                    self.last_provider_used = "brave"
                    return [asdict(item) for item in results]
                _log.warning("Brave returned 0 results for %r, falling back to DDGS", clean)
            except Exception as exc:
                _log.warning("Brave search failed (%s: %s), falling back to DDGS", type(exc).__name__, exc)

        if provider in ("ddgs", "brave"):
            if self.ddgs_available:
                try:
                    results = self._dedupe_results(
                        self._search_ddgs(clean, original=query, count=count),
                        count=count,
                    )
                    if results:
                        self.last_provider_used = "ddgs"
                        return [asdict(item) for item in results]
                    _log.warning("DDGS returned 0 results for %r, falling back to DuckDuckGo", clean)
                except Exception as exc:
                    _log.warning(
                        "DDGS search failed (%s: %s), falling back to DuckDuckGo",
                        type(exc).__name__,
                        exc,
                    )
            else:
                _log.warning("DDGS provider requested but package is unavailable; falling back to DuckDuckGo")

        results = self._dedupe_results(
            self._search_duckduckgo(clean, count=count),
            count=count,
        )
        self.last_provider_used = "duckduckgo"
        return [asdict(item) for item in results]

    def _search_brave(self, query: str, original: str = "", count: int = 5) -> List[WebSearchResult]:
        if not BRAVE_API_KEY:
            raise RuntimeError("BRAVE_API_KEY not set")

        timeout = WEB_REQUEST_TIMEOUT_SECONDS if WEB_REQUEST_TIMEOUT_SECONDS > 0 else 10
        realtime = _is_realtime(original or query)
        results: List[WebSearchResult] = []

        params: dict = {
            "q": query,
            "count": min(count + 3, 10),
            "result_filter": "web,news" if realtime else "web",
            "search_lang": "en",
        }
        if realtime:
            params["freshness"] = "pd"

        search_url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            search_url,
            headers={
                "X-Subscription-Token": BRAVE_API_KEY,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
        )

        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            encoding = (response.headers.get("Content-Encoding") or "").lower()
            if encoding == "gzip":
                raw = gzip.decompress(raw)

        payload = json.loads(raw.decode("utf-8", errors="ignore"))

        if realtime:
            for item in (payload.get("news", {}).get("results") or [])[:3]:
                results.append(WebSearchResult(
                    title="[noticia] " + (item.get("title") or "").strip(),
                    url=(item.get("url") or "").strip(),
                    description=(item.get("description") or "").strip(),
                    age=(item.get("age") or item.get("page_age") or "").strip(),
                ))

        for item in (payload.get("web", {}).get("results") or [])[:count]:
            results.append(WebSearchResult(
                title=(item.get("title") or "").strip(),
                url=(item.get("url") or "").strip(),
                description=(item.get("description") or "").strip(),
                age=(item.get("age") or item.get("page_age") or "").strip(),
            ))

        _log.info("Brave: %d results for %r", len(results), query)
        return results

    def _search_ddgs(self, query: str, original: str = "", count: int = 5) -> List[WebSearchResult]:
        if DDGS is None:
            raise RuntimeError("ddgs package is not installed")

        timeout = WEB_REQUEST_TIMEOUT_SECONDS if WEB_REQUEST_TIMEOUT_SECONDS > 0 else 10
        realtime = _is_realtime(original or query)
        results: List[WebSearchResult] = []
        news_count = 0
        text_count = 0

        with DDGS(timeout=timeout) as client:
            if realtime:
                try:
                    news_items = client.news(
                        query,
                        region="wt-wt",
                        safesearch="off",
                        timelimit="d",
                        backend="duckduckgo",
                        max_results=min(count, 3),
                    )
                except Exception as exc:
                    _log.debug("DDGS news failed for %r: %s", query, exc)
                    news_items = []
                news_count = len(news_items)
                for item in news_items[:3]:
                    source = (item.get("source") or "").strip()
                    date = (item.get("date") or "").strip()
                    age = " | ".join(part for part in (source, date) if part)
                    results.append(WebSearchResult(
                        title="[noticia] " + (item.get("title") or "").strip(),
                        url=(item.get("url") or "").strip(),
                        description=(item.get("body") or "").strip(),
                        age=age,
                    ))

            text_items = client.text(
                query,
                region="wt-wt",
                safesearch="off",
                timelimit="d" if realtime else None,
                backend="duckduckgo",
                max_results=count,
            )
            text_count = len(text_items)
            for item in text_items[:count]:
                results.append(WebSearchResult(
                    title=(item.get("title") or "").strip(),
                    url=(item.get("href") or item.get("url") or "").strip(),
                    description=(item.get("body") or item.get("description") or "").strip(),
                    age=(item.get("date") or "").strip(),
                ))

        _log.info(
            "DDGS: %d results (news=%d, text=%d) for %r",
            len(results),
            news_count,
            text_count,
            query,
        )
        return results

    def _search_duckduckgo(self, query: str, count: int = 5) -> List[WebSearchResult]:
        try:
            ia_url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
                {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
            )
            req_ia = urllib.request.Request(ia_url, headers={"User-Agent": "Mike/3.5"})
            with _urlopen_optional_timeout(req_ia) as response:
                raw = response.read()
                encoding = (response.headers.get("Content-Encoding") or "").lower()
                if encoding == "gzip":
                    raw = gzip.decompress(raw)
            payload = json.loads(raw.decode("utf-8", errors="ignore"))

            ia_results: List[WebSearchResult] = []
            if payload.get("AbstractText"):
                ia_results.append(WebSearchResult(
                    title=payload.get("Heading", query),
                    url=payload.get("AbstractURL", ""),
                    description=payload["AbstractText"][:400],
                ))
            for topic in (payload.get("RelatedTopics") or [])[:count]:
                if isinstance(topic, dict) and topic.get("Text"):
                    ia_results.append(WebSearchResult(
                        title=topic.get("Text", "")[:80],
                        url=(topic.get("FirstURL") or ""),
                        description=topic.get("Text", ""),
                    ))
            if ia_results:
                return ia_results
        except Exception as exc:
            _log.debug("DDG instant answer failed: %s", exc)

        url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Mike/3.5)"},
        )
        with _urlopen_optional_timeout(request) as response:
            page = response.read().decode("utf-8", errors="ignore")

        titles = re.findall(
            r'<a rel="nofollow" class="result__a" href="([^"]+)">(.+?)</a>',
            page,
            flags=re.IGNORECASE | re.DOTALL,
        )
        snippets = re.findall(
            r'<a class="result__snippet".*?>(.+?)</a>|<div class="result__snippet".*?>(.+?)</div>',
            page,
            flags=re.IGNORECASE | re.DOTALL,
        )

        results: List[WebSearchResult] = []
        for index, (href, title_html) in enumerate(titles[:count]):
            snippet_parts = snippets[index] if index < len(snippets) else ("", "")
            snippet_html = snippet_parts[0] or snippet_parts[1] or ""
            results.append(WebSearchResult(
                title=self._clean_html(title_html),
                url=self._decode_duckduckgo_url(href),
                description=self._clean_html(snippet_html),
            ))
        if not results:
            _log.warning("DuckDuckGo returned 0 results for %r - HTML layout may have changed.", query)
        return results

    def _decode_duckduckgo_url(self, href: str) -> str:
        if href.startswith("//duckduckgo.com/l/?"):
            parsed = urllib.parse.urlparse("https:" + href)
            params = urllib.parse.parse_qs(parsed.query)
            return params.get("uddg", [href])[0]
        return href

    def _clean_html(self, value: str) -> str:
        text = re.sub(r"<.*?>", "", value or "")
        return html.unescape(text).strip()

    def _dedupe_results(self, results: List[WebSearchResult], count: int) -> List[WebSearchResult]:
        deduped: List[WebSearchResult] = []
        seen: set = set()
        for item in results:
            normalized_url = (item.url or "").strip().lower().rstrip("/")
            if not normalized_url:
                continue
            if normalized_url in seen:
                continue
            seen.add(normalized_url)
            deduped.append(item)
            if len(deduped) >= count:
                break
        return deduped
