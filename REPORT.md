# CostGuard Implementation & Performance Report 🛡️

---

## 1. What We Built

**CostGuard** is a standalone, high-performance CLI tool designed for FinOps and DevOps engineering teams to inspect Terraform plan JSON files and predict monthly Azure cloud infrastructure cost impacts prior to deployment. 

The application operates without Azure credentials or subscription access by utilizing the public **Azure Retail Prices REST API**. It maintains a persistent local **SQLite database** (`pricing_cache.db`) to cache pricing records, reducing network overhead and API call latency for repeated resource checks.

### Key Highlights:
- **Zero-Auth Azure Pricing Engine**: Queries `https://prices.azure.com/api/retail/prices` dynamically using OData query filters.
- **SQLite Local Cache**: High-speed SQLite caching with `--clear-cache` capability.
- **Resource Lifecycle Delta Tracking**: Evaluates `create`, `delete`, `update`, and `replace` (`delete` + `create`) resource actions.
- **CI/CD Circuit Breaker**: Exits with exit code `1` and emits a policy verdict message when net monthly cost exceeds `--max-increase`.
- **Noise Filtering**: Ignores non-billable Azure resources (Resource Groups, VNets, Subnets, NSGs, Route Tables) and `no-op` changes gracefully.
- **Rich & Markdown Formats**: Offers styled CLI table rendering via `rich` and standard GitHub Markdown rendering via `--markdown`.

---

## 2. Detection & Extraction Logic

CostGuard inspects the `resource_changes[]` array in the standard Terraform plan JSON.

### Resource Change Parsing
For each resource change entry:
1. `address`: Identifies resource location in Terraform state (e.g. `azurerm_linux_virtual_machine.web_server`).
2. `type`: Checked against billable vs non-billable resource registries.
3. `change.actions`: Evaluated to classify action:
   - `["no-op"]` / `["read"]` -> Skipped cleanly.
   - `["create"]` -> Evaluates `change.after` configuration. Old = $0.00.
   - `["delete"]` -> Evaluates `change.before` configuration. New = $0.00.
   - `["update"]` -> Compares `change.before` and `change.after`.
   - `["delete", "create"]` or `["create", "delete"]` -> Classified as replacement. Compares `change.before` and `change.after`.

### Attribute Extraction Rules

| Resource Type | Extracted Attributes | SKU Derivation / Mapping |
|---|---|---|
| `azurerm_linux_virtual_machine` | `size` / `vm_size`, `location` | Direct Azure VM SKU (e.g., `Standard_B1s`, `Standard_D2s_v3`). OS set to `linux`. |
| `azurerm_windows_virtual_machine` | `size` / `vm_size`, `location` | Direct Azure VM SKU (e.g., `Standard_B2s`). OS set to `windows`. |
| `azurerm_virtual_machine` | `size` / `vm_size`, `location`, `storage_os_disk`, `os_profile_windows_config` | Evaluates OS configuration to route to `windows` or `linux` VM pricing meters. |
| `azurerm_managed_disk` | `storage_account_type`, `disk_size_gb`, `location` | Maps storage type + size GB to Azure Managed Disk SKU codes (e.g. `Standard_LRS` 32GB $\rightarrow$ `S4`, `Premium_LRS` 128GB $\rightarrow$ `P10`, `StandardSSD_LRS` 128GB $\rightarrow$ `E10`). |
| Non-Billable Resources | `azurerm_resource_group`, `azurerm_virtual_network`, `azurerm_subnet`, `azurerm_network_security_group`, etc. | Safely skipped without performing pricing lookups or breaking execution. |

---

## 3. Methods Table

