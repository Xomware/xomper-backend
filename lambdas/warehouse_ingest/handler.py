"""
Warehouse — Projections Ingest (scheduled)
==========================================
Runs nightly. Reads Sleeper's season projections and writes them to the
warehouse bucket as Parquet, then refreshes the slimmed player metadata
table that the frontend reads instead of downloading the ~5 MB
/players/nfl dump on every session.

Triggered by EventBridge cron at 08:00 UTC — after the US night, before
anyone opens the app. No API Gateway integration.

Why DuckDB rather than plain Python
-----------------------------------
The valuation is a dot product of a league's scoring_settings against
projected stats, then a per-position ranking. That is a window function,
not a hand-rolled sort. A spike (xomper-frontend/tools/duckdb-spike/)
established two things worth knowing here:

- The SQL port is byte-identical to the TypeScript engine the app ships:
  all 3,227 scored players match on points and value.
- DuckDB reads the Sleeper endpoint directly. `read_json_auto()` over the
  URL returned 3,302 rows in 597 ms, so there is no download-then-parse
  step in this handler at all.

Deliberately NOT done here: computing and storing a values grid. Values
are a function of each league's own scoring_settings and roster_positions,
which are arbitrary, and computing one league on demand measured ~10 ms.
Storing a cross product would be precomputing something cheaper to derive.
This job stores the *inputs*; the API computes per request.

Idempotency: the nightly write is a full replace of `projections/current`,
plus a dated snapshot. Re-invoking on the same day overwrites both with
identical content.
"""
import json
from datetime import datetime, timezone
from typing import Any

import boto3
import duckdb

from lambdas.common.constants import (
    PLAYERS_TABLE_NAME,
    WAREHOUSE_BUCKET_NAME,
)
from lambdas.common.errors import handle_errors
from lambdas.common.ffc_adp import fetch_all as fetch_adp
from lambdas.common.espn_crosswalk import (
    COVERAGE_FLOOR,
    build_crosswalk,
    fetch_espn_players,
    fetch_fantasycalc,
)
from lambdas.common.logger import get_logger
from lambdas.common.sleeper_helper import fetch_nfl_players, get_nfl_state
from lambdas.common.utility_helpers import success_response

HANDLER = "warehouse_ingest"
log = get_logger(HANDLER)

PROJECTIONS_URL = (
    "https://api.sleeper.com/projections/nfl/{season}"
    "?season_type=regular"
    "&position[]=QB&position[]=RB&position[]=WR&position[]=TE"
    "&position[]=K&position[]=DEF"
)

# Attribution, not a workaround. Sleeper rejects Python's DEFAULT urllib
# agent and returns nothing -- that bit the Phase 3 coverage measurement,
# where every redraft league silently scored 0% until a header was added.
# DuckDB's HTTP client sends its own agent and Sleeper accepts it, verified
# before this handler was written. This just identifies the caller.
#
# Note `custom_user_agent` APPENDS to DuckDB's agent rather than replacing
# it. The setting is not `http_useragent`, which does not exist.
USER_AGENT = "xomper-warehouse-ingest/1.0"

# The only writable path in a Lambda execution environment.
EPHEMERAL_DIR = "/tmp"

VALUED_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

