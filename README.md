# CostGuard 🛡️

**CostGuard** is a standalone, lightweight, developer CLI tool built in Python that analyzes Terraform plan JSON files and predicts Azure infrastructure monthly cost impact before deployment. 

It integrates with CI/CD pipelines (e.g., GitHub Actions, Azure DevOps) to serve as a **cost policy circuit breaker**, blocking pull requests or deployments that breach predefined budget thresholds.

---

## 🌟 Key Features

- ⚡ **Zero-Credential Azure Pricing Lookup**: Uses the official public Azure Retail Prices REST API (`https://prices.azure.com/api/retail/prices`). No Azure login, API keys, or subscriptions required.
- 💾 **SQLite Price Caching (`pricing_cache.db`)**: High-performance local caching using standard Python `sqlite3`. Minimizes latency and API rate-limiting on repeat executions.
- 🔄 **Full Lifecycle Cost Delta Analysis**: Calculates monthly cost impact for `create`, `delete`, `update`, and `replace` (`delete` + `create`) resource changes.
- 🛡️ **Budget Policy Circuit Breaker**: Exits with code `1` and emits a block signal (`[CIRCUIT BREAKER]`) when net monthly cost increases exceed `--max-increase`.
- 🔇 **Noise Resilience**: Safely skips non-billable resources (Resource Groups, VNets, Subnets, NSGs) and `no-op` changes without crashing.
- 📊 **Rich Terminal & Markdown Output**: Beautiful colored terminal tables powered by `rich` or GitHub-compatible Markdown output for PR comments (`--markdown`).
- 📥 **Flexible CLI Input**: Supports stdin piping (`terraform show -json tfplan.binary | python costguard.py`) or direct file input (`--plan`).

---

## 🏗️ Architecture Overview

```
                          ┌───────────────────────────┐
                          │   Terraform Plan JSON     │
                          │   (File or Stdin Pipe)    │
                          └─────────────┬─────────────┘
                                        │
                                        ▼
                          ┌───────────────────────────┐
                          │   CostGuard CLI Parser    │
                          └─────────────┬─────────────┘
                                        │
                         Extracts resource_changes[]
                                        │
                                        ▼
                        ┌───────────────────────────────┐
                        │   Billable Spec Extractor     │
                        │ (VM Size, Region, Disk SKU)   │
                        └───────────────┬───────────────┘
                                        │
                                        ▼
                          ┌───────────────────────────┐
                          │  SQLite Pricing Cache DB  │
                          │     (pricing_cache.db)    │
                          └──────┬──────────────┬─────┘
                        Hit      │              │ Miss
                     ┌───────────┘              └───────────┐
                     ▼                                      ▼
           ┌──────────────────┐                   ┌──────────────────┐
           │ Return Local DB  │                   │  Azure Retail    │
           │ Cached Price     │                   │  Prices REST API │
           └──────────────────┘                   └────────┬─────────┘
                                                           │ Cache Write
                                                           ▼
                                                  ┌──────────────────┐
                                                  │ Store in DB      │
                                                  └──────────────────┘
                                        │
                                        ▼
                        ┌───────────────────────────────┐
                        │    Monthly Cost Calculator    │
                        │    (Hourly Rate * 730 hrs)    │
                        └───────────────┬───────────────┘
                                        │
                                        ▼
                        ┌───────────────────────────────┐
                        │  Policy Verdict & Output      │
                        │   (Exit Code 0 or Code 1)     │
                        └───────────────────────────────┘
```

---

## 🛠️ Installation

CostGuard requires **Python 3.11+**.

```bash
# 1. Clone the repository
git clone https://github.com/your-username/CostGuard.git
cd CostGuard

# 2. Create and activate a virtual environment (optional)
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## 🚀 Usage & CLI Commands

### Command-Line Arguments

| Argument | Description | Default |
|---|---|---|
| `--plan` | Path to Terraform JSON plan file | Read from `stdin` |
| `--max-increase` | Maximum net monthly cost increase allowed (float) | None (no limit) |
| `--currency` | Currency code (e.g., `USD`, `EUR`, `GBP`) | `USD` |
| `--clear-cache` | Clear SQLite pricing cache before execution | False |
| `--markdown` | Render report as GitHub-compatible Markdown | False |

---

### Basic Usage Examples

#### 1. Analyze via Stdin (Terraform CLI integration)

```bash
terraform show -json tfplan.binary | python costguard.py --max-increase 50
```

#### 2. Analyze via Plan File

```bash
python costguard.py --plan test-plans/plan1_create_vm.json --max-increase 50
```

#### 3. Change Currency (EUR)

```bash
python costguard.py --plan test-plans/plan1_create_vm.json --currency EUR
```

#### 4. Clear Cache

```bash
python costguard.py --plan test-plans/plan1_create_vm.json --clear-cache
```

#### 5. Generate Markdown Report for GitHub PR Comments

```bash
python costguard.py --plan test-plans/plan1_create_vm.json --markdown
```

---

## 🌐 Azure Retail Prices API & Pricing Calculation

CostGuard queries the official public Azure Retail Prices REST API:
`https://prices.azure.com/api/retail/prices`

