# 🌐 Catalyst Center API Menu

A simple interactive CLI for pulling common data out of Cisco Catalyst
Center (formerly DNA Center) over its REST API — no need to remember
endpoint paths or write one-off `curl` commands. 🚀

Run it, enter the appliance IP/hostname and your login (or store them in
a `.env` file), and pick from a menu of read-only API calls.

## ✨ Features

- 🔑 Loads credentials from `.env` automatically — with interactive fallback if not set
- 🔄 One pooled HTTPS session, proactive token refresh (before the ~60 min expiry) and re-auth on 401
- 🔁 Automatic retry with backoff on rate limits (429) and transient 5xx errors
- 📋 Menu-driven access to:
  - 💚 Overall network health (per-category breakdown)
  - 🖥️ Device inventory (model/PID with stack dedup, reachability summary)
  - 🏢 Site health
  - 📱 Client health (site names resolved)
  - ⚠️ Active issues (configurable look-back window)
  - ✅ Compliance status (one bulk call, non-compliant first)
  - 💿 Software images (golden-image flag)
  - 🗺️ Physical topology summary
  - 🔧 Custom raw GET call
- 🔍 Filter, column-filter, exclude and sort any results table; export it to CSV
- ⚡ Headless mode: `--run devices compliance` writes CSV/JSON for cron or pipelines
- 📄 Auto-pagination with a loop guard; large inventories are fetched in parallel
- 🎨 Pretty table output via `tabulate`, colored terminal output via `colorama`

## 📦 Requirements

- Python 3.8+
- Network reachability to your Catalyst Center appliance (HTTPS, port 443)
- An account with API access on Catalyst Center

## ⚙️ Installation

```bash
git clone https://github.com/mizitheji/catalyst-center-API.git
cd catalyst-center-API

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 🔐 Credentials via `.env`

Store your connection details in a `.env` file so you are not prompted every time:

```bash
cp .env.example .env
# edit .env with your real values
```

```ini
# .env
DNAC_HOST=10.10.20.1
DNAC_USERNAME=admin
DNAC_PASSWORD=S3cur3P@ss!
```

At startup, values found in `.env` are confirmed and the rest are prompted:

```
Host loaded from .env: 10.10.20.1
Username loaded from .env: admin
Password loaded from .env.

Authenticating to 10.10.20.1 ...
Authentication successful.
```

- Any variable left blank or omitted falls back to an interactive prompt.
- `.env` is listed in `.gitignore` — your credentials will not be committed.
- The loader is pure Python (`os` stdlib) — no extra package required.

## ▶️ Usage

```bash
python3 dnac_menu.py
```

```
Select an API call to run:
  1. Overall Network Health
  2. Device Inventory
  3. Site Health
  4. Client Health
  5. Open Issues
  6. Compliance Status
  7. Software Images
  8. Physical Topology Summary
  9. Custom raw GET call
  0. Exit
  (type '/keyword' to search the menu, e.g. /health, /issue)

Choice:
```

### ⌨️ Command-line options

```bash
python3 dnac_menu.py --list                        # list report names
python3 dnac_menu.py --run devices sites           # headless: export to ./exports/*.csv
python3 dnac_menu.py --run all --format json --out-dir /var/reports
python3 dnac_menu.py --host 10.0.0.5:443 --ca-bundle corp-ca.pem
```

| Option | Meaning |
|---|---|
| `--run REPORT [...]` | Run reports without the menu and export them (`health devices sites clients issues compliance images topology all`) |
| `--format csv\|json` | Export format for `--run` (CSV is UTF-8 with BOM so Excel opens it cleanly) |
| `--out-dir DIR` | Where exports go (default `exports/`) |
| `--verify` / `--ca-bundle FILE` | Verify the TLS certificate (default is off) |
| `--host` / `--user` | Override `DNAC_HOST` / `DNAC_USERNAME` |

The password is deliberately **not** a flag (it would show up in `ps`); use `DNAC_PASSWORD`
or the prompt. In headless mode with missing credentials the tool exits instead of prompting.
Exit code is `1` if any report failed, so it works in cron and CI.

### 🖥️ Device Inventory (Option 2)

```
=== Device Inventory ===
  ...fetched 500 record(s) so far
  ...fetched 623 record(s) so far

