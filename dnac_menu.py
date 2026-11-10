#!/usr/bin/env python3
"""
Cisco Catalyst Center (DNA Center) API Menu Tool
--------------------------------------------------
Interactive CLI *and* headless exporter for common read-only Catalyst Center
API calls: health, device inventory, site health, client health, issues,
compliance, software images, physical topology.

Interactive:  python3 dnac_menu.py
Headless:     python3 dnac_menu.py --run devices compliance --format csv

Author: mizitheji
License: MIT
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import sys
import threading
import time
import traceback
from collections import Counter
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
        self._cache: Dict[tuple, Tuple[float, list]] = {}

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
            return list(hit[1])
        return None

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


def fetch_topology(dnac: CatalystCenter) -> Table:
    data = dnac.get("/dna/intent/api/v1/topology/physical-topology")
    resp = data.get("response") if isinstance(data, dict) else None
    if not resp:
        raise NoData("No topology data returned.")
    nodes, links = resp.get("nodes", []), resp.get("links", [])
    rows = [[n.get("label", "-"), n.get("family", "-"), n.get("role", "-"), n.get("ip", "-")] for n in nodes]
    return Table(["Label", "Family", "Role", "IP"], rows, [f"Nodes: {len(nodes)}  |  Links: {len(links)}"])


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
    Report("topology", "Physical Topology Summary", fetch_topology),
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
# Interactive menu
# --------------------------------------------------------------------------- #
MENU_ITEMS: List[Tuple[str, Callable[[CatalystCenter], None]]] = (
    [(r.title, (lambda d, r=r: run_report(d, r))) for r in REPORTS]
    + [("Custom raw GET call", show_raw_json)]
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
    batch = bool(args.run)
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
        keys = sorted(REPORT_BY_KEY) if "all" in args.run else args.run
        return headless(dnac, keys, args.out_dir, args.format)
    return interactive(dnac)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(Fore.MAGENTA + "\nInterrupted. Exiting.")
        sys.exit(130)