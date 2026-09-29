#!/usr/bin/env python3
"""
CostGuard - Azure Terraform Plan Cost Impact Predictor
Standalone developer CLI tool that analyzes Terraform plan JSON
and predicts Azure infrastructure monthly cost impact before deployment.
"""

import sys
import os
import json
import argparse
import sqlite3
import time
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, Any, List, Optional, Tuple

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.theme import Theme
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

DB_PATH = "pricing_cache.db"
AZURE_RETAIL_API_URL = "https://prices.azure.com/api/retail/prices"
HOURS_PER_MONTH = 730

CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "INR": "₹",
    "CAD": "CA$",
    "AUD": "A$",
    "JPY": "¥"
}

NON_BILLABLE_TYPES = {
    "azurerm_resource_group",
    "azurerm_virtual_network",
    "azurerm_subnet",
    "azurerm_network_security_group",
    "azurerm_network_security_rule",
    "azurerm_route_table",
    "azurerm_route",
    "azurerm_network_interface",
    "azurerm_public_ip",
    "azurerm_subnet_network_security_group_association",
    "azurerm_subnet_route_table_association",
    "azurerm_network_interface_security_group_association",
    "azurerm_private_endpoint",
    "azurerm_dns_zone",
    "azurerm_private_dns_zone",
    "azurerm_role_assignment",
    "azurerm_user_assigned_identity"
}


def init_db(db_path: str = DB_PATH) -> None:
    """Initialize SQLite database table for pricing cache."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pricing_cache (
            sku TEXT NOT NULL,
            region TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            hourly_rate REAL NOT NULL,
            cached_at INTEGER NOT NULL,
            PRIMARY KEY (sku, region, currency)
        );
    """)
    conn.commit()
    conn.close()


