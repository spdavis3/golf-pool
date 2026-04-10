#!/usr/bin/env python3
"""
Kapelke Golf Pool Dashboard
"""

import json
import io
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

_EASTERN = timezone(timedelta(hours=-4), 'ET')  # EDT (UTC-4, used Apr–Nov)
from http.server import HTTPServer, BaseHTTPRequestHandler
import webbrowser


_DATA_DIR        = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
PICKS_FILE       = os.path.join(_DATA_DIR, 'picks.json')
TOURNAMENT_FILE  = os.path.join(_DATA_DIR, 'tournament.json')
HISTORY_FILE     = os.path.join(_DATA_DIR, 'history.json')

_DEFAULT_TOURNAMENT = {
    'name': 'Kapelke Golf Pool',
    'dates': '',
    'course': '',
    'pga_tour_id': '',
    'entry_fee': 25,
    'admin_password': 'golf',
    'show_medals': False,
    'counts_for_career': True,
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
    """Returns True if entries are manually locked via admin."""
    return load_picks().get('locked', False)


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


def _normalize(s):
    import unicodedata
    return unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii').lower().strip()

def get_owgr_rank(name):
    """Get a golfer's world ranking. Returns (rank, True) or (999, False) if not found."""
    rankings = fetch_owgr()
    key = name.lower().strip()
    if key in rankings:
        return rankings[key], True
    # Try normalized (strip diacritics)
    key_norm = _normalize(name)
    for rname, rank in rankings.items():
        if key_norm == _normalize(rname):
            return rank, True
    # Fuzzy match (normalized)
    for rname, rank in rankings.items():
        rname_norm = _normalize(rname)
        if key_norm in rname_norm or rname_norm in key_norm:
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
    """Fetch player names for the configured tournament field."""
    global _player_names_cache
    if _player_names_cache:
        return _player_names_cache

    cfg = load_tournament()
    configured_name = cfg.get('name', '').lower()
    name_words = [w for w in configured_name.split() if len(w) > 3]

    def _extract_names(events):
        for event in events:
            event_name = event.get('name', '').lower()
            if any(w in event_name for w in name_words):
                competitors = event.get('competitions', [{}])[0].get('competitors', [])
                names = sorted({
                    c.get('athlete', {}).get('fullName', '').strip()
                    for c in competitors
                } - {''})
                if names:
                    return names
        return []

    # 1. Try current ESPN scoreboard
    try:
        req = urllib.request.Request(ESPN_SCOREBOARD_URL, headers={'User-Agent': 'Mozilla/5.0'})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode('utf-8'))
        names = _extract_names(data.get('events', []))
        if names:
            _player_names_cache = names
            print(f"  Cached {len(names)} field players from current ESPN scoreboard")
            return _player_names_cache
    except Exception as e:
        print(f"  ESPN current scoreboard failed: {e}")

    # 2. Try upcoming window (next 21 days) to find pre-tournament field
    try:
        from datetime import date as _date, timedelta as _td
        today = _date.today()
        d_start = today.strftime('%Y%m%d')
        d_end   = (today + _td(days=21)).strftime('%Y%m%d')
        url = f'{ESPN_SCOREBOARD_URL}?dates={d_start}-{d_end}&limit=5'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode('utf-8'))
        names = _extract_names(data.get('events', []))
        if names:
            _player_names_cache = names
            print(f"  Cached {len(names)} field players from upcoming ESPN window")
            return _player_names_cache
    except Exception as e:
        print(f"  ESPN upcoming window failed: {e}")

    print("  No matching tournament field found — autocomplete unavailable")
    return _player_names_cache


def fetch_next_tournament():
    """Fetch the next upcoming PGA Tour event from the PGA Tour GraphQL API."""
    from datetime import datetime as _dt
    query = '{ upcomingSchedule(tourCode: "R", year: "2026") { id tournaments { id tournamentName date startDate courseName city state } } }'
    body = json.dumps({'query': query}).encode()
    req = urllib.request.Request(
        PGA_TOUR_API_URL, data=body,
        headers={'Content-Type': 'application/json', 'x-api-key': PGA_TOUR_API_KEY, 'User-Agent': 'Mozilla/5.0'},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read())
    tournaments = data['data']['upcomingSchedule']['tournaments']
    today_ms = _dt(*_dt.now().timetuple()[:3]).timestamp() * 1000  # midnight today
    upcoming = [t for t in tournaments if t.get('startDate', 0) >= today_ms]
    if not upcoming:
        return None
    t = upcoming[0]
    return {
        'name':        t['tournamentName'],
        'dates':       t['date'],
        'course':      t['courseName'],
        'pga_tour_id': t['id'],
    }


def _parse_espn_tee_time(s):
    """Parse ESPN tee time string (e.g. 'Thu Apr 09 08:14:00 PDT 2026') to ET display + sort key.
    ESPN labels times with PDT/PST but they are already in ET — no conversion needed."""
    import re
    m = re.match(r'\w+ \w+ \d+ (\d+):(\d+):\d+ \w+ \d+', s)
    if not m:
        return '', ''
    hour, minute = int(m.group(1)), int(m.group(2))
    try:
        h = hour % 12 or 12
        ampm = 'AM' if hour < 12 else 'PM'
        return f'{h}:{minute:02d} {ampm}', f'{hour:02d}:{minute:02d}'
    except Exception:
        return '', ''