### VM Query Filters (OData)
```odata
serviceName eq 'Virtual Machines' 
and armRegionName eq '{region}' 
and armSkuName eq '{sku}' 
and priceType eq 'Consumption'
```

- **Filtering Out Noise**: CostGuard automatically filters out `Spot` and `Low Priority` pricing meters unless explicitly configured for spot compute.
- **OS Specifics**: Differentiates Linux compute rates vs Windows compute rates.

### Standardized Monthly Hours
Azure Retail Prices returns hourly rates (`unitOfMeasure == '1 Hour'`). 
CostGuard normalizes monthly cost using the industry standard **730 hours per month**:

$$\text{Monthly Cost} = \text{Hourly Rate} \times 730$$

For managed storage disks returned with monthly billing rates (`unitOfMeasure == '1/Month'`), the monthly cost equals the retail price directly.

---

## 💾 SQLite Caching Engine

CostGuard uses a local SQLite database (`pricing_cache.db`) with the following schema:

```sql
CREATE TABLE IF NOT EXISTS pricing_cache (
    sku TEXT NOT NULL,
    region TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    hourly_rate REAL NOT NULL,
    cached_at INTEGER NOT NULL,
    PRIMARY KEY (sku, region, currency)
);
```

### Lookup Workflow:
1. **Cache Check**: Query `pricing_cache.db` for `(sku, region, currency)`.
2. **Cache Hit**: Instantly return cached hourly rate. Increment `Cache hits` counter.
3. **Cache Miss**: Fetch live rate from Azure API, store in `pricing_cache.db`, and increment `API lookups` counter.

---

## 🚦 Exit Codes

CostGuard adheres to standard Unix exit codes for CI/CD pipeline enforcement:

| Exit Code | Condition | Description |
|:---:|---|---|
| `0` | **Success / Within Budget** | Plan executed successfully and net cost impact is within `--max-increase`. |
| `1` | **Budget Threshold Breached** | Net monthly impact exceeds `--max-increase`. Deployment is blocked. |
| `2` | **Invalid Input / Execution Error** | Missing plan file/stdin, malformed JSON, or invalid arguments. |

---

## 🧪 Test Plans & Verification

CostGuard includes 4 pre-packaged realistic test plans in `test-plans/`:

```bash
# Test 1: Create Virtual Machine & Managed Disk
python costguard.py --plan test-plans/plan1_create_vm.json --max-increase 50

# Test 2: Delete Virtual Machine (Shows negative cost impact / savings)
python costguard.py --plan test-plans/plan2_delete_vm.json

# Test 3: Upgrade Virtual Machine (Standard_B2s -> Standard_D2s_v3)
python costguard.py --plan test-plans/plan3_upgrade_vm.json --max-increase 20

# Test 4: Hostile Noise Plan (16 Non-billable resources: RGs, VNets, Subnets, NSGs)
python costguard.py --plan test-plans/plan4_hostile_noise.json --max-increase 10
```

### Running Unit Tests

Run the full pytest suite (covering 14 comprehensive tests):

```bash
pytest -v
```

---

## 🐙 GitHub Actions CI/CD Integration Example

Add CostGuard to your GitHub Actions workflow (`.github/workflows/costguard.yml`):

```yaml
name: CostGuard Infrastructure Budget Check

on:
  pull_request:
    branches: [ main ]

jobs:
  costguard:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Code
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install CostGuard
        run: pip install -r requirements.txt

      - name: Generate Terraform Plan JSON
        run: |
          terraform init
          terraform plan -out=tfplan.binary
          terraform show -json tfplan.binary > plan.json

      - name: Run CostGuard Budget Policy Check
        run: |
          python costguard.py --plan plan.json --max-increase 50 --markdown > report.md
          cat report.md

      - name: Comment PR with Cost Impact
        if: always()
        uses: pytoolz/comment-on-pr@v1
        with:
          message-file: report.md
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```