╒══════════════════════════════╤═════════════════╤══════════════════════════╤══════════════╤════════════════╤════════════════════════════╕
│ Hostname                     │ Mgmt IP         │ Family                   │ SW Version   │ Reachability   │ Collection                 │
╞══════════════════════════════╪═════════════════╪══════════════════════════╪══════════════╪════════════════╪════════════════════════════╡
│ HQ-CORE-SW-01.example.com    │ 10.10.1.1       │ Switches and Hubs        │ 17.9.4       │ Reachable      │ Managed                    │
├──────────────────────────────┼─────────────────┼──────────────────────────┼──────────────┼────────────────┼────────────────────────────┤
│ BR-KL-ACC-SW-03.example.com  │ 10.10.2.13      │ Switches and Hubs        │ 17.9.4       │ Reachable      │ Managed                    │
├──────────────────────────────┼─────────────────┼──────────────────────────┼──────────────┼────────────────┼────────────────────────────┤
│ BR-JB-ACC-SW-01.example.com  │ 10.20.1.5       │ Switches and Hubs        │ 16.12.6      │ Unreachable    │ Managed                    │
├──────────────────────────────┼─────────────────┼──────────────────────────┼──────────────┼────────────────┼────────────────────────────┤
│ BR-PG-WLC-01.example.com     │ 10.30.1.2       │ Wireless Controller      │ 17.9.4       │ Reachable      │ Managed                    │
╘══════════════════════════════╧═════════════════╧══════════════════════════╧══════════════╧════════════════╧════════════════════════════╛
623 row(s) shown (of 623 total)
Search/filter (Enter to go back to menu, 'c' to clear filter):
```

### 🏢 Site Health (Option 3)

```
=== Site Health ===
  ...fetched 25 record(s) so far
  ...fetched 50 record(s) so far
  ...fetched 72 record(s) so far

╒══════════════════════════╤════════╤═════════╤═════════════╤══════════════╤════════════════╤══════════════════╤═════════════════════╕
│ Site                     │ Type   │   Total │ Good Health │  Poor Health │   Net Health % │   Wired Health % │   Wireless Health % │
╞══════════════════════════╪════════╪═════════╪═════════════╪══════════════╪════════════════╪══════════════════╪═════════════════════╡
│ All Sites                │ area   │     623 │         591 │           32 │             95 │               98 │                  91 │
├──────────────────────────┼────────┼─────────┼─────────────┼──────────────┼────────────────┼──────────────────┼─────────────────────┤
│ HQ Putrajaya             │ area   │     104 │         104 │            0 │            100 │              100 │                  96 │
├──────────────────────────┼────────┼─────────┼─────────────┼──────────────┼────────────────┼──────────────────┼─────────────────────┤
│ Branch Kuala Lumpur      │ area   │      58 │          52 │            6 │             90 │               97 │                  88 │
├──────────────────────────┼────────┼─────────┼─────────────┼──────────────┼────────────────┼──────────────────┼─────────────────────┤
│ Branch Johor Bahru       │ area   │      31 │          25 │            6 │             81 │               95 │                  79 │
├──────────────────────────┼────────┼─────────┼─────────────┼──────────────┼────────────────┼──────────────────┼─────────────────────┤
│ Branch Penang            │ area   │      22 │          22 │            0 │            100 │              100 │                 100 │
╘══════════════════════════╧════════╧═════════╧═════════════╧══════════════╧════════════════╧══════════════════╧═════════════════════╛
72 row(s) shown (of 72 total)
Search/filter (Enter to go back to menu, 'c' to clear filter):
```

### 🔍 Searching & Filtering

**Filter results tables** — after any table is printed, type a term to filter rows.
The filter is case-insensitive and matches any column:

```
Search/filter (Enter to go back to menu, 'c' to clear filter): johor

