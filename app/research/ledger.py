"""Run-scoped record of what the research tools actually returned.

The ledger is the anti-fabrication anchor: an evidence URL cited by an agent only counts as
"verified" if a search tool really returned that URL during the same run. Competitor ratings are
copied from Apple's API responses recorded here, never from model-written text.
"""
import re
import threading
from datetime import datetime, timezone
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

_TRACKING_PARAMS = {"gclid", "fbclid", "ref", "ref_src", "mc_cid", "mc_eid"}
# Two separate caps so that bulk listing rows (a chart call returns up to 50, a store search up to 40) can
# never crowd out the pages that carry evidence. The first real games run filled a single shared cap of 400
# with chart rows before its first web search, so every web result was dropped and all web evidence was
# wrongly marked unverified.
MAX_ENTRIES = 600  # evidence pages: web results, review pages, the chart pages themselves
MAX_BULK_ENTRIES = 2000  # listing rows from charts and store searches
# Keep everything the model was shown (tools return up to 1200 chars) so any claim can be audited against it.
SNIPPET_CHARS = 1200


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_url(url: str | None) -> str | None:
    """Canonical form for comparing URLs: lowercase scheme/host, no fragment, no tracking params,
    no trailing slash, `www.` dropped."""
    if not url or not isinstance(url, str):
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
        ]
    )
    path = parsed.path.rstrip("/")
    if host == "apps.apple.com":
        # Listing URLs differ by tracking params (?uo=4) and by the cosmetic name slug, which can be
        # percent-encoded Arabic or missing: the app id is the identity. /sa/app/<slug>/id123 == /sa/app/id123.
        query = ""
        listing = re.fullmatch(r"/([a-z]{2})/app/(?:[^/]+/)?(id\d+)", unquote(path))
        if listing:
            path = f"/{listing.group(1)}/app/{listing.group(2)}"
    port = f":{parsed.port}" if parsed.port and parsed.port not in (80, 443) else ""
    return urlunparse(("https", host + port, path, "", query, ""))


class EvidenceLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.entries: dict[str, dict] = {}  # normalized url -> entry
        self.apple: dict[str, dict] = {}  # normalized listing url -> record
        self.calls: list[dict] = []
        self._pages = 0  # entries counted against MAX_ENTRIES
        self._bulk = 0  # entries counted against MAX_BULK_ENTRIES
        self.dropped = 0  # results that could not be recorded because a cap was reached

    def record_call(self, tool: str, query: str, ok: bool, result_count: int = 0, error: str | None = None) -> None:
        with self._lock:
            self.calls.append(
                {"tool": tool, "query": query[:300], "ok": ok, "results": result_count,
                 "error": (error or "")[:300] or None, "at": now_iso()}
            )

    def record_result(
        self, tool: str, url: str, title: str | None, published: str | None, snippet: str | None,
        bulk: bool = False,
    ) -> None:
        key = normalize_url(url)
        if key is None:
            return
        with self._lock:
            if key in self.entries:
                return
            if (self._bulk >= MAX_BULK_ENTRIES) if bulk else (self._pages >= MAX_ENTRIES):
                self.dropped += 1
                return
            if bulk:
                self._bulk += 1
            else:
                self._pages += 1
            self.entries[key] = {
                "url": url, "title": (title or "")[:200] or None, "published_date": published,
                "retrieved_at": now_iso(), "tool": tool, "snippet": (snippet or "")[:SNIPPET_CHARS] or None,
            }

    def record_apple(self, record: dict) -> None:
        key = normalize_url(record.get("url"))
        if key is None:
            return
        with self._lock:
            self.apple.setdefault(key, {**record, "retrieved_at": now_iso()})
        self.record_result("apple_app_store", record["url"], record.get("name"), None, None, bulk=True)

    def lookup(self, url: str | None) -> dict | None:
        key = normalize_url(url)
        return self.entries.get(key) if key else None

    def lookup_apple(self, url: str | None) -> dict | None:
        key = normalize_url(url)
        return self.apple.get(key) if key else None

    def successful_searches(self) -> int:
        return sum(1 for c in self.calls if c["ok"] and c["results"] > 0)

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "calls": list(self.calls),
                "sources": list(self.entries.values()),
                "apple_records": list(self.apple.values()),
                "dropped_sources": self.dropped,
            }