def fetch_leaderboard_espn(cfg):
    """Fetch live leaderboard from ESPN scoreboard API."""
    req = urllib.request.Request(
        ESPN_SCOREBOARD_URL,
        headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
    )
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read().decode('utf-8'))

    events = data.get('events', [])
    if not events:
        raise ValueError('No events in ESPN response')
    event = events[0]
    comps = event.get('competitions', [{}])[0]
    competitors = comps.get('competitors', [])
    if not competitors:
        return None, []

    comp_status = comps.get('status', {})
    status_type = comp_status.get('type', {})
    status_detail = status_type.get('detail', 'In Progress')
    current_round = comp_status.get('period', 1)
    round_in_progress = status_type.get('state') == 'in'

    tournament = {
        'name': cfg['name'],
        'espn_event_name': event.get('name', ''),
        'date': '',
        'status': status_detail,
        'course': cfg['course'],
        'current_round': current_round,
    }

    players = []
    for c in competitors:
        name = c.get('athlete', {}).get('fullName', '').strip()
        if not name:
            continue
        position = c.get('order', 999)
        score = c.get('score', 'E') or 'E'
        linescores = c.get('linescores', [])
        # Round scores: linescores[i] is round i+1
        round_scores = []
        today_score = '-'
        thru = '-'
        for ls in linescores:
            rnd = ls.get('period', 0)
            val = ls.get('displayValue')
            if val:
                round_scores.append(val)
                if rnd == current_round:
                    today_score = val
            if rnd == current_round and round_in_progress:
                hole_scores = ls.get('linescores', [])
                if len(hole_scores) >= 18:
                    thru = 'F'
                elif hole_scores:
                    thru = str(hole_scores[-1].get('period', len(hole_scores)))
        max_round = max((ls.get('period', 0) for ls in linescores), default=0)
        is_cut = current_round >= 3 and 0 < max_round <= 2
        # Extract tee time for current round from linescore statistics
        tee_time_et = ''
        tee_time_sort = ''
        cur_ls = next((ls for ls in linescores if ls.get('period') == current_round), {})
        for stat in cur_ls.get('statistics', {}).get('categories', [{}])[0].get('stats', []):
            dv = stat.get('displayValue', '')
            if any(tz in dv for tz in ('PDT', 'PST', 'EDT', 'EST')):
                tee_time_et, tee_time_sort = _parse_espn_tee_time(dv)
                break
        players.append({
            'name': name,
            'position': position,
            'score': score,
            'today': today_score,
            'thru': thru,
            'linescores': round_scores,
            'cut': is_cut,
            'rounds_complete': max_round,
            'tee_time': tee_time_et,
            'tee_time_sort': tee_time_sort,
        })

    # Re-assign positions based on score so tied players share the same position
    def score_to_num(s):
        if s in ('E', '0', ''): return 0
        try: return int(str(s).replace('+', ''))
        except: return 999
    def holes_played(p):
        thru = p.get('thru', '-')
        completed = p.get('rounds_complete', 0)
        if thru == 'F': return 18 * completed
        if thru == '-': return 18 * max(0, completed - 1) if completed > 0 else 0
        try: return int(thru) + 18 * max(0, completed - 1)
        except: return 0
    active = [p for p in players if not p.get('cut') and (p.get('thru', '-') != '-' or p.get('rounds_complete', 0) > 0)]
    active.sort(key=lambda p: (score_to_num(p['score']), -holes_played(p)))
    pos = 1
    for i, p in enumerate(active):
        if i > 0 and score_to_num(p['score']) == score_to_num(active[i - 1]['score']):
            p['position'] = active[i - 1]['position']
        else:
            p['position'] = pos
        pos = i + 2
    players.sort(key=lambda p: (1 if p.get('cut') else 0, p['position'], -holes_played(p)))
    return tournament, players