╒══════════════════════════╤════════╤═════════╤═════════════╤══════════════╤════════════════╤══════════════════╤═════════════════════╕
│ Site                     │ Type   │   Total │ Good Health │  Poor Health │   Net Health % │   Wired Health % │   Wireless Health % │
╞══════════════════════════╪════════╪═════════╪═════════════╪══════════════╪════════════════╪══════════════════╪═════════════════════╡
│ Branch Johor Bahru       │ area   │      31 │          25 │            6 │             81 │               95 │                  79 │
╘══════════════════════════╧════════╧═════════╧═════════════╧══════════════╧════════════════╧══════════════════╧═════════════════════╛
1 row(s) shown (of 72 total)
```

| Filter term | What you get |
|---|---|
| `johor` | Site rows whose name contains `johor` |
| `BR-KL` | Devices whose hostname contains `BR-KL` |
| `10.20.1` | Devices on that IP subnet |
| `Unreachable` | Only unreachable devices (Device Inventory) |
| `17.9.4` | Devices running that software version |
| `reach:unreach` | Match only within one column (column name prefix, then `:`) |
| `!17.9` | Exclude rows containing the term |
| `sort hostname` / `sort serial desc` | Sort by a column (numeric-aware) |
| `x` | Export the currently filtered/sorted rows to CSV |
| `a` | Show all rows (only the first 100 are printed by default) |
| `c` | Clear the current filter — show all rows again |
| *(Enter)* | Exit the filter prompt and return to the main menu |

**Search the menu itself** — type `/keyword` at `Choice:` instead of a number:

```
Choice: /health

Matches:
  1. Overall Network Health
  3. Site Health
  4. Client Health
```

## 📊 Site Health — Column Reference

| Column | Source field | Meaning |
|---|---|---|
| Total | `numberOfNetworkDevice` | All network devices assigned to the site |
| Good Health | `healthyNetworkDevicePercentage × Total` | Devices with a good health score |
| Poor Health | `Total − Good Health` | Devices with a fair/bad/no health score |
| Net Health % | `networkHealthAverage` | Overall network health score (0–100) |
| Wired Health % | `clientHealthWired` | Wired client health score |
| Wireless Health % | `clientHealthWireless` | Wireless client health score |

> **Note:** *Good/Poor Health* reflects the **health score** (CPU, memory,
> link state, etc.), **not reachability**. A device can be reachable but have
> a poor health score (e.g. high CPU or a degraded uplink). To check
> per-device reachability, use **Option 2 — Device Inventory** and filter by
> `Reachable` or `Unreachable`.

## 📝 Notes

- 🔓 TLS certificate verification is **off by default** (most Catalyst Center
  deployments use self-signed certs) and the tool prints a warning when it is.
  Turn it on with `DNAC_VERIFY_SSL=true`, `--verify`, or point at your CA with
  `DNAC_CA_BUNDLE` / `--ca-bundle`.
- ✅ This tool only issues GET requests — it does not push config or make
  any changes to your environment.
- 📄 Catalyst Center caps most list endpoints at 500 records per call and Site
  Health at 50. `CatalystCenter.get_all_pages()` handles both. If an endpoint
  ignores `offset`/`limit` and keeps returning the same page, the loop stops
  instead of spinning forever. The device list is fetched in parallel once it
  exceeds one page, and device/site lists are cached for a couple of minutes so
  Inventory → Compliance doesn't re-download them.
- 🧾 CSV exports neutralise cells starting with `= + @` (device-supplied names
  can otherwise become Excel formulas).
- 🗂️ The **Custom raw GET call** option prints only the first 4 000
  characters to the terminal but always saves the **full, untruncated**
  response to `raw_output/<timestamp>.json` (created automatically, git-ignored).
  It also asks whether to auto-paginate — say yes for list endpoints, no
  for single-object endpoints like `/count`.

## 🧩 Extending it

Each report is a function that **fetches and returns a `Table`** — no printing, no
`input()` — so the same code serves the menu, the filter/export UI and headless mode.

```python
def fetch_licenses(dnac):
    items = dnac.get_all_pages("/dna/intent/api/v1/some/list", page_size=500)
    if not items:
        raise NoData("No licenses returned.")
    rows = [[i.get("name", "-"), i.get("status", "-")] for i in items]
    return Table(["Name", "Status"], rows, notes=[f"{len(rows)} licenses"])

REPORTS.append(Report("licenses", "Licenses", fetch_licenses))
```

That's it — the menu entry, `/search`, filter/sort/export, `--run licenses` and
error handling all come for free. Use `parallel_map(fn, items)` for N+1 style
lookups (keep `WORKERS` low; the controller rate-limits).

## License

MIT — see [LICENSE](LICENSE).