| Function / Component | Signature / Location | Description |
|---|---|---|
| `init_db` | `init_db(db_path)` in `costguard.py` | Initializes `pricing_cache` SQLite table schema. |
| `clear_cache` | `clear_cache(db_path)` in `costguard.py` | Flushes all entries from SQLite database table. |
| `get_cached_price` | `get_cached_price(sku, region, currency)` | Queries SQLite DB for cached hourly rate. |
| `save_cached_price` | `save_cached_price(sku, region, currency, hourly_rate)` | Inserts or updates price entry in SQLite DB. |
| `fetch_azure_price` | `fetch_azure_price(sku, region, currency, os_type, resource_type)` | Queries live Azure Retail Prices API with OData filters. Excludes Spot/Low-Priority meters. |
| `extract_billable_spec` | `extract_billable_spec(resource_type, config)` | Extracts region, SKU, and OS parameters from Terraform config dict. |
| `map_disk_size_to_sku` | `map_disk_size_to_sku(storage_type, disk_size_gb)` | Converts Terraform disk size GB & storage type to Azure disk SKU code (`P10`, `S4`, `E10`). |
| `CostGuardEngine` | `CostGuardEngine.process_plan(plan_data)` | Core pricing engine executing plan delta calculation, accumulating hits & lookups. |
| `render_terminal_report` | `render_terminal_report(results, currency, max_increase)` | Generates styled terminal output with `rich` or fallback ascii layout. |
| `render_markdown_report` | `render_markdown_report(results, currency, max_increase)` | Renders GitHub-compatible Markdown summary report. |

---

## 4. Results Matrix

The test suite and test plans were executed against CostGuard. The operational outcomes are summarized below:

| Test Plan | Scenario | Primary Action | Prior Monthly | Projected Monthly | Net Monthly Impact | Exit Code | Policy Status |
|---|---|---|---:|---:|---:|:---:|:---:|
| **Plan 1** | Create Linux VM + Managed Disk | `create` | $0.00 | $9.13 | +$9.13 | `0` | PASSED |
| **Plan 2** | Delete Existing VM | `delete` | $7.59 | $0.00 | -$7.59 | `0` | PASSED |
| **Plan 3** | Upgrade VM (`Standard_B2s` $\rightarrow$ `Standard_D2s_v3`) | `update` | $30.37 | $70.08 | +$39.71 | `0` | PASSED |
| **Plan 3 (Breach)** | Upgrade VM with `--max-increase 20` | `update` | $30.37 | $70.08 | +$39.71 | `1` | **FAILED** |
| **Plan 4** | Hostile Noise (16 non-billable resources) | `create`/`update`/`delete`/`no-op` | $0.00 | $0.00 | $0.00 | `0` | PASSED |
| **Malformed JSON** | Invalid input format | N/A | N/A | N/A | N/A | `2` | Error Exit |

---

## 5. Limitations & Next Steps

### Current Limitations:
1. **Azure Resource Scope**: Curated support currently covers compute VMs (`azurerm_linux_virtual_machine`, `azurerm_windows_virtual_machine`, `azurerm_virtual_machine`) and managed disks (`azurerm_managed_disk`).
2. **Complex Tiering / Data Transfer**: Network ingress/egress, bandwidth data transfers, and sub-hour auto-scaling metrics are not embedded in static Terraform plans.
3. **Reserved Instances / Savings Plans**: Azure Retail Prices API queries consumption (Pay-As-You-Go) pricing. Enterprise Agreement (EA) custom discount rates or Reserved Instances are not currently factored into public retail queries.

### Future Roadmap / Next Steps:
- **Expanded Azure Services**: Add support for Azure Database for PostgreSQL/MySQL, AKS clusters, Azure App Service plans, and Azure SQL databases.
- **Custom EA Pricing Support**: Allow passing custom discount rate modifiers or API keys for Azure Enterprise Agreements.
- **Infracost Compatibility**: Add output compatibility with standard CI SARIF or JSON report schemas.

---

## 6. How to Run It

### Prerequisites
Python 3.11+ and dependencies installed:
```bash
pip install -r requirements.txt
```

### Running Test Plans
```bash
# Plan 1: Net-new VM Creation
python costguard.py --plan test-plans/plan1_create_vm.json --max-increase 50

# Plan 2: VM Deletion (Savings)
python costguard.py --plan test-plans/plan2_delete_vm.json

# Plan 3: VM Upgrade with Circuit Breaker Enforcement
python costguard.py --plan test-plans/plan3_upgrade_vm.json --max-increase 20

# Plan 4: Hostile Noise Verification
python costguard.py --plan test-plans/plan4_hostile_noise.json

# Markdown Report Generation
python costguard.py --plan test-plans/plan1_create_vm.json --markdown
```

### Running Automated Test Suite
```bash
pytest -v
```
