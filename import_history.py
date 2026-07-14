#!/usr/bin/env python3
"""
One-time import: reads history.json into picklab.db.

Run once from ~/Desktop/golf-pool/:
    python3 import_history.py

Re-running is safe — already-imported tournaments are skipped.

Data reality:
  - Genesis (entry 0): no picks recorded, only final standings → tournament shell only.
  - Masters (entry 1): full picks per participant → imported completely.
  - U.S. Open: currently in progress, in picks.json not history.json → skipped here.
    Re-run this script after the U.S. Open is archived to import it.
"""

import json
import sqlite3
import os
import sys
from datetime import datetime

DATA_DIR   = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
PICKLAB_DB = os.path.join(DATA_DIR, 'picklab.db')
HISTORY_FILE = os.path.join(DATA_DIR, 'history.json')


def get_or_create_participant(conn, name):
    row = conn.execute(
        "SELECT id FROM participants WHERE LOWER(name) = LOWER(?)", (name,)
    ).fetchone()
    if row:
        return row[0]
    cur = conn.execute("INSERT INTO participants (name) VALUES (?)", (name,))
    return cur.lastrowid


def compute_tendency_scores(conn):
    """Inline tendency computation (mirrors picklab.compute_tendency_scores)."""
    rows = conn.execute(
        "SELECT tournament_id, participant_id, LOWER(TRIM(player_name)) AS pname FROM picks"
    ).fetchall()

    by_tourney = {}
    for r in rows:
        by_tourney.setdefault(r[0], []).append((r[1], r[2]))

    overlaps = {}
    for tid, picks in by_tourney.items():
        pick_counts = {}
        for _, pname in picks:
            pick_counts[pname] = pick_counts.get(pname, 0) + 1

        n_participants = len({pid for pid, _ in picks})
        max_overlap = max(n_participants - 1, 1)

        for pid, pname in picks:
            overlap = (pick_counts[pname] - 1) / max_overlap
            overlaps.setdefault(pid, []).append(overlap)

    now = datetime.utcnow().isoformat()
    conn.execute("DELETE FROM tendency_scores WHERE as_of_tournament_id IS NULL")

    print("\nTendency scores:")
    for pid, vals in overlaps.items():
        avg  = sum(vals) / len(vals)
        chalk = round(avg * 10, 1)
        name = conn.execute(
            "SELECT name FROM participants WHERE id=?", (pid,)
        ).fetchone()[0]
        print(f"  {name:<12} chalk={chalk}  contrarian={round(10 - chalk, 1)}")
        conn.execute(
            """INSERT INTO tendency_scores
               (participant_id, as_of_tournament_id, chalk_score, contrarian_score, computed_at)
               VALUES (?, NULL, ?, ?, ?)""",
            (pid, chalk, round(10 - chalk, 1), now)
        )

    conn.commit()


def main():
    if not os.path.exists(PICKLAB_DB):
        print(f"ERROR: {PICKLAB_DB} not found.")
        print("Start the server once first (it calls picklab.init_db() on startup).")
        sys.exit(1)

    with open(HISTORY_FILE) as f:
        history = json.load(f)

    conn = sqlite3.connect(PICKLAB_DB)
    conn.row_factory = sqlite3.Row

    for entry in history:
        name    = entry.get('tournament', 'Unknown')
        dates   = entry.get('dates', '')
        year    = entry.get('year', '')
        results = entry.get('results', [])

        # Skip if already imported
        existing = conn.execute(
            "SELECT id FROM tournaments WHERE name=?", (name,)
        ).fetchone()
        if existing:
            print(f"Skip (already imported): {name}")
            continue

        has_picks = any(r.get('picks') for r in results)

        cur = conn.execute(
            """INSERT INTO tournaments (name, pool_size, winner_participant, source_note)
               VALUES (?, ?, ?, ?)""",
            (
                name,
                len(results),
                next((r['name'] for r in results if r.get('place') == '1st'), None),
                f"Imported from history.json — {dates} {year}".strip(),
            )
        )
        tournament_id = cur.lastrowid
        conn.commit()
        print(f"\nImported: {name}  (id={tournament_id})")

        if not has_picks:
            print(f"  No picks in history.json for {name} — tournament shell created only.")
            continue

        for result in results:
            participant_name = result['name']
            picks            = result.get('picks', [])
            if not picks:
                continue

            pid = get_or_create_participant(conn, participant_name)
            for rank, player_name in enumerate(picks, start=1):
                conn.execute(
                    """INSERT INTO picks
                       (tournament_id, participant_id, player_name, pick_rank)
                       VALUES (?, ?, ?, ?)""",
                    (tournament_id, pid, player_name, rank)
                )
            conn.commit()
            print(f"  {participant_name}: {len(picks)} picks")

    print("\nComputing tendency scores...")
    compute_tendency_scores(conn)
    conn.close()
    print("\nDone. picklab.db is ready.")


if __name__ == '__main__':
    main()