def clear_cache(db_path: str = DB_PATH) -> None:
    """Clear all records from the SQLite pricing cache."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM pricing_cache;")
    conn.commit()
    conn.close()


def get_cached_price(sku: str, region: str, currency: str, db_path: str = DB_PATH) -> Optional[float]:
    """Retrieve cached hourly price if present."""
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT hourly_rate FROM pricing_cache WHERE sku = ? AND region = ? AND currency = ?",
            (sku, region.lower(), currency.upper())
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def save_cached_price(sku: str, region: str, currency: str, hourly_rate: float, db_path: str = DB_PATH) -> None:
    """Save price into SQLite pricing cache."""
    init_db(db_path)
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO pricing_cache (sku, region, currency, hourly_rate, cached_at) VALUES (?, ?, ?, ?, ?)",
            (sku, region.lower(), currency.upper(), hourly_rate, int(time.time()))
        )
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


def map_disk_size_to_sku(storage_type: str, disk_size_gb: int) -> str:
    """Map disk storage type and size GB to Azure Disk SKU code."""
    sizes = [
        (4, "4"), (8, "6"), (16, "6"), (32, "4"), (64, "6"), (128, "10"),
        (256, "15"), (512, "20"), (1024, "30"), (2048, "40"), (4096, "50"),
        (8192, "60"), (16384, "70"), (32768, "80")
    ]
    size_code = "80"
    for limit, code in sizes:
        if disk_size_gb <= limit:
            size_code = code
            break

    prefix_map = {
        "Premium_LRS": "P",
        "StandardSSD_LRS": "E",
        "Standard_LRS": "S",
        "UltraSSD_LRS": "U"
    }
    prefix = prefix_map.get(storage_type, "P")
    return f"{prefix}{size_code}"


def fetch_azure_price(sku: str, region: str, currency: str = "USD", os_type: str = "linux", resource_type: Optional[str] = None) -> Optional[float]:
    """
    Fetch price from official Azure Retail Prices public API.
    Returns hourly rate or None if not found / network error.
    """
    arm_region = region.lower().replace(" ", "").replace("-", "")

    if resource_type == "azurerm_managed_disk" or os_type == "disk":
        filter_str = f"serviceName eq 'Storage' and armRegionName eq '{arm_region}' and priceType eq 'Consumption' and (skuName eq '{sku}' or skuName eq '{sku} LRS')"
    else:
        filter_str = f"serviceName eq 'Virtual Machines' and armRegionName eq '{arm_region}' and armSkuName eq '{sku}' and priceType eq 'Consumption'"

    params = {
        "currencyCode": currency.upper(),
        "$filter": filter_str
    }
    url = f"{AZURE_RETAIL_API_URL}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CostGuard/1.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status != 200:
                print(f"[WARN] Azure Retail API returned HTTP status {response.status}. Skipping.", file=sys.stderr)
                return None
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        print("[WARN] Network unavailable; defaulting SKU to $0.00.", file=sys.stderr)
        return None
    except Exception:
        print("[WARN] Network unavailable; defaulting SKU to $0.00.", file=sys.stderr)
        return None

    items = data.get("Items", [])
    if not items:
        print(f"[WARN] SKU '{sku}' not found in Azure Retail API. Skipping.", file=sys.stderr)
        return None

    # Filtering logic for VM and Disk pricing
    matched_rate: Optional[float] = None

    if resource_type == "azurerm_managed_disk" or os_type == "disk":
        for item in items:
            meter = item.get("meterName", "")
            if "Disk" in meter and "Mount" not in meter and "Operations" not in meter and "Restore" not in meter:
                price = float(item.get("retailPrice", 0.0))
                unit = item.get("unitOfMeasure", "")
                if unit == "1/Month":
                    matched_rate = price / HOURS_PER_MONTH
                else:
                    matched_rate = price
                break
    else:
        is_spot = "spot" in sku.lower() or "lowpriority" in sku.lower()
        for item in items:
            meter_name = item.get("meterName", "")
            sku_name = item.get("skuName", "")
            prod_name = item.get("productName", "")

            # Exclude Spot and Low Priority unless requested
            if not is_spot:
                if "spot" in meter_name.lower() or "spot" in sku_name.lower():
                    continue
                if "low priority" in meter_name.lower() or "low priority" in sku_name.lower():
                    continue

            if os_type == "windows":
                if "windows" not in prod_name.lower() and "windows" not in meter_name.lower():
                    continue
            else:
                if "windows" in prod_name.lower() or "windows" in meter_name.lower():
                    continue

            matched_rate = float(item.get("retailPrice", 0.0))
            break

    if matched_rate is None and items and (resource_type != "azurerm_managed_disk" and os_type != "disk"):
        # Fallback to first non-spot consumption item if strict OS check missed
        for item in items:
            meter_name = item.get("meterName", "")
            sku_name = item.get("skuName", "")
            if "spot" not in meter_name.lower() and "low priority" not in meter_name.lower():
                matched_rate = float(item.get("retailPrice", 0.0))
                break

    if matched_rate is None:
        print(f"[WARN] SKU '{sku}' not found in Azure Retail API. Skipping.", file=sys.stderr)
        return None

    return matched_rate


def extract_billable_spec(resource_type: str, config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Extract billable SKU, region, and OS details from Terraform resource configuration."""
    if not config or not isinstance(config, dict):
        return None

    if resource_type in NON_BILLABLE_TYPES:
        return None

    region = config.get("location", "eastus")

    if resource_type in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine", "azurerm_virtual_machine"):
        # Extract size/SKU
        vm_size = config.get("size") or config.get("vm_size")
        if not vm_size:
            hw_prof = config.get("hardware_profile")
            if isinstance(hw_prof, list) and hw_prof and isinstance(hw_prof[0], dict):
                vm_size = hw_prof[0].get("vm_size")
            elif isinstance(hw_prof, dict):
                vm_size = hw_prof.get("vm_size")

        if not vm_size:
            return None

        # Extract OS type
        if resource_type == "azurerm_linux_virtual_machine":
            os_type = "linux"
        elif resource_type == "azurerm_windows_virtual_machine":
            os_type = "windows"
        else:
            if config.get("os_profile_windows_config") or config.get("license_type"):
                os_type = "windows"
            else:
                os_disk = config.get("storage_os_disk")
                if isinstance(os_disk, list) and os_disk and isinstance(os_disk[0], dict):
                    os_type = "windows" if os_disk[0].get("os_type", "").lower() == "windows" else "linux"
                elif isinstance(os_disk, dict):
                    os_type = "windows" if os_disk.get("os_type", "").lower() == "windows" else "linux"
                else:
                    os_type = "linux"

        return {
            "sku": vm_size,
            "region": region,
            "os_type": os_type,
            "resource_type": resource_type
        }

    elif resource_type == "azurerm_managed_disk":
        storage_type = config.get("storage_account_type", "Standard_LRS")
        disk_size_gb = int(config.get("disk_size_gb", 32))
        disk_sku = map_disk_size_to_sku(storage_type, disk_size_gb)
        return {
            "sku": disk_sku,
            "region": region,
            "os_type": "disk",
            "resource_type": resource_type,
            "storage_type": storage_type,
            "disk_size_gb": disk_size_gb
        }

    return None


