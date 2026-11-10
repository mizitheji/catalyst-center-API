#!/usr/bin/env python3
"""
Cisco Catalyst Center (DNA Center) API Menu Tool
--------------------------------------------------
Interactive CLI *and* headless exporter for common read-only Catalyst Center
API calls: health, device inventory, site health, client health, issues,
compliance, software images, plus a per-site network diagram.

Interactive:  python3 dnac_menu.py
Headless:     python3 dnac_menu.py --run devices compliance --format csv

Author: mizitheji
License: MIT
"""

from __future__ import annotations

import argparse
import csv
import getpass
import html
import json
import os
import re
import sys
import threading
import time
import traceback
import webbrowser
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

import requests
import urllib3
from colorama import Fore, Style, init as colorama_init
from requests.adapters import HTTPAdapter
from tabulate import tabulate
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
TIMEOUT = (5, 60)            # (connect, read) seconds - big list calls need a long read
TOKEN_MAX_AGE = 50 * 60      # Catalyst Center tokens live ~60 min; refresh early
PAGE_SIZE = 500              # max for most list endpoints
SITE_PAGE_SIZE = 50          # documented max for site-health
MAX_PAGES = 200              # hard stop so a misbehaving endpoint can never loop forever
WORKERS = 5                  # parallel calls; keep low, the controller rate-limits
DISPLAY_LIMIT = 100          # rows printed before "show all" is required
CACHE_TTL = 120              # seconds to reuse device/site lists within one session
COMPLIANCE_FALLBACK_MAX = 200
AUTH_PATH = "/dna/system/api/v1/auth/token"


class ApiError(Exception):
    """Any failure talking to Catalyst Center (network, TLS, auth, HTTP, JSON)."""


class NoData(Exception):
    """The call worked but there is nothing to show."""


# --------------------------------------------------------------------------- #
# .env loader (no external dependency)
# --------------------------------------------------------------------------- #
def _parse_env_value(raw: str) -> str:
    raw = raw.strip()
    # Only strip a *matching* pair of quotes, so a password ending in " or ' survives.
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    cut = raw.find(" #")  # inline comment (unquoted values only)
    return (raw[:cut] if cut != -1 else raw).strip()


def load_dotenv(filename: str = ".env") -> None:
    """Populate os.environ from .env (script dir, then cwd). Real env vars win."""
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, os.getcwd()):
        path = os.path.join(base, filename)
        if not os.path.isfile(path):
            continue
        try:
            if os.name == "posix" and os.stat(path).st_mode & 0o077:
                print(Fore.YELLOW + f"Warning: {path} is readable by other users (chmod 600 it).",
                      file=sys.stderr)
        except OSError:
            pass
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:]
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), _parse_env_value(value))
        return


def env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def pick(d: dict, *keys: str, default="-"):
    """First non-empty value among several possible field names."""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def fmt_ts(v):
    """Epoch-milliseconds -> 'YYYY-MM-DD HH:MM'; anything else passes through."""
    try:
        return datetime.fromtimestamp(int(v) / 1000).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return v if v not in (None, "") else "-"


def log(msg: str) -> None:
    print(Fore.CYAN + msg, file=sys.stderr)


def parallel_map(fn: Callable, items: list, workers: int = WORKERS, label: str = "") -> list:
    """Run fn over items in a small thread pool; Ctrl-C cancels queued work."""
    results = [None] * len(items)
    if not items:
        return results
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fn, it): i for i, it in enumerate(items)}
        try:
            for n, fut in enumerate(as_completed(futures), 1):
                results[futures[fut]] = fut.result()
                if label and (n % 50 == 0 or n == len(items)):
                    log(f"  ...{label}: {n}/{len(items)}")
        except BaseException:
            for f in futures:
                f.cancel()
            raise
    return results


