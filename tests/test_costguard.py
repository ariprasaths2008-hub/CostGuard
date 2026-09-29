"""
Unit and integration tests for CostGuard CLI tool.
"""

import os
import sys
import json
import sqlite3
import subprocess
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from costguard import (
    CostGuardEngine,
    extract_billable_spec,
    clear_cache,
    init_db,
    get_cached_price,
    save_cached_price,
    map_disk_size_to_sku,
    DB_PATH
)

TEST_DB_PATH = "test_pricing_cache.db"


@pytest.fixture(autouse=True)
def cleanup_test_db():
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    yield
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)


def test_extract_billable_spec_vm():
    config_linux = {
        "location": "eastus",
        "size": "Standard_B1s"
    }
    spec = extract_billable_spec("azurerm_linux_virtual_machine", config_linux)
    assert spec is not None
    assert spec["sku"] == "Standard_B1s"
    assert spec["region"] == "eastus"
    assert spec["os_type"] == "linux"

    config_windows = {
        "location": "westus",
        "vm_size": "Standard_D2s_v3"
    }
    spec_win = extract_billable_spec("azurerm_windows_virtual_machine", config_windows)
    assert spec_win is not None
    assert spec_win["sku"] == "Standard_D2s_v3"
    assert spec_win["os_type"] == "windows"


def test_extract_billable_spec_disk():
    config_disk = {
        "location": "eastus",
        "storage_account_type": "Standard_LRS",
        "disk_size_gb": 32
    }
    spec = extract_billable_spec("azurerm_managed_disk", config_disk)
    assert spec is not None
    assert spec["sku"] == "S4"
    assert spec["os_type"] == "disk"


def test_extract_non_billable_spec():
    config_rg = {"location": "eastus", "name": "rg-test"}
    spec = extract_billable_spec("azurerm_resource_group", config_rg)
    assert spec is None

    config_vnet = {"location": "eastus", "address_space": ["10.0.0.0/16"]}
    spec_vnet = extract_billable_spec("azurerm_virtual_network", config_vnet)
    assert spec_vnet is None


def test_map_disk_size_to_sku():
    assert map_disk_size_to_sku("Premium_LRS", 32) == "P4"
    assert map_disk_size_to_sku("Premium_LRS", 128) == "P10"
    assert map_disk_size_to_sku("StandardSSD_LRS", 128) == "E10"
    assert map_disk_size_to_sku("Standard_LRS", 32) == "S4"


def test_plan1_create():
    engine = CostGuardEngine(currency="USD", db_path=TEST_DB_PATH)
    plan_path = os.path.join(os.path.dirname(__file__), "..", "test-plans", "plan1_create_vm.json")
    with open(plan_path, "r") as f:
        plan_data = json.load(f)

    results = engine.process_plan(plan_data)
    assert len(results["items"]) == 2
    assert results["prior_total"] == 0.0
    assert results["projected_total"] > 0.0
    assert results["net_impact"] > 0.0
    assert results["items"][0]["action"] == "create"
    assert results["items"][1]["action"] == "create"


def test_plan2_delete():
    engine = CostGuardEngine(currency="USD", db_path=TEST_DB_PATH)
    plan_path = os.path.join(os.path.dirname(__file__), "..", "test-plans", "plan2_delete_vm.json")
    with open(plan_path, "r") as f:
        plan_data = json.load(f)

    results = engine.process_plan(plan_data)
    assert len(results["items"]) == 1
    assert results["prior_total"] > 0.0
    assert results["projected_total"] == 0.0
    assert results["net_impact"] < 0.0
    assert results["items"][0]["action"] == "delete"


def test_plan3_upgrade():
    engine = CostGuardEngine(currency="USD", db_path=TEST_DB_PATH)
    plan_path = os.path.join(os.path.dirname(__file__), "..", "test-plans", "plan3_upgrade_vm.json")
    with open(plan_path, "r") as f:
        plan_data = json.load(f)

    results = engine.process_plan(plan_data)
    assert len(results["items"]) == 1
    assert results["items"][0]["action"] == "update"
    assert results["items"][0]["sku"] == "Standard_D2s_v3"
    assert results["net_impact"] > 0.0


def test_replacement_vm():
    engine = CostGuardEngine(currency="USD", db_path=TEST_DB_PATH)
    replace_plan = {
        "format_version": "1.2",
        "resource_changes": [
            {
                "address": "azurerm_linux_virtual_machine.replaced_vm",
                "type": "azurerm_linux_virtual_machine",
                "change": {
                    "actions": ["delete", "create"],
                    "before": {"location": "eastus", "size": "Standard_B1s"},
                    "after": {"location": "eastus", "size": "Standard_B2s"}
                }
            }
        ]
    }
    results = engine.process_plan(replace_plan)
    assert len(results["items"]) == 1
    assert results["items"][0]["action"] == "replace"
    assert results["net_impact"] > 0.0


def test_noop_and_non_billable():
    engine = CostGuardEngine(currency="USD", db_path=TEST_DB_PATH)
    plan_path = os.path.join(os.path.dirname(__file__), "..", "test-plans", "plan4_hostile_noise.json")
    with open(plan_path, "r") as f:
        plan_data = json.load(f)

    results = engine.process_plan(plan_data)
    assert len(results["items"]) == 0
    assert results["prior_total"] == 0.0
    assert results["projected_total"] == 0.0
    assert results["net_impact"] == 0.0


def test_unknown_sku():
    engine = CostGuardEngine(currency="USD", db_path=TEST_DB_PATH)
    unknown_plan = {
        "format_version": "1.2",
        "resource_changes": [
            {
                "address": "azurerm_linux_virtual_machine.fake_vm",
                "type": "azurerm_linux_virtual_machine",
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {"location": "eastus", "size": "Standard_FAKE_SKU_99"}
                }
            }
        ]
    }
    results = engine.process_plan(unknown_plan)
    assert len(results["items"]) == 1
    assert results["items"][0]["new_cost"] == 0.0
    assert results["net_impact"] == 0.0


def test_cache_hit_and_miss():
    engine = CostGuardEngine(currency="USD", db_path=TEST_DB_PATH)
    plan = {
        "format_version": "1.2",
        "resource_changes": [
            {
                "address": "azurerm_linux_virtual_machine.vm1",
                "type": "azurerm_linux_virtual_machine",
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {"location": "eastus", "size": "Standard_B1s"}
                }
            }
        ]
    }

    # First run -> API Lookup
    res1 = engine.process_plan(plan)
    assert res1["api_lookups"] == 1
    assert res1["cache_hits"] == 0

    # Second run -> Cache Hit
    engine2 = CostGuardEngine(currency="USD", db_path=TEST_DB_PATH)
    res2 = engine2.process_plan(plan)
    assert res2["api_lookups"] == 0
    assert res2["cache_hits"] == 1


def test_cli_budget_pass():
    cmd = [
        sys.executable,
        "costguard.py",
        "--plan", "test-plans/plan1_create_vm.json",
        "--max-increase", "100"
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0
    assert "PASSED" in proc.stdout


def test_cli_budget_failure():
    cmd = [
        sys.executable,
        "costguard.py",
        "--plan", "test-plans/plan3_upgrade_vm.json",
        "--max-increase", "5"
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 1
    assert "FAILED" in proc.stdout
    assert "[CIRCUIT BREAKER]" in proc.stdout


def test_cli_invalid_json(tmp_path):
    bad_json_file = tmp_path / "bad.json"
    bad_json_file.write_text("{ this is invalid json }")

    cmd = [
        sys.executable,
        "costguard.py",
        "--plan", str(bad_json_file)
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 2
    assert "Error" in proc.stderr
