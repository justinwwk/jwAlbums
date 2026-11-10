# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Detection of the Photos.sqlite schema and the album<->asset join table.

The Core Data many-to-many table that links albums to assets is renamed between iOS
versions (Z_26ASSETS, Z_28ASSETS, Z_30ASSETS, ...).  It is discovered from the data:
the column whose values are overwhelmingly album primary keys paired with the column
whose values are overwhelmingly asset primary keys, cross-checked against the albums'
cached counts.  If no candidate is clearly best we refuse to guess.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from photos.models import KIND_USER_ALBUM

log = logging.getLogger(__name__)

SAMPLE_ROWS = 50_000
MIN_RATE = 0.98
MIN_MARGIN = 0.5

MSG_UNSUPPORTED = ("Album database format is different from the supported version.\n"
                   "A diagnostic file was created.")

ASSET_REQUIRED = {"Z_PK", "ZDIRECTORY", "ZFILENAME"}
ALBUM_REQUIRED = {"Z_PK", "ZTITLE", "ZKIND", "ZPARENTFOLDER"}


class SchemaUnsupportedError(Exception):
    def __init__(self, reason: str, analysis: list[dict] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.analysis = analysis or []
        self.diagnostic_path: Path | None = None


@dataclass
class JoinTable:
    table: str
    album_col: str
    asset_col: str
    score: float


@dataclass
class PhotosSchema:
    asset_table: str
    album_table: str
    asset_cols: set[str]
    album_cols: set[str]
    join: JoinTable | None            # None only when there are no user albums at all
    analysis: list[dict] = field(default_factory=list)


def _q(name: str) -> str:
    """Quote an identifier that came from sqlite_master."""
    return '"' + name.replace('"', '""') + '"'


def table_names(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({_q(table)})")]


def _pk_set(conn: sqlite3.Connection, table: str) -> set[int]:
    return {r[0] for r in conn.execute(f"SELECT Z_PK FROM {_q(table)}")}


def _candidate_tables(tables: list[str]) -> list[str]:
    pat = re.compile(r"^Z_\d+[A-Z0-9_]+$")
    return [t for t in tables if pat.match(t)]


def _analyze_table(conn: sqlite3.Connection, table: str, album_pks: set[int],
                   asset_pks: set[int]) -> dict:
    cols = columns(conn, table)
    rows = conn.execute(f"SELECT * FROM {_q(table)} LIMIT {SAMPLE_ROWS}").fetchall()
    stats: dict[str, dict] = {}
    for i, c in enumerate(cols):
        vals = [r[i] for r in rows if isinstance(r[i], int)]
        n = len(vals)
        stats[c] = {
            "n": n,
            "album_rate": (sum(v in album_pks for v in vals) / n) if n else 0.0,
            "asset_rate": (sum(v in asset_pks for v in vals) / n) if n else 0.0,
            "distinct": len(set(vals)),
        }
    return {"table": table, "columns": cols, "rows_sampled": len(rows), "stats": stats}


def _score_pairs(info: dict) -> list[dict]:
    out = []
    stats = info["stats"]
    for a, sa in stats.items():
        for b, sb in stats.items():
            if a == b or sa["n"] == 0 or sb["n"] == 0:
                continue
            # Z_FOK_* columns are Core Data ordering keys, never foreign keys; their values
            # (1024, 2048, ...) can coincide with real primary keys.
            if a.upper().startswith("Z_FOK_") or b.upper().startswith("Z_FOK_"):
                continue
            if sa["album_rate"] < MIN_RATE or sb["asset_rate"] < MIN_RATE:
                continue
            hint = (1 if "ALBUM" in a.upper() else 0) + (1 if "ASSET" in b.upper() else 0) \
                - (1 if "ASSET" in a.upper() else 0) - (1 if "ALBUM" in b.upper() else 0)
            discrim = sa["album_rate"] - sb["album_rate"]
            score = sa["album_rate"] + sb["asset_rate"] + discrim + 0.5 * hint
            if info["table"].upper().endswith("ASSETS"):
                score += 0.25
            out.append({"table": info["table"], "album_col": a, "asset_col": b, "score": score,
                        "name_hint": hint, "discrimination": discrim})
    return out


def _validate_pair(conn: sqlite3.Connection, pair: dict, album_table: str, album_cols: set[str]) -> float:
    """Fraction of user albums whose membership count agrees with ZCACHEDCOUNT (or is non-empty)."""
    t, a = _q(pair["table"]), _q(pair["album_col"])
    counts = dict(conn.execute(f"SELECT {a}, COUNT(*) FROM {t} GROUP BY {a}").fetchall())
    has_cached = "ZCACHEDCOUNT" in album_cols
    sel = "Z_PK, ZCACHEDCOUNT" if has_cached else "Z_PK, NULL"
    albums = conn.execute(f"SELECT {sel} FROM {_q(album_table)} WHERE ZKIND = ?", (KIND_USER_ALBUM,)).fetchall()
    checked = agree = 0
    for pk, cached in albums:
        got = counts.get(pk, 0)
        if has_cached and isinstance(cached, int):
            if cached == 0:
                continue
            checked += 1
            # Cached counts can exclude hidden/trashed items; allow some slack.
            if cached * 0.8 - 2 <= got <= cached * 1.25 + 2:
                agree += 1
        else:
            checked += 1
            agree += 1 if got > 0 else 0
    return (agree / checked) if checked else 1.0


def discover_join_table(conn: sqlite3.Connection, asset_table: str, album_table: str,
                        album_cols: set[str]) -> tuple[JoinTable | None, list[dict]]:
    album_pks = _pk_set(conn, album_table)
    asset_pks = _pk_set(conn, asset_table)
    analysis: list[dict] = []
    pairs: list[dict] = []
    for t in _candidate_tables(table_names(conn)):
        info = _analyze_table(conn, t, album_pks, asset_pks)
        info["pairs"] = _score_pairs(info)
        for p in info["pairs"]:
            p["validation"] = _validate_pair(conn, p, album_table, album_cols)
            p["score"] += p["validation"]
        analysis.append(info)
        pairs.extend(info["pairs"])
    pairs.sort(key=lambda p: p["score"], reverse=True)
    if not pairs:
        return None, analysis
    best = pairs[0]
    rivals = [p for p in pairs[1:] if p["table"] != best["table"]
              or {p["album_col"], p["asset_col"]} != {best["album_col"], best["asset_col"]}]
    strong = best["name_hint"] >= 1 or best["discrimination"] >= 0.2 or best["validation"] >= 0.8
    if not strong or best["validation"] < 0.5:
        return None, analysis
    if rivals and best["score"] - rivals[0]["score"] < MIN_MARGIN:
        return None, analysis
    return JoinTable(best["table"], best["album_col"], best["asset_col"], best["score"]), analysis


def detect_schema(conn: sqlite3.Connection) -> PhotosSchema:
    tables = set(table_names(conn))
    if "ZASSET" in tables:
        asset_table = "ZASSET"
    elif "ZGENERICASSET" in tables:
        asset_table = "ZGENERICASSET"
    else:
        raise SchemaUnsupportedError("no ZASSET or ZGENERICASSET table")
    if "ZGENERICALBUM" not in tables:
        raise SchemaUnsupportedError("no ZGENERICALBUM table")
    asset_cols = set(columns(conn, asset_table))
    album_cols = set(columns(conn, "ZGENERICALBUM"))
    missing = (ASSET_REQUIRED - asset_cols) | (ALBUM_REQUIRED - album_cols)
    if missing:
        raise SchemaUnsupportedError(f"missing columns: {sorted(missing)}")

    join, analysis = discover_join_table(conn, asset_table, "ZGENERICALBUM", album_cols)
    if join is None:
        n_user = conn.execute("SELECT COUNT(*) FROM ZGENERICALBUM WHERE ZKIND = ?", (KIND_USER_ALBUM,)).fetchone()[0]
        if n_user:
            raise SchemaUnsupportedError("could not confidently identify the album membership table", analysis)
        log.info("no user albums; membership table not required")
    else:
        log.info("album membership table: %s (%s -> %s), score %.2f",
                 join.table, join.album_col, join.asset_col, join.score)
    return PhotosSchema(asset_table, "ZGENERICALBUM", asset_cols, album_cols, join, analysis)


def write_diagnostic(conn: sqlite3.Connection | None, path: Path, ios_version: str, reason: str,
                     analysis: list[dict]) -> Path:
    """Write schema_diagnostic.txt: structure only, never photo metadata."""
    lines = [f"iOS version: {ios_version}", f"Reason: {reason}"]
    if conn is not None:
        try:
            lines.append(f"PRAGMA user_version: {conn.execute('PRAGMA user_version').fetchone()[0]}")
            lines.append("")
            lines.append("Tables and columns:")
            for t in table_names(conn):
                lines.append(f"  {t}: {', '.join(columns(conn, t))}")
        except sqlite3.Error as e:
            lines.append(f"(could not read schema: {e})")
    lines.append("")
    lines.append("Candidate relationship-table analysis:")
    for info in analysis:
        lines.append(f"  {info['table']} rows_sampled={info['rows_sampled']}")
        for c, s in info["stats"].items():
            lines.append(f"    {c}: n={s['n']} album_rate={s['album_rate']:.3f} "
                         f"asset_rate={s['asset_rate']:.3f} distinct={s['distinct']}")
        for p in info.get("pairs", []):
            lines.append(f"    pair {p['album_col']}->{p['asset_col']} score={p['score']:.3f} "
                         f"hint={p['name_hint']} discrim={p['discrimination']:.3f} "
                         f"validation={p.get('validation', 0):.3f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
