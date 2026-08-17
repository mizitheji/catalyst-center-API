# Catalyst Center API Menu

A simple interactive CLI for pulling common data out of Cisco Catalyst
Center (formerly DNA Center) over its REST API — no need to remember
endpoint paths or write one-off `curl` commands.

Run it, enter the appliance IP/hostname and your login, and pick from
a menu of read-only API calls.

## Features

- Prompts for Catalyst Center IP/hostname, username, and password
- Authenticates via `/dna/system/api/v1/auth/token` and auto re-authenticates if the token expires
- Menu-driven access to:
  - Overall network health
  - Device inventory
  - Site health
  - Client health
  - Open issues
  - Compliance status
  - Software images
  - Physical topology summary
  - Custom raw GET call (any endpoint path, for anything not covered above)
- Pretty table output via `tabulate`, colored terminal output via `colorama`

## Requirements

- Python 3.8+
- Network reachability to your Catalyst Center appliance (HTTPS, port 443)
- An account with API access on Catalyst Center

## Installation

```bash
git clone https://github.com/<your-username>/catalyst-center-menu.git
cd catalyst-center-menu
pip install -r requirements.txt
```

## Usage

```bash
python3 dnac_menu.py
```

You'll be prompted for:

```
Catalyst Center IP/hostname: 10.10.10.1
Username: admin
Password:
```

Then pick a number from the menu to run that API call.

### Searching

- **Search the menu itself** — type `/keyword` at the `Choice:` prompt (e.g. `/health`, `/issue`) instead of a number. Handy once you've added a lot of menu items.
- **Search within results** — any menu item that returns a table (inventory, health, issues, compliance, etc.) drops you into a search box after printing the table. Type any term to filter rows where any column contains it (case-insensitive), `c` to clear the filter, or hit Enter to go back to the main menu.

## Notes

- TLS certificate verification is disabled by default (`verify=False`) since most
  Catalyst Center deployments use self-signed certs. If your appliance has a
  trusted cert, you can set `verify_ssl=True` when constructing `CatalystCenter`
  in `dnac_menu.py`.
- This tool only issues GET requests — it does not push config or make changes
  to your environment.
- Catalyst Center caps most list endpoints at 500 records per call
  (`offset`/`limit` query params). Device Inventory and Compliance Status
  use `CatalystCenter.get_all_pages()`, which pages through automatically
  until every record is fetched — so a site with 800+ devices will still
  return everything, not just the first 500. If you add a new menu item for
  another list endpoint that could exceed 500 results, call
  `dnac.get_all_pages(path, params=...)` instead of `dnac.get(...)` to get
  the same behavior.
- The "Custom raw GET call" menu option lets you hit any other read-only
  endpoint from the [Catalyst Center API docs](https://developer.cisco.com/docs/dna-center/)
  without needing to add a dedicated menu function first.

## License

MIT — see [LICENSE](LICENSE).
