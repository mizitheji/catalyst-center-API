# 🌐 Catalyst Center API Menu

A simple interactive CLI for pulling common data out of Cisco Catalyst
Center (formerly DNA Center) over its REST API — no need to remember
endpoint paths or write one-off `curl` commands. 🚀

Run it, enter the appliance IP/hostname and your login (or store them in
a `.env` file), and pick from a menu of read-only API calls.

## ✨ Features

- 🔑 Loads credentials from `.env` automatically — with interactive fallback if not set
- 🔄 Authenticates via `/dna/system/api/v1/auth/token` and auto re-authenticates if the token expires
- 📋 Menu-driven access to:
  - 💚 Overall network health
  - 🖥️ Device inventory
  - 🏢 Site health (total devices, good/poor health counts, network & client health %)
  - 📱 Client health
  - ⚠️ Open issues
  - ✅ Compliance status
  - 💿 Software images
  - 🗺️ Physical topology summary
  - 🔧 Custom raw GET call (any endpoint path, for anything not covered above)
- 🔍 Search the menu itself, or filter/search within any results table
- 📄 Auto-pagination for endpoints that return 500+ records — nothing gets silently cut off
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

- 🔓 TLS certificate verification is disabled by default (`verify=False`) since most
  Catalyst Center deployments use self-signed certs. If your appliance has a
  trusted cert, set `verify_ssl=True` when constructing `CatalystCenter`
  in `dnac_menu.py`.
- ✅ This tool only issues GET requests — it does not push config or make
  any changes to your environment.
- 📄 Catalyst Center caps most list endpoints at 500 records per call.
  The Site Health endpoint caps at 50. Both limits are handled automatically
  by `CatalystCenter.get_all_pages()`. If you add a new menu item for a list
  endpoint, call `dnac.get_all_pages(path, page_size=N)` instead of
  `dnac.get(...)` and set `page_size` to the endpoint's documented maximum.
- 🗂️ The **Custom raw GET call** option prints only the first 4 000
  characters to the terminal but always saves the **full, untruncated**
  response to `raw_output/<timestamp>.json` (created automatically, git-ignored).
  It also asks whether to auto-paginate — say yes for list endpoints, no
  for single-object endpoints like `/count`.

## 🧩 Extending it

Adding a new menu item is a copy-paste job:

1. Write a function that takes `dnac`, calls `dnac.get(path)` or
   `dnac.get_all_pages(path, page_size=N)` for list endpoints, and passes
   results to `display_table(rows, headers)`.
2. Add one line to the `MENU_ITEMS` list at the bottom of `dnac_menu.py`.

Token refresh, error handling, pagination, and table search all come for free.

## License

MIT — see [LICENSE](LICENSE).