class CostGuardEngine:
    def __init__(self, currency: str = "USD", db_path: str = DB_PATH):
        self.currency = currency.upper()
        self.db_path = db_path
        self.cache_hits = 0
        self.api_lookups = 0
        init_db(self.db_path)

    def get_hourly_rate(self, spec: Optional[Dict[str, Any]]) -> float:
        if not spec:
            return 0.0

        sku = spec["sku"]
        region = spec["region"]
        os_type = spec.get("os_type", "linux")
        resource_type = spec.get("resource_type")

        cache_key = f"{sku}:{os_type}"

        # 1. Check DB Cache
        cached_rate = get_cached_price(cache_key, region, self.currency, self.db_path)
        if cached_rate is not None:
            self.cache_hits += 1
            return cached_rate

        # 2. Cache Miss -> API Call
        self.api_lookups += 1
        rate = fetch_azure_price(sku, region, self.currency, os_type, resource_type)

        if rate is not None:
            save_cached_price(cache_key, region, self.currency, rate, self.db_path)
            return rate

        return 0.0

    def process_plan(self, plan_data: Dict[str, Any]) -> Dict[str, Any]:
        resource_changes = plan_data.get("resource_changes", [])
        items = []

        prior_total = 0.0
        projected_total = 0.0

        for res in resource_changes:
            address = res.get("address", "unknown")
            res_type = res.get("type", "unknown")
            change = res.get("change", {})
            actions = change.get("actions", [])

            if "no-op" in actions or "read" in actions or not actions:
                continue

            before_config = change.get("before")
            after_config = change.get("after")

            old_spec = extract_billable_spec(res_type, before_config)
            new_spec = extract_billable_spec(res_type, after_config)

            # Skip non-billable resources completely
            if not old_spec and not new_spec:
                continue

            # Determine action text
            if actions == ["create"]:
                action_str = "create"
            elif actions == ["delete"]:
                action_str = "delete"
            elif actions == ["update"]:
                action_str = "update"
            elif "create" in actions and "delete" in actions:
                action_str = "replace"
            else:
                action_str = "/".join(actions)

            # Calculate costs
            if action_str in ("update", "replace") and old_spec and new_spec and old_spec == new_spec:
                # Metadata-only change
                old_rate = self.get_hourly_rate(old_spec)
                old_cost = old_rate * HOURS_PER_MONTH
                new_cost = old_cost
                delta = 0.0
            else:
                old_rate = self.get_hourly_rate(old_spec) if old_spec else 0.0
                new_rate = self.get_hourly_rate(new_spec) if new_spec else 0.0

                old_cost = old_rate * HOURS_PER_MONTH
                new_cost = new_rate * HOURS_PER_MONTH
                delta = new_cost - old_cost

            prior_total += old_cost
            projected_total += new_cost

            display_region = new_spec["region"] if new_spec else (old_spec["region"] if old_spec else "N/A")
            display_sku = new_spec["sku"] if new_spec else (old_spec["sku"] if old_spec else "N/A")

            items.append({
                "address": address,
                "action": action_str,
                "region": display_region,
                "sku": display_sku,
                "old_cost": old_cost,
                "new_cost": new_cost,
                "delta": delta
            })

        net_impact = projected_total - prior_total

        return {
            "items": items,
            "prior_total": prior_total,
            "projected_total": projected_total,
            "net_impact": net_impact,
            "cache_hits": self.cache_hits,
            "api_lookups": self.api_lookups
        }


def format_currency(amount: float, currency: str = "USD") -> str:
    sym = CURRENCY_SYMBOLS.get(currency.upper(), "$")
    if amount < 0:
        return f"-{sym}{abs(amount):,.2f}"
    return f"{sym}{amount:,.2f}"


