#!/usr/bin/env python3
"""
Cisco Catalyst Center (DNA Center) API Menu Tool
--------------------------------------------------
Interactive CLI that authenticates against a Catalyst Center appliance
and lets you run a menu of common read-only API calls: health checks,
device inventory, site health, client health, issues, compliance, etc.

Author: mizitheji
License: MIT
"""

import sys
import os
import getpass
import json
from datetime import datetime

import requests
import urllib3
from tabulate import tabulate
from colorama import Fore, Style, init as colorama_init

colorama_init(autoreset=True)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT = 15


# --------------------------------------------------------------------------- #
# Auth / session handling
# --------------------------------------------------------------------------- #
class CatalystCenter:
    def __init__(self, host, username, password, verify_ssl=False):
        self.host = host.rstrip("/")
        self.base_url = f"https://{self.host}"
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.token = None
        self.token_time = None

    def authenticate(self):
        url = f"{self.base_url}/dna/system/api/v1/auth/token"
        try:
            resp = requests.post(
                url,
                auth=(self.username, self.password),
                verify=self.verify_ssl,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            self.token = resp.json()["Token"]
            self.token_time = datetime.now()
            return True
        except requests.exceptions.HTTPError:
            print(Fore.RED + f"Authentication failed: HTTP {resp.status_code} - {resp.text}")
        except requests.exceptions.ConnectionError:
            print(Fore.RED + f"Could not connect to {self.host}. Check the IP/hostname and network path.")
        except requests.exceptions.Timeout:
            print(Fore.RED + "Connection timed out.")
        except Exception as e:
            print(Fore.RED + f"Unexpected error during authentication: {e}")
        return False

    def _headers(self):
        return {"X-Auth-Token": self.token, "Content-Type": "application/json"}

    def get(self, path, params=None):
        """GET wrapper with automatic re-auth on 401."""
        url = f"{self.base_url}{path}"
        try:
            resp = requests.get(
                url, headers=self._headers(), params=params,
                verify=self.verify_ssl, timeout=TIMEOUT,
            )
            if resp.status_code == 401:
                print(Fore.YELLOW + "Token expired, re-authenticating...")
                if self.authenticate():
                    resp = requests.get(
                        url, headers=self._headers(), params=params,
                        verify=self.verify_ssl, timeout=TIMEOUT,
                    )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError:
            print(Fore.RED + f"API call failed: HTTP {resp.status_code} - {resp.text[:300]}")
        except requests.exceptions.ConnectionError:
            print(Fore.RED + "Connection error during API call.")
        except requests.exceptions.Timeout:
            print(Fore.RED + "API call timed out.")
        except Exception as e:
            print(Fore.RED + f"Unexpected error: {e}")
        return None

    def get_all_pages(self, path, params=None, page_size=500):
        """GET wrapper that pages through offset/limit until all records
        are collected. Catalyst Center endpoints like network-device cap
        each response at 500 records (offset is 1-indexed), so anything
        beyond that needs multiple calls.
        """
        all_items = []
        offset = 1
        while True:
            call_params = dict(params or {})
            call_params["offset"] = offset
            call_params["limit"] = page_size
            data = self.get(path, params=call_params)
            if not data or not data.get("response"):
                break
            batch = data["response"]
            all_items.extend(batch)
            print(Fore.CYAN + f"  ...fetched {len(all_items)} record(s) so far")
            if len(batch) < page_size:
                break
            offset += page_size
        return all_items


# --------------------------------------------------------------------------- #
# Table display with built-in row search/filter
# --------------------------------------------------------------------------- #
def display_table(rows, headers):
    """Print a table, then offer an interactive search box to filter rows.

    Typing a term filters to rows where ANY column contains that term
    (case-insensitive). Blank input shows everything and exits the search
    loop back to the caller (which returns to the main menu).
    """
    if not rows:
        print(Fore.YELLOW + "No data to display.")
        return

    current = rows
    while True:
        print(tabulate(current, headers=headers, tablefmt="fancy_grid"))
        print(Fore.GREEN + f"{len(current)} row(s) shown (of {len(rows)} total)")
        term = input(
            Fore.CYAN + "Search/filter (Enter to go back to menu, 'c' to clear filter): "
        ).strip()
        if term == "":
            return
        if term.lower() == "c":
            current = rows
            continue
        term_lower = term.lower()
        current = [
            r for r in rows
            if any(term_lower in str(cell).lower() for cell in r)
        ]
        if not current:
            print(Fore.YELLOW + f"No rows match '{term}'.")
            current = rows


# --------------------------------------------------------------------------- #
# API call functions - each returns nothing, just prints a formatted table
# --------------------------------------------------------------------------- #
def show_system_health(dnac):
    print(Fore.CYAN + "\n=== Overall Network Health ===")
    data = dnac.get("/dna/intent/api/v1/network-health")
    resp = data.get("response") if data else None
    if not resp:
        print(Fore.YELLOW + "No health data returned.")
        return
    rows = []
    for entry in resp:
        rows.append([
            entry.get("category", "-"),
            entry.get("totalCount", "-"),
            entry.get("goodCount", "-"),
            entry.get("noHealthCount", "-"),
            f'{entry.get("goodPercentage", "-")}%',
        ])
    display_table(rows, ["Category", "Total", "Good", "No Data", "Good %"])


def show_device_inventory(dnac):
    print(Fore.CYAN + "\n=== Device Inventory ===")
    devices = dnac.get_all_pages("/dna/intent/api/v1/network-device")
    if not devices:
        print(Fore.YELLOW + "No devices returned.")
        return
    rows = []
    for d in devices:
        rows.append([
            d.get("hostname", "-"),
            d.get("managementIpAddress", "-"),
            d.get("family", "-"),
            d.get("softwareVersion", "-"),
            d.get("reachabilityStatus", "-"),
            d.get("collectionStatus", "-"),
        ])
    print(Fore.GREEN + f"Total devices: {len(devices)}")
    display_table(rows, ["Hostname", "Mgmt IP", "Family", "SW Version", "Reachability", "Collection"])


def show_site_health(dnac):
    print(Fore.CYAN + "\n=== Site Health ===")
    sites = dnac.get_all_pages("/dna/intent/api/v1/site-health")
    if not sites:
        print(Fore.YELLOW + "No site health data returned.")
        return
    rows = []
    for s in sites:
        rows.append([
            s.get("siteName", "-"),
            s.get("siteType", "-"),
            s.get("networkHealthAverage", "-"),
            s.get("clientHealthWired", "-"),
            s.get("clientHealthWireless", "-"),
        ])
    display_table(rows, ["Site", "Type", "Network Health", "Wired Client Health", "Wireless Client Health"])


def show_client_health(dnac):
    print(Fore.CYAN + "\n=== Client Health ===")
    data = dnac.get("/dna/intent/api/v1/client-health")
    if not data or not data.get("response"):
        print(Fore.YELLOW + "No client health data returned.")
        return
    rows = []
    for c in data["response"]:
        for score in c.get("scoreDetail", []):
            rows.append([
                c.get("siteId", "-"),
                score.get("scoreCategory", {}).get("value", "-"),
                score.get("scoreValue", "-"),
                score.get("clientCount", "-"),
            ])
    display_table(rows, ["Site ID", "Category", "Score", "Client Count"])


def show_issues(dnac):
    print(Fore.CYAN + "\n=== Open Issues ===")
    issues = dnac.get_all_pages("/dna/intent/api/v1/issues")
    if not issues:
        print(Fore.YELLOW + "No open issues returned.")
        return
    rows = []
    for i in issues:
        rows.append([
            i.get("name", "-"),
            i.get("priority", "-"),
            i.get("category", "-"),
            i.get("deviceRole", "-"),
            i.get("status", "-"),
        ])
    print(Fore.GREEN + f"Total issues: {len(issues)}")
    display_table(rows, ["Issue", "Priority", "Category", "Device Role", "Status"])


def show_compliance(dnac):
    print(Fore.CYAN + "\n=== Compliance Status (all devices) ===")
    devices = dnac.get_all_pages("/dna/intent/api/v1/network-device")
    if not devices:
        print(Fore.YELLOW + "No devices found.")
        return
    rows = []
    for d in devices[:50]:  # cap to avoid hammering the API
        device_id = d.get("id")
        comp = dnac.get(f"/dna/intent/api/v1/compliance/{device_id}/detail")
        if comp and comp.get("response"):
            for c in comp["response"]:
                rows.append([
                    d.get("hostname", "-"),
                    c.get("complianceType", "-"),
                    c.get("status", "-"),
                ])
    display_table(rows, ["Hostname", "Compliance Type", "Status"])


def show_software_images(dnac):
    print(Fore.CYAN + "\n=== Software Images ===")
    images = dnac.get_all_pages("/dna/intent/api/v1/image/importation")
    if not images:
        print(Fore.YELLOW + "No software image data returned.")
        return
    rows = []
    for img in images:
        rows.append([
            img.get("name", "-"),
            img.get("version", "-"),
            img.get("family", "-"),
            img.get("imageType", "-"),
        ])
    display_table(rows, ["Image Name", "Version", "Family", "Type"])


def show_physical_topology(dnac):
    print(Fore.CYAN + "\n=== Physical Topology (summary) ===")
    data = dnac.get("/dna/intent/api/v1/topology/physical-topology")
    if not data or "response" not in data:
        print(Fore.YELLOW + "No topology data returned.")
        return
    nodes = data["response"].get("nodes", [])
    links = data["response"].get("links", [])
    print(Fore.GREEN + f"Nodes: {len(nodes)}  |  Links: {len(links)}")
    rows = [[n.get("label", "-"), n.get("family", "-"), n.get("ip", "-")] for n in nodes[:50]]
    display_table(rows, ["Label", "Family", "IP"])


def show_raw_json(dnac):
    path = input("Enter API path (e.g. /dna/intent/api/v1/network-device/count): ").strip()
    if not path:
        return
    paginate = input(
        "Is this a list endpoint that might return >500 records? "
        "Auto-paginate through offset/limit? (y/N): "
    ).strip().lower() == "y"

    if paginate:
        items = dnac.get_all_pages(path)
        data = {"response": items, "totalFetched": len(items)}
    else:
        data = dnac.get(path)

    if data is None:
        return

    full_json = json.dumps(data, indent=2)

    # Terminal display gets truncated for large payloads - save the
    # untruncated response to a file so nothing is silently lost.
    output_dir = "raw_output"
    os.makedirs(output_dir, exist_ok=True)
    filename = os.path.join(output_dir, f"raw_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    try:
        with open(filename, "w") as f:
            f.write(full_json)
        print(Fore.GREEN + f"Full response saved to {filename} ({len(full_json)} chars)")
    except Exception as e:
        print(Fore.RED + f"Could not save output file: {e}")

    preview_limit = 4000
    print(Fore.CYAN + full_json[:preview_limit])
    if len(full_json) > preview_limit:
        print(Fore.YELLOW + f"... (truncated in terminal — see {filename} for the full response)")


# --------------------------------------------------------------------------- #
# Menu
# --------------------------------------------------------------------------- #
MENU_ITEMS = [
    ("Overall Network Health", show_system_health),
    ("Device Inventory", show_device_inventory),
    ("Site Health", show_site_health),
    ("Client Health", show_client_health),
    ("Open Issues", show_issues),
    ("Compliance Status", show_compliance),
    ("Software Images", show_software_images),
    ("Physical Topology Summary", show_physical_topology),
    ("Custom raw GET call", show_raw_json),
]


def print_banner():
    print(Fore.MAGENTA + Style.BRIGHT + r"""
   ___      _        _         _    ____
  / __\__ _| |_ __ _| |_   _ __| |_ / ___| ___ _ __
 / /  / _` | __/ _` | | | | / __| __| |   / _ \ '_  \
/ /__| (_| | || (_| | | |_| \__ \ |_| |__|  __/ | | |
\____/\__,_|\__\__,_|_|\__, |___/\__|\____|\___|_| |_|
                        |___/    Center API Menu
""")


def print_menu(items):
    print(Fore.CYAN + "\nSelect an API call to run:")
    for idx, (label, _) in enumerate(items, start=1):
        print(f"  {Fore.YELLOW}{idx}{Style.RESET_ALL}. {label}")
    print(f"  {Fore.YELLOW}0{Style.RESET_ALL}. Exit")
    print(Fore.MAGENTA + "  (type '/keyword' to search the menu, e.g. /health, /issue)")


def main():
    print_banner()
    host = input("Catalyst Center IP/hostname: ").strip()
    username = input("Username: ").strip()
    password = getpass.getpass("Password: ")

    dnac = CatalystCenter(host, username, password)
    print(Fore.CYAN + f"\nAuthenticating to {host} ...")
    if not dnac.authenticate():
        sys.exit(1)
    print(Fore.GREEN + "Authentication successful.\n")

    while True:
        print_menu(MENU_ITEMS)
        choice = input("\nChoice: ").strip()
        if choice == "0":
            print(Fore.MAGENTA + "Goodbye.")
            break

        if choice.startswith("/"):
            term = choice[1:].strip().lower()
            matches = [item for item in MENU_ITEMS if term in item[0].lower()]
            if not matches:
                print(Fore.YELLOW + f"No menu items match '{term}'.")
                continue
            print(Fore.CYAN + "\nMatches:")
            for idx, (label, _) in enumerate(matches, start=1):
                print(f"  {Fore.YELLOW}{idx}{Style.RESET_ALL}. {label}")
            print(f"  {Fore.YELLOW}0{Style.RESET_ALL}. Cancel")
            pick = input("\nChoice: ").strip()
            if pick == "0":
                continue
            try:
                pick_idx = int(pick) - 1
                if pick_idx < 0 or pick_idx >= len(matches):
                    raise ValueError
            except ValueError:
                print(Fore.RED + "Invalid choice.")
                continue
            _, func = matches[pick_idx]
            try:
                func(dnac)
            except Exception as e:
                print(Fore.RED + f"Error running this call: {e}")
            continue

        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(MENU_ITEMS):
                raise ValueError
        except ValueError:
            print(Fore.RED + "Invalid choice.")
            continue
        _, func = MENU_ITEMS[idx]
        try:
            func(dnac)
        except Exception as e:
            print(Fore.RED + f"Error running this call: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(Fore.MAGENTA + "\nInterrupted. Exiting.")
        sys.exit(0)