# --------------------------------------------------------------------------- #
# Client: one pooled session, early token refresh, retry/backoff, caching
# --------------------------------------------------------------------------- #
class CatalystCenter:
    def __init__(self, host: str, username: str, password: str,
                 verify: bool = False, ca_bundle: Optional[str] = None):
        host = host.strip().rstrip("/")
        self.base_url = host if "://" in host else f"https://{host}"  # also allows host:port
        self.host = self.base_url.split("://", 1)[1]
        self.username = username
        self.password = password
        self.verify = ca_bundle or verify
        self.quiet = False

        self._token: Optional[str] = None
        self._token_time = 0.0
        self._auth_lock = threading.Lock()
        self._cache: Dict[tuple, Tuple[float, object]] = {}

        # One Session = connection reuse (no new TCP+TLS handshake per call).
        # Retry covers 429 (rate limit; honours Retry-After) and transient 5xx.
        retry = Retry(
            total=3, backoff_factor=1.0,
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=WORKERS * 2)
        self.session = requests.Session()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # -- plumbing ----------------------------------------------------------- #
    def _log(self, msg: str) -> None:
        if not self.quiet:
            log(msg)

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        try:
            return self.session.request(method, url, verify=self.verify, timeout=TIMEOUT, **kw)
        except requests.exceptions.SSLError as e:
            raise ApiError("TLS verification failed. Install a trusted cert, set DNAC_CA_BUNDLE, "
                           "or set DNAC_VERIFY_SSL=false.") from e
        except requests.exceptions.Timeout as e:
            raise ApiError("Connection timed out.") from e
        except requests.exceptions.ConnectionError as e:
            raise ApiError(f"Could not connect to {self.host}. Check the IP/hostname and network path.") from e
        except requests.exceptions.RequestException as e:
            raise ApiError(f"Request error: {e}") from e

    def authenticate(self) -> None:
        resp = self._request("POST", f"{self.base_url}{AUTH_PATH}", auth=(self.username, self.password))
        if resp.status_code in (401, 403):
            raise ApiError(f"Authentication failed (HTTP {resp.status_code}): "
                           "check username/password and that the account has API access.")
        if not resp.ok:
            raise ApiError(f"Authentication failed: HTTP {resp.status_code} - {resp.text[:200]}")
        try:
            self._token = resp.json()["Token"]
        except (ValueError, KeyError) as e:
            raise ApiError("Auth response did not contain a token.") from e
        self._token_time = time.monotonic()

    def _ensure_token(self) -> None:
        if self._token and time.monotonic() - self._token_time < TOKEN_MAX_AGE:
            return
        with self._auth_lock:  # threads share one refresh instead of each re-authing
            if not self._token or time.monotonic() - self._token_time >= TOKEN_MAX_AGE:
                self.authenticate()

    def _refresh_token(self, stale: Optional[str]) -> None:
        with self._auth_lock:
            if self._token == stale:  # nobody refreshed it while we waited
                self.authenticate()

    # -- public API --------------------------------------------------------- #
    def get(self, path: str, params: Optional[dict] = None):
        """GET -> parsed JSON. Raises ApiError. Re-auths once on 401."""
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.base_url}{path}"
        self._ensure_token()
        resp = None
        for attempt in (1, 2):
            sent = self._token
            resp = self._request("GET", url, params=params,
                                 headers={"X-Auth-Token": sent, "Accept": "application/json"})
            if resp.status_code == 401 and attempt == 1:
                self._log("Token rejected, re-authenticating...")
                self._refresh_token(sent)
                continue
            break
        if not resp.ok:
            raise ApiError(f"HTTP {resp.status_code} for {path}: {resp.text[:300].strip()}")
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as e:
            raise ApiError(f"Non-JSON response from {path}") from e

    def _cache_get(self, key: tuple, ttl: int):
        hit = self._cache.get(key)
        if ttl and hit and time.monotonic() - hit[0] < ttl:
            return list(hit[1]) if isinstance(hit[1], list) else hit[1]
        return None

    def get_cached(self, path: str, params: Optional[dict] = None, ttl: int = CACHE_TTL):
        """GET with a short in-session cache (for big single-object payloads like topology)."""
        key = ("get", path, json.dumps(params or {}, sort_keys=True))
        hit = self._cache_get(key, ttl)
        if hit is not None:
            return hit
        data = self.get(path, params)
        self._cache[key] = (time.monotonic(), data)
        return data

    def get_all_pages(self, path: str, params: Optional[dict] = None,
                      page_size: int = PAGE_SIZE, cache_ttl: int = 0) -> list:
        """Walk offset/limit (1-indexed) until exhausted.

        Safe against endpoints that ignore offset/limit: a repeated page stops the
        loop instead of spinning forever (the old version could do that).
        """
        key = ("pages", path, json.dumps(params or {}, sort_keys=True), page_size)
        cached = self._cache_get(key, cache_ttl)
        if cached is not None:
            return cached

        items: list = []
        seen = set()
        offset = 1
        for _ in range(MAX_PAGES):
            data = self.get(path, params={**(params or {}), "offset": offset, "limit": page_size})
            batch = data.get("response") if isinstance(data, dict) else None
            if not batch or not isinstance(batch, list):
                break
            fingerprint = json.dumps(batch[0], sort_keys=True, default=str)
            if fingerprint in seen:
                self._log(Fore.YELLOW + f"  {path} ignored offset/limit (repeated page) - stopping.")
                break
            seen.add(fingerprint)
            items.extend(batch)
            self._log(f"  ...fetched {len(items)} record(s) so far")
            if len(batch) < page_size:
                break
            offset += page_size
        else:
            self._log(Fore.YELLOW + f"  Hit the {MAX_PAGES}-page safety limit on {path}.")

        if cache_ttl:
            self._cache[key] = (time.monotonic(), list(items))
        return items

    def devices(self) -> list:
        """Full device list. First page sequentially; if it is full, the rest in parallel."""
        key = ("devices",)
        cached = self._cache_get(key, CACHE_TTL)
        if cached is not None:
            return cached
        path = "/dna/intent/api/v1/network-device"
        data = self.get(path, params={"offset": 1, "limit": PAGE_SIZE})
        items = list((data or {}).get("response") or [])
        if len(items) >= PAGE_SIZE:
            try:
                total = int(self.get(path + "/count")["response"])
                offsets = list(range(PAGE_SIZE + 1, total + 1, PAGE_SIZE))

                def fetch(off):
                    d = self.get(path, params={"offset": off, "limit": PAGE_SIZE})
                    return (d or {}).get("response") or []

                for page in parallel_map(fetch, offsets, label="device pages"):
                    items.extend(page)
            except (ApiError, KeyError, TypeError, ValueError):
                items = self.get_all_pages(path)  # fall back to plain sequential walk
        self._log(f"  ...fetched {len(items)} device(s)")
        self._cache[key] = (time.monotonic(), list(items))
        return items

    def site_names(self) -> Dict[str, str]:
        try:
            sites = self.get_all_pages("/dna/intent/api/v1/site-health",
                                       page_size=SITE_PAGE_SIZE, cache_ttl=300)
        except ApiError:
            return {}
        return {s["siteId"]: s.get("siteName", s["siteId"]) for s in sites if s.get("siteId")}


# --------------------------------------------------------------------------- #
# Table model + display (filter, sort, export)
# --------------------------------------------------------------------------- #
@dataclass
class Table:
    headers: List[str]
    rows: List[list]
    notes: List[str] = field(default_factory=list)
    name: str = "table"


def _csv_safe(v):
    """Stop device-supplied text (hostnames, issue names) from becoming Excel formulas."""
    if isinstance(v, str) and v:
        if v[0] in "=+@" or (v[0] == "-" and len(v) > 1 and not v[1:].replace(".", "", 1).isdigit()):
            return "'" + v
    return v


