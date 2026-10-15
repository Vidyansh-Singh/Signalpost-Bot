"""Polite, cached, robots-respecting HTTP fetcher (stdlib only). Failures are values."""
from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from .core import utcnow

UA = "SignalpostResearchBot/1.0 (evidence-backed company profiles; respects robots.txt)"
MAX_BYTES = 3_000_000


@dataclass
class FetchResult:
    url: str
    final_url: str | None = None
    status: int | None = None
    ok: bool = False
    body: bytes = b""
    text: str = ""
    content_type: str = ""
    content_sha256: str = ""
    error: str | None = None
    blocked_by_robots: bool = False
    retrieved_at: str = field(default_factory=utcnow)
    from_cache: bool = False

    @property
    def is_html(self) -> bool:
        return "html" in (self.content_type or "").lower()


class Fetcher:
    def __init__(self, cache_dir: str | Path | None = None, *, timeout: float = 15.0,
                 delay: float = 1.0, obey_robots: bool = True, retries: int = 2):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout, self.delay = timeout, delay
        # open government API tolerates fast access; third-party and company hosts stay polite
        self.host_delay = {"data.brreg.no": min(delay, 0.05), "api.gleif.org": min(delay, 0.3)}
        self.obey_robots, self.retries = obey_robots, retries
        self.requests = self.cache_hits = self.robots_denied = 0
        self.hosts: set[str] = set()
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    # ---- cache ----
    def _cp(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")

    def _cache_get(self, url: str, ttl: int) -> FetchResult | None:
        p = self._cp(url)
        if ttl <= 0 or not p or not p.exists():
            return None
        try:
            d = json.loads(p.read_text("utf-8"))
            if time.time() - d.pop("_t", 0) > ttl:
                return None
            body = bytes.fromhex(d.pop("body_hex", ""))
            names = {f.name for f in fields(FetchResult)}
            r = FetchResult(**{k: v for k, v in d.items() if k in names and k not in ("body", "text", "from_cache")})
            r.body, r.text, r.from_cache = body, body.decode("utf-8", "replace"), True
            return r
        except Exception:
            return None

    def _cache_put(self, r: FetchResult) -> None:
        p = self._cp(r.url)
        if not p:
            return
        d = {f.name: getattr(r, f.name) for f in fields(r)
             if f.name not in ("body", "text", "from_cache")}
        d["body_hex"], d["_t"] = r.body.hex(), time.time()
        try:
            p.write_text(json.dumps(d), "utf-8")
        except Exception:
            pass

    # ---- politeness ----
    def _host_lock(self, host: str) -> threading.Lock:
        with self._lock:
            return self._host_locks.setdefault(host, threading.Lock())

    def _wait(self, host: str) -> None:
        with self._host_lock(host):
            gap = self.host_delay.get(host, self.delay) - (time.monotonic() - self._last.get(host, 0.0))
            if gap > 0:
                time.sleep(gap)
            self._last[host] = time.monotonic()

    def _allowed(self, url: str) -> bool:
        if not self.obey_robots:
            return True
        pr = urlparse(url)
        host = pr.netloc
        with self._lock:
            known = host in self._robots
        if not known:
            rp: RobotFileParser | None = RobotFileParser()
            try:
                self._wait(host)
                req = urllib.request.Request(f"{pr.scheme}://{host}/robots.txt",
                                             headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    rp.parse(resp.read(512_000).decode("utf-8", "replace").splitlines())
            except Exception:
                rp = None  # unreachable robots.txt => allowed, politely
            with self._lock:
                self._robots[host] = rp
        rp = self._robots.get(host)
        return True if rp is None else rp.can_fetch(UA, url)

    # ---- public ----
    def get(self, url: str, *, ttl: int = 0, accept: str | None = None) -> FetchResult:
        if not url or not url.startswith(("http://", "https://")):
            return FetchResult(url=url or "", error="invalid_url")
        hit = self._cache_get(url, ttl)
        if hit:
            self.cache_hits += 1
            return hit
        if not self._allowed(url):
            self.robots_denied += 1
            return FetchResult(url=url, error="robots_disallow", blocked_by_robots=True)
        host = urlparse(url).netloc
        self.hosts.add(host)
        last = FetchResult(url=url, error="unattempted")
        for attempt in range(self.retries + 1):
            self._wait(host)
            self.requests += 1
            headers = {"User-Agent": UA, "Accept-Language": "nb,no,en;q=0.7"}
            if accept:
                headers["Accept"] = accept
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read(MAX_BYTES)
                    charset = resp.headers.get_content_charset() or "utf-8"
                    out = FetchResult(
                        url=url, final_url=resp.geturl(), status=resp.status, ok=True,
                        body=body, text=body.decode(charset, "replace"),
                        content_type=resp.headers.get("content-type", ""),
                        content_sha256=hashlib.sha256(body).hexdigest())
                    self._cache_put(out) if ttl > 0 else None
                    return out
            except urllib.error.HTTPError as e:
                last = FetchResult(url=url, status=e.code, error=f"http_{e.code}")
                if e.code not in (429, 500, 502, 503, 504):
                    return last
            except Exception as e:  # noqa: BLE001 - failures are values
                reason = getattr(e, "reason", e)
                last = FetchResult(url=url, error=type(e).__name__)
                if isinstance(reason, (socket.gaierror, ConnectionRefusedError)) or "certificate" in str(reason).lower():
                    return last  # dead/unresolvable host: retrying only burns the time budget
            if attempt < self.retries:
                time.sleep(1.5 * (attempt + 1))
        return last

    def get_json(self, url: str, *, ttl: int = 0):
        r = self.get(url, ttl=ttl, accept="application/json")
        if not r.ok:
            return None, r
        try:
            return json.loads(r.text), r
        except Exception:
            r.error, r.ok = "json_decode_error", False
            return None, r
