"""Rebuild the demo CSVs from the frozen public ESPN responses (Python 3.10+).

No third-party Python dependencies. Run from any directory: python build_data.py
To fetch fresh responses first: python build_data.py --refresh --as-of YYYY-MM-DD
Current-season scoring always ends at the END of --as-of in America/New_York.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent

DATA_DIR = PROJECT_ROOT / "data"
AUDIT_DIR = ROOT / "audit"

DATA_DIR.mkdir(exist_ok=True)
AUDIT_DIR.mkdir(exist_ok=True)

YEARS = [2016, 2017, 2018, 2019, 2021, 2022, 2023, 2024, 2025, 2026]
RULES = {2016:(10,20,4),2017:(10,24,4),2018:(9,24,4),2019:(9,24,4),
         2021:(10,24,6),2022:(12,22,6),2023:(12,22,6),2024:(14,26,8),
         2025:(14,26,8),2026:(16,30,8)}
CHECKPOINTS = [0.4, 0.5, 0.6, 0.7, 0.8]
FEATURES = ['season_pct','points_per_game','win_pct','goal_diff_per_game',
            'last_5_points','last_5_goal_diff','points_rank_pct',
            'points_gap_to_cutline','playoff_share']
MATCH_URL = 'https://site.api.espn.com/apis/site/v2/sports/soccer/usa.nwsl/scoreboard?dates={year}&limit=1000'
STANDINGS_URL = 'https://site.web.api.espn.com/apis/v2/sports/soccer/usa.nwsl/standings?season={year}'


def write_csv(name, rows, fields=None, directory=ROOT):
    if fields is None:
        fields = list(rows[0])
    with (directory/name).open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_json(name):
    return json.loads((ROOT/'raw'/name).read_text(encoding='utf-8'))


def team_name(year, team_id, source_name):
    # ESPN backfills modern club names into historical events.
    if team_id == '15366' and year == 2016:
        return 'Western New York Flash'
    if team_id == '15364' and year <= 2019:
        return 'Sky Blue FC'
    if team_id == '15360' and year <= 2024:
        return 'Chicago Red Stars'
    if team_id == '15363':
        if year == 2019:
            return 'Reign FC'
        if 2021 <= year <= 2023:
            return 'OL Reign'
    if team_id == '20907' and year == 2021:
        return 'Kansas City NWSL'
    return source_name


def normalize():
    overrides = json.loads((ROOT/'source_corrections.json').read_text())
    fixes = {x['match_id']: x for x in overrides['matches']}
    rows, excluded, manifests = [], [], []
    for y in YEARS:
        for kind, template in [('espn', MATCH_URL), ('standings', STANDINGS_URL)]:
            file = ROOT/'raw'/f'{kind}_{y}.json'
            manifests.append(dict(season=y,kind=kind,source_url=template.format(year=y),
                local_file=str(file.relative_to(ROOT)),sha256=hashlib.sha256(file.read_bytes()).hexdigest(),
                retrieved_utc=read_json('retrieval_metadata.json').get(file.name, '2026-09-02')))
        events = read_json(f'espn_{y}.json')['events']
        assert len(events) < 1000, 'Feed limit reached; pagination required.'
        seen = set()
        for e in events:
            assert e['id'] not in seen, (y, e['id'])
            seen.add(e['id'])
            c = e['competitions'][0]
            home = next(x for x in c['competitors'] if x['homeAway']=='home')
            away = next(x for x in c['competitors'] if x['homeAway']=='away')
            t = dt.datetime.fromisoformat(e['date'].replace('Z','+00:00'))
            day = t.astimezone(ZoneInfo('America/New_York')).date().isoformat()
            status = e['status']['type']
            finished = status['completed']
            regular = e['season']['slug'].startswith('regular')
            r = dict(season=y,match_id=e['id'],stage=e['season']['slug'],
                match_date=day,result_available_date=day,source_kickoff_utc=e['date'],
                home_team_id=home['id'],home_team=team_name(y,home['id'],home['team']['displayName']),
                away_team_id=away['id'],away_team=team_name(y,away['id'],away['team']['displayName']),
                home_goals=int(home['score']) if finished else None,
                away_goals=int(away['score']) if finished else None,
                completed=finished,source_status=status['name'],
                source_url=f"https://www.espn.com/soccer/match/_/gameId/{e['id']}",
                correction_source='',correction_note='')
            if e['id'] in fixes:
                x = fixes[e['id']]
                r.update(x['corrected_values'])
                r['correction_source'] = x['source_url']
                r['correction_note'] = x['reason']
            if not r['completed'] and status['name'] != 'STATUS_SCHEDULED':
                excluded.append(dict(season=y,match_id=e['id'],stage=e['season']['slug'],
                    original_date=e['date'],reason='Uncompleted postponed record; excluded to avoid duplicate counting.',
                    source_url=r['source_url']))
                continue
            r['is_regular_season'] = regular
            rows.append(r)
    write_csv('source_manifest.csv', manifests, directory=AUDIT_DIR)
    write_csv('excluded_records.csv', excluded, directory=AUDIT_DIR)
    write_csv(
        'matches_clean.csv',
        sorted(rows, key=lambda r:(r['season'],r['match_date'],r['match_id'])),
        directory=AUDIT_DIR,
    )
    return rows


def points_deduction(y, team_id, day):
    return -3 if y == 2024 and team_id == '21422' and day >= '2024-10-03' else 0


def table_at(matches, y, day, teams):
    histories = {tid: [] for tid in teams}
    for m in sorted(matches,key=lambda r:(r['match_date'],r['match_id'])):
        if m['season'] != y or not m['is_regular_season'] or not m['completed']:
            continue
        if m['result_available_date'] > day or m['match_date'] > day:
            continue
        for side,other in [('home','away'),('away','home')]:
            gf,ga = m[f'{side}_goals'],m[f'{other}_goals']
            histories[m[f'{side}_team_id']].append(dict(gf=gf,ga=ga,pts=3*(gf>ga)+(gf==ga),
                win=int(gf>ga),draw=int(gf==ga),loss=int(gf<ga),date=m['match_date']))
    table = []
    for tid,name in teams.items():
        h = histories[tid]
        n = len(h)
        if not n:
            raise ValueError(f'No games for {tid} at {day}')
        gf,ga = sum(x['gf'] for x in h),sum(x['ga'] for x in h)
        points_raw = sum(x['pts'] for x in h)
        adj = points_deduction(y,tid,day)
        r = dict(team_id=tid,team=name,games_played=n,wins=sum(x['win'] for x in h),
                 draws=sum(x['draw'] for x in h),losses=sum(x['loss'] for x in h),
                 goals_for=gf,goals_against=ga,goal_diff=gf-ga,
                 match_points=points_raw,points_adjustment=adj,points=points_raw+adj,
                 last_5_points=sum(x['pts'] for x in h[-5:]),
                 last_5_goal_diff=sum(x['gf']-x['ga'] for x in h[-5:]),
                 last_5_games=len(h[-5:]),last_match_date=h[-1]['date'])
        table.append(r)
    return table


def snapshot(matches, y, day, checkpoint, teams, qualified):
    nteams, scheduled, spots = RULES[y]
    table = table_at(matches,y,day,teams)
    cutoff = sorted([r['points'] for r in table],reverse=True)[spots-1]
    # A transparent comparison rule, not a reconstruction of official tiebreakers.
    baseline_order = sorted(table,key=lambda r:(-r['points'],-r['goal_diff'],-r['goals_for'],r['team_id']))
    baseline_ids = {r['team_id'] for r in baseline_order[:spots]}
    out = []
    for r in table:
        better = sum(x['points']>r['points'] for x in table)
        equal = sum(x['points']==r['points'] for x in table)
        rank_pct = (better+(equal-1)/2)/(nteams-1)
        label = ('Yes' if r['team_id'] in qualified else 'No') if qualified is not None else None
        out.append(dict(season=y,team_id=r['team_id'],team=r['team'],
            team_season_id=f"{y}_{r['team_id']}",snapshot_date=day,checkpoint_pct=checkpoint,
            dataset_role='score' if y==2026 else ('test' if y==2025 else 'train'),
            made_playoffs=label,season_pct=r['games_played']/scheduled,
            points_per_game=r['points']/r['games_played'],win_pct=r['wins']/r['games_played'],
            goal_diff_per_game=r['goal_diff']/r['games_played'],
            last_5_points=r['last_5_points'],last_5_goal_diff=r['last_5_goal_diff'],
            points_rank_pct=rank_pct,points_gap_to_cutline=r['points']-cutoff,playoff_share=spots/nteams,
            games_played=r['games_played'],scheduled_games=scheduled,
            games_remaining=scheduled-r['games_played'],league_teams=nteams,playoff_spots=spots,
            points=r['points'],match_points=r['match_points'],points_adjustment=r['points_adjustment'],
            wins=r['wins'],draws=r['draws'],losses=r['losses'],goals_for=r['goals_for'],
            goals_against=r['goals_against'],goal_diff=r['goal_diff'],cutline_points=cutoff,
            last_5_games=r['last_5_games'],last_match_date=r['last_match_date'],
            baseline_top_k='Yes' if r['team_id'] in baseline_ids else 'No',
            source_url=MATCH_URL.format(year=y)))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--as-of',default='2026-09-01',help='Inclusive Eastern date (default: last complete day before retrieval).')
    parser.add_argument('--refresh',action='store_true')
    args=parser.parse_args()
    dt.date.fromisoformat(args.as_of)
    assert args.as_of.startswith('2026-'), 'This bundle supports 2026 scoring only.'
    if args.refresh:
        metadata = {}
        for y in YEARS:
            for kind,url in [('espn',MATCH_URL),('standings',STANDINGS_URL)]:
                name=f'{kind}_{y}.json'
                with urllib.request.urlopen(url.format(year=y),timeout=60) as response:
                    (ROOT/'raw'/name).write_bytes(response.read())
                metadata[name]=dt.datetime.now(dt.timezone.utc).isoformat()
        (ROOT/'raw'/'retrieval_metadata.json').write_text(json.dumps(metadata,indent=2))
    matches=normalize()
    all_snapshots, current, labels, coverage, qa, standings_clean = [], [], [], [], [], []
    for y in YEARS:
        expected_teams,expected_games,spots=RULES[y]
        ss=read_json(f'standings_{y}.json')['children'][0]['standings']['entries']
        teams={e['team']['id']:team_name(y,e['team']['id'],e['team']['displayName']) for e in ss}
        assert len(teams)==expected_teams,(y,'team count')
        played=[m for m in matches if m['season']==y and m['is_regular_season'] and m['completed']]
        playoff=[m for m in matches if m['season']==y and not m['is_regular_season'] and m['completed']]
        qualified={m[f'{side}_team_id'] for m in playoff for side in ['home','away']} if y<2026 else None
        if qualified is not None:
            assert len(qualified)==spots,(y,'playoff participants')
            assert len(playoff)==spots-1,(y,'playoff match count')
            assert len(played)==expected_teams*expected_games//2,(y,'regular season count')
            assert qualified=={e['team']['id'] for e in ss[:spots]},(y,'playoff labels vs standings')
            for tid,name in teams.items():
                evidence=next((m['source_url'] for m in playoff if tid in [m['home_team_id'],m['away_team_id']]),STANDINGS_URL.format(year=y))
                labels.append(dict(season=y,team_id=tid,team=name,made_playoffs='Yes' if tid in qualified else 'No',source_url=evidence))
        last_day=max(m['result_available_date'] for m in played)
        final_table=table_at(matches,y,last_day,teams)
        lookup={r['team_id']:r for r in final_table}
        mapping={'gamesPlayed':'games_played','points':'points','wins':'wins','ties':'draws',
                 'losses':'losses','pointsFor':'goals_for','pointsAgainst':'goals_against'}
        for e in ss:
            tid=e['team']['id']
            stats={s['name']:s.get('value') for s in e['stats']}
            rank=next(i+1 for i,x in enumerate(ss) if x['team']['id']==tid)
            standings_clean.append(dict(season=y,team_id=tid,team=teams[tid],source_rank=rank,
                **{v:stats[k] for k,v in mapping.items()},source_url=STANDINGS_URL.format(year=y)))
            for sk,ck in mapping.items():
                expected,actual=stats[sk],lookup[tid][ck]
                qa.append(dict(season=y,team_id=tid,team=teams[tid],metric=ck,source_value=expected,
                    reconstructed_value=actual,passed=expected==actual))
        if y<2026:
            for pct in CHECKPOINTS:
                threshold=math.ceil(expected_teams*expected_games/2*pct)
                dates=sorted(m['result_available_date'] for m in played)
                day=dates[threshold-1]
                all_snapshots.extend(snapshot(matches,y,day,pct,teams,qualified))
        else:
            current=snapshot(matches,y,args.as_of,None,teams,None)
        included=[m for m in played if y<2026 or m['result_available_date']<=args.as_of]
        counts=collections.Counter(tid for m in included for tid in [m['home_team_id'],m['away_team_id']])
        coverage.append(dict(season=y,league_teams=expected_teams,scheduled_games_per_team=expected_games,
            playoff_spots=spots,regular_matches_in_dataset=len(included),playoff_matches_for_labels=len(playoff),
            min_games_per_team=min(counts.values()),max_games_per_team=max(counts.values()),
            latest_result_date=max(m['result_available_date'] for m in included),
            snapshot_rows=expected_teams*5 if y<2026 else expected_teams,
            role='score' if y==2026 else ('test' if y==2025 else 'train')))
    write_csv(
        "standings_reconciliation.csv",
        qa,
        directory=AUDIT_DIR,
    )

    assert all(r["passed"] for r in qa), (
        "Standings mismatch: inspect "
        "data-prep/audit/standings_reconciliation.csv before use."
    )
    assert len({(r['team_season_id'],r['checkpoint_pct']) for r in all_snapshots})==len(all_snapshots)
    assert all(r['last_match_date']<=r['snapshot_date'] for r in all_snapshots+current)
    assert all(r['last_5_games']==5 for r in all_snapshots+current)
    assert all(r['wins']+r['draws']+r['losses']==r['games_played'] for r in all_snapshots+current)
    assert all(r['points']==r['wins']*3+r['draws']+r['points_adjustment'] for r in all_snapshots+current)
    assert all(r['made_playoffs'] is None for r in current)
    assert all(all(isinstance(r[c],(float,int)) and math.isfinite(r[c]) for c in FEATURES) for r in all_snapshots+current)
    # Modeling datasets used by the R demo
    write_csv(
        "training_data.csv",
        [r for r in all_snapshots if r["dataset_role"] == "train"],
        directory=DATA_DIR,
    )

    write_csv(
        "test_data.csv",
        [r for r in all_snapshots if r["dataset_role"] == "test"],
        directory=DATA_DIR,
    )

    write_csv(
        "current_2026.csv",
        current,
        directory=DATA_DIR,
    )

    write_csv(
        "playoff_labels.csv",
        labels,
        directory=AUDIT_DIR,
    )

    write_csv(
        "season_coverage.csv",
        coverage,
        directory=AUDIT_DIR,
    )

    write_csv(
        "source_standings.csv",
        standings_clean,
        directory=AUDIT_DIR,
    )

    (ROOT / "features.json").write_text(
        json.dumps(FEATURES, indent=2) + "\n"
    )

    summary = dict(
        as_of_eastern=args.as_of,
        historical_rows=len(all_snapshots),
        distinct_historical_team_seasons=len(labels),
        train_rows=sum(r["dataset_role"] == "train" for r in all_snapshots),
        test_rows=sum(r["dataset_role"] == "test" for r in all_snapshots),
        current_rows=len(current),
        current_completed_matches=coverage[-1]["regular_matches_in_dataset"],
        reconciliation_checks=len(qa),
        all_checks_passed=True,
    )

    (AUDIT_DIR / "validation_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )

    print(json.dumps(summary, indent=2))


if __name__=='__main__':
    main()