def fetch_leaderboard():
    """Fetch live leaderboard — tries ESPN first, falls back to PGA Tour GraphQL."""
    import gzip as _gzip, base64 as _base64
    cfg = load_tournament()

    # Try ESPN first (more reliable during active rounds)
    try:
        tournament, players = fetch_leaderboard_espn(cfg)
        if players:
            print(f"  ESPN: {len(players)} players, status={tournament['status']}")
            return tournament, players
    except Exception as e:
        print(f"  ESPN error: {e}")

    # Fallback: PGA Tour GraphQL
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

        if not data.get('players'):
            raise ValueError('PGA Tour API returned no players')

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

    # Build lookup: lowercase player name -> position, cut status, started status
    pos_lookup = {}
    cut_lookup = {}
    thru_lookup = {}
    started_lookup = {}
    for p in players:
        key = p['name'].lower()
        pos_lookup[key] = p['position']
        cut_lookup[key] = p.get('cut', False)
        thru_lookup[key] = p.get('thru', '-')
        started_lookup[key] = p.get('thru', '-') != '-' or p.get('rounds_complete', 0) > 0
        parts = p['name'].lower().split()
        if len(parts) >= 2:
            pos_lookup[parts[-1]] = pos_lookup.get(parts[-1], p['position'])
            cut_lookup[parts[-1]] = cut_lookup.get(parts[-1], p.get('cut', False))
            thru_lookup[parts[-1]] = thru_lookup.get(parts[-1], p.get('thru', '-'))
            started_lookup[parts[-1]] = started_lookup.get(parts[-1], started_lookup[key])

    def get_position(pick_name):
        name = pick_name.lower().strip()
        if name in pos_lookup:
            return pos_lookup[name] if started_lookup.get(name) else 999
        for pname, pos in pos_lookup.items():
            if name in pname or pname in name:
                return pos if started_lookup.get(pname) else 999
        return 999

    def is_cut(pick_name):
        name = pick_name.lower().strip()
        if name in cut_lookup:
            return cut_lookup[name]
        for pname, val in cut_lookup.items():
            if name in pname or pname in name:
                return val
        return False

    def has_started(pick_name):
        name = pick_name.lower().strip()
        if name in started_lookup:
            return started_lookup[name]
        for pname, started in started_lookup.items():
            if name in pname or pname in name:
                return started
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
            started = has_started(pick)
            picks_with_pos.append({
                'name': pick,
                'position': pos,
                'unique': unique,
                'cut': is_cut(pick),
                'started': started,
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
        n = i + 1
        suffix = 'th' if 11 <= n <= 13 else {1:'st',2:'nd',3:'rd'}.get(n % 10, 'th')
        if i == 0:
            s['prize'] = total_pot - entry_fee if len(participants) > 1 else total_pot
            s['place'] = '1st'
        elif i == 1:
            s['prize'] = entry_fee
            s['place'] = '2nd'
        else:
            s['prize'] = 0
            s['place'] = f'{n}{suffix}'

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
.pick-unique { color: #7ed87e; font-weight: bold; }
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
.open-entry-btn--locked { background: #7a2020; color: #f8d7d7; cursor: default; }

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
    now = datetime.now(tz=_EASTERN).strftime('%B %d, %Y at %I:%M %p ET')
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

    # Detect pre-tournament: either ESPN returns a scheduled time, or the live event is a different tournament
    status_str = tournament.get('status', '')
    scheduled = any(x in status_str for x in [' AM ', ' PM ', 'AM EDT', 'PM EDT', 'AM ET', 'PM ET'])
    cfg_name = (cfg or load_tournament()).get('name', '').lower()
    name_words = [w for w in cfg_name.split() if len(w) > 3]
    espn_event = tournament.get('espn_event_name', '').lower()
    wrong_event = bool(name_words) and bool(espn_event) and not any(w in espn_event for w in name_words)
    pre_tourney = scheduled or wrong_event
    display_status = 'Not Started' if wrong_event else status_str
    any_started = not pre_tourney and any(
        p.get('thru', '-') != '-' and not p.get('cut', False) for p in players
    )

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
                <div class="status-value">{display_status}</div>
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
    is_official = cfg.get('show_medals', False)
    standings_html = ""
    if standings and not pre_tourney:
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
                if pre_tourney:
                    pos_str = ''
                    best_picks.append(f'<span class="{css}">{pk["name"]}</span>')
                else:
                    if pk.get('cut'):
                        pos_str = 'CUT'
                    elif not pk.get('started', True):
                        pos_str = '—'
                    elif pk['position'] < 999:
                        pos_str = f"T{pk['position']}"
                    else:
                        pos_str = '—'
                    best_picks.append(f'<span class="{css}">{pk["name"]}<span class="pick-pos">({pos_str})</span></span>')
            prize_str = '-'
            if not pre_tourney:
                prize_str = f'<span class="prize">${s["prize"]}</span>' if s['prize'] > 0 else '-'
            participant_started = any(pk.get('started', False) for pk in s['picks'])
            place_cell = '—' if not participant_started else place_display
            rows += f"""
            <tr>
                <td class="{place_class}" style="font-size:{'1.4em' if is_official and s['place'] in ('1st','2nd') else '1em'}">{place_cell}</td>
                <td>{s['name']}</td>
                <td>{' &middot; '.join(best_picks)}</td>
                <td>{prize_str}</td>
            </tr>"""
        standings_html = f"""
        <div class="card full-width">
            <h2>Pool Standings</h2>
            <table class="standings-table">
                <thead><tr><th>Place</th><th>Name</th><th>Top Picks (Position) <span style="font-weight:normal;color:#7a9a7a;font-size:0.8em">· bold = solo pick</span></th><th>Prize</th></tr></thead>
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
    if players and not pre_tourney:
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
            tee_time_cell = ''
            if is_cut:
                pos_display = '<span class="cut-badge">CUT</span>'
                row_class = 'picked picked-cut'
                score_class = 'score-even'
                total_today = f'{p["score"]} / CUT'
            else:
                thru_val = p.get('thru', '-')
                pos_display = str(p['position']) if p.get('rounds_complete', 0) > 0 or thru_val != '-' else 'N/A'
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
                thru = p.get('thru', '-')
                thru_str = f'({thru})' if thru not in ('-', 'F') else ('' if thru == '-' else '(F)')
                if thru == '-':
                    total_today = f'{p["score"]}' if p["score"] not in ('', None) else '—'
                    score_class = 'score-even'
                    if p.get('tee_time'):
                        tee_time_cell = p['tee_time']
                else:
                    total_today = f'{p["score"]} / {p.get("today", "-")}{thru_str}'

            rows += f"""
            <tr class="{row_class}">
                <td>{pos_display}</td>
                <td>{p['name']}{badge}</td>
                <td class="{score_class}">{total_today}</td>
                <td style="color:#8abf8a;font-size:0.85em;white-space:nowrap">{tee_time_cell}</td>
            </tr>"""
        rnd_label = f'Round {tournament.get("current_round", 1)}'
        leaderboard_html = f"""
        <div class="card full-width">
            <h2>Live Leaderboard &mdash; All Picks</h2>
            <table class="lb-table">
                <thead><tr><th>Pos</th><th>Player</th><th>{rnd_label} (Total / Today)</th><th>Tee Time</th></tr></thead>
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
    tournament_live = len(players) > 0 and not pre_tourney
    career_lookup = {c['name'].lower(): c for c in (career or [])}
    picks_html = ""
    if participants:
        cards = ""
        for p in participants:
            # Build pick data with OWGR ranking and tournament position
            pick_data = []
            for pick in p['picks']:
                owgr_rank, found = get_owgr_rank(pick)
                # Find tournament position
                tourn_pos = '-'
                tourn_pos_num = 998  # not found / not started
                tee_time = ''
                tee_time_sort = ''
                for pl in players:
                    if pick.lower().strip() in pl['name'].lower() or pl['name'].lower() in pick.lower().strip():
                        tee_time = pl.get('tee_time', '')
                        tee_time_sort = pl.get('tee_time_sort', '')
                        if pl.get('cut'):
                            tourn_pos = 'CUT'
                            tourn_pos_num = 999
                        elif pl.get('thru', '-') == '-' and pl.get('rounds_complete', 0) == 0:
                            tourn_pos = 'N/A'
                            tourn_pos_num = 998
                        else:
                            tourn_pos = f"T{pl['position']}" if pl['position'] > 1 else '1st'
                            tourn_pos_num = pl['position']
                        break
                pick_data.append({
                    'name': pick,
                    'owgr': owgr_rank,
                    'owgr_found': found,
                    'tourn_pos': tourn_pos,
                    'tourn_pos_num': tourn_pos_num,
                    'tee_time': tee_time,
                    'tee_time_sort': tee_time_sort,
                })
            # When live: sort by position (unstarted last, by tee time); pre-tourney: tee time or OWGR
            if tournament_live:
                pick_data.sort(key=lambda x: (x['tourn_pos_num'], x['tee_time_sort'] or '99:99'))
            else:
                has_tee_times = any(pd['tee_time_sort'] for pd in pick_data)
                if has_tee_times:
                    pick_data.sort(key=lambda x: (x['tee_time_sort'] or '99:99', x['owgr']))
                else:
                    pick_data.sort(key=lambda x: x['owgr'])

            pick_items = ""
            for pd in pick_data:
                owgr_str = f"#{pd['owgr']}" if pd['owgr_found'] else 'NR'
                if tournament_live:
                    # Show tournament position as main badge
                    if pd['tourn_pos_num'] == 998:
                        badge_color = '#555555'
                    elif pd['tourn_pos_num'] <= 20:
                        badge_color = '#4a9e5c'
                    elif pd['tourn_pos_num'] <= 40:
                        badge_color = '#e8d44d'
                    elif pd['tourn_pos_num'] == 999:
                        badge_color = '#2a2a2a'
                    else:
                        badge_color = '#7a2020'
                    badge = f'<span class="owgr-rank" style="color:{badge_color};border-color:{badge_color}">{pd["tourn_pos"]}</span>'
                    pick_items += f"""
                <div class="pick-item">
                    <span class="pick-golfer">{badge} {pd['name']}</span>
                    <span class="pick-pos">{owgr_str}</span>
                </div>"""
                else:
                    # Pre-tournament: show OWGR rank as main badge
                    pick_items += f"""
                <div class="pick-item">
                    <span class="pick-golfer"><span class="owgr-rank">{owgr_str}</span> {pd['name']}</span>
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

    # Career standings card (collapsible)
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
        career_body = f"""
                <table class="standings-table">
                    <thead><tr>
                        <th>Name</th><th style="text-align:center">Tournaments</th>
                        <th style="text-align:center">Wins</th><th style="text-align:center">2nds</th>
                        <th style="text-align:right">Total Winnings</th>
                    </tr></thead>
                    <tbody>{crow}</tbody>
                </table>"""
    else:
        career_body = '<div class="no-data">No tournament history yet — career winnings will appear here after the first archived tournament.</div>'
    career_html = f"""
        <div class="card full-width" style="margin-top:8px">
            <h2>From Tee to Eternity</h2>
            {career_body}
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
                    <span class="open-entry-wrap">
                        {'<a href="/enter" class="open-entry-btn">&#x26F3; Open Entry</a>' if not locked else '<span class="open-entry-btn open-entry-btn--locked">Entry Locked</span>'}
                        {'<button class="lock-icon-btn" onclick="toggleLock(false)" title="Click to unlock">&#x1F512;</button>' if locked else '<button class="lock-icon-btn" onclick="toggleLock(true)" title="Lock entries">&#x1F513;</button>'}
                    </span>
                    <div id="entry-deadline" style="margin-top:6px;font-size:0.85em;color:#aaa"></div>
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

        // Entry deadline countdown — Thursday Mar 12 2026 4:00 AM EST (= 09:00 UTC)
        var deadlineEl = document.getElementById('entry-deadline');
        var deadline = new Date('2026-03-12T09:00:00Z');
        function updateDeadline() {{
            var now = new Date();
            var diff = deadline - now;
            if (diff <= 0) {{
                deadlineEl.textContent = '';
                return;
            }}
            var totalMin = Math.floor(diff / 60000);
            var h = Math.floor(totalMin / 60);
            var m = totalMin % 60;
            deadlineEl.textContent = 'Entries close in ' + h + 'h ' + m + 'm';
        }}
        updateDeadline();
        setInterval(updateDeadline, 60000);
    </script>
</body>
</html>"""


def _autocomplete_js(player_names):
    names_json = json.dumps(sorted(player_names, key=lambda n: n.split()[-1].lower()) if player_names else [])
    return f"""
<style>
.ac-wrap {{ position: relative; }}
.ac-dropdown {{ position: absolute; top: 100%; left: 0; right: 0; background: #1a3320;
  border: 1px solid #4a9e5c; border-top: none; border-radius: 0 0 6px 6px;
  z-index: 200; display: none; max-height: 220px; overflow-y: auto; }}
.ac-item {{ padding: 8px 12px; cursor: pointer; color: #e8efe8; font-size: 0.9em; }}
.ac-item:hover, .ac-item.active {{ background: #2d5a38; }}
</style>
<script>
const GOLFERS = {names_json};
function setupAC(input) {{
  const wrap = input.parentElement;
  wrap.classList.add('ac-wrap');
  const dd = document.createElement('div');
  dd.className = 'ac-dropdown';
  wrap.appendChild(dd);
  let activeIdx = -1;
  function show(val) {{
    dd.innerHTML = ''; activeIdx = -1;
    if (!val) {{ dd.style.display = 'none'; return; }}
    const v = val.toLowerCase();
    const matches = GOLFERS.filter(n => n.toLowerCase().includes(v)).slice(0, 12);
    if (!matches.length) {{ dd.style.display = 'none'; return; }}
    matches.forEach((name, i) => {{
      const item = document.createElement('div');
      item.className = 'ac-item'; item.textContent = name;
      item.addEventListener('mousedown', e => {{ e.preventDefault(); input.value = name; dd.style.display = 'none'; }});
      dd.appendChild(item);
    }});
    dd.style.display = 'block';
  }}
  input.addEventListener('input', () => show(input.value));
  input.addEventListener('keydown', e => {{
    const items = dd.querySelectorAll('.ac-item');
    if (e.key === 'ArrowDown') {{ e.preventDefault(); activeIdx = Math.min(activeIdx+1, items.length-1); items.forEach((el,i) => el.classList.toggle('active', i===activeIdx)); }}
    else if (e.key === 'ArrowUp') {{ e.preventDefault(); activeIdx = Math.max(activeIdx-1, 0); items.forEach((el,i) => el.classList.toggle('active', i===activeIdx)); }}
    else if (e.key === 'Enter' && activeIdx >= 0) {{ e.preventDefault(); input.value = items[activeIdx].textContent; dd.style.display = 'none'; }}
    else if (e.key === 'Escape') {{ dd.style.display = 'none'; }}
  }});
  input.addEventListener('blur', () => setTimeout(() => {{ dd.style.display = 'none'; }}, 150));
}}
document.querySelectorAll('.golfer-input').forEach(setupAC);
</script>"""


def generate_entry_html(message='', error=False, player_names=None, past_names=None):
    cfg = load_tournament()
    now = datetime.now(tz=_EASTERN).strftime('%B %d, %Y at %I:%M %p ET')

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
            <input type="text" name="pick{i}" class="golfer-input" placeholder="Golfer name" autocomplete="off" required>
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
            </div>
        </div>
    </div>
    {_autocomplete_js(player_names)}
</body>
</html>"""


def generate_edit_html(participant, message='', error=False, player_names=None, cfg=None):
    if cfg is None:
        cfg = load_tournament()
    now = datetime.now(tz=_EASTERN).strftime('%B %d, %Y at %I:%M %p ET')

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
            <input type="text" name="pick{i}" class="golfer-input" value="{current}" autocomplete="off" required>
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
                    <h1>Edit Picks &mdash; {cfg['name']} {cfg['dates']}</h1>
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
            </div>
        </div>
    </div>
    {_autocomplete_js(player_names)}
</body>
</html>"""


# --- HTTP Server ---

class GolfPoolHandler(BaseHTTPRequestHandler):

    def _admin_authed(self):
        cookie_header = self.headers.get('Cookie', '')
        password = load_tournament()['admin_password']
        for part in cookie_header.split(';'):
            k, _, v = part.strip().partition('=')
            if k.strip() == 'admin_auth':
                try:
                    stored_pw, ts = v.strip().rsplit(':', 1)
                    age = datetime.now(timezone.utc).timestamp() - int(ts)
                    if stored_pw == password and age < 1800:  # 30 minutes
                        return True
                except (ValueError, TypeError):
                    pass
        return False

    def _require_admin(self):
        """Redirect to login if not authed. Returns True if auth passed."""
        if self._admin_authed():
            return True
        self.send_response(303)
        self.send_header('Location', '/admin/login')
        self.end_headers()
        return False

    def _serve_admin_login(self, error=False):
        err_html = '<div style="background:#4a1a1a;border:1px solid #9e4a4a;border-radius:8px;padding:12px 16px;margin-bottom:20px;color:#bf8a8a">✗ Incorrect password.</div>' if error else ''
        html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin Login — Kapelke Golf Pool</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Georgia','Times New Roman',serif; background:#0c1a0c; color:#e8efe8; display:flex; align-items:center; justify-content:center; min-height:100vh; }}
.box {{ background:#1a3320; border:1px solid #2d5a38; border-radius:12px; padding:36px 32px; width:100%; max-width:360px; }}
h1 {{ color:#e8d44d; font-size:1.4em; margin-bottom:24px; text-align:center; }}
label {{ display:block; font-size:0.9em; color:#8abf8a; margin-bottom:6px; }}
input[type=password] {{ width:100%; padding:10px 12px; background:#0f2615; border:1px solid #2d5a38; border-radius:6px; color:#e8efe8; font-size:1em; margin-bottom:20px; }}
input[type=password]:focus {{ outline:none; border-color:#4a9e5c; }}
button {{ width:100%; padding:12px; background:#2d7a3e; color:#e8efe8; border:none; border-radius:6px; font-size:1em; cursor:pointer; font-family:inherit; }}
button:hover {{ background:#3a9e50; }}
</style></head>
<body><div class="box">
<h1>⛳ Admin</h1>
{err_html}
<form method="POST" action="/admin/login">
  <label>Password</label>
  <input type="password" name="password" autofocus>
  <button type="submit">Enter</button>
</form>
</div></body></html>"""
        self._send_html(html)

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
        elif self.path == '/admin/login':
            self._serve_admin_login()
        elif self.path == '/admin/fetch-next':
            if not self._require_admin(): return
            self._handle_fetch_next()
        elif self.path.startswith('/admin'):
            if not self._require_admin(): return
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
        elif self.path == '/api/autolock':
            self._handle_autolock()
        elif self.path == '/admin/login':
            self._handle_admin_login()
        elif self.path == '/admin/setlock':
            if not self._require_admin(): return
            self._handle_admin_setlock()
        elif self.path == '/admin/update':
            if not self._require_admin(): return
            self._handle_admin_update()
        elif self.path == '/admin/store':
            if not self._require_admin(): return
            self._handle_admin_store()
        elif self.path == '/admin/reset':
            if not self._require_admin(): return
            self._handle_admin_reset()
        elif self.path == '/admin/reset-only':
            if not self._require_admin(): return
            self._handle_admin_reset_only()
        elif self.path == '/admin/load-next':
            if not self._require_admin(): return
            self._handle_load_next()
        elif self.path == '/admin/rename-participant':
            if not self._require_admin(): return
            self._handle_rename_participant()
        elif self.path == '/admin/delete-participant':
            if not self._require_admin(): return
            self._handle_delete_participant_history()
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
        # Use leaderboard players only if they match the configured tournament
        tournament, players = fetch_leaderboard()
        cfg_name = load_tournament().get('name', '').lower()
        lb_name = tournament.get('status', '').lower()
        name_words = [w for w in cfg_name.split() if len(w) > 3]
        lb_matches = any(w in lb_name for w in name_words)
        player_names = [p['name'] for p in players] if (players and lb_matches) else fetch_player_names()
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
        cfg = load_tournament()
        tournament, players = fetch_leaderboard()
        cfg_name = cfg.get('name', '').lower()
        lb_name = tournament.get('status', '').lower()
        name_words = [w for w in cfg_name.split() if len(w) > 3]
        lb_matches = any(w in lb_name for w in name_words)
        player_names = [p['name'] for p in players] if (players and lb_matches) else fetch_player_names()
        html = generate_edit_html(participant, message=message, error=error, player_names=player_names, cfg=cfg)
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

    def _handle_rename_participant(self):
        content_length = int(self.headers.get('Content-Length', 0))
        params = urllib.parse.parse_qs(self.rfile.read(content_length).decode('utf-8'))
        old_name = params.get('old_name', [''])[0].strip()
        new_name = params.get('new_name', [''])[0].strip()
        if not old_name or not new_name:
            self.send_response(303)
            self.send_header('Location', '/admin')
            self.end_headers()
            return
        # Rename in history
        history = load_history()
        for t in history:
            for r in t.get('results', []):
                if r['name'] == old_name:
                    r['name'] = new_name
        save_history(history)
        # Rename in current picks
        data = load_picks()
        for p in data.get('participants', []):
            if p['name'] == old_name:
                p['name'] = new_name
        save_picks(data)
        self.send_response(303)
        self.send_header('Location', '/admin?success=renamed')
        self.end_headers()

    def _handle_delete_participant_history(self):
        content_length = int(self.headers.get('Content-Length', 0))
        params = urllib.parse.parse_qs(self.rfile.read(content_length).decode('utf-8'))
        name = params.get('name', [''])[0].strip()
        if not name:
            self.send_response(303)
            self.send_header('Location', '/admin')
            self.end_headers()
            return
        # Remove from history
        history = load_history()
        for t in history:
            t['results'] = [r for r in t.get('results', []) if r['name'] != name]
        save_history(history)
        # Remove from current picks
        data = load_picks()
        data['participants'] = [p for p in data.get('participants', []) if p['name'] != name]
        save_picks(data)
        self.send_response(303)
        self.send_header('Location', '/admin?success=removed')
        self.end_headers()

    def _handle_admin_login(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        params = urllib.parse.parse_qs(body)
        password = params.get('password', [''])[0]
        if password != load_tournament()['admin_password']:
            self._serve_admin_login(error=True)
            return
        ts = int(datetime.now(timezone.utc).timestamp())
        self.send_response(303)
        self.send_header('Set-Cookie', f'admin_auth={password}:{ts}; Path=/; HttpOnly; SameSite=Strict; Max-Age=1800')
        self.send_header('Location', '/admin')
        self.end_headers()

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

    def _handle_autolock(self):
        self._serve_json({'success': False, 'msg': 'Autolock disabled — use admin lock/unlock'})

    def _handle_admin_setlock(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        params = urllib.parse.parse_qs(body)
        password = params.get('password', [''])[0]
        if password != load_tournament()['admin_password']:
            self.send_response(303)
            self.send_header('Location', '/admin?error=badpass')
            self.end_headers()
            return
        action = params.get('action', ['lock'])[0]
        data = load_picks()
        data['locked'] = (action == 'lock')
        save_picks(data)
        redir = '/admin?success=locked' if action == 'lock' else '/admin?success=unlocked'
        self.send_response(303)
        self.send_header('Location', redir)
        self.end_headers()

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
        elif '?success=stored' in path:
            msg = '<div style="background:#1a4a1a;border:1px solid #4a9e5c;border-radius:8px;padding:12px 16px;margin-bottom:20px;color:#8abf8a">✓ Results stored to career history. Picks unchanged.</div>'
        elif '?success=reset' in path:
            msg = '<div style="background:#1a4a1a;border:1px solid #4a9e5c;border-radius:8px;padding:12px 16px;margin-bottom:20px;color:#8abf8a">✓ Tournament archived and reset. Picks cleared.</div>'
        elif '?success=resetonly' in path:
            msg = '<div style="background:#1a4a1a;border:1px solid #4a9e5c;border-radius:8px;padding:12px 16px;margin-bottom:20px;color:#8abf8a">✓ Picks cleared. History unchanged.</div>'
        elif '?success=locked' in path:
            msg = '<div style="background:#1a4a1a;border:1px solid #4a9e5c;border-radius:8px;padding:12px 16px;margin-bottom:20px;color:#8abf8a">✓ Entries locked.</div>'
        elif '?success=unlocked' in path:
            msg = '<div style="background:#1a4a1a;border:1px solid #4a9e5c;border-radius:8px;padding:12px 16px;margin-bottom:20px;color:#8abf8a">✓ Entries unlocked.</div>'
        elif '?error=badpass' in path:
            msg = '<div style="background:#4a1a1a;border:1px solid #9e4a4a;border-radius:8px;padding:12px 16px;margin-bottom:20px;color:#bf8a8a">✗ Incorrect password.</div>'
        elif '?success=renamed' in path:
            msg = '<div style="background:#1a4a1a;border:1px solid #4a9e5c;border-radius:8px;padding:12px 16px;margin-bottom:20px;color:#8abf8a">✓ Participant renamed.</div>'
        elif '?success=removed' in path:
            msg = '<div style="background:#1a4a1a;border:1px solid #4a9e5c;border-radius:8px;padding:12px 16px;margin-bottom:20px;color:#8abf8a">✓ Participant removed from history.</div>'

        # Build participant management section
        all_names = sorted(_all_historical_names())
        # Also include current pool participants
        for p in participants:
            if p['name'] not in all_names:
                all_names.append(p['name'])
        all_names = sorted(set(all_names))
        participant_rows = ''
        for name in all_names:
            participant_rows += f"""
        <tr>
          <td style="padding:8px 10px;color:#e8efe8">{name}</td>
          <td style="padding:8px 10px">
            <form method="POST" action="/admin/rename-participant" style="display:inline-flex;gap:6px;align-items:center">
              <input type="hidden" name="old_name" value="{name}">
              <input type="text" name="new_name" placeholder="New name" style="width:140px;padding:5px 8px;background:#0f2615;border:1px solid #2d5a38;border-radius:4px;color:#e8efe8;font-size:0.85em">
              <button type="submit" style="padding:5px 10px;background:#2d5a38;color:#e8efe8;border:none;border-radius:4px;cursor:pointer;font-size:0.85em">Rename</button>
            </form>
          </td>
          <td style="padding:8px 10px">
            <form method="POST" action="/admin/delete-participant" onsubmit="return confirm('Remove {name} from history?')">
              <input type="hidden" name="name" value="{name}">
              <button type="submit" style="padding:5px 10px;background:#4a1a1a;color:#e87a5c;border:1px solid #7a3030;border-radius:4px;cursor:pointer;font-size:0.85em">Delete</button>
            </form>
          </td>
        </tr>"""
        participant_mgmt_block = f"""
        <div class="card">
          <h2>Participant Names</h2>
          <div style="font-size:0.85em;color:#6b7280;margin-bottom:14px">Rename or remove participants from history and the name dropdown.</div>
          {'<table style="width:100%;border-collapse:collapse"><tbody>' + participant_rows + '</tbody></table>' if all_names else '<div style="color:#6b7280;font-size:0.9em">No participants in history.</div>'}
        </div>"""

        # Build pick archive section
        history = load_history()
        archive_html = ''
        for t in reversed(history):
            results = t.get('results', [])
            if not any(r.get('picks') for r in results):
                continue  # skip old entries with no picks
            rows = ''
            for r in results:
                picks_str = ', '.join(r.get('picks', [])) or '—'
                rows += f'<tr><td style="padding:6px 10px;color:#e8d44d;white-space:nowrap">{r["place"]}</td><td style="padding:6px 10px;color:#e8efe8">{r["name"]}</td><td style="padding:6px 10px;color:#8abf8a;font-size:0.85em">{picks_str}</td></tr>'
            archive_html += f"""
        <div style="margin-bottom:20px">
          <div style="font-weight:700;color:#e8d44d;margin-bottom:8px">{t['tournament']} — {t.get('dates','')} {t.get('year','')}</div>
          <table style="width:100%;border-collapse:collapse;font-size:0.88em">
            <thead><tr>
              <th style="padding:6px 10px;color:#8abf8a;text-align:left;border-bottom:1px solid #2d5a38">Place</th>
              <th style="padding:6px 10px;color:#8abf8a;text-align:left;border-bottom:1px solid #2d5a38">Name</th>
              <th style="padding:6px 10px;color:#8abf8a;text-align:left;border-bottom:1px solid #2d5a38">Picks</th>
            </tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""
        pick_archive_block = f"""
        <div class="card">
          <h2>Pick Archive</h2>
          <div style="font-size:0.85em;color:#6b7280;margin-bottom:16px">All participant picks by tournament — for predictor engine use.</div>
          {archive_html if archive_html else '<div style="color:#6b7280;font-size:0.9em">No archived picks yet. Picks are saved when you Archive &amp; Reset at end of tournament.</div>'}
        </div>"""

        counts = cfg.get('counts_for_career', True)
        if counts:
            eot_block = """
        <div style="font-size:0.85em;color:#6b7280;margin-bottom:14px;line-height:1.5">
            Saves final standings to career history and clears all picks.
        </div>
        <form method="POST" action="/admin/reset">
            <button type="submit" class="btn btn-red">Archive &amp; Reset</button>
        </form>"""
        else:
            eot_block = """
        <div style="font-size:0.85em;color:#6b7280;margin-bottom:14px;line-height:1.5">
            Clears all picks and unlocks entries for the next tournament.
        </div>
        <form method="POST" action="/admin/reset-only">
            <button type="submit" class="btn btn-red">Reset Picks</button>
        </form>"""

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
        <div style="margin-bottom:16px">
            <form method="POST" action="/admin/load-next" style="display:inline">
                <button type="submit" class="btn btn-green" style="margin-top:0">⬇ Load Next Tournament from PGA Tour</button>
            </form>
            <div style="font-size:0.82em;color:#6b7280;margin-top:6px">Fetches name, dates, course &amp; ID from PGA Tour and saves immediately.</div>
        </div>
        <form method="POST" action="/admin/update" id="settings-form">
            <label>Tournament Name</label>
            <input type="text" name="name" id="f-name" value="{cfg['name']}" required>
            <label>Dates (e.g. Apr 10–13, 2026)</label>
            <input type="text" name="dates" id="f-dates" value="{cfg['dates']}">
            <label>Course</label>
            <input type="text" name="course" id="f-course" value="{cfg['course']}">
            <label>PGA Tour ID (e.g. R2026007)</label>
            <input type="text" name="pga_tour_id" id="f-pga-id" value="{cfg['pga_tour_id']}">
            <label>Entry Fee ($)</label>
            <input type="number" name="entry_fee" value="{cfg['entry_fee']}" min="1" required>
            <label>New Admin Password (leave blank to keep current)</label>
            <input type="password" name="new_password" placeholder="Leave blank to keep current">
            <label style="display:flex;align-items:center;gap:10px;cursor:pointer;margin-top:14px">
                <input type="checkbox" name="show_medals" value="1" {'checked' if cfg.get('show_medals') else ''} style="width:auto;margin:0">
                Show medals 🥇🥈 (enable only when tournament is fully complete)
            </label>
            <label style="display:flex;align-items:center;gap:10px;cursor:pointer;margin-top:10px">
                <input type="checkbox" name="counts_for_career" value="1" {'checked' if cfg.get('counts_for_career', True) else ''} style="width:auto;margin:0">
                Counts toward career earnings (uncheck for non-counting tournaments)
            </label>
            <label>Current Password (required)</label>
            <input type="password" name="password" required>
            <button type="submit" class="btn btn-green">Save Settings</button>
        </form>
    </div>

    <!-- Status + Lock -->
    <div class="card">
        <h2>Tournament Status</h2>
        <div class="status-row"><span>Participants</span><span>{len(participants)}</span></div>
        <div class="status-row"><span>Prize Pool</span><span style="color:#e8d44d">${total_pot}</span></div>
        <div class="status-row">
            <span>Entry Status</span>
            <span>{'&#x1F512; Locked' if locked else '&#x1F7E2; Open'}</span>
        </div>
        <form method="POST" action="/admin/setlock" style="margin-top:16px">
            <label>Admin Password (required to change)</label>
            <input type="password" name="password" required>
            <div style="display:flex;gap:10px;margin-top:12px">
                <button type="submit" name="action" value="lock" class="btn btn-red" style="margin-top:0" {'disabled style="opacity:0.4;cursor:not-allowed;margin-top:0"' if locked else ''}>&#x1F512; Lock</button>
                <button type="submit" name="action" value="unlock" class="btn btn-green" style="margin-top:0" {'disabled style="opacity:0.4;cursor:not-allowed;margin-top:0"' if not locked else ''}>&#x1F513; Unlock</button>
            </div>
        </form>
    </div>

    {participant_mgmt_block}

    {pick_archive_block}

    <!-- End of Tournament -->
    <div class="card" id="eot-section">
        <h2>End of Tournament</h2>
        {eot_block}</div>
</div>
<script>
function loadNextTournament() {{
    var status = document.getElementById('fetch-status');
    status.style.display = 'block';
    status.textContent = 'Fetching next tournament...';
    status.style.color = '#8abf8a';
    fetch('/admin/fetch-next')
        .then(function(r) {{ return r.json(); }})
        .then(function(data) {{
            if (!data.ok) {{
                status.style.color = '#e87a5c';
                status.textContent = 'Error: ' + data.error;
                return;
            }}
            var t = data.tournament;
            document.getElementById('f-name').value    = t.name;
            document.getElementById('f-dates').value   = t.dates;
            document.getElementById('f-course').value  = t.course;
            document.getElementById('f-pga-id').value  = t.pga_tour_id;
            status.style.color = '#8abf8a';
            status.textContent = '✓ Loaded: ' + t.name + ' — ' + t.course + ' (' + t.dates + '). Review and save.';

            if (data.participant_count > 0) {{
                var warn = document.createElement('div');
                warn.id = 'picks-warning';
                warn.style.cssText = 'margin-top:12px;padding:12px 14px;background:#2a1a0a;border:1px solid #7a4a1a;border-radius:8px;font-size:0.85em;color:#c8a06a;line-height:1.6';
                warn.innerHTML = '&#9888; <b>' + data.participant_count + ' participant' + (data.participant_count > 1 ? 's' : '') + '</b> from the previous tournament are still loaded. Scroll down to <a href="#eot" onclick="document.getElementById(\'eot-section\').scrollIntoView({{behavior:\'smooth\'}});return false;" style="color:#e8d44d;font-weight:700">End of Tournament</a> to reset before opening entries.';
                var existing = document.getElementById('picks-warning');
                if (existing) existing.remove();
                document.getElementById('fetch-status').after(warn);
            }}
        }})
        .catch(function(e) {{
            status.style.color = '#e87a5c';
            status.textContent = 'Error fetching tournament: ' + e;
        }});
}}
</script>
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
        cfg['show_medals']       = '1' in params.get('show_medals', [])
        cfg['counts_for_career'] = '1' in params.get('counts_for_career', [])
        new_pw = params.get('new_password', [''])[0].strip()
        if new_pw:
            cfg['admin_password'] = new_pw
        save_tournament(cfg)
        self.send_response(303)
        self.send_header('Location', '/admin?success=updated')
        self.end_headers()

    def _handle_admin_store(self):
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
        if cfg.get('counts_for_career', True):
            picks_data = load_picks()
            tournament, players = fetch_leaderboard()
            standings = calculate_standings(picks_data.get('participants', []), players)
            picks_by_name = {p['name']: p['picks'] for p in picks_data.get('participants', [])}
            history = load_history()
            history.append({
                'tournament': cfg['name'],
                'dates':      cfg['dates'],
                'year':       datetime.now().year,
                'results':    [{'name': s['name'], 'place': s['place'], 'prize': s['prize'],
                                'picks': picks_by_name.get(s['name'], [])}
                               for s in standings],
            })
            save_history(history)
        self.send_response(303)
        self.send_header('Location', '/admin?success=stored')
        self.end_headers()

    def _handle_admin_reset(self):
        cfg = load_tournament()
        # Archive current standings (only if counts_for_career)
        if cfg.get('counts_for_career', True):
            try:
                picks_data = load_picks()
                tournament, players = fetch_leaderboard()
                standings = calculate_standings(picks_data.get('participants', []), players)
                picks_by_name = {p['name']: p['picks'] for p in picks_data.get('participants', [])}
                history = load_history()
                history.append({
                    'tournament': cfg['name'],
                    'dates':      cfg['dates'],
                    'year':       datetime.now().year,
                    'results':    [{'name': s['name'], 'place': s['place'], 'prize': s['prize'],
                                    'picks': picks_by_name.get(s['name'], [])}
                                   for s in standings],
                })
                save_history(history)
            except Exception as e:
                print(f"  Archive skipped (leaderboard unavailable): {e}")
        # Reset picks
        save_picks({'entry_fee': cfg['entry_fee'], 'locked': False, 'participants': []})
        self.send_response(303)
        self.send_header('Location', '/admin?success=reset')
        self.end_headers()

    def _handle_admin_reset_only(self):
        """Clear picks for the current tournament without archiving to history."""
        cfg = load_tournament()
        save_picks({'entry_fee': cfg['entry_fee'], 'locked': False, 'participants': []})
        self.send_response(303)
        self.send_header('Location', '/admin?success=resetonly')
        self.end_headers()

    def _handle_fetch_next(self):
        """Return next upcoming PGA Tour tournament info as JSON."""
        try:
            info = fetch_next_tournament()
            if info:
                picks_data = load_picks()
                participant_count = len(picks_data.get('participants', []))
                self._serve_json({'ok': True, 'tournament': info, 'participant_count': participant_count})
            else:
                self._serve_json({'ok': False, 'error': 'No upcoming tournaments found'})
        except Exception as e:
            self._serve_json({'ok': False, 'error': str(e)})

    def _handle_load_next(self):
        """Fetch next tournament from PGA Tour and save it directly."""
        try:
            info = fetch_next_tournament()
            if not info:
                self.send_response(303)
                self.send_header('Location', '/admin?error=notfound')
                self.end_headers()
                return
            cfg = load_tournament()
            cfg['name']        = info['name']
            cfg['dates']       = info['dates']
            cfg['course']      = info['course']
            cfg['pga_tour_id'] = info['pga_tour_id']
            cfg['show_medals'] = False
            save_tournament(cfg)
            self.send_response(303)
            self.send_header('Location', '/admin?success=updated')
            self.end_headers()
        except Exception as e:
            self.send_response(303)
            self.send_header('Location', f'/admin?error=fetch')
            self.end_headers()

    def _serve_entry_form_redirect(self, message, error=False):
        tournament, players = fetch_leaderboard()
        cfg_name = load_tournament().get('name', '').lower()
        lb_name = tournament.get('status', '').lower()
        name_words = [w for w in cfg_name.split() if len(w) > 3]
        lb_matches = any(w in lb_name for w in name_words)
        player_names = [p['name'] for p in players] if (players and lb_matches) else fetch_player_names()
        past_names = _all_historical_names()
        html = generate_entry_html(message=message, error=error, player_names=player_names, past_names=past_names)
        self._send_html(html)

    def _send_html(self, html):
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(body)

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
