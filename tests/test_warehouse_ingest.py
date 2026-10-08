"""
Tests for `lambdas.warehouse_ingest.handler`.

Named to match the lambda directory so the deploy workflow actually runs it —
it does `pytest tests/test_<lambda>.py` and silently skips anything else.

The logic worth pinning here is the ESPN crosswalk coverage guard. If the
resolution rate quietly drops, ESPN boards lose players with nothing pointing
back at this job, so the ingest is meant to fail loudly rather than publish a
half-mapped table.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

# duckdb ships to the Lambda in a layer, not requirements.txt.
if "duckdb" not in sys.modules:
    _duckdb = types.ModuleType("duckdb")
    _duckdb.connect = MagicMock(name="duckdb.connect")
    _duckdb.DuckDBPyConnection = object
    sys.modules["duckdb"] = _duckdb

from lambdas.common.espn_crosswalk import COVERAGE_FLOOR  # noqa: E402
from lambdas.warehouse_ingest import handler as mod  # noqa: E402


def _crosswalk(coverage: float, misses: int = 0):
    return {
        "mapping": {"900": {"sleeperId": "p1", "source": "sleeper_espn_id"}},
        "sources": {"sleeper_espn_id": 1},
        "misses": [{"espnId": str(i), "name": "x", "position": "WR"} for i in range(misses)],
        "coverage": coverage,
    }


class TestCrosswalkCoverageGuard:
    def test_publishes_when_coverage_is_healthy(self, monkeypatch):
        # 0.9967 is the measured figure; the floor sits below it on purpose.
        monkeypatch.setattr(mod, "build_crosswalk", lambda *a: _crosswalk(0.9967, 4))
        monkeypatch.setattr(mod, "fetch_espn_players", lambda season: [])
        monkeypatch.setattr(mod, "fetch_fantasycalc", lambda: [])

        out = mod._espn_ids_by_sleeper_id("2025", {})

        assert out == {"p1": {"espn_id": "900", "source": "sleeper_espn_id"}}

    def test_raises_below_the_floor(self, monkeypatch):
        monkeypatch.setattr(mod, "build_crosswalk", lambda *a: _crosswalk(0.5, 600))
        monkeypatch.setattr(mod, "fetch_espn_players", lambda season: [])
        monkeypatch.setattr(mod, "fetch_fantasycalc", lambda: [])

        with pytest.raises(RuntimeError) as err:
            mod._espn_ids_by_sleeper_id("2025", {})

        # The message has to name the number, or the alert says nothing useful.
        assert "0.5" in str(err.value)
        assert "600" in str(err.value)

    def test_exactly_at_the_floor_still_publishes(self, monkeypatch):
        monkeypatch.setattr(mod, "build_crosswalk", lambda *a: _crosswalk(COVERAGE_FLOOR))
        monkeypatch.setattr(mod, "fetch_espn_players", lambda season: [])
        monkeypatch.setattr(mod, "fetch_fantasycalc", lambda: [])

        assert mod._espn_ids_by_sleeper_id("2025", {}) != {}

    def test_inverts_the_mapping_onto_sleeper_ids(self, monkeypatch):
        # The ingest writes onto Sleeper player rows, so the map has to flip.
        wide = _crosswalk(1.0)
        wide["mapping"]["901"] = {"sleeperId": "p2", "source": "fantasycalc"}
        monkeypatch.setattr(mod, "build_crosswalk", lambda *a: wide)
        monkeypatch.setattr(mod, "fetch_espn_players", lambda season: [])
        monkeypatch.setattr(mod, "fetch_fantasycalc", lambda: [])

        out = mod._espn_ids_by_sleeper_id("2025", {})

        assert out["p2"] == {"espn_id": "901", "source": "fantasycalc"}


def test_refresh_players_stores_the_depth_slot_and_injury_body_part():
    import boto3
    from moto import mock_aws

    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=mod.PLAYERS_TABLE_NAME,
            KeySchema=[{"AttributeName": "playerId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "playerId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        players = {
            "9997": {
                "position": "WR",
                "team": "LAC",
                "depth_chart_order": 1,
                "depth_chart_position": "SWR",
                "injury_status": "Questionable",
                "injury_body_part": "Foot",
            },
            # Not a valued position, so it is never written.
            "9998": {"position": "OT", "team": "LAC", "depth_chart_position": "LT"},
        }

        assert mod._refresh_players(players, {}) == 1
        item = table.get_item(Key={"playerId": "9997"})["Item"]

    assert item["depth_chart_position"] == "SWR"
    assert item["injury_body_part"] == "Foot"
