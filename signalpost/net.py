"""HTTP access with a hard request budget, robots.txt, a safe-URL policy and content-addressed snapshots.

Every HTTP attempt (redirect hops, retries and robots.txt included) is counted against the budget.
Every response body is written to snapshots/<sha256>.<ext> so each claim can cite the exact bytes.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

AGENT_NAME = "SignalpostResearchAgent"
USER_AGENT = f"{AGENT_NAME}/1.0 (+https://github.com/anur4ag/signalpost)"
MAX_CRAWL_DELAY = 10.0  # a site asking for a longer delay is skipped, never fetched faster than it asks
API_HOSTS = {"data.brreg.no"}  # open-data APIs: no per-request delay, bounded concurrency instead
EXT = {"json": ".json", "html": ".html", "xml": ".xml", "pdf": ".pdf", "text/plain": ".txt"}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Resp:
    url: str
    final_url: str
    status: int | None
    body: bytes = b""
    content_type: str = ""
    retrieved_at: str = ""
    sha256: str | None = None
    snapshot_path: str | None = None
    redirects: list = field(default_factory=list)
    error: str | None = None
    redacted: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.error is None

    @property
    def blocked(self) -> bool:
        return self.error in {"robots_disallowed", "robots_crawl_delay_too_long", "unsafe_url"} or self.status in {401, 403, 429}

    def text(self) -> str:
        charset = (re.search(r"charset=([\w-]+)", self.content_type or "", re.I)
                   or re.search(r"<meta[^>]+charset=[\"']?([\w-]+)", self.body[:4000].decode("ascii", "ignore"), re.I))
        try:
            return self.body.decode(charset.group(1) if charset else "utf-8", "replace")
        except LookupError:
            return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class Robots:
    """RFC 9309 robots.txt: the product-token group (else "*"), longest match wins, Allow wins ties, `*` and `$` operators."""

    def __init__(self, text: str, agent: str = AGENT_NAME):
        groups, agents, rules, delay, in_rules = [], [], [], None, False
        self.sitemaps: list[str] = []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key in {"user-agent", "useragent"}:
                if in_rules:
                    groups.append((agents, rules, delay))
                    agents, rules, delay, in_rules = [], [], None, False
                agents.append(value.lower())
            elif key in {"allow", "disallow"}:
                in_rules = True
                if agents and value:  # an empty rule matches nothing
                    rules.append((key == "allow", value))
            elif key == "crawl-delay":
                in_rules = True
                try:
                    delay = float(value)
                except ValueError:
                    pass
            elif key == "sitemap" and value:
                self.sitemaps.append(value)
        if agents:
            groups.append((agents, rules, delay))
        token = agent.lower()
        mine = [g for g in groups if any(a != "*" and (a == token or token.startswith(a)) for a in g[0])]
        mine = mine or [g for g in groups if "*" in g[0]]
        self.rules = [(allow, pattern, self._compile(pattern)) for g in mine for allow, pattern in g[1]]
        delays = [g[2] for g in mine if g[2] is not None]
        self.delay = max(delays) if delays else 0.0

    @staticmethod
    def normalize(text: str, operators: bool) -> str:
        """RFC 9309 §2.2.2-3 octets: non-ASCII is percent-encoded, %XX of unreserved characters is decoded, other escapes
        stay encoded (upper-case hex). In a URI a literal '*' or '$' becomes %2A / %24 so it can only match an encoded
        pattern; in a pattern the bare '*' and '$' stay operators while %2A / %24 stay literal."""
        out, i = [], 0
        while i < len(text):
            ch = text[i]
            if ch == "%" and re.fullmatch(r"[0-9A-Fa-f]{2}", text[i + 1:i + 3]):
                decoded = chr(int(text[i + 1:i + 3], 16))
                out.append(decoded if re.fullmatch(r"[A-Za-z0-9._~-]", decoded) else "%" + text[i + 1:i + 3].upper())
                i += 3
                continue
            if ord(ch) > 127:
                out.append("".join(f"%{b:02X}" for b in ch.encode("utf-8")))
            elif ch in "*$" and not operators:
                out.append("%2A" if ch == "*" else "%24")
            else:
                out.append(ch)
            i += 1
        return "".join(out)

    @classmethod
    def _compile(cls, pattern: str) -> re.Pattern:
        pattern = cls.normalize(pattern, operators=True)
        anchored = pattern.endswith("$")
        body = "".join(".*" if ch == "*" else re.escape(ch) for ch in (pattern[:-1] if anchored else pattern))
        return re.compile(body + (r"\Z" if anchored else ""), re.S)

    def allowed(self, url: str) -> bool:
        """A query may be written literally or with its reserved characters percent-encoded (RFC 9309 §2.2.2 table);
        the URL is fetched only if both spellings are allowed."""
        parts = urllib.parse.urlsplit(url)
        if (parts.path or "/") == "/robots.txt":
            return True
        spellings = {parts.query, re.sub(r"[:/?#\[\]@!$'()*+,;]", lambda m: "%%%02X" % ord(m.group()), parts.query)} if parts.query else {""}
        return all(self._allowed(self.normalize((parts.path or "/") + (f"?{q}" if q else ""), operators=False)) for q in spellings)

    def _allowed(self, path: str) -> bool:
        best_len, best_allow = -1, True
        for allow, pattern, regex in self.rules:
            size = len(self.normalize(pattern, operators=True))  # most octets wins
            if regex.match(path) and (size > best_len or (size == best_len and allow)):
                best_len, best_allow = size, allow
        return best_allow


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


_DNS: dict[str, bool | None] = {}


def resolve_public(host: str) -> bool | None | str:
    """True: every address is publicly routable. False: some address is private (SSRF guard). None: no such host.
    "retry": DNS kept failing temporarily (not cached, and never treated as proof of absence)."""
    if host in _DNS:
        return _DNS[host]
    for attempt in range(3):
        try:
            infos = socket.getaddrinfo(host, None)
            _DNS[host] = bool(infos) and all(ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)
            return _DNS[host]
        except socket.gaierror as error:
            if error.errno != socket.EAI_AGAIN:
                _DNS[host] = None
                return None
            time.sleep(0.5 * (attempt + 1))
        except (UnicodeError, OSError):
            _DNS[host] = None
            return None
    return "retry"


def decode_body(body: bytes, encoding: str) -> bytes:
    """Undo Content-Encoding; some servers compress even when not asked. Unknown encodings stay raw."""
    encoding = (encoding or "").lower().strip()
    try:
        if encoding in {"gzip", "x-gzip"} or body[:2] == b"\x1f\x8b":
            return zlib.decompress(body, 16 + zlib.MAX_WBITS)
        if encoding == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except zlib.error:
        pass
    return body


def unsafe_reason(url: str) -> str | None:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "unparseable_url"
    if parts.scheme not in {"http", "https"}:
        return "non_http_scheme"
    if parts.username or parts.password:
        return "embedded_credentials"
    try:
        port = parts.port
    except ValueError:
        return "bad_port"
    if port not in {None, 80, 443}:
        return "non_standard_port"
    if not parts.hostname:
        return "no_host"
    public = resolve_public(parts.hostname)
    if public is None:
        return "dns_failed"
    if public == "retry":
        return "dns_temporary"
    return None if public is True else "non_public_address"


def ext_for(content_type: str) -> str:
    content_type = (content_type or "").lower()
    for key, ext in EXT.items():
        if key in content_type:
            return ext
    return ".bin"


class Fetcher:
    def __init__(self, out_dir: Path, max_requests: int = 1900, timeout: float = 12.0, web_delay: float = 1.0):
        self.out_dir = Path(out_dir)
        self.snap_dir = self.out_dir / "snapshots"
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        self.max_requests = max_requests
        self.timeout = timeout
        self.web_delay = web_delay
        self.requests = 0
        self.bytes = 0
        self.latencies: list[int] = []
        self.by_host: dict[str, int] = {}
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_next: dict[str, float] = {}
        self._robots: dict[str, tuple[Robots | None, str]] = {}
        self._api_slots = threading.BoundedSemaphore(6)
        self._failures: dict[str, int] = {}  # consecutive 5xx/network failures per service, for the retry breaker

    # -- bookkeeping -------------------------------------------------------------------------
    def _take(self, host: str) -> bool:
        with self._lock:
            if self.requests >= self.max_requests:
                return False
            self.requests += 1
            self.by_host[host] = self.by_host.get(host, 0) + 1
            return True

    def remaining(self) -> int:
        return self.max_requests - self.requests

    def snapshot(self, body: bytes, content_type: str) -> tuple[str, str]:
        digest = sha256(body)
        rel = f"snapshots/{digest}{ext_for(content_type)}"
        path = self.out_dir / rel
        if not path.exists():
            tmp = path.with_suffix(path.suffix + f".{threading.get_ident()}.tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
        return digest, rel

    def _host_lock(self, host: str) -> threading.Lock:
        with self._lock:
            return self._host_locks.setdefault(host, threading.Lock())

    # -- one HTTP attempt ----------------------------------------------------------------------
    def _attempt(self, url: str, accept: str, max_bytes: int, headers: dict | None = None) -> tuple[int | None, str, bytes, str | None, str | None]:
        host = urllib.parse.urlsplit(url).hostname or ""
        if not self._take(host):
            return None, "", b"", None, "budget_exhausted"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8,en;q=0.6",
                                                       "Accept-Encoding": "gzip, deflate", **(headers or {})})
        started = time.monotonic()
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                body = response.read(max_bytes + 1)
                status, headers = response.status, response.headers
        except urllib.error.HTTPError as error:
            status, headers = error.code, error.headers
            try:
                body = error.read(max_bytes + 1)
            except Exception:
                body = b""
        except Exception as error:  # timeouts, TLS, connection resets
            with self._lock:
                self.latencies.append(int((time.monotonic() - started) * 1000))
            return None, "", b"", None, f"network_error:{type(error).__name__}"
        with self._lock:
            self.latencies.append(int((time.monotonic() - started) * 1000))
            self.bytes += len(body)
        location = headers.get("Location") if headers else None
        body = decode_body(body[:max_bytes], headers.get("Content-Encoding") if headers else "")
        return status, (headers.get("Content-Type") if headers else "") or "", body[:max_bytes], location, None

    def _wait_turn(self, host: str, delay: float) -> None:
        with self._host_lock(host):
            now = time.monotonic()
            wait = self._host_next.get(host, 0) - now
            self._host_next[host] = max(now, self._host_next.get(host, 0)) + delay
        if wait > 0:
            time.sleep(wait)

    # -- robots.txt ----------------------------------------------------------------------------
    def robots(self, url: str) -> tuple[str, float]:
        """("allow" | "disallow" | "slow" | "unreachable", crawl_delay).
        RFC 9309: a 4xx robots.txt means allow all; a 5xx or unreachable robots.txt means disallow all."""
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._host_lock("robots:" + origin):
            if origin not in self._robots:
                response = self.get(origin + "/robots.txt", accept="text/plain,*/*", robots=False)
                if response.status is None:
                    self._robots[origin] = (None, "unreachable")
                elif response.ok:
                    self._robots[origin] = (Robots(response.text()), "parse")
                else:
                    self._robots[origin] = (None, "allow" if 400 <= response.status < 500 else "disallow")
        parsed, rule = self._robots[origin]
        if rule != "parse":
            return rule, 0.0
        if parsed.delay > MAX_CRAWL_DELAY:
            return "slow", parsed.delay
        return ("allow" if parsed.allowed(url) else "disallow"), parsed.delay

    def sitemaps(self, origin: str) -> list[str]:
        parsed = self._robots.get(origin, (None,))[0]
        return list(parsed.sitemaps) if parsed else []

    # -- public GET ----------------------------------------------------------------------------
    def get(self, url: str, accept: str = "*/*", robots: bool = True, max_redirects: int = 4,
            retries: int = 0, max_bytes: int = 4_000_000, headers: dict | None = None, save: bool = True,
            redact=None) -> Resp:
        """save=False is for discovery-only reads (e.g. job-feed index pages) that no claim cites.
        redact(body) -> bytes removes personal data before the snapshot is written; the evidence hash covers the saved copy."""
        retrieved_at = utc_now()
        redirects: list[dict] = []
        current = url
        for _ in range(max_redirects + 1):
            reason = unsafe_reason(current)
            if reason:
                error = {"dns_failed": "dns_not_found", "dns_temporary": "network_error:dns_temporary"}.get(reason, "unsafe_url")
                return Resp(url, current, None, retrieved_at=retrieved_at, redirects=redirects, error=error)
            host = urllib.parse.urlsplit(current).hostname or ""
            delay = 0.0
            if robots and host not in API_HOSTS:
                verdict, delay = self.robots(current)
                if verdict != "allow":
                    error = {"unreachable": "host_unreachable", "slow": "robots_crawl_delay_too_long"}.get(verdict, "robots_disallowed")
                    return Resp(url, current, None, retrieved_at=retrieved_at, redirects=redirects, error=error)
            service = "/".join(current.split("/")[:4])
            if self._failures.get(service, 0) >= 30:
                retries = 0  # ponytail: circuit breaker; a service in outage gets one attempt per call, not four
            for attempt in range(retries + 1):
                if host in API_HOSTS:
                    with self._api_slots:
                        status, ctype, body, location, error = self._attempt(current, accept, max_bytes, headers)
                else:
                    self._wait_turn(host, max(self.web_delay, delay))
                    status, ctype, body, location, error = self._attempt(current, accept, max_bytes, headers)
                transient = bool(error and error != "budget_exhausted") or status in {429, 500, 502, 503, 504}
                with self._lock:
                    self._failures[service] = self._failures.get(service, 0) + 1 if transient else 0
                if error == "budget_exhausted" or not transient or attempt == retries:
                    break
                time.sleep(2 * (attempt + 1))
            retrieved_at = utc_now()
            if error:
                return Resp(url, current, status, retrieved_at=retrieved_at, redirects=redirects, error=error)
            if status in {301, 302, 303, 307, 308} and location:
                target = urllib.parse.urljoin(current, location)
                redirects.append({"from": current, "to": target, "status": status})
                current = target
                continue
            saved = redact(body) if redact and body and status == 200 else body
            digest, rel = self.snapshot(saved, ctype) if saved and save else (sha256(saved) if saved else None, None)
            return Resp(url, current, status, body, ctype, retrieved_at, digest, rel, redirects, redacted=saved is not body)
        return Resp(url, current, None, retrieved_at=retrieved_at, redirects=redirects, error="too_many_redirects")
