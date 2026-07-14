"""
Pick Lab — tendency analysis and pick advisor for Kapelke Golf Pool.
Imported by server.py; all routes are under /admin/picklab (auth-gated).
"""

import sqlite3
import os
import urllib.parse
from datetime import datetime

_DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
PICKLAB_DB = os.path.join(_DATA_DIR, 'picklab.db')

# Plain string — not an f-string — so { } are literal CSS braces.
PICKLAB_CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
:root {
    --c-bg:#0c1a0c; --c-bg2:#1a3320; --c-bg-dark:#0f2615; --c-bg3:#142a1a;
    --c-border:#2d5a38; --c-accent:#4a9e5c; --c-accent2:#5cb86e;
    --c-gold:#e8d44d; --c-text:#e8efe8; --c-muted:#7a9a7a; --c-tint:#8abf8a;
}
body { font-family:'Georgia','Times New Roman',serif; background:var(--c-bg); color:var(--c-text); padding:30px 20px; }
.container { max-width:820px; margin:0 auto; }
h1 { color:var(--c-gold); font-size:1.8em; margin-bottom:6px; }
h2 { color:var(--c-gold); font-size:1.1em; }
.back { color:var(--c-accent); font-size:0.9em; text-decoration:none; display:inline-block; }
.back:hover { text-decoration:underline; }
.nav-links { display:flex; gap:16px; margin-bottom:24px; flex-wrap:wrap; align-items:center; }
.card { background:var(--c-bg2); border:1px solid var(--c-border); border-radius:12px; padding:24px; margin-bottom:24px; }
.card h2 { margin-bottom:16px; padding-bottom:10px; border-bottom:2px solid var(--c-accent); }
.warn { background:#2a1a0a; border:1px solid #7a4a1a; border-radius:8px; padding:12px 16px; font-size:0.85em; color:#c8a06a; margin-bottom:20px; line-height:1.5; }
.info { background:#0a1a2a; border:1px solid #1a4a6a; border-radius:8px; padding:12px 16px; font-size:0.85em; color:#6ab0c8; margin-bottom:16px; line-height:1.5; }
.success { background:#0a2a0a; border:1px solid #2a6a2a; border-radius:8px; padding:12px 16px; font-size:0.85em; color:var(--c-tint); margin-bottom:20px; }
.err { background:#2a0a0a; border:1px solid #6a2a2a; border-radius:8px; padding:12px 16px; font-size:0.85em; color:#c86a6a; margin-bottom:20px; }
table { width:100%; border-collapse:collapse; }
th { padding:8px 10px; color:var(--c-tint); text-align:left; border-bottom:1px solid var(--c-border); font-size:0.82em; font-weight:normal; letter-spacing:0.04em; }
td { padding:8px 10px; border-bottom:1px solid #1a3020; font-size:0.9em; vertical-align:middle; }
tr:last-child td { border-bottom:none; }
label { display:block; font-size:0.88em; color:var(--c-tint); margin-bottom:5px; margin-top:14px; }
label:first-of-type { margin-top:0; }
input[type=text], input[type=number], select {
    width:100%; padding:9px 12px; background:var(--c-bg-dark); border:1px solid var(--c-border);
    border-radius:8px; color:var(--c-text); font-family:'Georgia',serif; font-size:0.95em;
}
input:focus, select:focus { outline:none; border-color:var(--c-accent); }
.btn { display:inline-block; padding:10px 22px; border:none; border-radius:8px;
       font-family:'Georgia',serif; font-size:0.95em; font-weight:700; cursor:pointer; }
.btn-green { background:var(--c-accent); color:var(--c-bg); margin-top:16px; }
.btn-green:hover { background:var(--c-accent2); }
.btn-red { background:#7a2020; color:var(--c-text); border:none; cursor:pointer;
           font-family:'Georgia',serif; border-radius:6px; padding:4px 10px; font-size:0.82em; }
.btn-red:hover { background:#9e3030; }
.tier-header td { background:var(--c-bg3); font-weight:700; font-size:0.8em; letter-spacing:0.08em; padding:7px 10px; }
"""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(PICKLAB_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tournaments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            course TEXT,
            tournament_type TEXT,
            start_date TEXT,
            end_date TEXT,
            pool_size INTEGER,
            winner_participant TEXT,
            source_note TEXT
        );
        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id INTEGER NOT NULL REFERENCES tournaments(id),
            participant_id INTEGER NOT NULL REFERENCES participants(id),
            player_name TEXT NOT NULL,
            pick_rank INTEGER,
            final_position TEXT,
            owgr_at_pick_time INTEGER
        );
        CREATE TABLE IF NOT EXISTS tendency_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_id INTEGER NOT NULL REFERENCES participants(id),
            as_of_tournament_id INTEGER REFERENCES tournaments(id),
            chalk_score REAL,
            contrarian_score REAL,
            notes TEXT,
            computed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS advisor_tournaments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            course TEXT,
            tournament_type TEXT,
            pool_size INTEGER,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS advisor_players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            advisor_tournament_id INTEGER NOT NULL REFERENCES advisor_tournaments(id),
            player_name TEXT NOT NULL,
            win_prob REAL,
            course_fit INTEGER,
            form_score INTEGER,
            pool_score REAL,
            tier TEXT
        );
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tendency score computation
# ---------------------------------------------------------------------------

def compute_tendency_scores():
    """
    For each pick a participant made, count how many OTHER participants in
    that same tournament also picked that player.  Average this normalised
    overlap across all picks → chalk_score (0–10).  contrarian = 10 – chalk.
    Replaces the "current" rolling scores (as_of_tournament_id IS NULL).
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT tournament_id, participant_id, LOWER(TRIM(player_name)) AS pname FROM picks"
        ).fetchall()

        # {tournament_id: [(participant_id, player_name), ...]}
        by_tourney = {}
        for r in rows:
            by_tourney.setdefault(r['tournament_id'], []).append(
                (r['participant_id'], r['pname'])
            )

        # participant_id -> list of normalised overlap values (0–1 each)
        overlaps = {}

        for tid, picks in by_tourney.items():
            # how many participants picked each player in this tournament
            pick_counts = {}
            for _, pname in picks:
                pick_counts[pname] = pick_counts.get(pname, 0) + 1

            n_participants = len({pid for pid, _ in picks})
            max_overlap = max(n_participants - 1, 1)

            for pid, pname in picks:
                overlap = (pick_counts[pname] - 1) / max_overlap  # 0–1
                overlaps.setdefault(pid, []).append(overlap)

        now = datetime.utcnow().isoformat()
        conn.execute("DELETE FROM tendency_scores WHERE as_of_tournament_id IS NULL")

        for pid, vals in overlaps.items():
            avg = sum(vals) / len(vals)
            chalk = round(avg * 10, 1)
            conn.execute(
                """INSERT INTO tendency_scores
                   (participant_id, as_of_tournament_id, chalk_score, contrarian_score, computed_at)
                   VALUES (?, NULL, ?, ?, ?)""",
                (pid, chalk, round(10 - chalk, 1), now)
            )

        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Advisor helpers
# ---------------------------------------------------------------------------

def _assign_tier(win_prob):
    if win_prob >= 10:
        return 'anchor'
    if win_prob >= 4:
        return 'value'
    if win_prob >= 2:
        return 'floor'
    return 'dart'


def _pool_score(win_prob, course_fit, form_score):
    return round(win_prob * 0.45 + course_fit * 0.30 + form_score * 0.25, 1)


def create_advisor_tournament(name, course, tournament_type, pool_size):
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO advisor_tournaments (name, course, tournament_type, pool_size, created_at) VALUES (?,?,?,?,?)",
            (name, course, tournament_type, pool_size, datetime.utcnow().isoformat())
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def add_advisor_player(tid, player_name, win_prob, course_fit, form_score):
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO advisor_players
               (advisor_tournament_id, player_name, win_prob, course_fit, form_score, pool_score, tier)
               VALUES (?,?,?,?,?,?,?)""",
            (tid, player_name, win_prob, course_fit, form_score,
             _pool_score(win_prob, course_fit, form_score),
             _assign_tier(win_prob))
        )
        conn.commit()
    finally:
        conn.close()


def delete_advisor_player(pid):
    conn = get_db()
    try:
        conn.execute("DELETE FROM advisor_players WHERE id=?", (pid,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tendency dashboard: /admin/picklab
# ---------------------------------------------------------------------------

def generate_picklab_html(participant=None):
    conn = get_db()
    try:
        tourney_rows = conn.execute("""
            SELECT t.id, t.name, t.course, t.start_date, t.winner_participant,
                   COUNT(DISTINCT p.participant_id) AS n_participants
            FROM tournaments t
            LEFT JOIN picks p ON p.tournament_id = t.id
            GROUP BY t.id
            ORDER BY t.id DESC
        """).fetchall()

        scores = conn.execute("""
            SELECT pt.name, ts.chalk_score, ts.contrarian_score, ts.notes, ts.computed_at
            FROM tendency_scores ts
            JOIN participants pt ON pt.id = ts.participant_id
            WHERE ts.as_of_tournament_id IS NULL
            ORDER BY ts.chalk_score DESC
        """).fetchall()

        n_with_picks = sum(1 for t in tourney_rows if t['n_participants'] > 0)

        data_warn = ''
        if n_with_picks < 3:
            data_warn = (
                f'<div class="warn">Limited data — {n_with_picks} tournament(s) with pick data '
                f'recorded. Tendency scores will become more reliable after 3+ tournaments.</div>'
            )

        # --- Tendency table ---
        if scores:
            score_rows = ''
            for s in scores:
                chalk = s['chalk_score']
                contra = s['contrarian_score']
                pct_chalk = int(chalk * 10)
                pct_contra = int(contra * 10)
                bar_chalk = (
                    f'<div style="height:5px;background:var(--c-accent);border-radius:3px;'
                    f'width:{pct_chalk}%;min-width:2px"></div>'
                )
                bar_contra = (
                    f'<div style="height:5px;background:#c8a06a;border-radius:3px;'
                    f'width:{pct_contra}%;min-width:2px"></div>'
                )
                link = (
                    f'<a href="/admin/picklab?participant={urllib.parse.quote(s["name"])}" '
                    f'style="color:var(--c-gold);text-decoration:none">{s["name"]}</a>'
                )
                score_rows += f"""
                <tr>
                  <td>{link}</td>
                  <td>
                    <div style="display:flex;align-items:center;gap:8px">
                      <span style="min-width:28px;color:var(--c-accent)">{chalk}</span>
                      <div style="flex:1">{bar_chalk}</div>
                    </div>
                  </td>
                  <td>
                    <div style="display:flex;align-items:center;gap:8px">
                      <span style="min-width:28px;color:#c8a06a">{contra}</span>
                      <div style="flex:1">{bar_contra}</div>
                    </div>
                  </td>
                  <td style="color:var(--c-tint);font-size:0.82em">{s['notes'] or '—'}</td>
                </tr>"""
            recompute_btn = (
                '<div style="margin-top:14px">'
                '<form method="POST" action="/admin/picklab/recompute" style="display:inline">'
                '<button type="submit" class="btn btn-green" '
                'style="padding:6px 16px;font-size:0.82em;margin-top:0">Recompute Scores</button>'
                '</form></div>'
            )
            scores_html = f"""
            <table>
              <thead><tr>
                <th>Participant</th>
                <th>Chalk Score (/10)</th>
                <th>Contrarian (/10)</th>
                <th>Notes</th>
              </tr></thead>
              <tbody>{score_rows}</tbody>
            </table>
            {recompute_btn}"""
        else:
            scores_html = (
                '<div style="color:var(--c-tint);font-size:0.9em">'
                'No tendency data yet. Run <code>python3 import_history.py</code> '
                'to import historical picks.</div>'
            )

        # --- Tournament history table ---
        hist_rows = ''
        for t in tourney_rows:
            n = t['n_participants']
            field_cell = f'{n} participants' if n else '<span style="color:var(--c-tint)">no pick data</span>'
            winner_cell = t['winner_participant'] or '<span style="color:var(--c-tint)">—</span>'
            hist_rows += f"""
            <tr>
              <td style="color:var(--c-gold)">{t['name']}</td>
              <td style="color:var(--c-tint);font-size:0.88em">{t['course'] or '—'}</td>
              <td>{field_cell}</td>
              <td>{winner_cell}</td>
            </tr>"""
        if not hist_rows:
            hist_rows = '<tr><td colspan="4" style="color:var(--c-tint)">No tournaments imported yet.</td></tr>'
        history_html = f"""
        <table>
          <thead><tr>
            <th>Tournament</th><th>Course</th><th>Field</th><th>Pool Winner</th>
          </tr></thead>
          <tbody>{hist_rows}</tbody>
        </table>"""

        # --- Participant drill-down ---
        drilldown_html = ''
        if participant:
            pick_rows = conn.execute("""
                SELECT pk.player_name, pk.pick_rank, pk.owgr_at_pick_time,
                       t.name AS tournament_name
                FROM picks pk
                JOIN participants pt ON pt.id = pk.participant_id
                JOIN tournaments t ON t.id = pk.tournament_id
                WHERE LOWER(pt.name) = LOWER(?)
                ORDER BY t.id DESC, pk.pick_rank
            """, (participant,)).fetchall()

            if pick_rows:
                by_tourney = {}
                for r in pick_rows:
                    by_tourney.setdefault(r['tournament_name'], []).append(r)

                drill_content = ''
                for tname, tpicks in by_tourney.items():
                    inner = ''
                    for pk in tpicks:
                        owgr = f"#{pk['owgr_at_pick_time']}" if pk['owgr_at_pick_time'] else '—'
                        inner += f'<tr><td>{pk["player_name"]}</td><td style="color:var(--c-tint)">{owgr}</td></tr>'
                    drill_content += f"""
                    <div style="margin-bottom:20px">
                      <div style="color:var(--c-gold);font-weight:700;margin-bottom:8px">{tname}</div>
                      <table>
                        <thead><tr><th>Player picked</th><th>OWGR at pick time</th></tr></thead>
                        <tbody>{inner}</tbody>
                      </table>
                    </div>"""
                drilldown_html = f"""
                <div class="card">
                  <h2>{participant} — Pick History</h2>
                  <a href="/admin/picklab" style="font-size:0.85em;color:var(--c-accent);text-decoration:none">← All participants</a>
                  <div style="margin-top:16px">{drill_content}</div>
                </div>"""
            else:
                drilldown_html = (
                    f'<div class="card"><h2>{participant}</h2>'
                    f'<div style="color:var(--c-tint)">No pick data found for this participant.</div></div>'
                )

        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pick Lab — Kapelke Golf Pool</title>
<style>{PICKLAB_CSS}</style>
</head>
<body>
<div class="container">
  <h1>Pick Lab</h1>
  <div class="nav-links">
    <a href="/admin" class="back">← Admin</a>
    <a href="/admin/picklab/advisor" class="back">Pick Advisor →</a>
  </div>
  {data_warn}
  <div class="card">
    <h2>Chalk Tendencies</h2>
    <div style="font-size:0.82em;color:var(--c-tint);margin-bottom:14px">
      Higher chalk score = participant picks more popular/overlapping players.
      Click a name to see their full pick history.
    </div>
    {scores_html}
  </div>
  <div class="card">
    <h2>Tournament History</h2>
    {history_html}
  </div>
  {drilldown_html}
</div>
</body>
</html>"""
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pick Advisor: /admin/picklab/advisor
# ---------------------------------------------------------------------------

def generate_picklab_advisor_html(tid=None, msg='', error=False):
    conn = get_db()
    try:
        advisor_tourneys = conn.execute(
            "SELECT * FROM advisor_tournaments ORDER BY id DESC"
        ).fetchall()

        msg_html = ''
        if msg:
            css_class = 'err' if error else 'success'
            msg_html = f'<div class="{css_class}">{msg}</div>'

        # Sidebar: existing sessions + create-new form
        session_links = ''
        for at in advisor_tourneys:
            is_active = tid and int(tid) == at['id']
            active_style = (
                'background:var(--c-accent);color:var(--c-bg);border-color:var(--c-accent)'
                if is_active else ''
            )
            session_links += (
                f'<a href="/admin/picklab/advisor?tid={at["id"]}" '
                f'style="display:block;padding:10px 14px;border-radius:8px;color:var(--c-text);'
                f'text-decoration:none;margin-bottom:6px;border:1px solid var(--c-border);{active_style}">'
                f'{at["name"]}'
                f'<span style="font-size:0.78em;color:{"var(--c-bg)" if is_active else "var(--c-tint)"};'
                f'display:block;margin-top:2px">{at["course"] or "—"}</span>'
                f'</a>'
            )

        sessions_card = ''
        if session_links:
            sessions_card = f'<div class="card"><h2>Sessions</h2>{session_links}</div>'

        create_card = f"""
        <div class="card">
          <h2>New Session</h2>
          <form method="POST" action="/admin/picklab/advisor/create-tournament">
            <label>Tournament Name</label>
            <input type="text" name="name" placeholder="e.g. The Open Championship" required>
            <label>Course</label>
            <input type="text" name="course" placeholder="e.g. Royal Portrush">
            <label>Type</label>
            <select name="tournament_type">
              <option value="major">Major</option>
              <option value="signature">Signature Event</option>
              <option value="other">Other</option>
            </select>
            <label>Pool size (# of participants)</label>
            <input type="number" name="pool_size" value="8" min="2" max="50">
            <button type="submit" class="btn btn-green" style="padding:8px 18px;font-size:0.9em">Create</button>
          </form>
        </div>"""

        sidebar = f'<div style="flex:0 0 210px;min-width:0">{sessions_card}{create_card}</div>'

        # Main content
        main_content = '<div class="card"><div style="color:var(--c-tint)">Select a session on the left, or create a new one to start building your lineup.</div></div>'

        if tid:
            at_row = conn.execute(
                "SELECT * FROM advisor_tournaments WHERE id=?", (tid,)
            ).fetchone()

            if at_row:
                players = conn.execute(
                    "SELECT * FROM advisor_players WHERE advisor_tournament_id=? ORDER BY pool_score DESC",
                    (tid,)
                ).fetchall()

                # Chalk context from tendency scores
                avg_row = conn.execute(
                    "SELECT AVG(chalk_score) AS avg FROM tendency_scores WHERE as_of_tournament_id IS NULL"
                ).fetchone()
                chalk_context = ''
                if avg_row['avg'] is not None:
                    avg = avg_row['avg']
                    if avg >= 6:
                        label = 'very chalky'
                        note = 'Unique picks carry significant value here — the field heavily overlaps on favorites.'
                    elif avg >= 4:
                        label = 'moderately chalky'
                        note = 'Some overlap on top picks. Unique mid-tier selections have decent equity.'
                    else:
                        label = 'contrarian'
                        note = 'The pool spreads picks widely. Uniqueness matters less; pick quality is the main edge.'
                    chalk_context = (
                        f'<div class="info">Pool chalk average: <strong>{avg:.1f}/10</strong> — '
                        f'this pool is {label}. {note}</div>'
                    )

                # Group players by tier
                TIER_ORDER = ('anchor', 'value', 'floor', 'dart')
                TIER_LABELS = {
                    'anchor': ('Anchor', '#e8d44d'),
                    'value':  ('Value',  '#4a9e5c'),
                    'floor':  ('Floor',  '#4a7a9e'),
                    'dart':   ('Dart',   '#9e7a4a'),
                }
                tiers = {t: [] for t in TIER_ORDER}
                for p in players:
                    tiers.get(p['tier'] or 'dart', tiers['dart']).append(p)

                player_rows = ''
                for tier_key in TIER_ORDER:
                    tier_players = tiers[tier_key]
                    if not tier_players:
                        continue
                    label, color = TIER_LABELS[tier_key]
                    player_rows += (
                        f'<tr class="tier-header">'
                        f'<td colspan="6" style="color:{color}">{label.upper()}</td>'
                        f'</tr>'
                    )
                    for p in tier_players:
                        player_rows += f"""<tr>
                          <td style="color:var(--c-text);font-weight:500">{p['player_name']}</td>
                          <td style="text-align:center;color:var(--c-gold)">{p['win_prob']:.1f}%</td>
                          <td style="text-align:center">{p['course_fit']}</td>
                          <td style="text-align:center">{p['form_score']}</td>
                          <td style="text-align:center;color:var(--c-accent);font-weight:700">{p['pool_score']:.1f}</td>
                          <td style="text-align:center">
                            <form method="POST" action="/admin/picklab/advisor/delete-player" style="display:inline">
                              <input type="hidden" name="pid" value="{p['id']}">
                              <input type="hidden" name="tid" value="{tid}">
                              <button type="submit" class="btn-red" onclick="return confirm('Remove {p["player_name"]}?')">✕</button>
                            </form>
                          </td>
                        </tr>"""

                empty_row = (
                    '<tr><td colspan="6" style="color:var(--c-tint);text-align:center;padding:20px">'
                    'No players added yet. Use the form below to build your field.</td></tr>'
                )

                add_form = f"""
                <div style="margin-top:20px;padding-top:20px;border-top:1px solid var(--c-border)">
                  <div style="font-size:0.88em;color:var(--c-tint);margin-bottom:12px">Add player</div>
                  <form method="POST" action="/admin/picklab/advisor/add-player">
                    <input type="hidden" name="tid" value="{tid}">
                    <div style="display:grid;grid-template-columns:2fr 1fr 1fr 1fr;gap:10px;align-items:end">
                      <div>
                        <label style="margin-top:0">Player Name</label>
                        <input type="text" name="player_name" placeholder="e.g. Scottie Scheffler" required>
                      </div>
                      <div>
                        <label style="margin-top:0">Win % <span style="opacity:0.6;font-size:0.85em">(0–100)</span></label>
                        <input type="number" name="win_prob" step="0.1" min="0" max="100" placeholder="e.g. 18.5" required>
                      </div>
                      <div>
                        <label style="margin-top:0">Course Fit <span style="opacity:0.6;font-size:0.85em">(1–100)</span></label>
                        <input type="number" name="course_fit" min="1" max="100" placeholder="e.g. 80" required>
                      </div>
                      <div>
                        <label style="margin-top:0">Form <span style="opacity:0.6;font-size:0.85em">(1–100)</span></label>
                        <input type="number" name="form_score" min="1" max="100" placeholder="e.g. 75" required>
                      </div>
                    </div>
                    <button type="submit" class="btn btn-green" style="padding:8px 18px;font-size:0.88em;margin-top:12px">Add Player</button>
                  </form>
                </div>"""

                main_content = f"""
                <div class="card">
                  <h2>{at_row['name']}{' — ' + at_row['course'] if at_row['course'] else ''}</h2>
                  <div style="font-size:0.80em;color:var(--c-tint);margin-bottom:14px;line-height:1.6">
                    Pool score = Win% × 0.45 + Course Fit × 0.30 + Form × 0.25 &nbsp;·&nbsp;
                    Tiers: Anchor ≥10% · Value 4–10% · Floor 2–4% · Dart &lt;2%<br>
                    Win% tip: +350 odds ≈ 22%, +600 ≈ 14%, +1000 ≈ 9%, +2000 ≈ 5%, +5000 ≈ 2%
                  </div>
                  {chalk_context}
                  <table>
                    <thead><tr>
                      <th>Player</th>
                      <th style="text-align:center">Win %</th>
                      <th style="text-align:center">Course Fit</th>
                      <th style="text-align:center">Form</th>
                      <th style="text-align:center">Pool Score</th>
                      <th></th>
                    </tr></thead>
                    <tbody>{player_rows if player_rows else empty_row}</tbody>
                  </table>
                  {add_form}
                </div>"""

        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pick Advisor — Kapelke Golf Pool</title>
<style>
{PICKLAB_CSS}
.layout {{ display:flex; gap:24px; align-items:flex-start; }}
@media(max-width:680px) {{ .layout {{ flex-direction:column; }} .layout > div:first-child {{ width:100% !important; flex:none !important; }} }}
</style>
</head>
<body>
<div class="container" style="max-width:1000px">
  <h1>Pick Advisor</h1>
  <div class="nav-links">
    <a href="/admin" class="back">← Admin</a>
    <a href="/admin/picklab" class="back">Tendency Dashboard</a>
  </div>
  {msg_html}
  <div class="layout">
    {sidebar}
    <div style="flex:1;min-width:0">{main_content}</div>
  </div>
</div>
</body>
</html>"""
    finally:
        conn.close()
