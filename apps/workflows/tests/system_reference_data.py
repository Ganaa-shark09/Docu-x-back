"""Idempotent canonical reference data for workflow tests only.

A Django TransactionTestCase may flush rows populated by historical data
migrations. Each APITestCase fixture must re-establish its own reference data.
The migration definitions remain the single source of truth.
"""
from importlib import import_module
from django.apps import apps


def _seed(name: str) -> None:
    module = import_module(f"apps.workflows.migrations.{name}")
    module.seed(apps, None)


def ensure_invoice_schema() -> None:
    _seed("0006_seed_invoice_schema")


def ensure_contract_schema() -> None:
    _seed("0008_seed_contract_schema")


def ensure_clause_taxonomy() -> None:
    _seed("0010_seed_clause_taxonomy")