def render_terminal_report(results: Dict[str, Any], currency: str, max_increase: Optional[float]) -> None:
    sym = CURRENCY_SYMBOLS.get(currency.upper(), "$")
    prior = results["prior_total"]
    projected = results["projected_total"]
    net = results["net_impact"]
    items = results["items"]

    is_failed = max_increase is not None and net > max_increase

    if RICH_AVAILABLE:
        console = Console()
        console.print()
        console.print("========================================================", style="bold blue")
        console.print("COSTGUARD: Azure Infrastructure Cost Impact Report", style="bold white on blue")
        console.print("========================================================", style="bold blue")

        table = Table(show_header=True, header_style="bold cyan", expand=True)
        table.add_column("Resource Address", style="bold white", no_wrap=False, min_width=30)
        table.add_column("Action", justify="center")
        table.add_column("Region", justify="center")
        table.add_column("SKU / Meter", justify="left")
        table.add_column(f"Old ({sym}/mo)", justify="right")
        table.add_column(f"New ({sym}/mo)", justify="right")
        table.add_column(f"Delta ({sym}/mo)", justify="right")

        for item in items:
            act = item["action"]
            if act == "create":
                act_styled = f"[green]{act}[/green]"
            elif act == "delete":
                act_styled = f"[red]{act}[/red]"
            elif act == "update":
                act_styled = f"[yellow]{act}[/yellow]"
            else:
                act_styled = f"[cyan]{act}[/cyan]"

            delta_str = format_currency(item["delta"], currency)
            delta_styled = f"[red]+{delta_str}[/red]" if item["delta"] > 0 else (f"[green]{delta_str}[/green]" if item["delta"] < 0 else f"[dim]{delta_str}[/dim]")

            table.add_row(
                item["address"],
                act_styled,
                item["region"],
                item["sku"],
                format_currency(item["old_cost"], currency),
                format_currency(item["new_cost"], currency),
                delta_styled
            )

        console.print(table)
        console.print("-" * 56, style="dim")
        console.print("FINANCIAL SUMMARY:", style="bold yellow")
        console.print(f"Prior Monthly Total:      {format_currency(prior, currency)}")
        console.print(f"Projected Monthly Total:  {format_currency(projected, currency)}")
        console.print(f"Net Monthly Impact:       {format_currency(net, currency)}")
        console.print()
        console.print(f"Cache hits:               {results['cache_hits']}")
        console.print(f"API lookups:              {results['api_lookups']}")
        console.print("-" * 56, style="dim")
        console.print("POLICY VERDICT:", style="bold yellow")
        thresh_str = format_currency(max_increase, currency) if max_increase is not None else "N/A"
        console.print(f"Budget Threshold: {thresh_str}")

        if is_failed:
            console.print("Status: FAILED (Exceeds budget allowance)", style="bold red")
            console.print("========================================================", style="bold red")
            console.print("[CIRCUIT BREAKER]", style="bold red")
            console.print("CostGuard: Budget threshold breached. Deployment blocked.", style="bold red")
        else:
            console.print("Status: PASSED (Within budget allowance)", style="bold green")
            console.print("========================================================", style="bold green")
    else:
        print("\n========================================================")
        print("COSTGUARD: Azure Infrastructure Cost Impact Report")
        print("========================================================")
        header = f"{'Resource Address':<40} {'Action':<8} {'Region':<10} {'SKU':<15} {'Old':<10} {'New':<10} {'Delta':<10}"
        print(header)
        print("-" * len(header))
        for item in items:
            print(f"{item['address']:<40} {item['action']:<8} {item['region']:<10} {item['sku']:<15} {format_currency(item['old_cost'], currency):<10} {format_currency(item['new_cost'], currency):<10} {format_currency(item['delta'], currency):<10}")

        print("-" * 56)
        print("FINANCIAL SUMMARY:\n")
        print(f"Prior Monthly Total:     {format_currency(prior, currency)}")
        print(f"Projected Monthly Total: {format_currency(projected, currency)}")
        print(f"Net Monthly Impact:      {format_currency(net, currency)}")
        print(f"\nCache hits:  {results['cache_hits']}")
        print(f"API lookups: {results['api_lookups']}")
        print("-" * 56)
        print("POLICY VERDICT:\n")
        thresh_str = format_currency(max_increase, currency) if max_increase is not None else "N/A"
        print(f"Budget Threshold: {thresh_str}")

        if is_failed:
            print("Status: FAILED (Exceeds budget allowance)")
            print("========================================================")
            print("[CIRCUIT BREAKER]")
            print("CostGuard: Budget threshold breached. Deployment blocked.")
        else:
            print("Status: PASSED (Within budget allowance)")
            print("========================================================")


