#!/usr/bin/env python3
"""
Kapelke Golf Pool Dashboard
"""

import json
import io
import os
import urllib.request
import urllib.parse
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import webbrowser

PICKS_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'picks.json')
TOURNAMENT_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tournament.json')
HISTORY_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'history.json')

_DEFAULT_TOURNAMENT = {
    'name': 'Kapelke Golf Pool',
    'dates': '',
    'course': '',
    'pga_tour_id': '',
    'entry_fee': 25,
    'admin_password': 'golf',
}

def load_tournament():
    try:
        with open(TOURNAMENT_FILE) as f:
            cfg = json.load(f)
            for k, v in _DEFAULT_TOURNAMENT.items():
                cfg.setdefault(k, v)
            return cfg
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULT_TOURNAMENT)

def save_tournament(cfg):
    with open(TOURNAMENT_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

def load_history():
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_history(data):
    with open(HISTORY_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def _all_historical_names():
    """Return sorted list of all unique participant names from history."""
    names = set()
    for t in load_history():
        for r in t.get('results', []):
            names.add(r['name'])
    return sorted(names)

def career_standings():
    """Aggregate history.json into per-person career totals."""
    totals = {}
    for t in load_history():
        for r in t.get('results', []):
            n = r['name']
            if n not in totals:
                totals[n] = {'name': n, 'tournaments': 0, 'wins': 0, 'seconds': 0, 'winnings': 0}
            totals[n]['tournaments'] += 1
            totals[n]['winnings']    += r.get('prize', 0)
            if r['place'] == '1st': totals[n]['wins']    += 1
            if r['place'] == '2nd': totals[n]['seconds'] += 1
    return sorted(totals.values(), key=lambda x: -x['winnings'])
PGA_TOUR_API_URL  = 'https://orchestrator.pgatour.com/graphql'
PGA_TOUR_API_KEY  = 'da2-gsrx5bibzbb4njvhl7t37wqyl4'
ESPN_SCOREBOARD_URL = 'https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard'
OWGR_URL = 'https://apiweb.owgr.com/api/owgr/rankings/getRankings?pageSize=300&pageNumber=1'

# Cache for the PGA Tour player names (for autocomplete before tournament starts)
_player_names_cache = []
# Cache for OWGR rankings: name (lowercase) -> rank number
_owgr_cache = {}


def is_locked():
    """Returns True if entries have been manually locked."""
    data = load_picks()
    return data.get('locked', False)


def fetch_owgr():
    """Fetch Official World Golf Rankings. Returns dict of lowercase name -> rank."""
    global _owgr_cache
    if _owgr_cache:
        return _owgr_cache
    try:
        req = urllib.request.Request(OWGR_URL, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode('utf-8'))
        for r in data.get('rankingsList', []):
            name = r['player']['fullName']
            _owgr_cache[name.lower()] = r['rank']
        print(f"  Cached {len(_owgr_cache)} OWGR rankings")
    except Exception as e:
        print(f"  Could not fetch OWGR: {e}")
    return _owgr_cache


def get_owgr_rank(name):
    """Get a golfer's world ranking. Returns (rank, True) or (999, False) if not found."""
    rankings = fetch_owgr()
    key = name.lower().strip()
    if key in rankings:
        return rankings[key], True
    # Fuzzy match
    for rname, rank in rankings.items():
        if key in rname or rname in key:
            return rank, True
    return 999, False

# --- Data functions ---

def load_picks():
    try:
        with open(PICKS_FILE, 'r') as f:
            data = json.load(f)
            data.setdefault('locked', False)
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"entry_fee": load_tournament()['entry_fee'], "locked": False, "participants": []}


def save_picks(data):
    with open(PICKS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def fetch_player_names():
    """Fetch PGA Tour player names from recent events for autocomplete."""
    global _player_names_cache
    if _player_names_cache:
        return _player_names_cache
    all_names = set()
    # Pull from recent 2026 and late 2025 events for broad coverage
    urls = [
        f'{ESPN_SCOREBOARD_URL}?dates=20260101-20260301&limit=10',
        f'{ESPN_SCOREBOARD_URL}?dates=20250901-20251231&limit=10',
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            response = urllib.request.urlopen(req, timeout=10)
            data = json.loads(response.read().decode('utf-8'))
            for event in data.get('events', []):
                for c in event.get('competitions', [{}])[0].get('competitors', []):
                    name = c.get('athlete', {}).get('displayName', '')
                    if name:
                        all_names.add(name)
        except Exception as e:
            print(f"  Warning fetching player names: {e}")
    if all_names:
        _player_names_cache = sorted(all_names)
        print(f"  Cached {len(_player_names_cache)} PGA Tour player names for autocomplete")
    return _player_names_cache


def fetch_leaderboard():
    """Fetch live leaderboard from PGA Tour GraphQL API."""
    import gzip as _gzip, base64 as _base64
    cfg = load_tournament()
    query = '{ leaderboardCompressedV2(id: "' + cfg['pga_tour_id'] + '") { id payload } }'
    body = json.dumps({'query': query}).encode('utf-8')
    try:
        req = urllib.request.Request(
            PGA_TOUR_API_URL,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'x-api-key': PGA_TOUR_API_KEY,
                'User-Agent': 'Mozilla/5.0',
            }
        )
        response = urllib.request.urlopen(req, timeout=10)
        resp_data = json.loads(response.read().decode('utf-8'))
        payload = resp_data['data']['leaderboardCompressedV2']['payload']
        data = json.loads(_gzip.decompress(_base64.b64decode(payload)))

        courses = [c.get('courseName', '') for c in data.get('courses', [])]
        course = courses[0] if courses else cfg['course']
        status = data.get('roundStatusDisplay', data.get('roundStatus', 'Scheduled'))
        current_round = 1

        tournament = {
            'name': cfg['name'],
            'date': '',
            'status': status,
            'course': course,
        }

        players = []
        cut_indicators = {'cut', 'mc', 'wd', 'dq', 'mdf', 'dns', 'dqf'}
        for p in data.get('players', []):
            player_info = p.get('player', {})
            name = f"{player_info.get('firstName') or ''} {player_info.get('lastName') or ''}".strip()
            if not name:
                continue

            position_str = str(p.get('position', '999') or '999')
            player_status = str(p.get('status', '') or '').lower()
            is_cut = position_str.lower() in cut_indicators or player_status in cut_indicators

            if not is_cut:
                try:
                    position = int(position_str.replace('T', '').strip())
                    if position <= 0:
                        position = 999
                except (ValueError, AttributeError, TypeError):
                    position = 999
                if position == 999:
                    continue

            rnd = p.get('currentRound', 1)
            current_round = max(current_round, rnd)
            thru = p.get('thru', '-') or '-'
            total = p.get('total', 'E') or 'E'
            today = p.get('score', '-') or '-'
            rounds = p.get('rounds', [])

            players.append({
                'name': name,
                'position': 999 if is_cut else position,
                'score': total,
                'today': today,
                'thru': thru,
                'linescores': rounds,
                'cut': is_cut,
            })

        tournament['current_round'] = current_round
        players.sort(key=lambda p: (1 if p.get('cut') else 0, p['position']))
        return tournament, players

    except Exception as e:
        print(f"PGA Tour API error: {e}")
        cfg = load_tournament()
        return {
            'name': cfg['name'],
            'date': '',
            'status': 'Unable to fetch live data',
            'course': cfg['course'],
            'current_round': 1,
        }, []


def calculate_standings(participants, players):
    """Calculate pool standings using tiebreaker rules."""
    if not participants or not players:
        return []

    # Build lookup: lowercase player name -> position and cut status
    pos_lookup = {}
    cut_lookup = {}
    for p in players:
        key = p['name'].lower()
        pos_lookup[key] = p['position']
        cut_lookup[key] = p.get('cut', False)
        parts = p['name'].lower().split()
        if len(parts) >= 2:
            pos_lookup[parts[-1]] = pos_lookup.get(parts[-1], p['position'])
            cut_lookup[parts[-1]] = cut_lookup.get(parts[-1], p.get('cut', False))

    def get_position(pick_name):
        name = pick_name.lower().strip()
        if name in pos_lookup:
            return pos_lookup[name]
        for pname, pos in pos_lookup.items():
            if name in pname or pname in name:
                return pos
        return 999

    def is_cut(pick_name):
        name = pick_name.lower().strip()
        if name in cut_lookup:
            return cut_lookup[name]
        for pname, val in cut_lookup.items():
            if name in pname or pname in name:
                return val
        return False

    # Count how many times each golfer was picked
    pick_counts = {}
    for participant in participants:
        for pick in participant['picks']:
            key = pick.lower().strip()
            pick_counts[key] = pick_counts.get(key, 0) + 1

    standings = []
    for participant in participants:
        picks_with_pos = []
        for pick in participant['picks']:
            pos = get_position(pick)
            unique = pick_counts.get(pick.lower().strip(), 0) == 1
            picks_with_pos.append({
                'name': pick,
                'position': pos,
                'unique': unique,
                'cut': is_cut(pick),
            })
        # Sort by position (best first)
        picks_with_pos.sort(key=lambda x: x['position'])
        standings.append({
            'name': participant['name'],
            'picks': picks_with_pos,
            'sort_key': [p['position'] for p in picks_with_pos],
        })

    # Sort standings: best pick wins, cascade to next best on ties
    standings.sort(key=lambda s: s['sort_key'])

    # Assign prizes
    entry_fee = load_tournament()['entry_fee']
    total_pot = len(participants) * entry_fee
    for i, s in enumerate(standings):
        if i == 0:
            s['prize'] = total_pot - entry_fee if len(participants) > 1 else total_pot
            s['place'] = '1st'
        elif i == 1:
            s['prize'] = entry_fee
            s['place'] = '2nd'
        else:
            s['prize'] = 0
            s['place'] = f'{i + 1}th'

    return standings


# --- HTML generation ---

STYLES = """
* { margin: 0; padding: 0; box-sizing: border-box; }

body {
    font-family: 'Georgia', 'Times New Roman', serif;
    background-color: #0c1a0c;
    color: #e8efe8;
    padding: 20px;
}

.container { max-width: 1400px; margin: 0 auto; }

.header {
    background: linear-gradient(135deg, #1a3320 0%, #0f2615 100%);
    border: 2px solid #4a9e5c;
    border-radius: 12px;
    padding: 30px;
    margin-bottom: 30px;
}

.header-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 15px;
}

h1 {
    font-size: 2.2em;
    color: #e8d44d;
    margin-bottom: 5px;
    letter-spacing: 1px;
}

.subtitle {
    color: #8abf8a;
    font-size: 1.1em;
    font-style: italic;
}

.updated {
    color: #7a9a7a;
    font-size: 0.85em;
    margin-top: 8px;
}

.refresh-area {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 8px;
}

.refresh-btn {
    background: #4a9e5c;
    color: #0c1a0c;
    border: none;
    border-radius: 8px;
    padding: 12px 24px;
    font-size: 1em;
    font-weight: 700;
    font-family: 'Georgia', serif;
    cursor: pointer;
    transition: background 0.3s, transform 0.1s;
    display: flex;
    align-items: center;
    gap: 8px;
}

.refresh-btn:hover { background: #5cb86e; }
.refresh-btn:active { transform: scale(0.96); }
.refresh-btn.spinning .refresh-icon { animation: spin 0.8s linear infinite; }
.refresh-icon { display: inline-block; font-size: 1.1em; }
@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

.countdown {
    color: #7a9a7a;
    font-size: 0.8em;
    font-style: italic;
}

.grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 20px;
    margin-bottom: 30px;
}

.card {
    background: #1a3320;
    border: 1px solid #2d5a38;
    border-radius: 12px;
    padding: 25px;
    transition: border-color 0.3s;
}

.card:hover { border-color: #4a9e5c; }

.card h2 {
    color: #e8d44d;
    font-size: 1.15em;
    margin-bottom: 18px;
    padding-bottom: 10px;
    border-bottom: 2px solid #4a9e5c;
    letter-spacing: 0.5px;
}

.full-width { grid-column: 1 / -1; }

.status-card {
    background: linear-gradient(135deg, #14291a, #0c1a0c);
    border: 2px solid #4a9e5c;
}

.status-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 15px;
}

.status-item {
    text-align: center;
}

.status-label {
    color: #7a9a7a;
    font-size: 0.8em;
    text-transform: uppercase;
    letter-spacing: 1px;
}

.status-value {
    font-size: 1.4em;
    font-weight: bold;
    color: #e8efe8;
    margin-top: 5px;
}

/* Standings table */
.standings-table {
    width: 100%;
    border-collapse: collapse;
}

.standings-table th {
    text-align: left;
    color: #e8d44d;
    font-size: 0.8em;
    text-transform: uppercase;
    letter-spacing: 1px;
    padding: 10px 12px;
    border-bottom: 2px solid #4a9e5c;
}

.standings-table td {
    padding: 12px;
    border-bottom: 1px solid #2d5a38;
    color: #e8efe8;
}

.standings-table tr:hover { background: rgba(74, 158, 92, 0.1); }

.place-1 { color: #e8d44d; font-weight: bold; }
.place-2 { color: #c0c0c0; font-weight: bold; }

.prize { color: #e8d44d; font-weight: bold; }
.pick-unique { color: #7ed87e; }
.pick-shared { color: #7a9a7a; }
.pick-pos { font-size: 0.85em; color: #7a9a7a; margin-left: 4px; }

/* Leaderboard */
.lb-table {
    width: 100%;
    border-collapse: collapse;
}

.lb-table th {
    text-align: left;
    color: #e8d44d;
    font-size: 0.8em;
    text-transform: uppercase;
    letter-spacing: 1px;
    padding: 8px 12px;
    border-bottom: 2px solid #4a9e5c;
}

.lb-table td {
    padding: 8px 12px;
    border-bottom: 1px solid #2d5a38;
    color: #e8efe8;
    font-size: 0.95em;
}

.lb-table tr:hover { background: rgba(74, 158, 92, 0.1); }
.lb-table tr.picked { background: rgba(232, 212, 77, 0.08); border-left: 3px solid #e8d44d; }

.score-under { color: #f25c5c; font-weight: bold; }
.score-over { color: #7a9a7a; font-weight: bold; }
.score-even { color: #e8efe8; font-weight: bold; }

.picked-badge {
    background: rgba(232, 212, 77, 0.2);
    color: #e8d44d;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.75em;
    margin-left: 8px;
}

.lb-table tr.picked-cut { opacity: 0.5; }

.cut-badge {
    background: #5a2020;
    color: #e8efe8;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.8em;
    font-weight: bold;
}

/* Participants grid */
.participants-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 15px;
}

.participant-card {
    background: #142a1a;
    border: 1px solid #2d5a38;
    border-radius: 8px;
    padding: 18px;
    transition: border-color 0.3s;
}

.participant-card:hover { border-color: #4a9e5c; }

.participant-name {
    font-size: 1.2em;
    font-weight: bold;
    color: #e8d44d;
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid #2d5a38;
}

.pick-item {
    display: flex;
    justify-content: space-between;
    padding: 5px 0;
    font-size: 0.9em;
}

.pick-golfer { color: #e8efe8; }

.entry-link {
    display: inline-block;
    background: #4a9e5c;
    color: #0c1a0c;
    padding: 10px 20px;
    border-radius: 8px;
    text-decoration: none;
    font-weight: bold;
    font-family: 'Georgia', serif;
    transition: background 0.3s;
    margin-top: 15px;
}

.entry-link:hover { background: #5cb86e; }

.no-data {
    color: #7a9a7a;
    font-style: italic;
    text-align: center;
    padding: 30px;
}

/* Entry form styles */
.form-container {
    max-width: 600px;
    margin: 0 auto;
}

.form-group {
    margin-bottom: 18px;
}

.form-group label {
    display: block;
    color: #e8d44d;
    font-size: 0.9em;
    margin-bottom: 6px;
    letter-spacing: 0.5px;
}

.form-group input {
    width: 100%;
    padding: 12px;
    background: #142a1a;
    border: 1px solid #2d5a38;
    border-radius: 8px;
    color: #e8efe8;
    font-size: 1em;
    font-family: 'Georgia', serif;
    transition: border-color 0.3s;
}

.form-group input:focus {
    outline: none;
    border-color: #4a9e5c;
}

.form-group input::placeholder { color: #5a7a5a; }

.submit-btn {
    background: #4a9e5c;
    color: #0c1a0c;
    border: none;
    border-radius: 8px;
    padding: 14px 32px;
    font-size: 1.1em;
    font-weight: 700;
    font-family: 'Georgia', serif;
    cursor: pointer;
    width: 100%;
    transition: background 0.3s;
    margin-top: 10px;
}

.submit-btn:hover { background: #5cb86e; }

.back-link {
    color: #4a9e5c;
    text-decoration: none;
    font-size: 0.9em;
}

.back-link:hover { text-decoration: underline; }

.msg-success {
    background: rgba(74, 158, 92, 0.2);
    border: 1px solid #4a9e5c;
    padding: 15px;
    border-radius: 8px;
    color: #7ed87e;
    margin-bottom: 20px;
    text-align: center;
}

.msg-error {
    background: rgba(200, 60, 60, 0.2);
    border: 1px solid #c83c3c;
    padding: 15px;
    border-radius: 8px;
    color: #f25c5c;
    margin-bottom: 20px;
    text-align: center;
}

.participant-actions {
    display: flex;
    gap: 8px;
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid #2d5a38;
}

.btn-edit, .btn-delete {
    flex: 1;
    padding: 8px 12px;
    border: none;
    border-radius: 6px;
    font-family: 'Georgia', serif;
    font-size: 0.85em;
    font-weight: 600;
    cursor: pointer;
    text-align: center;
    text-decoration: none;
    transition: background 0.3s;
}

.btn-edit {
    background: #4a9e5c;
    color: #0c1a0c;
}

.btn-edit:hover { background: #5cb86e; }

.btn-delete {
    background: #5a2020;
    color: #e8efe8;
}

.btn-delete:hover { background: #7a3030; }

.owgr-rank {
    display: inline-block;
    background: #0c1a0c;
    color: #e8d44d;
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 0.8em;
    font-weight: bold;
    min-width: 32px;
    text-align: center;
    margin-right: 4px;
    border: 1px solid #2d5a38;
}

.locked-badge {
    background: #5a2020;
    color: #e8efe8;
    padding: 4px 12px;
    border-radius: 6px;
    font-size: 0.8em;
    text-align: center;
    margin-top: 12px;
    font-style: italic;
}

.open-entry-wrap {
    display: flex;
    align-items: center;
    gap: 6px;
}

.open-entry-btn {
    background: #4a9e5c;
    color: #0c1a0c;
    border: none;
    border-radius: 8px;
    padding: 10px 18px;
    font-size: 0.9em;
    font-weight: 700;
    font-family: 'Georgia', serif;
    cursor: pointer;
    text-decoration: none;
    transition: background 0.3s;
}

.open-entry-btn:hover { background: #5cb86e; }

.lock-icon-btn {
    background: none;
    border: none;
    font-size: 1.2em;
    cursor: pointer;
    opacity: 0.4;
    padding: 4px;
    transition: opacity 0.2s;
}

.lock-icon-btn:hover { opacity: 0.9; }

.entries-locked-badge {
    display: flex;
    align-items: center;
    gap: 8px;
    background: #5a2020;
    color: #e8efe8;
    padding: 8px 14px;
    border-radius: 8px;
    font-size: 0.9em;
    font-weight: 700;
    font-family: 'Georgia', serif;
    border: none;
    cursor: pointer;
    transition: background 0.3s;
}

.entries-locked-badge:hover { background: #7a3030; }
"""


def generate_dashboard_html(tournament, players, picks_data, standings, career=None, cfg=None):
    if cfg is None:
        cfg = load_tournament()
    now = datetime.now().strftime('%B %d, %Y at %I:%M %p')
    participants = picks_data.get('participants', [])
    entry_fee = picks_data.get('entry_fee', cfg['entry_fee'])
    total_pot = len(participants) * entry_fee
    locked = picks_data.get('locked', False)

    # Build set of picked golfer names (lowercase) -> who picked them
    picked_by = {}
    for p in participants:
        for pick in p['picks']:
            key = pick.lower().strip()
            picked_by.setdefault(key, []).append(p['name'])

    # Tournament status section
    status_html = f"""
    <div class="card status-card full-width">
        <h2>Tournament Status</h2>
        <div class="status-grid">
            <div class="status-item">
                <div class="status-label">Course</div>
                <div class="status-value">{tournament['course']}</div>
            </div>
            <div class="status-item">
                <div class="status-label">Status</div>
                <div class="status-value">{tournament['status']}</div>
            </div>
            <div class="status-item">
                <div class="status-label">Participants</div>
                <div class="status-value">{len(participants)}</div>
            </div>
            <div class="status-item">
                <div class="status-label">Prize Pool</div>
                <div class="status-value" style="color: #e8d44d;">${total_pot}</div>
            </div>
        </div>
    </div>
    """

    # Pool standings section
    is_official = tournament.get('status', '').lower() in ('official', 'final', 'complete', 'completed')
    standings_html = ""
    if standings:
        rows = ""
        for s in standings:
            place_class = 'place-1' if s['place'] == '1st' else ('place-2' if s['place'] == '2nd' else '')
            if is_official and s['place'] == '1st':
                place_display = '🥇'
            elif is_official and s['place'] == '2nd':
                place_display = '🥈'
            else:
                place_display = s['place']
            best_picks = []
            for pk in s['picks'][:3]:
                css = 'pick-unique' if pk['unique'] else 'pick-shared'
                pos_str = 'CUT' if pk.get('cut') else (f"T{pk['position']}" if pk['position'] < 999 else '-')
                best_picks.append(f'<span class="{css}">{pk["name"]}<span class="pick-pos">({pos_str})</span></span>')
            prize_str = f'<span class="prize">${s["prize"]}</span>' if s['prize'] > 0 else '-'
            rows += f"""
            <tr>
                <td class="{place_class}" style="font-size:{'1.4em' if is_official and s['place'] in ('1st','2nd') else '1em'}">{place_display}</td>
                <td>{s['name']}</td>
                <td>{' &middot; '.join(best_picks)}</td>
                <td>{prize_str}</td>
            </tr>"""
        standings_html = f"""
        <div class="card full-width">
            <h2>Pool Standings</h2>
            <table class="standings-table">
                <thead><tr><th>Place</th><th>Name</th><th>Top Picks (Position)</th><th>Prize</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>"""
    elif participants:
        standings_html = """
        <div class="card full-width">
            <h2>Pool Standings</h2>
            <div class="no-data">Standings will update once the tournament begins</div>
        </div>"""

    # Leaderboard section
    leaderboard_html = ""
    if players:
        rows = ""
        for i, p in enumerate(players):
            name_lower = p['name'].lower()
            pickers = []
            for pk, names in picked_by.items():
                if name_lower in pk or pk in name_lower:
                    pickers.extend(names)
            if not pickers:
                continue

            badge = f'<span class="picked-badge">{", ".join(pickers)}</span>'
            is_cut = p.get('cut', False)
            if is_cut:
                pos_display = '<span class="cut-badge">CUT</span>'
                row_class = 'picked picked-cut'
                score_class = 'score-even'
                total_today = p['score']
            else:
                pos_display = str(p['position'])
                row_class = 'picked'
                score_str = str(p['score'])
                if score_str.startswith('-'):
                    score_class = 'score-under'
                elif score_str in ('E', '0', 'E '):
                    score_class = 'score-even'
                elif score_str.startswith('+'):
                    score_class = 'score-over'
                else:
                    score_class = 'score-even'
                total_today = f'{p["score"]} / {p.get("today", "-")}'

            rows += f"""
            <tr class="{row_class}">
                <td>{pos_display}</td>
                <td>{p['name']}{badge}</td>
                <td>{p.get('thru', '-')}</td>
                <td class="{score_class}">{total_today}</td>
            </tr>"""
        rnd_label = f'Round {tournament.get("current_round", 1)}'
        leaderboard_html = f"""
        <div class="card full-width">
            <h2>Live Leaderboard &mdash; All Picks</h2>
            <table class="lb-table">
                <thead><tr><th>Pos</th><th>Player</th><th>Thru</th><th>{rnd_label} (Total / Today)</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>"""
    else:
        leaderboard_html = """
        <div class="card full-width">
            <h2>Live Leaderboard</h2>
            <div class="no-data">Leaderboard data will appear once the tournament begins</div>
        </div>"""

    # Participants & picks section
    career_lookup = {c['name'].lower(): c for c in (career or [])}
    picks_html = ""
    if participants:
        cards = ""
        for p in participants:
            # Build pick data with OWGR ranking and sort by it
            pick_data = []
            for pick in p['picks']:
                owgr_rank, found = get_owgr_rank(pick)
                # Find tournament position
                tourn_pos = '-'
                for pl in players:
                    if pick.lower().strip() in pl['name'].lower() or pl['name'].lower() in pick.lower().strip():
                        if pl.get('cut'):
                            tourn_pos = 'CUT'
                        else:
                            tourn_pos = f"T{pl['position']}"
                        break
                pick_data.append({
                    'name': pick,
                    'owgr': owgr_rank,
                    'owgr_found': found,
                    'tourn_pos': tourn_pos,
                })
            pick_data.sort(key=lambda x: x['owgr'])

            pick_items = ""
            for pd in pick_data:
                owgr_str = f"#{pd['owgr']}" if pd['owgr_found'] else 'NR'
                pick_items += f"""
                <div class="pick-item">
                    <span class="pick-golfer"><span class="owgr-rank">{owgr_str}</span> {pd['name']}</span>
                    <span class="pick-pos">{pd['tourn_pos']}</span>
                </div>"""
            encoded_name = urllib.parse.quote(p['name'])
            if locked:
                actions = '<div class="locked-badge">Picks locked - tournament in progress</div>'
            else:
                actions = f"""
                <div class="participant-actions">
                    <a href="/edit/{encoded_name}" class="btn-edit">Edit Picks</a>
                    <button class="btn-delete" onclick="deleteParticipant('{p['name']}')">Delete</button>
                </div>"""
            c_data = career_lookup.get(p['name'].lower())
            career_badge = ''
            if c_data and c_data['winnings'] > 0:
                career_badge = f'<span style="font-size:0.75em;color:#e8d44d;font-weight:700;margin-left:8px">Career: ${c_data["winnings"]}</span>'
            cards += f"""
            <div class="participant-card">
                <div class="participant-name">{p['name']}{career_badge}</div>
                {pick_items}
                {actions}
            </div>"""
        picks_html = f"""
        <div class="card full-width">
            <h2>Participants &amp; Picks</h2>
            <div class="participants-grid">{cards}</div>
        </div>"""

    # Career standings card
    career_html = ''
    career = career or []
    if career:
        top = career[0]['name']
        crow = ''
        for c in career:
            highlight = ' style="background:rgba(232,212,77,0.07)"' if c['name'] == top else ''
            crow += f"""
            <tr{highlight}>
                <td>{c['name']}</td>
                <td style="text-align:center">{c['tournaments']}</td>
                <td style="text-align:center">{'🥇 ' * c['wins'] if c['wins'] else '—'}</td>
                <td style="text-align:center">{'🥈 ' * c['seconds'] if c['seconds'] else '—'}</td>
                <td style="text-align:right;color:#e8d44d;font-weight:700">${c['winnings']}</td>
            </tr>"""
        career_html = f"""
        <div class="card full-width">
            <h2>Career Standings</h2>
            <table class="standings-table">
                <thead><tr>
                    <th>Name</th><th style="text-align:center">Tournaments</th>
                    <th style="text-align:center">Wins</th><th style="text-align:center">2nds</th>
                    <th style="text-align:right">Total Winnings</th>
                </tr></thead>
                <tbody>{crow}</tbody>
            </table>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Kapelke Golf Pool - {cfg['name']}</title>
    <style>{STYLES}</style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="header-row">
                <div>
                    <h1>Kapelke Golf Pool &mdash; {cfg['name']} {cfg['dates']}</h1>
                    {'<button class="entries-locked-badge" onclick="toggleLock(false)" title="Click to unlock">&#x1F512; Entries Locked</button>' if locked else '<span class="open-entry-wrap"><a href="/enter" class="open-entry-btn">&#x26F3; Open Entry</a><button class="lock-icon-btn" onclick="toggleLock(true)" title="Lock entries">&#x1F512;</button></span>'}
                    <div class="updated">Last Updated: {now}</div>
                </div>
                <div class="refresh-area">
                    <button class="refresh-btn" onclick="refreshDashboard()">
                        <span class="refresh-icon">&#x21bb;</span> Refresh
                    </button>
                    <div class="countdown" id="countdown">Auto-refresh in 5:00</div>
                </div>
            </div>
        </div>

        <div class="grid">
            {status_html}
        </div>

        {standings_html}
        {leaderboard_html}
        {picks_html}
        {career_html}

        <div style="text-align:center;margin-top:10px;padding-bottom:20px">
            <a href="/admin" style="color:#4a7a5a;font-size:0.8em;text-decoration:none;font-style:italic">⚙ Admin</a>
        </div>

    </div>

    <script>
        function refreshDashboard() {{
            var btn = document.querySelector('.refresh-btn');
            btn.classList.add('spinning');
            btn.disabled = true;
            fetch('/api/leaderboard').then(function() {{
                location.reload();
            }}).catch(function() {{
                location.reload();
            }});
        }}

        function deleteParticipant(name) {{
            if (!confirm('Delete ' + name + ' from the pool?')) return;
            fetch('/api/delete', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                body: 'name=' + encodeURIComponent(name)
            }}).then(function() {{ location.reload(); }});
        }}

        function toggleLock(lock) {{
            var pw = prompt(lock ? 'Enter password to lock entries:' : 'Enter password to unlock entries:');
            if (pw === null) return;
            fetch(lock ? '/api/lock' : '/api/unlock', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                body: 'password=' + encodeURIComponent(pw)
            }}).then(function(r) {{ return r.json(); }}).then(function(data) {{
                if (data.error) {{ alert(data.error); }} else {{ location.reload(); }}
            }});
        }}

        // Auto-refresh countdown
        var remaining = 300;
        var countdownEl = document.getElementById('countdown');
        setInterval(function() {{
            remaining--;
            if (remaining <= 0) {{
                location.reload();
                return;
            }}
            var m = Math.floor(remaining / 60);
            var s = remaining % 60;
            countdownEl.textContent = 'Auto-refresh in ' + m + ':' + (s < 10 ? '0' : '') + s;
        }}, 1000);
    </script>
</body>
</html>"""


def generate_entry_html(message='', error=False, player_names=None, past_names=None):
    cfg = load_tournament()
    now = datetime.now().strftime('%B %d, %Y at %I:%M %p')

    # Build datalist options from golfer names
    datalist_options = ''
    if player_names:
        for name in sorted(player_names):
            datalist_options += f'<option value="{name}">'

    # Build datalist options from past participants
    past_options = ''
    if past_names:
        for name in sorted(past_names):
            past_options += f'<option value="{name}">'

    msg_html = ''
    if message:
        cls = 'msg-error' if error else 'msg-success'
        msg_html = f'<div class="{cls}">{message}</div>'

    pick_fields = ''
    for i in range(1, 7):
        pick_fields += f"""
        <div class="form-group">
            <label>Pick #{i}</label>
            <input type="text" name="pick{i}" placeholder="Golfer name" list="golfers" required>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Enter Picks - Kapelke Golf Pool</title>
    <style>{STYLES}</style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="header-row">
                <div>
                    <h1>Enter Your Picks &mdash; {cfg['name']} {cfg['dates']}</h1>
                    <div class="updated">{now}</div>
                </div>
                <a href="/" class="back-link">&larr; Back to Dashboard</a>
            </div>
        </div>

        <div class="form-container">
            <div class="card" style="margin-top: 20px;">
                <h2>Participant Entry &mdash; ${cfg['entry_fee']} Buy-in</h2>
                {msg_html}
                <form method="POST" action="/api/picks">
                    <div class="form-group">
                        <label>Your Name</label>
                        <input type="text" name="name" placeholder="Select or type your name" list="participants" required>
                    </div>
                    {pick_fields}
                    <button type="submit" class="submit-btn">Submit Picks</button>
                </form>
                <datalist id="participants">{past_options}</datalist>
                <datalist id="golfers">{datalist_options}</datalist>
            </div>
        </div>
    </div>
</body>
</html>"""


def generate_edit_html(participant, message='', error=False, player_names=None):
    now = datetime.now().strftime('%B %d, %Y at %I:%M %p')

    datalist_options = ''
    if player_names:
        for name in sorted(player_names):
            datalist_options += f'<option value="{name}">'

    msg_html = ''
    if message:
        cls = 'msg-error' if error else 'msg-success'
        msg_html = f'<div class="{cls}">{message}</div>'

    pick_fields = ''
    for i in range(1, 7):
        current = participant['picks'][i - 1] if i - 1 < len(participant['picks']) else ''
        pick_fields += f"""
        <div class="form-group">
            <label>Pick #{i}</label>
            <input type="text" name="pick{i}" value="{current}" list="golfers" required>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Edit Picks - Kapelke Golf Pool</title>
    <style>{STYLES}</style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="header-row">
                <div>
                    <h1>Edit Picks &mdash; {TOURNAMENT_NAME} {TOURNAMENT_DATES}</h1>
                    <div class="updated">{now}</div>
                </div>
                <a href="/" class="back-link">&larr; Back to Dashboard</a>
            </div>
        </div>

        <div class="form-container">
            <div class="card" style="margin-top: 20px;">
                <h2>Edit Picks for {participant['name']}</h2>
                {msg_html}
                <form method="POST" action="/api/edit">
                    <input type="hidden" name="name" value="{participant['name']}">
                    {pick_fields}
                    <button type="submit" class="submit-btn">Save Changes</button>
                </form>
                <datalist id="golfers">{datalist_options}</datalist>
            </div>
        </div>
    </div>
</body>
</html>"""


# --- HTTP Server ---

class GolfPoolHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self._serve_dashboard()
        elif self.path == '/enter':
            if is_locked():
                self._send_html('<script>alert("Picks are locked - tournament has started.");location.href="/";</script>')
                return
            self._serve_entry_form()
        elif self.path.startswith('/edit/'):
            if is_locked():
                self._send_html('<script>alert("Picks are locked - tournament has started.");location.href="/";</script>')
                return
            name = urllib.parse.unquote(self.path[6:])
            self._serve_edit_form(name)
        elif self.path == '/api/picks':
            self._serve_json(load_picks())
        elif self.path == '/api/leaderboard':
            tournament, players = fetch_leaderboard()
            self._serve_json({'tournament': tournament, 'players': players})
        elif self.path.startswith('/admin'):
            self._serve_admin(self.path)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/api/picks':
            self._handle_submit_picks()
        elif self.path == '/api/edit':
            self._handle_edit_picks()
        elif self.path == '/api/delete':
            self._handle_delete_participant()
        elif self.path == '/api/lock':
            self._handle_set_lock(True)
        elif self.path == '/api/unlock':
            self._handle_set_lock(False)
        elif self.path == '/admin/update':
            self._handle_admin_update()
        elif self.path == '/admin/reset':
            self._handle_admin_reset()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_dashboard(self):
        cfg = load_tournament()
        tournament, players = fetch_leaderboard()
        picks_data = load_picks()
        standings = calculate_standings(picks_data.get('participants', []), players)
        career = career_standings()
        html = generate_dashboard_html(tournament, players, picks_data, standings, career=career, cfg=cfg)
        self._send_html(html)

    def _serve_entry_form(self, message='', error=False):
        # Use leaderboard players if tournament is live, otherwise fetch PGA names
        _, players = fetch_leaderboard()
        player_names = [p['name'] for p in players] if players else fetch_player_names()
        past_names = _all_historical_names()
        html = generate_entry_html(message=message, error=error, player_names=player_names, past_names=past_names)
        self._send_html(html)

    def _handle_submit_picks(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        params = urllib.parse.parse_qs(body)

        name = params.get('name', [''])[0].strip()
        picks = []
        for i in range(1, 7):
            pick = params.get(f'pick{i}', [''])[0].strip()
            if pick:
                picks.append(pick)

        if not name:
            self._serve_entry_form_redirect('Please enter your name.', error=True)
            return
        if len(picks) < 6:
            self._serve_entry_form_redirect('Please enter all 6 picks.', error=True)
            return

        data = load_picks()
        # Check for duplicate name
        for p in data['participants']:
            if p['name'].lower() == name.lower():
                self._serve_entry_form_redirect(f'{name} has already entered picks.', error=True)
                return

        data['participants'].append({'name': name, 'picks': picks})
        save_picks(data)

        # Redirect to dashboard after success
        self.send_response(303)
        self.send_header('Location', '/')
        self.end_headers()

    def _serve_edit_form(self, name, message='', error=False):
        data = load_picks()
        participant = None
        for p in data['participants']:
            if p['name'].lower() == name.lower():
                participant = p
                break
        if not participant:
            self._send_html('<script>alert("Participant not found.");location.href="/";</script>')
            return
        _, players = fetch_leaderboard()
        player_names = [p['name'] for p in players] if players else fetch_player_names()
        html = generate_edit_html(participant, message=message, error=error, player_names=player_names)
        self._send_html(html)

    def _handle_edit_picks(self):
        if is_locked():
            self._send_html('<script>alert("Picks are locked - tournament has started.");location.href="/";</script>')
            return
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        params = urllib.parse.parse_qs(body)

        name = params.get('name', [''])[0].strip()
        picks = []
        for i in range(1, 7):
            pick = params.get(f'pick{i}', [''])[0].strip()
            if pick:
                picks.append(pick)

        if len(picks) < 6:
            self._serve_edit_form(name)
            return

        data = load_picks()
        for p in data['participants']:
            if p['name'].lower() == name.lower():
                p['picks'] = picks
                break
        save_picks(data)
        self.send_response(303)
        self.send_header('Location', '/')
        self.end_headers()

    def _handle_delete_participant(self):
        if is_locked():
            self._serve_json({'error': 'Tournament has started, picks are locked'})
            return
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        params = urllib.parse.parse_qs(body)
        name = params.get('name', [''])[0].strip()

        data = load_picks()
        data['participants'] = [p for p in data['participants'] if p['name'].lower() != name.lower()]
        save_picks(data)
        self._serve_json({'success': True})

    def _handle_set_lock(self, locked):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        params = urllib.parse.parse_qs(body)
        password = params.get('password', [''])[0]
        if password != load_tournament()['admin_password']:
            self._serve_json({'error': 'Incorrect password.'})
            return
        data = load_picks()
        data['locked'] = locked
        save_picks(data)
        self._serve_json({'success': True, 'locked': locked})

    def _serve_admin(self, path=''):
        cfg = load_tournament()
        picks_data = load_picks()
        participants = picks_data.get('participants', [])
        locked = picks_data.get('locked', False)
        entry_fee = cfg['entry_fee']
        total_pot = len(participants) * entry_fee

        # Parse query string for status messages
        msg = ''
        if '?success=updated' in path:
            msg = '<div style="background:#1a4a1a;border:1px solid #4a9e5c;border-radius:8px;padding:12px 16px;margin-bottom:20px;color:#8abf8a">✓ Tournament settings updated.</div>'
        elif '?success=reset' in path:
            msg = '<div style="background:#1a4a1a;border:1px solid #4a9e5c;border-radius:8px;padding:12px 16px;margin-bottom:20px;color:#8abf8a">✓ Tournament archived and reset. Picks cleared.</div>'
        elif '?error=badpass' in path:
            msg = '<div style="background:#4a1a1a;border:1px solid #9e4a4a;border-radius:8px;padding:12px 16px;margin-bottom:20px;color:#bf8a8a">✗ Incorrect password.</div>'

        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Admin — Kapelke Golf Pool</title>
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ font-family:'Georgia','Times New Roman',serif; background:#0c1a0c; color:#e8efe8; padding:30px 20px; }}
        .container {{ max-width:680px; margin:0 auto; }}
        h1 {{ color:#e8d44d; font-size:1.8em; margin-bottom:6px; }}
        .back {{ color:#4a9e5c; font-size:0.9em; text-decoration:none; display:inline-block; margin-bottom:24px; }}
        .back:hover {{ text-decoration:underline; }}
        .card {{ background:#1a3320; border:1px solid #2d5a38; border-radius:12px; padding:24px; margin-bottom:24px; }}
        .card h2 {{ color:#e8d44d; font-size:1.1em; margin-bottom:16px; padding-bottom:10px; border-bottom:2px solid #4a9e5c; }}
        label {{ display:block; font-size:0.9em; color:#8abf8a; margin-bottom:5px; margin-top:14px; }}
        label:first-of-type {{ margin-top:0; }}
        input[type=text], input[type=number], input[type=password] {{
            width:100%; padding:10px 12px; background:#0f2615; border:1px solid #2d5a38;
            border-radius:8px; color:#e8efe8; font-family:'Georgia',serif; font-size:0.95em;
        }}
        input:focus {{ outline:none; border-color:#4a9e5c; }}
        .btn {{ display:inline-block; padding:10px 22px; border:none; border-radius:8px;
                font-family:'Georgia',serif; font-size:0.95em; font-weight:700; cursor:pointer; }}
        .btn-green {{ background:#4a9e5c; color:#0c1a0c; margin-top:18px; }}
        .btn-green:hover {{ background:#5cb86e; }}
        .btn-red {{ background:#7a2020; color:#e8efe8; margin-top:18px; }}
        .btn-red:hover {{ background:#9e3030; }}
        .status-row {{ display:flex; justify-content:space-between; padding:8px 0;
                       border-bottom:1px solid #2d5a38; font-size:0.9em; }}
        .status-row:last-child {{ border:none; }}
        .warn {{ background:#2a1a0a; border:1px solid #7a4a1a; border-radius:8px;
                 padding:12px 16px; font-size:0.85em; color:#c8a06a; margin-bottom:14px; line-height:1.5; }}
    </style>
</head>
<body>
<div class="container">
    <h1>⚙ Admin</h1>
    <a href="/" class="back">← Back to Dashboard</a>
    {msg}

    <!-- Tournament Settings -->
    <div class="card">
        <h2>Tournament Settings</h2>
        <form method="POST" action="/admin/update">
            <label>Tournament Name</label>
            <input type="text" name="name" value="{cfg['name']}" required>
            <label>Dates (e.g. Apr 10–13, 2026)</label>
            <input type="text" name="dates" value="{cfg['dates']}">
            <label>Course (fallback if API doesn't return it)</label>
            <input type="text" name="course" value="{cfg['course']}">
            <label>PGA Tour ID (e.g. R2026007)</label>
            <input type="text" name="pga_tour_id" value="{cfg['pga_tour_id']}">
            <label>Entry Fee ($)</label>
            <input type="number" name="entry_fee" value="{cfg['entry_fee']}" min="1" required>
            <label>New Admin Password (leave blank to keep current)</label>
            <input type="password" name="new_password" placeholder="Leave blank to keep current">
            <label>Current Password (required)</label>
            <input type="password" name="password" required>
            <button type="submit" class="btn btn-green">Save Settings</button>
        </form>
    </div>

    <!-- Current Status -->
    <div class="card">
        <h2>Current Tournament Status</h2>
        <div class="status-row"><span>Participants</span><span>{len(participants)}</span></div>
        <div class="status-row"><span>Prize Pool</span><span style="color:#e8d44d">${total_pot}</span></div>
        <div class="status-row"><span>Entries</span><span>{'🔒 Locked' if locked else '🟢 Open'}</span></div>
        <div class="status-row"><span>Tournaments in history</span><span>{len(load_history())}</span></div>
    </div>

    <!-- Archive & Reset -->
    <div class="card">
        <h2>Archive &amp; Reset for New Tournament</h2>
        <div class="warn">
            ⚠ This will save the current standings to career history, clear all picks,
            and unlock entries. Use this when a tournament ends and you're ready to start a new one.
            This cannot be undone.
        </div>
        <form method="POST" action="/admin/reset" onsubmit="return confirm('Archive results and reset picks for new tournament?')">
            <label>Current Password (required)</label>
            <input type="password" name="password" required>
            <button type="submit" class="btn btn-red">Archive &amp; Reset</button>
        </form>
    </div>
</div>
</body>
</html>"""
        self._send_html(html)

    def _handle_admin_update(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        params = urllib.parse.parse_qs(body)
        password = params.get('password', [''])[0]
        cfg = load_tournament()
        if password != cfg['admin_password']:
            self.send_response(303)
            self.send_header('Location', '/admin?error=badpass')
            self.end_headers()
            return
        cfg['name']         = params.get('name',         [cfg['name']])[0].strip()
        cfg['dates']        = params.get('dates',        [cfg['dates']])[0].strip()
        cfg['course']       = params.get('course',       [cfg['course']])[0].strip()
        cfg['pga_tour_id']  = params.get('pga_tour_id',  [cfg['pga_tour_id']])[0].strip()
        cfg['entry_fee']    = int(params.get('entry_fee', [cfg['entry_fee']])[0])
        new_pw = params.get('new_password', [''])[0].strip()
        if new_pw:
            cfg['admin_password'] = new_pw
        save_tournament(cfg)
        self.send_response(303)
        self.send_header('Location', '/admin?success=updated')
        self.end_headers()

    def _handle_admin_reset(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        params = urllib.parse.parse_qs(body)
        password = params.get('password', [''])[0]
        cfg = load_tournament()
        if password != cfg['admin_password']:
            self.send_response(303)
            self.send_header('Location', '/admin?error=badpass')
            self.end_headers()
            return
        # Archive current standings
        picks_data = load_picks()
        tournament, players = fetch_leaderboard()
        standings = calculate_standings(picks_data.get('participants', []), players)
        history = load_history()
        history.append({
            'tournament': cfg['name'],
            'dates':      cfg['dates'],
            'year':       datetime.now().year,
            'results':    [{'name': s['name'], 'place': s['place'], 'prize': s['prize']}
                           for s in standings],
        })
        save_history(history)
        # Reset picks
        save_picks({'entry_fee': cfg['entry_fee'], 'locked': False, 'participants': []})
        self.send_response(303)
        self.send_header('Location', '/admin?success=reset')
        self.end_headers()

    def _serve_entry_form_redirect(self, message, error=False):
        _, players = fetch_leaderboard()
        player_names = [p['name'] for p in players] if players else fetch_player_names()
        past_names = _all_historical_names()
        html = generate_entry_html(message=message, error=error, player_names=player_names, past_names=past_names)
        self._send_html(html)

    def _send_html(self, html):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def _serve_json(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def log_message(self, format, *args):
        print(f"  [{datetime.now().strftime('%H:%M:%S')}] {args[0]}")


def main():
    port = int(os.environ.get('PORT', 8051))
    host = '0.0.0.0' if os.environ.get('RENDER') else 'localhost'
    cfg = load_tournament()
    print("=" * 50)
    print("  Kapelke Golf Pool Dashboard")
    print(f"  {cfg['name']} {cfg['dates']}")
    print("=" * 50)
    server = HTTPServer((host, port), GolfPoolHandler)
    url = f'http://{host}:{port}'
    print(f"  Server running at {url}")
    print(f"  Entry form at {url}/enter")
    print("  Auto-refresh every 5 minutes")
    print("  Press Ctrl+C to stop")
    print("=" * 50)
    if not os.environ.get('RENDER'):
        webbrowser.open(f'http://localhost:{port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.server_close()


if __name__ == '__main__':
    main()