# Fields the frontend actually reads.
#
# Sleeper's /players/nfl dump is 14.6 MB and the browser downloads all of it
# every session. This is the subset the app touches, counted from usage across
# the Angular source rather than guessed: position (68 references), status
# (53), team (49), first/last name (12 each), number, years_exp,
# injury_status, age. Plus the espn/yahoo ids, which nothing renders but which
# are the cross-platform crosswalk.
#
# Adding a field here is cheap; omitting one the UI reads is not. An earlier
# version of this list left out status, injury_status, age, years_exp and
# number, which would have blanked those in every player view.
PLAYER_FIELDS = (
    "first_name",
    "last_name",
    "position",
    "team",
    "status",
    "injury_status",
    "age",
    "years_exp",
    "number",
    "espn_id",
    "yahoo_id",
    "search_rank",
    # Added after a field-by-field diff of the Angular source against this
    # list found these read in the UI but never stored. The Sleeper fallback
    # in PlayerService only fires on a network error, so a missing field is
    # not a failure it can detect -- it just serves blanks.
    # Templates bind player.full_name directly rather than going through
    # PlayerModel.getFullName(), so omitting it renders a roster row as
    # "PIT * QB * 16" with no name at all.
    "full_name",
    "fantasy_positions",
    "height",
    "weight",
    "college",
    "depth_chart_order",
    "search_full_name",
    # CLT's NFL team window draws the depth chart as a formation: the slot
    # (LWR, RWR, SWR, ...) splits receivers that depth_chart_order ranks as one
    # list, and the body part labels an injury badge.
    "depth_chart_position",
    "injury_body_part",
)


def _connect() -> duckdb.DuckDBPyConnection:
    # custom_user_agent is connect-time only. `SET` after the database is
    # running raises "Cannot change custom_user_agent setting while database
    # is running", so it has to go in the config dict.
    con = duckdb.connect(config={
        "custom_user_agent": USER_AGENT,
        # Lambda has no writable HOME. Without these DuckDB fails on connect
        # with "IO Error: Can't find the home directory at ''" before a single
        # query runs. /tmp is the only writable path in the execution
        # environment, and it survives for the life of the container so a warm
        # invoke reuses the extensions rather than downloading them again.
        "home_directory": EPHEMERAL_DIR,
        "extension_directory": f"{EPHEMERAL_DIR}/duckdb_extensions",
    })
    con.execute("INSTALL json; LOAD json;")
    con.execute("INSTALL httpfs; LOAD httpfs;")

    # Reads the standard AWS chain, so in Lambda this is the execution role
    # with no keys anywhere in config. Verified writing and reading Parquet
    # against the real bucket before this handler was written.
    con.execute("CREATE SECRET (TYPE S3, PROVIDER CREDENTIAL_CHAIN);")
    return con


def _ingest_projections(con: duckdb.DuckDBPyConnection, season: str) -> int:
    """Read projections straight from the API and land them as Parquet."""
    url = PROJECTIONS_URL.format(season=season)
    bucket = WAREHOUSE_BUCKET_NAME

    positions = ", ".join(f"'{p}'" for p in VALUED_POSITIONS)
    con.execute(f"""
        CREATE OR REPLACE TABLE proj AS
        SELECT player_id,
               upper(coalesce(player.position, '')) AS position,
               stats
        FROM read_json_auto('{url}')
        WHERE upper(coalesce(player.position, '')) IN ({positions})
    """)

    total = con.execute("SELECT count(*) FROM proj").fetchone()[0]
    if total == 0:
        # Treated as a hard failure, not an empty day. A silent empty write
        # would leave every league unpriceable until someone noticed.
        raise RuntimeError(
            f"projections for {season} came back empty — refusing to write "
            "an empty warehouse"
        )

    # Long form: one row per (player, stat). This is the shape the scoring
    # dot product wants and what a columnar engine is fastest at.
    con.execute("""
        CREATE OR REPLACE TABLE stat_long AS
        SELECT p.player_id,
               p.position,
               k.key AS stat_key,
               TRY_CAST(json_extract_string(to_json(p.stats),
                        '$."' || k.key || '"') AS DOUBLE) AS stat_value
        FROM proj p,
             LATERAL unnest(json_keys(to_json(p.stats))) AS k(key)
    """)

    rows = con.execute("SELECT count(*) FROM stat_long").fetchone()[0]

    # Current, plus a dated snapshot so a Sleeper outage degrades to
    # stale-but-present rather than broken. The snapshot prefix is what the
    # bucket's 90-day lifecycle rule expires.
    today = con.execute("SELECT strftime(current_date, '%Y-%m-%d')").fetchone()[0]

    for key in (
        f"projections/current/season={season}/stats.parquet",
        f"snapshots/season={season}/dt={today}/stats.parquet",
    ):
        con.execute(f"""
            COPY (SELECT * FROM stat_long)
            TO 's3://{bucket}/{key}'
            (FORMAT PARQUET, OVERWRITE_OR_IGNORE true)
        """)

    log.info(f"projections: {total} players -> {rows} stat rows for {season}")
    return rows