def render_markdown_report(results: Dict[str, Any], currency: str, max_increase: Optional[float]) -> str:
    sym = CURRENCY_SYMBOLS.get(currency.upper(), "$")
    prior = results["prior_total"]
    projected = results["projected_total"]
    net = results["net_impact"]
    items = results["items"]
    is_failed = max_increase is not None and net > max_increase

    lines = []
    lines.append("# CostGuard Azure Infrastructure Cost Impact Report\n")
    lines.append(f"| Resource Address | Action | Region | SKU / Meter | Old ({sym}/mo) | New ({sym}/mo) | Delta ({sym}/mo) |")
    lines.append("|---|---|---|---|---:|---:|---:|")

    for item in items:
        lines.append(
            f"| `{item['address']}` | `{item['action']}` | {item['region']} | {item['sku']} | "
            f"{format_currency(item['old_cost'], currency)} | {format_currency(item['new_cost'], currency)} | "
            f"{format_currency(item['delta'], currency)} |"
        )

    lines.append("\n## Financial Summary\n")
    lines.append(f"- **Prior Monthly Total**: {format_currency(prior, currency)}")
    lines.append(f"- **Projected Monthly Total**: {format_currency(projected, currency)}")
    lines.append(f"- **Net Monthly Impact**: {format_currency(net, currency)}")
    lines.append(f"- **Cache Hits**: {results['cache_hits']}")
    lines.append(f"- **API Lookups**: {results['api_lookups']}\n")

    lines.append("## Policy Verdict\n")
    thresh_str = format_currency(max_increase, currency) if max_increase is not None else "N/A"
    lines.append(f"- **Budget Threshold**: {thresh_str}")
    if is_failed:
        lines.append("- **Status**: **FAILED (Exceeds budget allowance)**\n")
        lines.append("> **[CIRCUIT BREAKER]**  ")
        lines.append("> **CostGuard: Budget threshold breached. Deployment blocked.**")
    else:
        lines.append("- **Status**: **PASSED (Within budget allowance)**")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CostGuard - Predict Azure Infrastructure Monthly Cost Impact from Terraform Plan JSON"
    )
    parser.add_argument("--plan", type=str, help="Path to Terraform plan JSON file")
    parser.add_argument("--max-increase", type=float, help="Maximum allowed net monthly cost increase")
    parser.add_argument("--currency", type=str, default="USD", help="Currency code (e.g. USD, EUR)")
    parser.add_argument("--clear-cache", action="store_true", help="Clear pricing cache database before execution")
    parser.add_argument("--markdown", action="store_true", help="Output report in Markdown format")

    args = parser.parse_args()

    if args.clear_cache:
        clear_cache(DB_PATH)

    plan_raw: Optional[str] = None

    if args.plan:
        if not os.path.exists(args.plan):
            print(f"Error: Plan file '{args.plan}' does not exist.", file=sys.stderr)
            sys.exit(2)
        try:
            with open(args.plan, "r", encoding="utf-8") as f:
                plan_raw = f.read()
        except Exception as e:
            print(f"Error reading plan file: {e}", file=sys.stderr)
            sys.exit(2)
    elif not sys.stdin.isatty():
        try:
            plan_raw = sys.stdin.read()
        except Exception as e:
            print(f"Error reading from stdin: {e}", file=sys.stderr)
            sys.exit(2)

    if not plan_raw:
        if args.clear_cache:
            print("Cache cleared successfully.")
            sys.exit(0)
        else:
            print("Error: Missing required Terraform plan JSON via --plan or stdin.", file=sys.stderr)
            sys.exit(2)

    try:
        plan_json = json.loads(plan_raw)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid Terraform JSON format: {e}", file=sys.stderr)
        sys.exit(2)

    if not isinstance(plan_json, dict) or "resource_changes" not in plan_json:
        print("Error: Invalid Terraform plan JSON structure. Missing 'resource_changes' array.", file=sys.stderr)
        sys.exit(2)

    engine = CostGuardEngine(currency=args.currency, db_path=DB_PATH)
    results = engine.process_plan(plan_json)

    if args.markdown:
        print(render_markdown_report(results, args.currency, args.max_increase))
    else:
        render_terminal_report(results, args.currency, args.max_increase)

    is_failed = args.max_increase is not None and results["net_impact"] > args.max_increase
    if is_failed:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
