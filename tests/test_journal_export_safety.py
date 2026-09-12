"""Regression coverage for spreadsheet formula injection in journal CSV export."""
from __future__ import annotations

import csv
import io

from app.routers import journal as journal_routes


async def test_journal_csv_neutralises_formula_prefixes_but_keeps_numbers(monkeypatch):
    monkeypatch.setattr(
        journal_routes,
        "_trades",
        lambda *args: [{
            "id": 1,
            "account_name": "+danger",
            "account_spec": "normal",
            "environment": "demo",
            "symbol": "MNQ",
            "root": "MNQ",
            "side": "long",
            "net_pnl": -12.5,
            "source": "manual",
            "note": "=HYPERLINK(\"https://example.test\",\"x\")",
            "tags": ["@cmd"],
        }],
    )

    response = await journal_routes.api_export()
    rows = list(csv.reader(io.StringIO(response.body.decode())))
    record = dict(zip(rows[0], rows[1]))

    assert record["account_name"] == "'+danger"
    assert record["note"].startswith("'=HYPERLINK")
    assert record["tags"] == "'@cmd"
    assert record["net_pnl"] == "-12.5"