def _espn_ids_by_sleeper_id(season: str, players: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Invert the ESPN crosswalk so it can be written onto Sleeper player rows.

    Raises rather than publishing a half-mapped table: a silent drop here would
    surface much later as an ESPN league whose board is missing players, with
    nothing pointing back at this job.
    """
    result = build_crosswalk(players, fetch_espn_players(season), fetch_fantasycalc())

    if result["misses"]:
        log.info(f"espn crosswalk unresolved: {result['misses'][:20]}")
    log.info(
        f"espn crosswalk: {result['coverage']:.4f} coverage, sources {result['sources']}"
    )
    if result["coverage"] < COVERAGE_FLOOR:
        raise RuntimeError(
            f"ESPN crosswalk coverage {result['coverage']:.4f} below floor "
            f"{COVERAGE_FLOOR}; {len(result['misses'])} players unresolved"
        )

    inverted: dict[str, dict[str, str]] = {}
    for espn_id, entry in result["mapping"].items():
        inverted[entry["sleeperId"]] = {"espn_id": espn_id, "source": entry["source"]}
    return inverted


def _write_adp(season: str) -> dict[str, Any]:
    """Snapshot FFC ADP beside the projections, current plus a dated copy.

    JSON rather than Parquet: it is a few thousand small rows served straight
    to the draft board, so the API can read it without DuckDB.
    """
    snapshot = fetch_adp(season)
    snapshot["capturedAt"] = datetime.now(timezone.utc).isoformat()

    body = json.dumps(snapshot).encode()
    today = snapshot["capturedAt"][:10]
    s3 = boto3.client("s3")
    for key in ("adp/current/adp.json", f"adp/snapshots/dt={today}/adp.json"):
        s3.put_object(
            Bucket=WAREHOUSE_BUCKET_NAME,
            Key=key,
            Body=body,
            ContentType="application/json",
        )

    if snapshot["failed"]:
        # Recorded, not raised: one dead format should not cost the night's
        # projections, but a silently stale format would be worse.
        log.warning(f"adp: formats failed {snapshot['failed']}")
    log.info(
        f"adp: {len(snapshot['formats'])} formats, "
        f"{sum(len(f['players']) for f in snapshot['formats'].values())} rows"
    )
    return snapshot


def _write_rankings(season: str, players: dict[str, Any], adp: dict[str, Any]) -> dict[str, Any]:
    """Consensus ranks from FFC ADP, FantasyCalc and ESPN, written as one blob.

    The point is not a better average. It is `spread`: a player three sources
    rank 107, 179 and 416 is a decision the drafter should see, and any single
    list hides that completely.

    A source that fails is recorded and skipped rather than raising. Two lists
    still beat one, and losing the night's projections because ESPN returned a
    502 would be a bad trade.
    """
    by_name: dict[tuple[str, str], str] = {}
    by_espn: dict[str, str] = {}
    for player_id, player in players.items():
        position = (player.get("position") or "").upper()
        if position in VALUED_POSITIONS:
            key = (norm_name(player.get("full_name") or player.get("last_name")), position)
            by_name.setdefault(key, player_id)
        if player.get("espn_id"):
            by_espn[str(player["espn_id"])] = player_id

    sources: dict[str, dict[str, int]] = {}
    failed: dict[str, str] = {}

    ppr = (adp.get("formats") or {}).get("ppr") or {}
    if ppr.get("players"):
        sources["ffc"] = adp_ranks(ppr["players"], by_name)

    for name, call in (
        ("fantasycalc", lambda: fantasycalc_ranks(fetch_fantasycalc(False, 1, 12, 1), by_name)),
        ("espn", lambda: espn_ranks(fetch_espn_ranks(season), by_espn, by_name)),
    ):
        try:
            ranks = call()
        except Exception as err:  # noqa: BLE001 - recorded below, not swallowed
            failed[name] = str(err)
            continue
        if ranks:
            sources[name] = ranks

    snapshot = {
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "season": season,
        "sources": sorted(sources),
        "failed": failed,
        "players": consensus(sources),
    }

    body = json.dumps(snapshot).encode()
    today = snapshot["capturedAt"][:10]
    s3 = boto3.client("s3")
    for key in ("rankings/current/rankings.json", f"rankings/snapshots/dt={today}/rankings.json"):
        s3.put_object(
            Bucket=WAREHOUSE_BUCKET_NAME, Key=key, Body=body, ContentType="application/json"
        )

    if failed:
        log.warning(f"rankings: sources failed {failed}")
    log.info(f"rankings: {len(sources)} sources, {len(snapshot['players'])} players")
    return snapshot


def _refresh_players(
    players: dict[str, Any], espn_by_sleeper: dict[str, dict[str, str]]
) -> int:
    """Slim the /players/nfl dump into DynamoDB for the frontend."""
    table = boto3.resource("dynamodb").Table(PLAYERS_TABLE_NAME)

    written = 0
    with table.batch_writer(overwrite_by_pkeys=["playerId"]) as batch:
        for player_id, player in players.items():
            position = (player.get("position") or "").upper()
            # Everything Sleeper knows about, including retired and practice
            # squad, is in this dump. Only positions we value are worth the
            # write cost.
            if position not in VALUED_POSITIONS:
                continue

            item = {"playerId": str(player_id)}
            for field in PLAYER_FIELDS:
                value = player.get(field)
                if value is None or value == "":
                    continue
                if isinstance(value, list):
                    # fantasy_positions. str() would store "['WR', 'FLEX']".
                    item[field] = [str(v) for v in value]
                elif isinstance(value, (int, float)):
                    item[field] = value
                else:
                    item[field] = str(value)
            # Sleeper's own espn_id covers 42% of ESPN's list. The crosswalk
            # fills the rest, and the source is stored so a consumer can tell a
            # published id from a name match.
            resolved = espn_by_sleeper.get(str(player_id))
            if resolved:
                item["espn_id"] = resolved["espn_id"]
                item["espn_id_source"] = resolved["source"]

            batch.put_item(Item=item)
            written += 1

    log.info(f"players: wrote {written} of {len(players)} to {PLAYERS_TABLE_NAME}")
    return written


@handle_errors(HANDLER)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log.info("Starting warehouse projections ingest...")

    # Season comes from Sleeper rather than the calendar. The two disagree
    # every January, and a wrong season silently ingests last year.
    state = get_nfl_state()
    season = str(state.get("season") or "")
    if not season:
        raise RuntimeError("could not determine current season from NFL state")

    con = _connect()
    stat_rows = _ingest_projections(con, season)
    # Fetched once and shared: the dump is 14.6 MB and both steps need it.
    players = fetch_nfl_players()
    espn_by_sleeper = _espn_ids_by_sleeper_id(season, players)
    players_written = _refresh_players(players, espn_by_sleeper)
    adp = _write_adp(season)
    rankings = _write_rankings(season, players, adp)

    log.info("Warehouse ingest complete.")
    return success_response(
        {
            "season": season,
            "statRows": stat_rows,
            "playersWritten": players_written,
            "espnCrosswalkSize": len(espn_by_sleeper),
            "adpFormats": sorted(adp["formats"]),
            "adpFailed": adp["failed"],
            "rankingSources": rankings["sources"],
            "rankingsFailed": rankings["failed"],
            "rankedPlayers": len(rankings["players"]),
        },
        is_api=False,
    )