def export_table(table: Table, out_dir: str = "exports", fmt: str = "csv") -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{table.name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{fmt}")
    if fmt == "json":
        with open(path, "w", encoding="utf-8") as f:
            json.dump([dict(zip(table.headers, r)) for r in table.rows], f, indent=2, default=str)
    else:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:  # BOM: Excel-friendly
            w = csv.writer(f)
            w.writerow(table.headers)
            w.writerows([[_csv_safe(c) for c in r] for r in table.rows])
    return path


def _sort_key(v):
    try:
        return (0, float(str(v).rstrip("%")), "")
    except ValueError:
        return (1, 0.0, str(v).lower())


def _find_col(headers: List[str], text: str) -> Optional[int]:
    text = text.strip().lower()
    for i, h in enumerate(headers):
        if h.lower().startswith(text):
            return i
    return None


def apply_filter(rows: List[list], headers: List[str], term: str) -> List[list]:
    """'text' = any column | 'col:text' = one column | '!text' = exclude."""
    negate = term.startswith("!")
    if negate:
        term = term[1:]
    col = None
    if ":" in term:
        head, _, val = term.partition(":")
        col = _find_col(headers, head)
        if col is not None:
            term = val
    t = term.strip().lower()

    def hit(r):
        cells = [r[col]] if col is not None else r
        return any(t in str(c).lower() for c in cells)

    return [r for r in rows if hit(r) != negate]


HELP = ("Filter: text | col:text | !text    sort <col> [desc]    x = export CSV    "
        "a = show all    c = clear    Enter = back")


def display_table(table: Table, export_dir: str = "exports") -> None:
    if not table.rows:
        print(Fore.YELLOW + "No data to display.")
        return
    current = table.rows
    show_all = False
    while True:
        shown = current if show_all else current[:DISPLAY_LIMIT]
        print(tabulate(shown, headers=table.headers, tablefmt="fancy_grid"))
        print(Fore.GREEN + f"{len(shown)} row(s) shown ({len(current)} matching, {len(table.rows)} total)")
        if len(shown) < len(current):
            print(Fore.YELLOW + f"Only the first {DISPLAY_LIMIT} are printed - filter, 'a' to show all, or 'x' to export.")
        try:
            cmd = input(Fore.CYAN + HELP + "\n> ").strip()
        except EOFError:
            return
        low = cmd.lower()
        if cmd == "":
            return
        if low == "c":
            current, show_all = table.rows, False
        elif low == "a":
            show_all = True
        elif low == "x":
            path = export_table(Table(table.headers, current, name=table.name), export_dir)
            print(Fore.GREEN + f"Exported {len(current)} row(s) to {path}")
        elif low.startswith("sort "):
            parts = cmd[5:].rsplit(" ", 1)
            desc = len(parts) == 2 and parts[1].lower() == "desc"
            col = _find_col(table.headers, parts[0] if desc or len(parts) == 1 else cmd[5:])
            if col is None:
                print(Fore.YELLOW + "Unknown column. Columns: " + ", ".join(table.headers))
            else:
                current = sorted(current, key=lambda r: _sort_key(r[col]), reverse=desc)
        else:
            result = apply_filter(table.rows, table.headers, cmd)
            if result:
                current = result
            else:
                print(Fore.YELLOW + f"No rows match '{cmd}'.")


# --------------------------------------------------------------------------- #
# Reports: each fetches data and returns a Table (no printing, no input()).
# That split is what makes the headless/CSV mode possible.
# --------------------------------------------------------------------------- #
def fetch_network_health(dnac: CatalystCenter) -> Table:
    data = dnac.get("/dna/intent/api/v1/network-health")
    if not isinstance(data, dict):
        raise NoData("No health data returned.")
    # Cisco's per-category field really is spelled 'healthDistirubution'.
    entries = (data.get("healthDistirubution") or data.get("healthDistribution")
               or data.get("response") or [])
    if not entries:
        raise NoData("No health data returned.")
    rows = []
    for e in entries:
        gp = pick(e, "goodPercentage")
        rows.append([pick(e, "category"), pick(e, "totalCount"), pick(e, "goodCount"),
                     pick(e, "fairCount"), pick(e, "badCount"),
                     pick(e, "unmonCount", "noHealthCount"),
                     f"{gp}%" if gp != "-" else "-"])
    notes = []
    if data.get("latestHealthScore") is not None:
        notes.append(f"Overall health score: {data['latestHealthScore']}  |  monitored devices: "
                     f"{data.get('monitoredDevices', '-')} of {data.get('totalDevices', '-')}")
    return Table(["Category", "Total", "Good", "Fair", "Bad", "No Data", "Good %"], rows, notes)


def dedupe_pids(raw) -> str:
    """platformId can be a comma-separated list for stacked switches; dedupe, keep order."""
    seen: List[str] = []
    for pid in str(raw or "").split(","):
        pid = pid.strip()
        if pid and pid not in seen:
            seen.append(pid)
    return ", ".join(seen) if seen else "-"


def fetch_devices(dnac: CatalystCenter) -> Table:
    devices = dnac.devices()
    if not devices:
        raise NoData("No devices returned.")
    rows = sorted(
        ([d.get("hostname", "-"), d.get("managementIpAddress", "-"), dedupe_pids(d.get("platformId")),
          d.get("family", "-"), d.get("softwareVersion", "-"),
          d.get("reachabilityStatus", "-"), d.get("collectionStatus", "-")] for d in devices),
        key=lambda r: str(r[0]).lower())
    reach = Counter(str(d.get("reachabilityStatus", "-")) for d in devices)
    notes = [f"Total devices: {len(devices)}  |  " + "  ".join(f"{k}: {v}" for k, v in reach.most_common())]
    return Table(["Hostname", "Mgmt IP", "Model (PID)", "Family", "SW Version",
                  "Reachability", "Collection"], rows, notes)


def fetch_site_health(dnac: CatalystCenter) -> Table:
    sites = dnac.get_all_pages("/dna/intent/api/v1/site-health", page_size=SITE_PAGE_SIZE, cache_ttl=300)
    if not sites:
        raise NoData("No site health data returned.")
    rows = []
    for s in sites:
        total = s.get("numberOfNetworkDevice", 0) or 0
        pct = s.get("healthyNetworkDevicePercentage") or 0
        good = round(total * pct / 100) if total else 0
        rows.append([s.get("siteName", "-"), s.get("siteType", "-"), total, good, total - good,
                     s.get("networkHealthAverage", "-"), s.get("clientHealthWired", "-"),
                     s.get("clientHealthWireless", "-")])
    notes = ["'Good'/'Poor' = device health score, NOT reachability. See Device Inventory for reachability."]
    return Table(["Site", "Type", "Total", "Good Health", "Poor Health", "Net Health %",
                  "Wired Health %", "Wireless Health %"], rows, notes)


def fetch_client_health(dnac: CatalystCenter) -> Table:
    data = dnac.get("/dna/intent/api/v1/client-health")
    entries = (data or {}).get("response") if isinstance(data, dict) else None
    if not entries:
        raise NoData("No client health data returned.")
    names = dnac.site_names()  # siteId is a UUID; show the human name instead
    rows = []
    for c in entries:
        sid = c.get("siteId", "-")
        for score in c.get("scoreDetail", []):
            rows.append([names.get(sid, sid), (score.get("scoreCategory") or {}).get("value", "-"),
                         score.get("scoreValue", "-"), score.get("clientCount", "-")])
    return Table(["Site", "Category", "Score", "Client Count"], rows)


def fetch_issues(dnac: CatalystCenter) -> Table:
    days = int(os.environ.get("DNAC_ISSUE_DAYS", "7") or 7)
    now_ms = int(time.time() * 1000)
    params = {"issueStatus": "ACTIVE", "startTime": now_ms - days * 86_400_000, "endTime": now_ms}
    issues = dnac.get_all_pages("/dna/intent/api/v1/issues", params=params)
    if not issues:
        raise NoData(f"No active issues in the last {days} day(s).")
    rows = sorted(
        ([i.get("name", "-"), i.get("priority", "-"), i.get("category", "-"), i.get("deviceRole", "-"),
          i.get("status", "-"), fmt_ts(pick(i, "last_occurence_time", "lastOccurrenceTime"))] for i in issues),
        key=lambda r: (str(r[1]), str(r[0])))
    return Table(["Issue", "Priority", "Category", "Device Role", "Status", "Last Seen"], rows,
                 [f"Active issues, last {days} day(s): {len(issues)}  (change with DNAC_ISSUE_DAYS)"])


def fetch_compliance(dnac: CatalystCenter) -> Table:
    devices = dnac.devices()  # cached - reused from inventory if it was just run
    if not devices:
        raise NoData("No devices found.")
    names = {d.get("id"): d.get("hostname") or d.get("managementIpAddress") or d.get("id") for d in devices}
    notes: List[str] = []
    rows: List[list] = []

    # Fast path: one bulk call for every device instead of one call per device.
    try:
        bulk = (dnac.get("/dna/intent/api/v1/compliance") or {}).get("response") or []
    except ApiError:
        bulk = []
    for c in bulk:
        rows.append([names.get(c.get("deviceUuid"), c.get("deviceUuid", "-")),
                     c.get("complianceType", "-"), c.get("status", "-"),
                     fmt_ts(c.get("lastSyncTime") or c.get("lastUpdateTime"))])

    # Fallback: per-device detail calls, in parallel, with a visible cap.
    if not rows:
        ids = [d["id"] for d in devices if d.get("id")]
        if len(ids) > COMPLIANCE_FALLBACK_MAX:
            notes.append(f"Bulk compliance call returned nothing; checked first {COMPLIANCE_FALLBACK_MAX} "
                         f"of {len(ids)} devices.")
            ids = ids[:COMPLIANCE_FALLBACK_MAX]
        failed = 0

        def one(dev_id):
            try:
                return dev_id, (dnac.get(f"/dna/intent/api/v1/compliance/{dev_id}/detail") or {}).get("response") or []
            except ApiError:
                return dev_id, None

        for dev_id, detail in parallel_map(one, ids, label="compliance"):
            if detail is None:
                failed += 1
                continue
            for c in detail:
                rows.append([names.get(dev_id, dev_id), c.get("complianceType", "-"), c.get("status", "-"),
                             fmt_ts(c.get("lastSyncTime") or c.get("lastUpdateTime"))])
        if failed:
            notes.append(f"{failed} device(s) could not be queried.")

    if not rows:
        raise NoData("No compliance data returned.")
    rows.sort(key=lambda r: (str(r[2]).upper() == "COMPLIANT", str(r[0]).lower()))  # problems first
    counts = Counter(str(r[2]) for r in rows)
    notes.insert(0, "Results: " + "  ".join(f"{k}: {v}" for k, v in counts.most_common()))
    return Table(["Hostname", "Compliance Type", "Status", "Last Sync"], rows, notes)


def fetch_images(dnac: CatalystCenter) -> Table:
    images = dnac.get_all_pages("/dna/intent/api/v1/image/importation")
    if not images:
        raise NoData("No software image data returned.")
    rows = [[i.get("name", "-"), i.get("version", "-"), i.get("family", "-"), i.get("imageType", "-"),
             "Yes" if i.get("isTaggedGolden") else "-"] for i in images]
    return Table(["Image Name", "Version", "Family", "Type", "Golden"], rows)


@dataclass
class Report:
    key: str
    title: str
    fetch: Callable[[CatalystCenter], Table]


REPORTS = [
    Report("health", "Overall Network Health", fetch_network_health),
    Report("devices", "Device Inventory", fetch_devices),
    Report("sites", "Site Health", fetch_site_health),
    Report("clients", "Client Health", fetch_client_health),
    Report("issues", "Open Issues", fetch_issues),
    Report("compliance", "Compliance Status", fetch_compliance),
    Report("images", "Software Images", fetch_images),
]
REPORT_BY_KEY = {r.key: r for r in REPORTS}


def run_report(dnac: CatalystCenter, report: Report) -> None:
    print(Fore.CYAN + f"\n=== {report.title} ===")
    try:
        table = report.fetch(dnac)
    except NoData as e:
        print(Fore.YELLOW + str(e))
        return
    except ApiError as e:
        print(Fore.RED + str(e))
        return
    table.name = report.key
    for note in table.notes:
        print(Fore.CYAN + note)
    display_table(table)


def show_raw_json(dnac: CatalystCenter) -> None:
    path = input("Enter API path (e.g. /dna/intent/api/v1/network-device/count): ").strip()
    if not path:
        return
    paginate = input("List endpoint with >500 records? Auto-paginate offset/limit? (y/N): ").strip().lower() == "y"
    try:
        if paginate:
            items = dnac.get_all_pages(path)
            data = {"response": items, "totalFetched": len(items)}
        else:
            data = dnac.get(path)
    except ApiError as e:
        print(Fore.RED + str(e))
        return

    full_json = json.dumps(data, indent=2)
    os.makedirs("raw_output", exist_ok=True)
    filename = os.path.join("raw_output", f"raw_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(full_json)
        try:
            os.chmod(filename, 0o600)  # device data can be sensitive
        except OSError:
            pass
        print(Fore.GREEN + f"Full response saved to {filename} ({len(full_json)} chars)")
    except OSError as e:
        print(Fore.RED + f"Could not save output file: {e}")

    limit = 4000
    print(Fore.CYAN + full_json[:limit])
    if len(full_json) > limit:
        print(Fore.YELLOW + f"... (truncated in terminal - see {filename} for the full response)")


# --------------------------------------------------------------------------- #
# Site network diagram: self-contained HTML/SVG (no CDN, works offline)
# --------------------------------------------------------------------------- #
TIER_NAMES = {0: "Core / Edge", 1: "Distribution / WLC", 2: "Access", 3: "Wireless / Other"}
NODE_W, NODE_H = 168, 58
GAP_X, GAP_Y = 28, 110
MARGIN_X, MARGIN_Y = 140, 110
MAX_ROW = 12                 # wrap a tier into several rows beyond this many nodes
SITE_LIST_LIMIT = 40         # rows shown in the site picker at once


@dataclass
class DNode:
    id: str
    label: str
    ip: str
    role: str
    family: str
    platform: str
    version: str
    status: str              # ok | bad | unk
    tier: int
    external: bool = False   # lives outside the selected site (drawn dashed)
    ap_count: int = 0        # access points collapsed into a badge
    x: float = 0.0
    y: float = 0.0


@dataclass
class DLink:
    a: str
    b: str
    ports: List[str]
    status: str              # up | down | partial


@dataclass
class DiagramResult:
    path: str
    table: Table
    notes: List[str]


def _is_ap(family) -> bool:
    f = (family or "").lower()
    return "unified ap" in f or "access point" in f


def _tier(role, family) -> int:
    r, f = (role or "").upper(), (family or "").lower()
    if _is_ap(f):
        return 3
    if "wireless controller" in f:
        return 1
    if r in ("BORDER ROUTER", "CORE") or "router" in f:
        return 0
    if r == "DISTRIBUTION":
        return 1
    if r == "ACCESS":
        return 2
    return 2 if "switch" in f else 3


def _status_class(reach) -> str:
    r = str(reach or "").lower()
    return "ok" if r == "reachable" else "bad" if r == "unreachable" else "unk"


# -- sites ------------------------------------------------------------------ #
def list_sites(dnac: CatalystCenter) -> List[dict]:
    """All sites as {'id', 'path'} where path is the full hierarchy (Global/Area/Building)."""
    try:
        raw = dnac.get_all_pages("/dna/intent/api/v1/site", cache_ttl=300)
    except ApiError:
        raw = []
    sites = [{"id": s["id"], "path": s.get("siteNameHierarchy") or s.get("name")}
             for s in raw if s.get("id") and (s.get("siteNameHierarchy") or s.get("name"))]
    if not sites:  # fallback: flat names from site-health
        sites = [{"id": sid, "path": name} for sid, name in dnac.site_names().items()]
    if not sites:
        raise ApiError("Could not list sites from Catalyst Center.")
    return sorted(sites, key=lambda s: s["path"].lower())


def child_sites(sites: List[dict], parent: dict) -> List[dict]:
    prefix = parent["path"] + "/"
    return [s for s in sites if s["path"].startswith(prefix)]


def resolve_site(sites: List[dict], query: str) -> dict:
    q = query.strip().lower()
    exact = [s for s in sites if s["path"].lower() == q]
    if exact:
        return exact[0]
    subs = [s for s in sites if q in s["path"].lower()]
    if len(subs) == 1:
        return subs[0]
    if not subs:
        raise ApiError(f"No site matches '{query}'.")
    raise ApiError(f"'{query}' matches {len(subs)} sites; be more specific, e.g. "
                   + ", ".join(s["path"] for s in subs[:5]))


def pick_site(sites: List[dict]) -> Optional[dict]:
    """Choose a site. Type words to filter (all must match), a number to select,
    n / p to page, a to show the whole list, Enter to cancel."""
    current, page, show_all = sites, 0, False
    size = SITE_LIST_LIMIT
    while True:
        pages = max(1, -(-len(current) // size))
        page = min(page, pages - 1)
        start = 0 if show_all else page * size
        shown = current if show_all else current[start:start + size]
        paged = pages > 1 and not show_all
        print(Fore.CYAN + f"\nSites ({len(current)} of {len(sites)})"
              + (f" - page {page + 1}/{pages}" if paged else ""))
        for i, s in enumerate(shown, start + 1):  # numbers stay valid across pages
            print(f"  {Fore.YELLOW}{i:>4}{Style.RESET_ALL}. {s['path']}")
        hint = "Type text to filter, a number to select"
        if paged:
            hint += ", n/p = next/prev page, a = show all"
        try:
            cmd = input(Fore.CYAN + hint + ", Enter to cancel: ").strip()
        except EOFError:
            return None
        low = cmd.lower()
        if not cmd:
            return None
        if low == "n" and pages > 1:
            page, show_all = min(page + 1, pages - 1), False
        elif low == "p" and pages > 1:
            page, show_all = max(page - 1, 0), False
        elif low == "a":
            show_all = True
        elif cmd.isdigit():
            idx = int(cmd) - 1
            if 0 <= idx < len(current):
                return current[idx]
            print(Fore.RED + "Number out of range.")
        else:
            result = [s for s in sites if all(w in s["path"].lower() for w in low.split())]
            if result:
                current, page, show_all = result, 0, False
            else:
                print(Fore.YELLOW + f"No sites match '{cmd}'.")


def site_member_ids(dnac: CatalystCenter, site_ids: List[str]) -> set:
    """Device IDs assigned to the given sites (membership API, one call per site, in parallel)."""
    def one(sid):
        try:
            data = dnac.get(f"/dna/intent/api/v1/membership/{sid}") or {}
        except ApiError:
            return None
        ids = set()
        for grp in data.get("device") or []:
            for d in grp.get("response") or []:
                for k in ("id", "instanceUuid"):
                    if d.get(k):
                        ids.add(d[k])
        return ids

    results = parallel_map(one, list(site_ids), label="site membership")
    if all(r is None for r in results):
        raise ApiError("Could not read site membership (/membership/{siteId}).")
    return set().union(*[r for r in results if r])


# -- graph + layout ----------------------------------------------------------- #
def build_graph(topo_nodes, topo_links, members, dev_by_id, include_aps, text):
    """Select the site's nodes/links from the global topology.

    Neighbours that sit outside the site are kept (dashed) so uplinks stay visible.
    Access points are collapsed into a '+N APs' badge unless include_aps.
    """
    by_id = {n["id"]: n for n in topo_nodes if n.get("id")}
    inside = {i for i in members if i in by_id}
    words = text.lower().split()
    if words:
        def hit(n):
            hay = " ".join(str(n.get(k, "")) for k in ("label", "ip", "role", "family", "platformId")).lower()
            return all(w in hay for w in words)
        inside = {i for i in inside if hit(by_id[i])}
    if not inside:
        raise NoData("No topology nodes found for this site"
                     + (f" matching '{text}'." if words else
                        " (its devices may not appear in the physical topology yet)."))

    def make(n, external):
        d = dev_by_id.get(n["id"], {})
        role = n.get("role") or d.get("role") or "-"
        fam = n.get("family") or d.get("family") or "-"
        return DNode(
            id=n["id"], label=str(n.get("label") or d.get("hostname") or n["id"]),
            ip=str(n.get("ip") or d.get("managementIpAddress") or "-"),
            role=str(role), family=str(fam),
            platform=dedupe_pids(n.get("platformId") or d.get("platformId")),
            version=str(d.get("softwareVersion") or n.get("softwareVersion") or "-"),
            status=_status_class(d.get("reachabilityStatus")), tier=_tier(role, fam), external=external)

    nodes: Dict[str, DNode] = {}
    for nid in inside:
        if include_aps or not _is_ap(by_id[nid].get("family")):
            nodes[nid] = make(by_id[nid], False)

    ap_ids = defaultdict(set)
    pairs: Dict[tuple, dict] = {}
    for l in topo_links:
        s, t = l.get("source"), l.get("target")
        if s not in by_id or t not in by_id or (s not in inside and t not in inside):
            continue
        s_ap, t_ap = _is_ap(by_id[s].get("family")), _is_ap(by_id[t].get("family"))
        if not include_aps and (s_ap or t_ap):
            if s_ap and not t_ap:
                ap_ids[t].add(s)
            elif t_ap and not s_ap:
                ap_ids[s].add(t)
            continue
        for nid in (s, t):
            if nid not in nodes:
                nodes[nid] = make(by_id[nid], nid not in inside)
        entry = pairs.setdefault(tuple(sorted((s, t))), {"ports": [], "states": []})
        port = (f"{nodes[s].label.split('.')[0]}:{l.get('startPortName') or '?'} <-> "
                f"{nodes[t].label.split('.')[0]}:{l.get('endPortName') or '?'}")
        if port not in entry["ports"]:
            entry["ports"].append(port)
            entry["states"].append("down" if "down" in str(l.get("linkStatus", "")).lower() else "up")

    links = []
    for (a, b), e in pairs.items():
        downs = e["states"].count("down")
        links.append(DLink(a, b, e["ports"], "down" if downs == len(e["states"]) else "partial" if downs else "up"))
    for sid, aps in ap_ids.items():
        if sid in nodes:
            nodes[sid].ap_count = len(aps)
    return nodes, links, sum(len(v) for v in ap_ids.values())


def layout(nodes: Dict[str, DNode], links: List[DLink]):
    """Tiered layout; barycenter sweeps reduce line crossings. Returns (W, H, bands)."""
    tiers = sorted({n.tier for n in nodes.values()})
    rows = {t: sorted((n for n in nodes.values() if n.tier == t),
                      key=lambda n: (n.external, n.label.lower())) for t in tiers}
    adj = defaultdict(set)
    for l in links:
        adj[l.a].add(l.b)
        adj[l.b].add(l.a)
    pos: Dict[str, int] = {}

    def reindex(t):
        for i, n in enumerate(rows[t]):
            pos[n.id] = i

    def frac(nid):
        return (pos[nid] + 0.5) / len(rows[nodes[nid].tier])

    def sweep(t, ref):
        def key(n):
            nb = [frac(m) for m in adj[n.id] if nodes[m].tier == ref]
            return sum(nb) / len(nb) if nb else frac(n.id)
        rows[t] = [n for _, n in sorted(((key(n), n) for n in rows[t]), key=lambda kn: kn[0])]
        reindex(t)

    for t in tiers:
        reindex(t)
    for _ in range(4):
        for i in range(1, len(tiers)):
            sweep(tiers[i], tiers[i - 1])
        for i in range(len(tiers) - 2, -1, -1):
            sweep(tiers[i], tiers[i + 1])

    step = NODE_W + GAP_X
    max_cols = max(min(len(r), MAX_ROW) for r in rows.values())
    avail = max_cols * step - GAP_X
    width = MARGIN_X + avail + 40
    y = MARGIN_Y
    bands = {}
    for t in tiers:
        top = y
        for start in range(0, len(rows[t]), MAX_ROW):
            chunk = rows[t][start:start + MAX_ROW]
            x0 = MARGIN_X + (avail - (len(chunk) * step - GAP_X)) / 2
            for i, n in enumerate(chunk):
                n.x, n.y = x0 + i * step, y
            y += NODE_H + 34
        bands[t] = (top, y - 34)
        y += GAP_Y - 34
    return width, y - GAP_Y + NODE_H // 2 + 40, bands


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%%TITLE%%</title>
<style>
:root{--bg:#ffffff;--fg:#1f2328;--muted:#656d76;--panel:#f6f8fa;--line:#8c959f;--ok:#1a7f37;--bad:#cf222e;--warn:#bf8700;--unk:#8c959f;--node:#ffffff;--band:#f6f8fa;--accent:#0969da}
@media (prefers-color-scheme: dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8b949e;--panel:#161b22;--line:#6e7681;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--unk:#6e7681;--node:#1c2128;--band:#12171e;--accent:#58a6ff}}
*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;flex-direction:column;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line)}
h1{font-size:16px;margin:0 0 4px}
.stats{color:var(--muted);font-size:12px}
.bar{display:flex;gap:10px;align-items:center;margin-top:8px;flex-wrap:wrap}
input{padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);min-width:240px}
button{padding:5px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);cursor:pointer}
.legend span{margin-right:12px;font-size:12px;color:var(--muted)}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px;vertical-align:-1px}
svg{flex:1;min-height:0;width:100%;cursor:grab;display:block}
svg.drag{cursor:grabbing}
.band{fill:var(--band)}
.tier{fill:var(--muted);font-size:12px;font-weight:600}
.link{fill:none;stroke:var(--line);stroke-width:2}
.link.down{stroke:var(--bad);stroke-dasharray:6 4}
.link.partial{stroke:var(--warn)}
.link.hl{stroke:var(--accent);stroke-width:3}
.lbl{fill:var(--muted);font-size:10px;text-anchor:middle}
.node rect.body{fill:var(--node);stroke:var(--unk);stroke-width:2}
.node.ok rect.body{stroke:var(--ok)}
.node.bad rect.body{stroke:var(--bad)}
.node.ext rect.body{stroke-dasharray:5 3}
.node.ext{opacity:.75}
.node{cursor:pointer}
.n1{font-size:12px;font-weight:600;fill:var(--fg)}
.n2,.n3{font-size:10px;fill:var(--muted)}
.badge rect{fill:var(--accent)}
.badge text{fill:#fff;font-size:10px;text-anchor:middle}
.dim,.faded{opacity:.15}
</style></head><body>
<header>
<h1>%%TITLE%%</h1>
<div class="stats">%%STATS%%</div>
<div class="bar">
<input id="q" type="search" placeholder="Filter devices (name, IP, role, model)..." autocomplete="off">
<span id="count" class="stats"></span>
<button id="reset" type="button">Reset view</button>
<span class="legend"><span><i class="dot" style="background:var(--ok)"></i>reachable</span><span><i class="dot" style="background:var(--bad)"></i>unreachable</span><span><i class="dot" style="background:var(--unk)"></i>unknown</span><span>dashed box = other site</span><span>dashed red line = link down</span><span>click a device to highlight its links</span></span>
</div></header>
<svg id="d" viewBox="0 0 %%W%% %%H%%" preserveAspectRatio="xMinYMin meet">
%%BODY%%
</svg>
<script>
(function(){
var svg=document.getElementById('d'),vb=svg.viewBox.baseVal,orig=[vb.x,vb.y,vb.width,vb.height];
function scale(){var r=svg.getBoundingClientRect();return Math.max(vb.width/r.width,vb.height/r.height);}
function pt(e){var r=svg.getBoundingClientRect(),s=scale();return{x:vb.x+(e.clientX-r.left)*s,y:vb.y+(e.clientY-r.top)*s};}
svg.addEventListener('wheel',function(e){e.preventDefault();var k=e.deltaY<0?0.9:1.1,p=pt(e);vb.x=p.x-(p.x-vb.x)*k;vb.y=p.y-(p.y-vb.y)*k;vb.width*=k;vb.height*=k;},{passive:false});
var drag=null,moved=false;
svg.addEventListener('mousedown',function(e){drag={x:e.clientX,y:e.clientY};moved=false;svg.classList.add('drag');});
window.addEventListener('mousemove',function(e){if(!drag)return;var s=scale(),dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.abs(dx)+Math.abs(dy)>3)moved=true;vb.x-=dx*s;vb.y-=dy*s;drag={x:e.clientX,y:e.clientY};});
window.addEventListener('mouseup',function(){drag=null;svg.classList.remove('drag');});
document.getElementById('reset').addEventListener('click',function(){vb.x=orig[0];vb.y=orig[1];vb.width=orig[2];vb.height=orig[3];clearSel();});
var nodes=[].slice.call(document.querySelectorAll('.node')),links=[].slice.call(document.querySelectorAll('.link'));
function clearSel(){nodes.forEach(function(n){n.classList.remove('faded');});links.forEach(function(l){l.classList.remove('hl','faded');});}
nodes.forEach(function(g){g.addEventListener('click',function(e){e.stopPropagation();if(moved)return;var id=g.dataset.id,nb={};nb[id]=1;
links.forEach(function(l){var on=l.dataset.a===id||l.dataset.b===id;l.classList.toggle('hl',on);l.classList.toggle('faded',!on);if(on){nb[l.dataset.a]=1;nb[l.dataset.b]=1;}});
nodes.forEach(function(n){n.classList.toggle('faded',!nb[n.dataset.id]);});});});
svg.addEventListener('click',function(){if(!moved)clearSel();});
var q=document.getElementById('q'),c=document.getElementById('count');
q.addEventListener('input',function(){var w=q.value.trim().toLowerCase().split(/\\s+/).filter(Boolean),n=0;
nodes.forEach(function(g){var ok=w.every(function(x){return g.dataset.text.indexOf(x)!==-1;});g.classList.toggle('dim',w.length>0&&!ok);if(w.length&&ok)n++;});
c.textContent=w.length?n+' match(es)':'';});
})();
</script></body></html>
"""


def render_html(title: str, nodes: Dict[str, DNode], links: List[DLink], lay, stats: str) -> str:
    width, height, bands = lay
    esc = lambda s: html.escape(str(s), quote=True)  # noqa: E731 - device text is untrusted
    out: List[str] = []
    for t, (y0, y1) in bands.items():
        out.append(f'<rect class="band" x="0" y="{y0 - 14}" width="{width:.0f}" height="{y1 - y0 + 28}"/>')
        out.append(f'<text class="tier" x="14" y="{(y0 + y1) / 2:.0f}">{esc(TIER_NAMES[t])}</text>')

    for l in links:
        a, b = nodes[l.a], nodes[l.b]
        if abs(a.y - b.y) < 1:  # same row: arc over the top
            x1, x2, y1 = a.x + NODE_W / 2, b.x + NODE_W / 2, a.y
            off = min(60.0, 22 + abs(x2 - x1) / 6)
            d = f"M{x1:.1f} {y1:.1f} Q{(x1 + x2) / 2:.1f} {y1 - 2 * off:.1f} {x2:.1f} {y1:.1f}"
            lx, ly = (x1 + x2) / 2, y1 - off - 4
        else:
            up, dn = (a, b) if a.y < b.y else (b, a)
            x1, y1, x2, y2 = up.x + NODE_W / 2, up.y + NODE_H, dn.x + NODE_W / 2, dn.y
            ym = (y1 + y2) / 2
            d = f"M{x1:.1f} {y1:.1f} C{x1:.1f} {ym:.1f} {x2:.1f} {ym:.1f} {x2:.1f} {y2:.1f}"
            lx, ly = (x1 + x2) / 2, ym - 3
        tip = f"{a.label} <-> {b.label}\n{len(l.ports)} link(s), status: {l.status}\n" + "\n".join(l.ports)
        out.append(f'<path class="link {l.status}" data-a="{esc(l.a)}" data-b="{esc(l.b)}" d="{d}"><title>{esc(tip)}</title></path>')
        if len(l.ports) > 1:
            out.append(f'<text class="lbl" x="{lx:.1f}" y="{ly:.1f}">x{len(l.ports)}</text>')

    for n in nodes.values():
        is_ip = n.label.replace(".", "").isdigit()
        short = n.label if is_ip else n.label.split(".")[0]
        sub = " · ".join(p for p in (n.role if n.role != "-" else "", n.platform if n.platform != "-" else "") if p) or n.family
        tip = "\n".join([n.label, f"IP: {n.ip}", f"Family: {n.family}", f"Role: {n.role}", f"Model: {n.platform}",
                         f"Software: {n.version}",
                         f"Status: {'reachable' if n.status == 'ok' else 'unreachable' if n.status == 'bad' else 'unknown'}"]
                        + (["(device is in another site)"] if n.external else [])
                        + ([f"{n.ap_count} access point(s) attached"] if n.ap_count else []))
        hay = " ".join([n.label, n.ip, n.role, n.family, n.platform]).lower()
        badge = (f'<g class="badge"><rect x="{NODE_W - 64}" y="-9" width="62" height="18" rx="9"/>'
                 f'<text x="{NODE_W - 33}" y="4">+{n.ap_count} APs</text></g>') if n.ap_count else ""
        out.append(
            f'<g class="node {n.status}{" ext" if n.external else ""}" data-id="{esc(n.id)}" data-text="{esc(hay)}" '
            f'transform="translate({n.x:.1f} {n.y:.1f})"><title>{esc(tip)}</title>'
            f'<rect class="body" width="{NODE_W}" height="{NODE_H}" rx="8"/>'
            f'<text class="n1" x="10" y="19">{esc(_trunc(short, 22))}</text>'
            f'<text class="n2" x="10" y="35">{esc(n.ip)}</text>'
            f'<text class="n3" x="10" y="50">{esc(_trunc(sub, 28))}</text>{badge}</g>')

    return (HTML_TEMPLATE.replace("%%TITLE%%", esc(title)).replace("%%STATS%%", esc(stats))
            .replace("%%W%%", f"{width:.0f}").replace("%%H%%", f"{height:.0f}").replace("%%BODY%%", "\n".join(out)))


def build_diagram(dnac: CatalystCenter, site: dict, sites: List[dict], children: bool,
                  include_aps: bool, text: str, out_dir: str = "exports") -> DiagramResult:
    targets = [site] + (child_sites(sites, site) if children else [])
    members = site_member_ids(dnac, [s["id"] for s in targets])
    topo = dnac.get_cached("/dna/intent/api/v1/topology/physical-topology")
    resp = topo.get("response") if isinstance(topo, dict) else None
    if not resp:
        raise NoData("No physical topology data returned.")
    dev_by_id = {d["id"]: d for d in dnac.devices() if d.get("id")}

    nodes, links, ap_total = build_graph(resp.get("nodes", []), resp.get("links", []),
                                         members, dev_by_id, include_aps, text)
    lay = layout(nodes, links)

    inside = [n for n in nodes.values() if not n.external]
    ext = len(nodes) - len(inside)
    down = sum(1 for l in links if l.status != "up")
    reach = Counter(n.status for n in inside)
    notes = [f"Site: {site['path']}" + (f"  (+{len(targets) - 1} child site(s))" if len(targets) > 1 else ""),
             f"Devices drawn: {len(inside)}  |  reachable: {reach['ok']}  unreachable: {reach['bad']}  unknown: {reach['unk']}",
             f"Links: {len(links)}  |  with a down link: {down}  |  neighbours in other sites: {ext}"]
    if ap_total:
        notes.append(f"{ap_total} access point(s) collapsed into '+N APs' badges (answer 'y' to draw them).")
    missing = len([i for i in members if i in dev_by_id]) - len(inside) - ap_total if not text else 0
    if missing > 0:
        notes.append(f"{missing} member device(s) are not in the physical topology map.")

    stats = " | ".join(notes[1:])
    title = f"Network diagram - {site['path']}"
    os.makedirs(out_dir, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", site["path"]).strip("-")[:60] or "site"
    path = os.path.join(out_dir, f"diagram_{slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_html(title, nodes, links, lay, stats))

    words = text.lower().split()
    rows = []
    for mid in members:
        d = dev_by_id.get(mid)
        if not d:
            continue
        row = [d.get("hostname", "-"), d.get("managementIpAddress", "-"), dedupe_pids(d.get("platformId")),
               d.get("role", "-"), d.get("family", "-"), d.get("softwareVersion", "-"),
               d.get("reachabilityStatus", "-")]
        if all(w in " ".join(map(str, row)).lower() for w in words):
            rows.append(row)
    rows.sort(key=lambda r: str(r[0]).lower())
    table = Table(["Hostname", "Mgmt IP", "Model (PID)", "Role", "Family", "SW Version", "Reachability"],
                  rows, name="site_devices")
    return DiagramResult(path, table, notes)


def site_diagram_menu(dnac: CatalystCenter) -> None:
    print(Fore.CYAN + "\n=== Site Network Diagram ===")
    sites = list_sites(dnac)
    site = pick_site(sites)
    if not site:
        return
    kids = child_sites(sites, site)
    children = True
    if kids:
        children = input(f"Include {len(kids)} child site(s)? (Y/n): ").strip().lower() != "n"
    aps = input("Draw access points individually? (y/N = collapse into '+N APs' badges): ").strip().lower() == "y"
    text = input("Only devices matching text - name, IP, role, model (Enter = all): ").strip()

    res = build_diagram(dnac, site, sites, children, aps, text)
    for n in res.notes:
        print(Fore.CYAN + n)
    print(Fore.GREEN + f"Diagram written to {os.path.abspath(res.path)}")
    try:
        if webbrowser.open("file://" + os.path.abspath(res.path)):
            print(Fore.GREEN + "Opened in your browser (scroll = zoom, drag = pan, click a device to highlight links).")
    except Exception:
        pass
    print(Fore.CYAN + "\nDevices in this selection:")
    display_table(res.table)


def headless_diagram(dnac: CatalystCenter, query: str, out_dir: str, children: bool,
                     include_aps: bool, text: str) -> int:
    try:
        sites = list_sites(dnac)
        site = resolve_site(sites, query)
        res = build_diagram(dnac, site, sites, children, include_aps, text, out_dir)
    except NoData as e:
        print(f"diagram: {e}", file=sys.stderr)
        return 1
    except ApiError as e:
        print(f"diagram: FAILED - {e}", file=sys.stderr)
        return 1
    print(f"diagram: {site['path']} -> {res.path}")
    return 0


# --------------------------------------------------------------------------- #
# Interactive menu
# --------------------------------------------------------------------------- #
MENU_ITEMS: List[Tuple[str, Callable[[CatalystCenter], None]]] = (
    [(r.title, (lambda d, r=r: run_report(d, r))) for r in REPORTS]
    + [("Site Network Diagram", site_diagram_menu), ("Custom raw GET call", show_raw_json)]
)


def print_banner() -> None:
    print(Fore.MAGENTA + Style.BRIGHT + r"""
   ___      _        _         _    ____
  / __\__ _| |_ __ _| |_   _ __| |_ / ___| ___ _ __
 / /  / _` | __/ _` | | | | / __| __| |   / _ \ '_  \
/ /__| (_| | || (_| | | |_| \__ \ |_| |__|  __/ | | |
\____/\__,_|\__\__,_|_|\__, |___/\__|\____|\___|_| |_|
                        |___/    Center API Menu
""")


def print_menu(items) -> None:
    print(Fore.CYAN + "\nSelect an API call to run:")
    for idx, (label, _) in enumerate(items, start=1):
        print(f"  {Fore.YELLOW}{idx}{Style.RESET_ALL}. {label}")
    print(f"  {Fore.YELLOW}0{Style.RESET_ALL}. Exit")
    print(Fore.MAGENTA + "  (type '/keyword' to search the menu, e.g. /health, /issue)")


def _resolve(choice: str, items):
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(items):
            return items[idx]
    except ValueError:
        pass
    print(Fore.RED + "Invalid choice.")
    return None


def _run_item(dnac: CatalystCenter, func) -> None:
    try:
        func(dnac)
    except KeyboardInterrupt:
        print(Fore.YELLOW + "\nCancelled.")
    except NoData as e:
        print(Fore.YELLOW + str(e))
    except ApiError as e:
        print(Fore.RED + str(e))
    except Exception as e:  # keep the menu alive on any bug
        print(Fore.RED + f"Unexpected error: {type(e).__name__}: {e}")
        if os.environ.get("DNAC_DEBUG"):
            traceback.print_exc()


def interactive(dnac: CatalystCenter) -> int:
    while True:
        print_menu(MENU_ITEMS)
        try:
            choice = input("\nChoice: ").strip()
        except EOFError:
            return 0
        if choice == "0":
            print(Fore.MAGENTA + "Goodbye.")
            return 0
        if choice.startswith("/"):
            term = choice[1:].strip().lower()
            matches = [m for m in MENU_ITEMS if term in m[0].lower()]
            if not matches:
                print(Fore.YELLOW + f"No menu items match '{term}'.")
                continue
            print(Fore.CYAN + "\nMatches:")
            for idx, (label, _) in enumerate(matches, start=1):
                print(f"  {Fore.YELLOW}{idx}{Style.RESET_ALL}. {label}")
            print(f"  {Fore.YELLOW}0{Style.RESET_ALL}. Cancel")
            pick_ = input("\nChoice: ").strip()
            if pick_ == "0":
                continue
            item = _resolve(pick_, matches)
        else:
            item = _resolve(choice, MENU_ITEMS)
        if item:
            _run_item(dnac, item[1])


# --------------------------------------------------------------------------- #
# Headless mode (cron / CI / scripts)
# --------------------------------------------------------------------------- #
def headless(dnac: CatalystCenter, keys: List[str], out_dir: str, fmt: str) -> int:
    dnac.quiet = False  # progress goes to stderr, results to files
    failures = 0
    for key in keys:
        report = REPORT_BY_KEY[key]
        try:
            table = report.fetch(dnac)
            table.name = key
            path = export_table(table, out_dir, fmt)
            print(f"{key}: {len(table.rows)} row(s) -> {path}")
        except NoData as e:
            print(f"{key}: {e}")
        except ApiError as e:
            failures += 1
            print(f"{key}: FAILED - {e}", file=sys.stderr)
    return 1 if failures else 0


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Catalyst Center API menu / exporter (read-only).")
    p.add_argument("--host", help="Catalyst Center IP/hostname[:port] (default: DNAC_HOST)")
    p.add_argument("--user", help="Username (default: DNAC_USERNAME)")
    p.add_argument("--run", nargs="+", metavar="REPORT", choices=sorted(REPORT_BY_KEY) + ["all"],
                   help="Run report(s) non-interactively and export: " + ", ".join(sorted(REPORT_BY_KEY)) + ", all")
    p.add_argument("--format", choices=("csv", "json"), default="csv", help="Export format for --run")
    p.add_argument("--out-dir", default="exports", help="Export directory (default: exports)")
    p.add_argument("--list", action="store_true", help="List report names and exit")
    p.add_argument("--diagram", metavar="SITE",
                   help="Write an HTML network diagram for a site (full path or unique substring) and exit")
    p.add_argument("--no-children", action="store_true", help="With --diagram: exclude child sites")
    p.add_argument("--aps", action="store_true", help="With --diagram: draw access points instead of '+N APs' badges")
    p.add_argument("--device-filter", default="", metavar="TEXT",
                   help="With --diagram: only devices matching these words (name, IP, role, model)")
    p.add_argument("--verify", action="store_true", help="Verify the TLS certificate (default: off)")
    p.add_argument("--ca-bundle", help="Path to a CA bundle to verify against (implies --verify)")
    return p.parse_args(argv)


def _need(value: str, prompt: str, label: str, secret: bool = False) -> str:
    if value:
        return value
    if not sys.stdin.isatty():
        sys.exit(f"Missing {label}: set it in the environment or .env (no terminal to prompt on).")
    return getpass.getpass(prompt) if secret else input(prompt).strip()


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.list:
        for r in REPORTS:
            print(f"{r.key:12} {r.title}")
        return 0

    colorama_init(autoreset=True)
    load_dotenv()
    batch = bool(args.run or args.diagram)
    if not batch:
        print_banner()

    host = (args.host or os.environ.get("DNAC_HOST", "")).strip()
    user = (args.user or os.environ.get("DNAC_USERNAME", "")).strip()
    password = os.environ.get("DNAC_PASSWORD", "").strip()  # never a CLI flag: it would show in `ps`
    if not batch:
        for label, val in (("Host", host), ("Username", user), ("Password", password)):
            if val:
                print(Fore.GREEN + f"{label} loaded from environment/.env" + ("" if label == "Password" else f": {val}"))
    host = _need(host, "Catalyst Center IP/hostname: ", "DNAC_HOST")
    user = _need(user, "Username: ", "DNAC_USERNAME")
    password = _need(password, "Password: ", "DNAC_PASSWORD", secret=True)

    ca_bundle = args.ca_bundle or os.environ.get("DNAC_CA_BUNDLE") or None
    verify = bool(ca_bundle) or args.verify or env_bool("DNAC_VERIFY_SSL", False)
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        print(Fore.YELLOW + "TLS certificate verification is OFF "
              "(use --verify / DNAC_VERIFY_SSL=true / DNAC_CA_BUNDLE to enable).", file=sys.stderr)

    dnac = CatalystCenter(host, user, password, verify=verify, ca_bundle=ca_bundle)
    print(Fore.CYAN + f"\nAuthenticating to {dnac.host} ...", file=sys.stderr if batch else sys.stdout)
    try:
        dnac.authenticate()
    except ApiError as e:
        print(Fore.RED + str(e), file=sys.stderr)
        return 1
    print(Fore.GREEN + "Authentication successful.\n", file=sys.stderr if batch else sys.stdout)

    if batch:
        rc = 0
        if args.run:
            keys = sorted(REPORT_BY_KEY) if "all" in args.run else args.run
            rc |= headless(dnac, keys, args.out_dir, args.format)
        if args.diagram:
            rc |= headless_diagram(dnac, args.diagram, args.out_dir, not args.no_children,
                                   args.aps, args.device_filter)
        return rc
    return interactive(dnac)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(Fore.MAGENTA + "\nInterrupted. Exiting.")
        sys.exit(130)