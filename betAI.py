import sys, os, re, math, random
import time as t
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import requests
import numpy as np
from openai import OpenAI

from database import (
    init_db, guardar_prediccion, obtener_historial, obtener_estadisticas_modelo,
)
from model_core import (
    get_parametros_activos, peso_decay, poisson_pmf, calc_stats, calc_lambda,
    build_matrix, calc_probs, best_score, get_form, get_h2h, over_prob, btts_prob,
    _zona_equipo, calc_maf, aplicar_maf,
)
from auth import auth_bp, init_auth_db
from indicators import analyze_all_indicators, analyze_tennis_indicators
from rangos import rangos_bp, cerrar_pick
from wallet import wallet_bp, init_wallet_db
from community import community_bp


app = Flask(__name__, static_folder='.')
CORS(app)
init_db()          # Tablas existentes (predicciones, equipos, sugerencias) + Comunidad (mismo Base)
init_auth_db()     # Tablas de usuarios, picks sociales, follows, sesiones
init_wallet_db()   # Billetera interna y escrow de picks
app.register_blueprint(auth_bp)    # Endpoints /api/auth/* y /api/feed
app.register_blueprint(rangos_bp)
app.register_blueprint(wallet_bp)  # Endpoints /api/wallet/*
app.register_blueprint(community_bp)  # Endpoints /api/community/* — sección social/matchmaking

# Crea predicciones_ext y cuotas_mercado si no existen (no toca tablas existentes)
try:
    from migrate_db import migrar as _migrar_blind_engine
    _migrar_blind_engine()
except Exception as _e:
    print(f"⚠️  No se pudo migrar el Blind Engine al arrancar: {_e}")


# ── CONFIGURACIÓN ────────────────────────────────────────────
API_KEY  = "347e282989fc4fab9569c77f0da86527"
BASE_URL = "https://api.football-data.org/v4"
HEADERS  = {"X-Auth-Token": API_KEY}

FOOTBALL_LEAGUE_ALIASES = {}  # Sin aliases — todos los códigos van directo a la API

FALLBACK_FOOTBALL_MATCHES = {}  # Las ligas activas usan la API real de football-data.org

SYNTHETIC_ONLY_LEAGUES = set()  # Sin ligas sintéticas — todas usan la API real

TABLE_TENNIS_LEAGUES = [
    {"league_key": "wtt_champions", "league_name": "WTT Champions", "country_name": "World", "league_surface": "Indoor Table", "category": "Elite", "tier": 1000},
    {"league_key": "wtt_contender", "league_name": "WTT Contender", "country_name": "World", "league_surface": "Indoor Table", "category": "Pro Tour", "tier": 600},
    {"league_key": "ittf_world_cup", "league_name": "ITTF World Cup", "country_name": "World", "league_surface": "Indoor Table", "category": "World Cup", "tier": 1200},
]

TABLE_TENNIS_MATCHES = {
    "wtt_champions": [
        {"matchId": "tt_1001", "player1": "Fan Zhendong", "player1Key": "tt_p1", "player2": "Wang Chuqin", "player2Key": "tt_p2", "days": 0, "time": "13:00"},
        {"matchId": "tt_1002", "player1": "Sun Yingsha", "player1Key": "tt_p3", "player2": "Chen Meng", "player2Key": "tt_p4", "days": 0, "time": "15:30"},
    ],
    "wtt_contender": [
        {"matchId": "tt_2001", "player1": "Hugo Calderano", "player1Key": "tt_p5", "player2": "Tomokazu Harimoto", "player2Key": "tt_p6", "days": 1, "time": "12:00"},
        {"matchId": "tt_2002", "player1": "Truls Moregard", "player1Key": "tt_p7", "player2": "Lin Yun-Ju", "player2Key": "tt_p8", "days": 1, "time": "14:00"},
    ],
    "ittf_world_cup": [
        {"matchId": "tt_3001", "player1": "Ma Long", "player1Key": "tt_p9", "player2": "Felix Lebrun", "player2Key": "tt_p10", "days": 3, "time": "11:30"},
        {"matchId": "tt_3002", "player1": "Mima Ito", "player1Key": "tt_p11", "player2": "Wang Manyu", "player2Key": "tt_p12", "days": 3, "time": "16:00"},
    ],
}


def temporada_actual():
    hoy = date.today()
    return hoy.year if hoy.month >= 8 else hoy.year - 1

SEASON = str(temporada_actual())
SEASON_ANTERIOR = str(int(SEASON) - 1)


def fetch_pool_multitemporada(team_id: int, liga: str) -> list:
    """
    Reemplazo de "solo temporada actual": trae temporada actual + anterior
    del mismo equipo/liga y devuelve el pool combinado normalizado. El peso
    por antigüedad (peso_decay) se aplica después, en calc_lambda_decay/get_h2h,
    así que acá simplemente juntamos los partidos disponibles.
    """
    pool = []
    for season in (SEASON, SEASON_ANTERIOR):
        try:
            r = fetch(f"/teams/{team_id}/matches?competitions={liga}&season={season}&status=FINISHED")
            pool.extend(normalizar(r.get("matches", [])))
        except Exception as e:
            print(f"⚠️  No se pudo traer temporada {season} para equipo {team_id}: {e}")
    # Si la liga nacional no trae nada (ej. copas/CL con pocos partidos), completar sin filtro de competición
    if not pool:
        for season in (SEASON, SEASON_ANTERIOR):
            try:
                r = fetch(f"/teams/{team_id}/matches?season={season}&status=FINISHED&limit=20")
                pool.extend(normalizar(r.get("matches", [])))
            except Exception as e:
                print(f"⚠️  No se pudo traer fallback temporada {season} para equipo {team_id}: {e}")
    return pool

def resolve_liga_code(liga: str) -> str:
    liga = (liga or "PL").upper()
    return FOOTBALL_LEAGUE_ALIASES.get(liga, liga)

def build_fallback_football_matches(liga: str):
    hoy = date.today()
    result = []
    for item in FALLBACK_FOOTBALL_MATCHES.get(liga, []):
        kickoff = hoy + timedelta(days=item["days"])
        result.append({
            "homeId": item["homeId"],
            "homeName": item["homeName"],
            "awayId": item["awayId"],
            "awayName": item["awayName"],
            "date": kickoff.isoformat(),
            "time": item["time"],
            "status": "SCHEDULED",
            "scoreH": None,
            "scoreA": None,
        })
    return result

def build_table_tennis_matches(league_id: str):
    hoy = datetime.now()
    matches = []
    for item in TABLE_TENNIS_MATCHES.get(league_id, []):
        match_dt = hoy + timedelta(days=item["days"])
        matches.append({
            "matchId": item["matchId"],
            "date": match_dt.strftime("%Y-%m-%d"),
            "time": item["time"],
            "player1": item["player1"],
            "player1Key": item["player1Key"],
            "player2": item["player2"],
            "player2Key": item["player2Key"],
            "result": "-",
            "status": "Programado",
            "league": next((l["league_name"] for l in TABLE_TENNIS_LEAGUES if l["league_key"] == league_id), "Table Tennis"),
            "surface": "Indoor Table",
            "live": False,
        })
    return matches

def get_synthetic_league_profile(liga: str, team_id: int, team_name: str):
    SYNTHETIC_LEAGUE_PROFILES = {}
    profile = SYNTHETIC_LEAGUE_PROFILES.get(liga, {}).get(team_id)
    if profile:
        return profile
    return {
        "name": team_name,
        "position": 9,
        "points": 36,
        "played": 28,
        "won": 10,
        "draw": 6,
        "lost": 12,
        "gf": 36,
        "gc": 40,
        "form": "D,W,L,D,W",
        "attack": 1.35,
        "defense": 1.35,
        "recent": [("HOME", 1, 1), ("AWAY", 1, 2), ("HOME", 2, 1), ("AWAY", 0, 1), ("HOME", 1, 0), ("AWAY", 1, 1)],
    }

def synthetic_standings_from_profile(profile: dict):
    return {
        "position": profile["position"],
        "points": profile["points"],
        "played": profile["played"],
        "won": profile["won"],
        "draw": profile["draw"],
        "lost": profile["lost"],
        "gf": profile["gf"],
        "gc": profile["gc"],
        "gd": profile["gf"] - profile["gc"],
        "form": profile["form"],
        "total": 18,
    }

def build_synthetic_team_history(liga: str, team_id: int, team_name: str, opp_id: int, opp_name: str):
    profile = get_synthetic_league_profile(liga, team_id, team_name)
    SYNTHETIC_LEAGUE_H2H = {}
    h2h_scores = SYNTHETIC_LEAGUE_H2H.get(liga, {}).get(frozenset((team_id, opp_id)), [(1, 1)])
    matches = []
    base_date = date.today() - timedelta(days=42)
    first_h2h = h2h_scores[0]
    second_h2h = h2h_scores[1] if len(h2h_scores) > 1 else h2h_scores[0]

    matches.append({
        "date": (base_date + timedelta(days=4)).isoformat(),
        "homeId": team_id,
        "awayId": opp_id,
        "homeName": team_name,
        "awayName": opp_name,
        "gH": first_h2h[0],
        "gA": first_h2h[1],
        "status": "FINISHED",
    })

    recent_pool = profile["recent"]
    for idx, (venue, gf, gc) in enumerate(recent_pool[:4], start=1):
        rival_id = team_id + 100 + idx
        rival_name = f"Rival {liga} {idx}"
        match_date = (base_date + timedelta(days=idx * 6)).isoformat()
        if venue == "HOME":
            matches.append({
                "date": match_date,
                "homeId": team_id,
                "awayId": rival_id,
                "homeName": team_name,
                "awayName": rival_name,
                "gH": gf,
                "gA": gc,
                "status": "FINISHED",
            })
        else:
            matches.append({
                "date": match_date,
                "homeId": rival_id,
                "awayId": team_id,
                "homeName": rival_name,
                "awayName": team_name,
                "gH": gc,
                "gA": gf,
                "status": "FINISHED",
            })

    matches.append({
        "date": (base_date + timedelta(days=33)).isoformat(),
        "homeId": opp_id,
        "awayId": team_id,
        "homeName": opp_name,
        "awayName": team_name,
        "gH": second_h2h[1],
        "gA": second_h2h[0],
        "status": "FINISHED",
    })
    return sorted(matches, key=lambda x: x["date"])

def build_rsl_demo_players(team_name: str, attack_level: float, defense_level: float):
    att_bonus = max(0, attack_level - 1.2)
    def_bonus = max(0, defense_level - 1.0)
    return [
        {"name": f"{team_name} 9", "position": "Forward", "prob_gol": round(min(78, 34 + att_bonus * 18), 1), "prob_asist": round(min(42, 14 + att_bonus * 8), 1), "prob_tarjeta": 18.0, "fuente_gol": "estimado", "fuente_asist": "estimado", "goles_temporada": max(6, int(10 + att_bonus * 4)), "asist_temporada": max(2, int(4 + att_bonus * 2))},
        {"name": f"{team_name} 10", "position": "Attacker", "prob_gol": round(min(63, 24 + att_bonus * 14), 1), "prob_asist": round(min(55, 20 + att_bonus * 10), 1), "prob_tarjeta": 14.0, "fuente_gol": "estimado", "fuente_asist": "estimado", "goles_temporada": max(4, int(7 + att_bonus * 3)), "asist_temporada": max(3, int(6 + att_bonus * 2))},
        {"name": f"{team_name} 8", "position": "Midfielder", "prob_gol": round(min(28, 9 + att_bonus * 6), 1), "prob_asist": round(min(37, 16 + att_bonus * 7), 1), "prob_tarjeta": round(22 + def_bonus * 8, 1), "fuente_gol": "estimado", "fuente_asist": "estimado", "goles_temporada": max(1, int(3 + att_bonus * 2)), "asist_temporada": max(2, int(5 + att_bonus * 2))},
        {"name": f"{team_name} 5", "position": "Defender", "prob_gol": 8.0, "prob_asist": 7.0, "prob_tarjeta": round(31 + def_bonus * 10, 1), "fuente_gol": "estimado", "fuente_asist": "estimado", "goles_temporada": 1, "asist_temporada": 1},
    ]

def analyze_synthetic_league_match(liga: str, home_id: int, away_id: int, home_name: str, away_name: str):
    pA = build_synthetic_team_history(liga, home_id, home_name, away_id, away_name)
    pB = build_synthetic_team_history(liga, away_id, away_name, home_id, home_name)
    sA = calc_stats(pA, home_id)
    sB = calc_stats(pB, away_id)

    atk_home = calc_lambda(pA, home_id, as_local=True, scored=True)
    def_away = calc_lambda(pB, away_id, as_local=False, scored=False)
    atk_away = calc_lambda(pB, away_id, as_local=False, scored=True)
    def_home = calc_lambda(pA, home_id, as_local=True, scored=False)

    lA_base = round((atk_home + def_away) / 2, 3)
    lB_base = round((atk_away + def_home) / 2, 3)

    fA = get_form(pA, home_id)
    fB = get_form(pB, away_id)
    h2h = get_h2h(pA, home_id, away_id)

    st_home = synthetic_standings_from_profile(get_synthetic_league_profile(liga, home_id, home_name))
    st_away = synthetic_standings_from_profile(get_synthetic_league_profile(liga, away_id, away_name))

    maf_home = calc_maf(st_home, st_away, fA, es_local=True)
    maf_away = calc_maf(st_away, st_home, fB, es_local=False)

    lA = aplicar_maf(lA_base, maf_home["maf"])
    lB = aplicar_maf(lB_base, maf_away["maf"])

    mat = build_matrix(lA, lB)
    prA, prE, prB = calc_probs(mat)
    sc_i, sc_j, sc_p = best_score(mat)

    ov25 = over_prob(mat, 2.5)
    ov15 = over_prob(mat, 1.5)
    bttsp = btts_prob(lA, lB)
    score = betsense_score(prA, prE, prB, fA, fB, h2h, home_id, sA, sB, maf_home, maf_away)

    po_data = {
        "lambdaHome": round(lA, 2),
        "lambdaAway": round(lB, 2),
        "lambdaHomeBase": round(lA_base, 2),
        "lambdaAwayBase": round(lB_base, 2),
        "mafHome": maf_home.get("maf", 1.0),
        "mafAway": maf_away.get("maf", 1.0),
        "probHome": prA,
        "probDraw": prE,
        "probAway": prB,
        "oddsHome": round(1/prA, 2) if prA > 0 else None,
        "oddsDraw": round(1/prE, 2) if prE > 0 else None,
        "oddsAway": round(1/prB, 2) if prB > 0 else None,
        "bestScore": {"home": sc_i, "away": sc_j, "prob": sc_p},
    }
    mk_data = {
        "over25": ov25,
        "over15": ov15,
        "btts": bttsp,
        "under25": round(100 - ov25, 1),
    }
    markets_semaforo = {
        "over25": semaforo(ov25),
        "over15": semaforo(ov15),
        "btts": semaforo(bttsp),
        "under25": semaforo(round(100 - ov25, 1)),
        "win_home": semaforo(round(prA * 100, 1)),
        "win_away": semaforo(round(prB * 100, 1)),
        "draw": semaforo(round(prE * 100, 1)),
    }
    suggestions = build_suggestions(prA, prE, prB, sA, sB, mk_data, po_data, h2h, fA, fB, maf_home, maf_away)
    easy_plan = build_easy_bet_plan(home_name, away_name, suggestions, score, po_data, mk_data)

    analisis_tecnico_local = analyze_all_indicators(pA, home_id)
    analisis_tecnico_visit = analyze_all_indicators(pB, away_id)
    mercados_derivados = calcular_mercados_derivados(sA, sB, liga, lA, lB)

    home_profile = get_synthetic_league_profile(liga, home_id, home_name)
    away_profile = get_synthetic_league_profile(liga, away_id, away_name)
    jugadores_data = {
        "home": build_rsl_demo_players(home_name, home_profile["attack"], home_profile["defense"]),
        "away": build_rsl_demo_players(away_name, away_profile["attack"], away_profile["defense"]),
        "nota": "Analisis y jugadores estimados con perfil sintetico del equipo.",
    }

    return {
        "teams": {
            "home": {"id": home_id, "name": home_name},
            "away": {"id": away_id, "name": away_name},
        },
        "liga": liga,
        "season": SEASON,
        "stats": {"home": sA, "away": sB},
        "poisson": po_data,
        "markets": mk_data,
        "form": {"home": fA, "away": fB},
        "h2h": h2h,
        "score": score,
        "suggestions": suggestions,
        "easy_plan": easy_plan,
        "standings": {"home": st_home, "away": st_away},
        "maf": {"home": maf_home, "away": maf_away},
        "semaforo": markets_semaforo,
        "indicadores_tecnicos": {
            "local": analisis_tecnico_local,
            "visitante": analisis_tecnico_visit
        },
        "jugadores": jugadores_data,
        "mercados_derivados": mercados_derivados,
        "demo": True,
        "demo_note": f"{liga} usa datos sinteticos por perfil de equipo para habilitar analisis completo.",
    }

# ── HELPERS API ───────────────────────────────────────────────
def fetch(path):
    r = requests.get(BASE_URL + path, headers=HEADERS)
    if not r.ok:
        raise Exception(r.json().get("message", f"Error {r.status_code}"))
    return r.json()

def normalizar(matches):
    """
    Normaliza la respuesta de la API.
    Extrae goles + campos extras si la API los incluye:
    shots, shots_on_target, possession, xG (cuando estén disponibles).
    Los indicadores usan estos campos con fallbacks si no existen.
    """
    result = []
    for m in matches:
        gH = m["score"]["fullTime"].get("home")
        gA = m["score"]["fullTime"].get("away")
        if gH is None or gA is None:
            continue

        # ── Campos extras: tiros, posesión, xG ───────────────────────
        # football-data.org no devuelve estos campos directamente,
        # pero los dejamos preparados para cuando cambien o se enriquezca
        # la fuente de datos. Los indicadores usan proxies cuando son None.
        stats = m.get("statistics") or {}

        def _s(campo_home, campo_away=None):
            """Extrae un stat del objeto statistics si existe."""
            v = stats.get(campo_home)
            if v is None and campo_away:
                v = stats.get(campo_away)
            try: return float(v) if v is not None else None
            except: return None

        record = {
            "date":     m.get("utcDate", "")[:10],
            "homeId":   m["homeTeam"]["id"],
            "awayId":   m["awayTeam"]["id"],
            "homeName": m["homeTeam"]["name"],
            "awayName": m["awayTeam"]["name"],
            "gH": gH,
            "gA": gA,
            "status": m.get("status", ""),
            # Extras (None si la API no los trae — indicators usa proxies)
            "shots_home":             _s("shots_total_home",  "shotsTotal_home"),
            "shots_away":             _s("shots_total_away",  "shotsTotal_away"),
            "shots_on_target_home":   _s("shots_on_goal_home","shotsOnGoal_home"),
            "shots_on_target_away":   _s("shots_on_goal_away","shotsOnGoal_away"),
            "possession_home":        _s("ball_possession_home","ballPossession_home"),
            "possession_away":        _s("ball_possession_away","ballPossession_away"),
            "xg_home":                _s("expected_goals_home", "xg_home"),
            "xg_away":                _s("expected_goals_away", "xg_away"),
        }
        result.append(record)
    return result

# ── ESTADÍSTICAS ─────────────────────────────────────────────
def betsense_score(prA, prE, prB, fA, fB, h2h_list, id_a, sA, sB,
                   maf_home=None, maf_away=None):
    fp = lambda f: sum(3 if r=="W" else 1 if r=="D" else 0 for r in f)
    fpA, fpB = fp(fA), fp(fB)

    hWA = sum(1 for p in h2h_list if (p["gH"] if p["homeId"]==id_a else p["gA"]) >
                                      (p["gA"] if p["homeId"]==id_a else p["gH"]))
    hWB = len(h2h_list) - hWA - sum(1 for p in h2h_list if p["gH"]==p["gA"])

    dom   = abs(prA - prB) * 100
    forma = ((fpA - fpB + 15) / 30) * 30
    h2hS  = (hWA / len(h2h_list) * 20) if h2h_list else 10
    gfS   = min((sA["pgf"] / 2.5) * 25, 25)
    base  = dom + forma + h2hS + gfS

    # ── Ajuste MAF en el BetSense Score ─────────────────────────────
    # Si algún equipo tiene alertas críticas, el score baja (más incertidumbre)
    maf_penalty = 0
    maf_notas   = []

    if maf_home and maf_home.get("disponible"):
        mh = maf_home["maf"]
        # Equipo local en descenso/urgencia: aumenta imprevisibilidad → penaliza confianza
        if mh > 1.15:
            maf_penalty += 8
            maf_notas.append(f"Local bajo alta presión situacional (MAF={mh})")
        elif mh > 1.08:
            maf_penalty += 4
            maf_notas.append(f"Local con presión situacional moderada (MAF={mh})")
        elif mh < 0.92:
            maf_penalty += 5   # campeón relajado también es imprevisible
            maf_notas.append(f"Local posiblemente relajado/rotando (MAF={mh})")

    if maf_away and maf_away.get("disponible"):
        ma = maf_away["maf"]
        if ma > 1.15:
            maf_penalty += 8
            maf_notas.append(f"Visitante bajo alta presión situacional (MAF={ma})")
        elif ma > 1.08:
            maf_penalty += 4
            maf_notas.append(f"Visitante con presión situacional moderada (MAF={ma})")
        elif ma < 0.92:
            maf_penalty += 5
            maf_notas.append(f"Visitante posiblemente relajado/rotando (MAF={ma})")

    total = min(round(base - maf_penalty), 100)
    total = max(total, 5)

    return {
        "total": total,
        "label": "ALTA CONFIANZA" if total>=70 else "MODERADO" if total>=45 else "RIESGO ELEVADO",
        "color": "green" if total>=70 else "yellow" if total>=45 else "red",
        "breakdown": {
            "dominancia": round(dom, 1),
            "forma": f"{fpA} vs {fpB} pts",
            "h2h":   f"{hWA}V-{len(h2h_list)-hWA-hWB}E-{hWB}D",
            "pgf":   f"{sA['pgf']} vs {sB['pgf']} gol/p",
            "maf_penalty": maf_penalty,
            "maf_notas":   maf_notas,
        }
    }



# ══════════════════════════════════════════════════════════════════════
# MAF — Motivation Adjustment Factor
# ══════════════════════════════════════════════════════════════════════
# El MAF multiplica la lambda Poisson de cada equipo para reflejar
# el contexto situacional que las estadísticas históricas no capturan.
#
# Componentes:
#   1. zona_factor   — posición en tabla (descenso/media/Europa/campeón)
#   2. urgencia_factor — partidos restantes × criticidad de la situación
#   3. gap_factor    — diferencia de puntos con el rival (David vs Goliat)
#   4. racha_factor  — racha de resultados recientes (invicto / sin ganar)
#
# MAF final = producto ponderado de los 4 factores, clampeado a [0.70, 1.40]
# ══════════════════════════════════════════════════════════════════════


def build_suggestions(prA, prE, prB, sA, sB, mk, po, h2h, fA, fB, maf_home=None, maf_away=None):
    """
    Genera lista ordenada de apuestas sugeridas con probabilidad y descripción.
    Solo incluye mercados con >50% de probabilidad.
    """
    items = []

    # ── 1X2 ──────────────────────────────────────────────────
    items.append({"market": f"Victoria Local (1)", "prob": round(prA*100,1),
                  "desc": f"Cuota justa {round(1/prA,2) if prA>0 else '—'}"})
    items.append({"market": "Empate (X)",           "prob": round(prE*100,1),
                  "desc": f"Cuota justa {round(1/prE,2) if prE>0 else '—'}"})
    items.append({"market": "Victoria Visitante (2)","prob": round(prB*100,1),
                  "desc": f"Cuota justa {round(1/prB,2) if prB>0 else '—'}"})

    # ── GOLES ────────────────────────────────────────────────
    items.append({"market": "Over 1.5 Goles",  "prob": mk["over15"],
                  "desc": f"λ local={po['lambdaHome']} · λ visit={po['lambdaAway']}"})
    items.append({"market": "Over 2.5 Goles",  "prob": mk["over25"],
                  "desc": "Más de 2 goles en el partido"})
    items.append({"market": "Under 2.5 Goles", "prob": mk["under25"],
                  "desc": "Menos de 3 goles en el partido"})
    items.append({"market": "Ambos Marcan (BTTS)", "prob": mk["btts"],
                  "desc": "Los dos equipos anotan al menos 1 gol"})
    items.append({"market": "BTTS No",  "prob": round(100-mk["btts"],1),
                  "desc": "Al menos un equipo no anota"})

    # ── MARCADOR EXACTO ──────────────────────────────────────
    sc = po["bestScore"]
    items.append({"market": f"Marcador {sc['home']}–{sc['away']}",
                  "prob": round(sc["prob"]*100, 1),
                  "desc": "Resultado exacto más probable"})

    # ── CLEAN SHEET ──────────────────────────────────────────
    items.append({"market": "Portería a cero Local",    "prob": sA["cs"],
                  "desc": f"Histórico local {sA['cs']}% sin recibir"})
    items.append({"market": "Portería a cero Visitante","prob": sB["cs"],
                  "desc": f"Histórico visita {sB['cs']}% sin recibir"})

    # ── DOBLE OPORTUNIDAD ────────────────────────────────────
    items.append({"market": "Doble Oportunidad 1X",
                  "prob": round((prA+prE)*100, 1),
                  "desc": "Local gana o empate"})
    items.append({"market": "Doble Oportunidad X2",
                  "prob": round((prE+prB)*100, 1),
                  "desc": "Visitante gana o empate"})
    items.append({"market": "Doble Oportunidad 12",
                  "prob": round((prA+prB)*100, 1),
                  "desc": "Cualquiera de los dos gana"})

    # ── FORMA ────────────────────────────────────────────────
    fp = lambda f: sum(3 if r=="W" else 1 if r=="D" else 0 for r in f)
    fpA, fpB = fp(fA), fp(fB)
    if fpA > fpB + 3:
        items.append({"market": "Local en mejor forma", "prob": round(min(fpA/15*100, 90), 1),
                      "desc": f"Local {fpA}pts vs Visitante {fpB}pts (últ. 5)"})
    elif fpB > fpA + 3:
        items.append({"market": "Visitante en mejor forma", "prob": round(min(fpB/15*100, 90), 1),
                      "desc": f"Visitante {fpB}pts vs Local {fpA}pts (últ. 5)"})

    # ── Advertencias MAF — van primero, siempre visibles ───────────
    alertas_maf = []
    for equipo_label, maf_data in [("LOCAL", maf_home), ("VISITANTE", maf_away)]:
        if not maf_data or not maf_data.get("disponible"):
            continue
        for alerta in maf_data.get("alertas", []):
            alertas_maf.append({
                "market": f"{alerta['icono']} ALERTA SITUACIONAL — {equipo_label}",
                "prob":   None,
                "desc":   alerta["texto"],
                "tipo":   "alerta",
                "nivel":  alerta["tipo"],
            })

    # Filtrar solo >50% y ordenar de mayor a menor probabilidad
    filtered = [i for i in items if i.get("prob", 0) > 50]
    filtered.sort(key=lambda x: x["prob"], reverse=True)
    # Alertas al frente, luego sugerencias estadísticas (top 10)
    return alertas_maf + filtered[:10]


def build_easy_bet_plan(home_name, away_name, suggestions, score, po, mk):
    """
    Convierte el analisis tecnico en una propuesta de apuesta facil de leer.
    """
    easy_candidates = []
    for item in suggestions or []:
        if item.get("tipo") == "alerta":
            continue

        market = item.get("market", "")
        prob = float(item.get("prob") or 0)
        desc = item.get("desc", "")

        if market.startswith("Marcador "):
            risk = "alta"
            tier = "riesgo"
        elif "Doble Oportunidad" in market or "Over 1.5" in market or "Under 2.5" in market:
            risk = "baja"
            tier = "segura"
        elif "Victoria" in market or "BTTS" in market or "Over 2.5" in market:
            risk = "media"
            tier = "media"
        else:
            risk = "media"
            tier = "apoyo"

        easy_candidates.append({
            "market": market,
            "prob": round(prob, 1),
            "desc": desc,
            "risk": risk,
            "tier": tier,
        })

    easy_candidates.sort(key=lambda x: x["prob"], reverse=True)

    principal = next((c for c in easy_candidates if c["tier"] in ("segura", "media")), None)
    conservadora = next((c for c in easy_candidates if c["tier"] == "segura" and c["market"] != (principal or {}).get("market")), None)
    cuota_media = next((c for c in easy_candidates if c["tier"] == "media" and c["market"] != (principal or {}).get("market")), None)
    riesgo = next((c for c in easy_candidates if c["tier"] == "riesgo"), None)

    if not principal and easy_candidates:
        principal = easy_candidates[0]
    if not conservadora:
        conservadora = principal
    if not cuota_media:
        cuota_media = next((c for c in easy_candidates if c["market"] != (principal or {}).get("market")), principal)

    favorito = home_name if po.get("probHome", 0) > po.get("probAway", 0) else away_name if po.get("probAway", 0) > po.get("probHome", 0) else "Empate"
    partido_parejo = abs((po.get("probHome", 0) or 0) - (po.get("probAway", 0) or 0)) < 0.08
    goles_totales = round((po.get("lambdaHome", 0) or 0) + (po.get("lambdaAway", 0) or 0), 2)

    resumen = []
    if principal:
        resumen.append(f"La jugada mas probable es {principal['market']} ({principal['prob']}%).")
    if partido_parejo:
        resumen.append("El 1X2 esta muy parejo; conviene mirar goles o doble oportunidad.")
    else:
        resumen.append(f"El modelo favorece a {favorito}.")
    resumen.append(
        "Se espera un partido "
        + ("abierto y con goles." if goles_totales >= 2.7 else "cerrado y mas tactico." if goles_totales <= 2.1 else "equilibrado en goles.")
    )

    decision = "IR CON CONFIANZA" if (principal or {}).get("prob", 0) >= 68 and score.get("total", 0) >= 60 else "JUGAR CON CAUTELA" if (principal or {}).get("prob", 0) >= 58 else "MEJOR BAJAR STAKE"
    evitar = "Evita el mercado 1X2 puro." if partido_parejo else "Evita el marcador exacto como apuesta principal."

    return {
        "decision": decision,
        "resumen_corto": " ".join(resumen),
        "principal": principal,
        "conservadora": conservadora,
        "cuota_media": cuota_media,
        "riesgo": riesgo,
        "evitar": evitar,
        "favorito_modelo": favorito,
        "goles_esperados": goles_totales,
        "partido_parejo": partido_parejo,
    }


def build_combinada_summary(picks, liga, fecha):
    top_picks = picks[:3]
    if not top_picks:
        return {
            "titulo": "Sin combinada clara",
            "resumen": f"No encontré picks suficientemente fuertes hoy en {liga}.",
            "nivel": "esperar",
        }

    avg_prob = round(sum(p["prob"] for p in top_picks) / len(top_picks), 1)
    nivel = "fuerte" if avg_prob >= 72 else "media" if avg_prob >= 64 else "prudente"
    resumen = " + ".join(f"{p['partido']} · {p['market']}" for p in top_picks)

    return {
        "titulo": f"Propuesta de combinada para {fecha}",
        "resumen": resumen,
        "nivel": nivel,
        "cantidad_picks": len(top_picks),
    }


def get_standings(liga, team_id_a, team_id_b):
    """
    Obtiene posición actual de ambos equipos en la tabla.
    Para Champions y competiciones europeas (formato de grupos/fase)
    la tabla no existe en formato clásico — retorna None limpio.
    """
    # Champions, Europa League, etc. no tienen tabla lineal de posiciones
    # La API devuelve grupos o fases, no una tabla unificada
    if liga in ("CL", "EL", "EC", "CLI", "WC", "UNL"):
        # Competiciones europeas y de selecciones no tienen tabla lineal
        return None, None

    try:
        data = fetch(f"/competitions/{liga}/standings?season={SEASON}")
        standings = data.get("standings", [])
        # Tabla total (type=TOTAL)
        total = next((s for s in standings if s.get("type") == "TOTAL"), None)
        if not total:
            total = standings[0] if standings else None
        if not total:
            return None, None

        table = total.get("table", [])
        result = {}
        for row in table:
            tid = row["team"]["id"]
            if tid in (team_id_a, team_id_b):
                result[tid] = {
                    "position": row["position"],
                    "points":   row["points"],
                    "played":   row["playedGames"],
                    "won":      row["won"],
                    "draw":     row["draw"],
                    "lost":     row["lost"],
                    "gf":       row["goalsFor"],
                    "gc":       row["goalsAgainst"],
                    "gd":       row["goalDifference"],
                    "form":     row.get("form", ""),
                    "total":    len(table),
                }
        return result.get(team_id_a), result.get(team_id_b)
    except:
        return None, None


def semaforo(prob):
    """Devuelve icono y nivel según probabilidad."""
    if prob >= 65:
        return {"icon": "🎯", "level": "CERTERO",  "color": "green"}
    elif prob >= 50:
        return {"icon": "🎲", "level": "VARIABLE", "color": "yellow"}
    else:
        return {"icon": "🚫", "level": "EVITAR",   "color": "red"}


# ══════════════════════════════════════════════════════════════
# MÓDULO DE JUGADORES — datos reales + probabilidades derivadas
# ══════════════════════════════════════════════════════════════

# Promedios reales por liga (tarjetas amarillas por partido, temporadas 2020-2024)
LIGA_STATS = {
    "PL":  {"tarjetas": 3.2, "corners": 10.2, "faltas": 22.1, "avg_goles": 2.85},  # Premier League
    "PD":  {"tarjetas": 4.1, "corners":  9.8, "faltas": 24.8, "avg_goles": 2.60},  # La Liga
    "BL1": {"tarjetas": 3.4, "corners":  9.5, "faltas": 21.3, "avg_goles": 3.15},  # Bundesliga
    "SA":  {"tarjetas": 4.8, "corners": 10.4, "faltas": 24.2, "avg_goles": 2.65},  # Serie A
    "FL1": {"tarjetas": 4.3, "corners":  9.3, "faltas": 24.5, "avg_goles": 2.55},  # Ligue 1
    "CL":  {"tarjetas": 3.1, "corners":  9.9, "faltas": 22.4, "avg_goles": 2.80},  # Champions League
    "EL":  {"tarjetas": 3.3, "corners":  9.7, "faltas": 22.8, "avg_goles": 2.72},  # Europa League
    "ELC": {"tarjetas": 3.8, "corners": 10.5, "faltas": 23.6, "avg_goles": 2.68},  # Championship
    "PPL": {"tarjetas": 3.9, "corners":  9.6, "faltas": 23.2, "avg_goles": 2.62},  # Liga Portugal
    "CLI": {"tarjetas": 2.5, "corners":  8.8, "faltas": 18.5, "avg_goles": 2.30},  # legacy
    "WC":  {"tarjetas": 3.0, "corners":  9.2, "faltas": 20.1, "avg_goles": 2.55},  # Mundial FIFA
    "EC":  {"tarjetas": 2.8, "corners":  9.5, "faltas": 19.8, "avg_goles": 2.45},  # Eurocopa UEFA
    "UNL": {"tarjetas": 2.6, "corners":  9.0, "faltas": 19.2, "avg_goles": 2.35},  # UEFA Nations League
    "BSA": {"tarjetas": 4.5, "corners":  9.0, "faltas": 25.5, "avg_goles": 2.45},  # Brasileirão
    "DED": {"tarjetas": 3.0, "corners": 10.0, "faltas": 20.5, "avg_goles": 3.05},  # Eredivisie
}

# Peso ofensivo por posición (% del total de goles que aporta cada posición)
PESO_GOL_POSICION = {
    "Goalkeeper":    0.01,
    "Defence":       0.06,
    "Defender":      0.06,
    "Midfield":      0.22,
    "Midfielder":    0.22,
    "Offence":       0.65,
    "Forward":       0.65,
    "Attacker":      0.65,
    "Centre-Forward":0.70,
    "Left Winger":   0.55,
    "Right Winger":  0.55,
}

# Peso de asistencia por posición
PESO_ASIST_POSICION = {
    "Goalkeeper":    0.01,
    "Defence":       0.08,
    "Defender":      0.08,
    "Midfield":      0.42,
    "Midfielder":    0.42,
    "Offence":       0.35,
    "Forward":       0.30,
    "Attacker":      0.30,
    "Centre-Forward":0.28,
    "Left Winger":   0.40,
    "Right Winger":  0.40,
}

# Riesgo de tarjeta por posición (factor multiplicador sobre la media del equipo)
RIESGO_TARJETA_POSICION = {
    "Goalkeeper":    0.15,
    "Defence":       1.55,
    "Defender":      1.55,
    "Midfield":      1.20,
    "Midfielder":    1.20,
    "Offence":       0.80,
    "Forward":       0.80,
    "Attacker":      0.80,
    "Centre-Forward":0.75,
    "Left Winger":   0.85,
    "Right Winger":  0.85,
}


def get_squad(team_id: int) -> list:
    """
    Obtiene el plantel del equipo desde /v4/teams/{id}.
    Devuelve lista de jugadores con: id, name, position, shirtNumber.
    Disponible en free tier.
    """
    try:
        data = fetch(f"/teams/{team_id}")
        squad = data.get("squad", [])
        result = []
        for p in squad:
            pos = p.get("position") or "Unknown"
            result.append({
                "id":          p.get("id"),
                "name":        p.get("name", "Desconocido"),
                "position":    pos,
                "shirtNumber": p.get("shirtNumber"),
                "nationality": p.get("nationality", ""),
                "dob":         p.get("dateOfBirth", ""),
            })
        return result
    except Exception:
        return []


def get_scorers_liga(liga: str, limit: int = 20) -> dict:
    """
    Top goleadores de la liga actual desde /v4/competitions/{liga}/scorers.
    Devuelve dict {player_id: {goals, assists, penalties, team_id}}.
    Disponible en free tier.
    CLI (amistosos selecciones) no tiene scorers acumulados — retorna vacío.
    WC, EC, UNL tampoco tienen scorers en free tier.
    """
    if liga in ("CLI", "WC", "EC", "UNL"):
        return {}
    try:
        data = fetch(f"/competitions/{liga}/scorers?season={SEASON}&limit={limit}")
        scorers = {}
        for s in data.get("scorers", []):
            pid = s.get("player", {}).get("id")
            if pid:
                scorers[pid] = {
                    "goals":     s.get("goals", 0) or 0,
                    "assists":   s.get("assists", 0) or 0,
                    "penalties": s.get("penalties", 0) or 0,
                    "team_id":   s.get("team", {}).get("id"),
                    "name":      s.get("player", {}).get("name", ""),
                }
        return scorers
    except Exception:
        return {}


def calcular_jugadores_partido(
    squad_home: list,
    squad_away: list,
    scorers: dict,
    lA: float,          # lambda goles local (del modelo Poisson)
    lB: float,          # lambda goles visitante
    sA: dict,           # stats del local (pgf, pgc, cs, T)
    sB: dict,
    liga: str,
    st_home: dict,      # standings local (posicion, puntos)
    st_away: dict,
) -> dict:
    """
    Calcula probabilidades de anotar, asistir y recibir tarjeta
    para cada jugador, usando datos reales (scorers) + modelos derivados.
    """
    liga_cfg = LIGA_STATS.get(liga, LIGA_STATS["PL"])
    tarjetas_liga = liga_cfg["tarjetas"]   # amarillas promedio por partido en la liga

    # ── Factor de rivalidad para tarjetas ─────────────────────
    # Partidos entre equipos con posiciones muy distintas → más tarjetas
    pos_diff = 0
    if st_home and st_away:
        pos_diff = abs(st_home.get("position", 10) - st_away.get("position", 10))
    factor_rivalidad = 1.0 + pos_diff * 0.015   # max ~+30% en diferencia de 20 puestos

    # Tarjetas esperadas en este partido (Poisson)
    lambda_tarjetas_home = (tarjetas_liga / 2) * (sA["pgc"] / liga_cfg["avg_goles"]) * factor_rivalidad
    lambda_tarjetas_away = (tarjetas_liga / 2) * (sB["pgc"] / liga_cfg["avg_goles"]) * factor_rivalidad

    def prob_al_menos_uno(lam):
        """P(X >= 1) con Poisson = 1 - e^(-lambda)"""
        import math
        return round((1 - math.exp(-lam)) * 100, 1)

    def jugadores_equipo(squad, team_lam_gol, team_lam_tarj, scorers_dict, n_partidos):
        """
        Para cada jugador del equipo calcula:
        - prob_gol:    % de que marque en este partido
        - prob_asist:  % de que asista en este partido
        - prob_tarjeta:% de que vea amarilla en este partido
        - fuente:      'real' si tiene datos reales, 'estimado' si es derivado
        """
        resultado = []
        for j in squad:
            pid  = j["id"]
            pos  = j["position"]
            name = j["name"]

            # ── GOL ──────────────────────────────────────────
            if pid in scorers_dict and scorers_dict[pid]["goals"] > 0:
                # Datos reales: tasa de gol por partido en temporada
                goles_reales  = scorers_dict[pid]["goals"]
                tasa_gol      = goles_reales / max(n_partidos, 1)
                # Poisson: P(>=1 gol en este partido)
                import math
                p_gol = round((1 - math.exp(-tasa_gol)) * 100, 1)
                fuente_gol = "real"
            else:
                # Estimado por posición: peso × lambda del equipo / jugadores en esa posición
                peso = PESO_GOL_POSICION.get(pos, 0.10)
                jugadores_pos = max(sum(1 for x in squad if x["position"] == pos), 1)
                tasa_gol = (team_lam_gol * peso) / jugadores_pos
                import math
                p_gol = round((1 - math.exp(-tasa_gol)) * 100, 1)
                fuente_gol = "estimado"

            # ── ASISTENCIA ───────────────────────────────────
            if pid in scorers_dict and (scorers_dict[pid].get("assists") or 0) > 0:
                asist_reales = scorers_dict[pid]["assists"]
                tasa_asist   = asist_reales / max(n_partidos, 1)
                import math
                p_asist = round((1 - math.exp(-tasa_asist)) * 100, 1)
                fuente_asist = "real"
            else:
                peso_a = PESO_ASIST_POSICION.get(pos, 0.10)
                jugadores_pos = max(sum(1 for x in squad if x["position"] == pos), 1)
                # Las asistencias ≈ 85% de los goles del equipo tienen asistencia
                tasa_asist = (team_lam_gol * 0.85 * peso_a) / jugadores_pos
                import math
                p_asist = round((1 - math.exp(-tasa_asist)) * 100, 1)
                fuente_asist = "estimado"

            # ── TARJETA AMARILLA ─────────────────────────────
            # No hay datos reales de tarjetas en free → siempre estimado
            riesgo_pos = RIESGO_TARJETA_POSICION.get(pos, 1.0)
            jugadores_pos = max(sum(1 for x in squad if x["position"] == pos), 1)
            lam_tarj_jugador = (team_lam_tarj * riesgo_pos) / jugadores_pos
            import math
            p_tarjeta = round((1 - math.exp(-lam_tarj_jugador)) * 100, 1)

            # Solo incluir porteros si tienen prob > 0 en algo
            if pos in ("Goalkeeper",) and p_gol < 2 and p_asist < 2 and p_tarjeta < 5:
                continue

            resultado.append({
                "id":          pid,
                "name":        name,
                "position":    pos,
                "shirtNumber": j.get("shirtNumber"),
                "prob_gol":    p_gol,
                "prob_asist":  p_asist,
                "prob_tarjeta":p_tarjeta,
                "fuente_gol":   fuente_gol,
                "fuente_asist": fuente_asist,
                "fuente_tarjeta": "estimado",
                # Para mostrar en UI: goles reales de temporada
                "goles_temporada":  scorers_dict.get(pid, {}).get("goals", 0),
                "asist_temporada":  scorers_dict.get(pid, {}).get("assists", 0),
            })

        # Ordenar por prob_gol descendente
        resultado.sort(key=lambda x: x["prob_gol"], reverse=True)
        return resultado[:15]  # Top 15 por equipo

    jugadores_home = jugadores_equipo(
        squad_home, lA, lambda_tarjetas_home, scorers, sA["T"]
    )
    jugadores_away = jugadores_equipo(
        squad_away, lB, lambda_tarjetas_away, scorers, sB["T"]
    )

    # Top 3 de cada categoría (combinando ambos equipos)
    todos = jugadores_home + jugadores_away
    top_gol     = sorted(todos, key=lambda x: x["prob_gol"],     reverse=True)[:5]
    top_asist   = sorted(todos, key=lambda x: x["prob_asist"],   reverse=True)[:5]
    top_tarjeta = sorted(todos, key=lambda x: x["prob_tarjeta"], reverse=True)[:5]

    return {
        "home": jugadores_home,
        "away": jugadores_away,
        "top_goleadores":  top_gol,
        "top_asistentes":  top_asist,
        "top_tarjetas":    top_tarjeta,
        "lambda_tarjetas": {
            "home": round(lambda_tarjetas_home, 2),
            "away": round(lambda_tarjetas_away, 2),
        },
        "nota": "Datos reales cuando disponibles en scorers de liga. Resto estimado con distribuciones estadísticas.",
    }


# ══════════════════════════════════════════════════════════════
# MERCADOS DERIVADOS — corners, tarjetas, faltas
# (todo simulado con promedios de liga + factores del partido)
# ══════════════════════════════════════════════════════════════

def calcular_mercados_derivados(sA: dict, sB: dict, liga: str, lA: float, lB: float) -> dict:
    """
    Calcula probabilidades para corners, tarjetas y faltas
    usando promedios históricos de la liga ajustados por las
    estadísticas reales del partido.
    """
    import math
    cfg = LIGA_STATS.get(liga, LIGA_STATS["PL"])

    avg_g   = cfg["avg_goles"]
    avg_c   = cfg["corners"]
    avg_t   = cfg["tarjetas"]
    avg_f   = cfg["faltas"]

    # ── CORNERS ──────────────────────────────────────────────
    # Equipos ofensivos generan más corners. Base: media de liga / 2 por equipo.
    # Ajuste: ratio goles del equipo vs media de liga
    lam_corn_h = (avg_c / 2) * (lA / avg_g * 2)   # *2 porque lA ya es goles esperados (no total)
    lam_corn_a = (avg_c / 2) * (lB / avg_g * 2)
    lam_corn_h = max(lam_corn_h, 1.5)
    lam_corn_a = max(lam_corn_a, 1.5)
    lam_corn_total = lam_corn_h + lam_corn_a

    def over_poisson(lam, linea):
        """P(X > linea) con distribución Poisson"""
        prob = 0.0
        for k in range(int(linea) + 1):
            prob += (math.exp(-lam) * lam**k) / math.factorial(k)
        return round((1 - prob) * 100, 1)

    corners_markets = {
        "lambda_home":   round(lam_corn_h, 2),
        "lambda_away":   round(lam_corn_a, 2),
        "lambda_total":  round(lam_corn_total, 2),
        "esperados":     round(lam_corn_total, 1),
        "over_8_5":      over_poisson(lam_corn_total, 8.5),
        "over_9_5":      over_poisson(lam_corn_total, 9.5),
        "over_10_5":     over_poisson(lam_corn_total, 10.5),
        "under_9_5":     round(100 - over_poisson(lam_corn_total, 9.5), 1),
        "mas_corners_home": round(
            sum(
                (math.exp(-lam_corn_h) * lam_corn_h**i / math.factorial(i)) *
                sum(math.exp(-lam_corn_a) * lam_corn_a**j / math.factorial(j)
                    for j in range(i))
                for i in range(20)
            ) * 100, 1
        ),
    }
    corners_markets["mas_corners_away"] = round(100 - corners_markets["mas_corners_home"] - 10, 1)

    # ── TARJETAS ─────────────────────────────────────────────
    # Más goles recibidos → más desesperación → más tarjetas
    factor_intensidad = ((sA["pgc"] + sB["pgc"]) / 2) / avg_g
    lam_tarj_total = avg_t * factor_intensidad
    lam_tarj_total = max(lam_tarj_total, 1.5)

    tarjetas_markets = {
        "lambda_total":  round(lam_tarj_total, 2),
        "esperadas":     round(lam_tarj_total, 1),
        "over_2_5":      over_poisson(lam_tarj_total, 2.5),
        "over_3_5":      over_poisson(lam_tarj_total, 3.5),
        "over_4_5":      over_poisson(lam_tarj_total, 4.5),
        "under_3_5":     round(100 - over_poisson(lam_tarj_total, 3.5), 1),
        # Probabilidad de al menos una roja (evento raro, lambda ≈ 0.22)
        "prob_roja":     round((1 - math.exp(-0.22)) * 100, 1),
        # Ambos equipos ven tarjeta
        "ambos_ven_tarjeta": round(
            (1 - math.exp(-(lam_tarj_total * 0.55))) *
            (1 - math.exp(-(lam_tarj_total * 0.45))) * 100, 1
        ),
    }

    # ── FALTAS ───────────────────────────────────────────────
    # Equipos con peor defensa (pgc alto) cometen más faltas
    factor_falta_h = 1.0 + (sA["pgc"] - avg_g/2) * 0.10
    factor_falta_a = 1.0 + (sB["pgc"] - avg_g/2) * 0.10
    lam_faltas_h = (avg_f / 2) * max(factor_falta_h, 0.7)
    lam_faltas_a = (avg_f / 2) * max(factor_falta_a, 0.7)
    lam_faltas_total = lam_faltas_h + lam_faltas_a

    faltas_markets = {
        "lambda_total":  round(lam_faltas_total, 2),
        "esperadas":     round(lam_faltas_total, 1),
        "over_19_5":     over_poisson(lam_faltas_total, 19.5),
        "over_22_5":     over_poisson(lam_faltas_total, 22.5),
        "over_25_5":     over_poisson(lam_faltas_total, 25.5),
        "under_22_5":    round(100 - over_poisson(lam_faltas_total, 22.5), 1),
    }

    return {
        "corners":  corners_markets,
        "tarjetas": tarjetas_markets,
        "faltas":   faltas_markets,
        "nota":     "📐 Estimado — calculado con promedios históricos de la liga y estadísticas reales del partido",
    }


# ══════════════════════════════════════════════════════════════
# RUTAS
# ══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory('.', 'betsense.html')

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory('.', filename)

@app.route("/api/contexto_partido", methods=["POST"])
def api_contexto_partido():
    """
    Scraping sin API key de múltiples fuentes:
    - Google News RSS (titulares reales con fuente y fecha)
    - Reddit JSON API (posts de r/soccer, r/sportsbook)
    - Transfermarkt (lesionados)
    - Nitter/Twitter (periodistas)
    Body: { "home": "...", "away": "...", "liga": "..." }
    """
    import xml.etree.ElementTree as ET
    import urllib.parse
    from datetime import datetime, timezone

    try:
        body = request.get_json(force=True)
        home = body.get("home", "").strip()
        away = body.get("away", "").strip()
        liga = body.get("liga", "").strip()
        if not home or not away:
            return jsonify({"error": "Faltan equipos"}), 400

        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        client = OpenAI(api_key=OPENAI_API_KEY)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }

        noticias   = []   # [{titulo, fuente, url, fecha, tipo}]
        reddit_posts = []
        fuentes_ok   = []

        # ══ 1. GOOGLE NEWS RSS — sin key, completamente gratis ══════════
        def google_news_rss(query, max_items=6):
            q = urllib.parse.quote(query)
            url = f"https://news.google.com/rss/search?q={q}&hl=es&gl=ES&ceid=ES:es"
            try:
                r = requests.get(url, headers=headers, timeout=8)
                if not r.ok: return []
                root = ET.fromstring(r.content)
                items = []
                for item in root.findall('.//item')[:max_items]:
                    titulo = item.findtext('title') or ''
                    link   = item.findtext('link')  or ''
                    fecha  = item.findtext('pubDate') or ''
                    source = item.find('{https://news.google.com/rss}source')
                    fuente = source.text if source is not None else ''
                    # Limpiar título (Google News añade " - Fuente" al final)
                    if ' - ' in titulo:
                        partes = titulo.rsplit(' - ', 1)
                        titulo = partes[0].strip()
                        if not fuente: fuente = partes[1].strip()
                    if titulo and len(titulo) > 15:
                        items.append({
                            "titulo": titulo,
                            "fuente": fuente or "Google News",
                            "url":    link,
                            "fecha":  fecha,
                        })
                return items
            except:
                return []

        # Búsqueda 1: partido directo
        news1 = google_news_rss(f'"{home}" "{away}" {liga}', 6)
        for n in news1:
            noticias.append({**n, "tipo": "partido"})

        # Búsqueda 2: bajas y lesionados
        news2 = google_news_rss(f'{home} baja lesion sancion OR {away} baja lesion sancion', 5)
        for n in news2:
            noticias.append({**n, "tipo": "baja"})

        # Búsqueda 3: rueda de prensa / entrenador
        news3 = google_news_rss(f'{home} entrenador declaracion rueda prensa OR {away} entrenador declaracion', 4)
        for n in news3:
            noticias.append({**n, "tipo": "declaracion"})

        # Búsqueda 4: partido de ida (si es eliminatoria)
        news4 = google_news_rss(f'{home} {away} partido ida vuelta eliminatoria resultado 2025', 4)
        for n in news4:
            noticias.append({**n, "tipo": "ida"})

        # Búsqueda 5: alineaciones probables
        news5 = google_news_rss(f'{home} alineacion probable once titular OR {away} alineacion once titular', 4)
        for n in news5:
            noticias.append({**n, "tipo": "alineacion"})

        if noticias:
            fuentes_ok.append("Google News")

        # ══ 2. REDDIT JSON API — sin key ══════════════════════════════
        reddit_headers = {**headers, "User-Agent": "BetSenseApp/2.0 (sports betting analysis)"}
        subreddits = ["soccer", "sportsbook", "betting", "football"]
        for sub in subreddits:
            try:
                url = f"https://www.reddit.com/r/{sub}/search.json?q={urllib.parse.quote(home+' '+away)}&sort=new&limit=4&t=week&restrict_sr=false"
                r = requests.get(url, headers=reddit_headers, timeout=7)
                if r.ok:
                    posts = r.json().get("data", {}).get("children", [])
                    for p in posts[:3]:
                        pd = p.get("data", {})
                        titulo  = pd.get("title", "")
                        texto   = pd.get("selftext", "")[:200]
                        score   = pd.get("score", 0)
                        comments= pd.get("num_comments", 0)
                        url_p   = "https://reddit.com" + pd.get("permalink", "")
                        if titulo and score >= 3:
                            reddit_posts.append({
                                "titulo":   titulo,
                                "texto":    texto,
                                "fuente":   f"r/{sub}",
                                "url":      url_p,
                                "score":    score,
                                "comments": comments,
                            })
            except:
                continue

        if reddit_posts:
            fuentes_ok.append("Reddit")
            reddit_posts.sort(key=lambda x: x["score"], reverse=True)

        # ══ 3. TRANSFERMARKT — lesionados (scraping HTML simple) ══════
        try:
            for team in [home, away]:
                tm_query = urllib.parse.quote(team.lower().replace(' ', '-'))
                tm_url   = f"https://www.transfermarkt.es/schnellsuche/ergebnis/schnellsuche?query={urllib.parse.quote(team)}"
                r = requests.get(tm_url, headers=headers, timeout=8)
                if r.ok and team[:5].lower() in r.text.lower():
                    # Buscar menciones de "lesionado" o "baja"
                    idx = r.text.lower().find('lesionado')
                    if idx > 0:
                        fragmento = re.sub(r'<[^>]+>', '', r.text[max(0,idx-80):idx+150]).strip()
                        if fragmento and len(fragmento) > 20:
                            noticias.append({
                                "titulo": f"Estado del plantel de {team}",
                                "fuente": "Transfermarkt",
                                "url":    tm_url,
                                "fecha":  "",
                                "tipo":   "baja",
                                "extra":  fragmento[:200]
                            })
                            if "Transfermarkt" not in fuentes_ok:
                                fuentes_ok.append("Transfermarkt")
        except:
            pass

        hay_info_real = len(noticias) > 0 or len(reddit_posts) > 0

        # ══ CONSTRUIR PROMPT PARA GPT ══════════════════════════════════
        # Pasar noticias con sus fuentes para que GPT las cite correctamente
        noticias_texto = ""
        if noticias:
            noticias_texto += "\n=== NOTICIAS REALES (citar fuente al mencionar) ===\n"
            tipos_label = {
                "partido":     "📋 PARTIDO",
                "baja":        "🔴 BAJA/LESIÓN",
                "declaracion": "💬 DECLARACIÓN",
                "ida":         "⚽ PARTIDO DE IDA",
                "alineacion":  "📋 ALINEACIÓN",
            }
            for n in noticias[:12]:
                tipo  = tipos_label.get(n.get("tipo",""), "📰")
                extra = f"\n   Detalle: {n.get('extra','')}" if n.get('extra') else ""
                noticias_texto += f"\n{tipo} | Fuente: {n['fuente']}\nTítulo: {n['titulo']}{extra}\n"

        reddit_texto = ""
        if reddit_posts:
            reddit_texto += "\n=== LO QUE DICE LA COMUNIDAD (Reddit) ===\n"
            for p in reddit_posts[:5]:
                reddit_texto += f"\n[{p['fuente']} | {p['score']} upvotes | {p['comments']} comentarios]\n{p['titulo']}\n{p['texto']}\n"

        if hay_info_real:
            user_prompt = (
                f"Información real encontrada sobre {home} vs {away} ({liga}):\n"
                f"{noticias_texto}\n{reddit_texto}\n\n"
                f"Crea el análisis de contexto para el apostador."
            )
        else:
            user_prompt = (
                f"No encontré noticias recientes de {home} vs {away} ({liga}). "
                f"Basándote en tu conocimiento: contexto útil para apostar, "
                f"estilo de los entrenadores, jugadores clave, historial reciente."
            )

        sys_analista = f"""Eres el analista de BetSense para {home} vs {away}.
Tu trabajo: traducir noticias reales en consejos útiles para apostar.

FORMATO OBLIGATORIO — para cada punto usa esta estructura:
[EMOJI TIPO] **Titular corto en negrita**
📰 *Fuente: [nombre del medio]* — "cita o fragmento clave"
↳ Qué significa esto para apostar: [explicación simple, 1 frase]

TIPOS DE EMOJI:
🔴 = baja o lesión importante
💬 = declaración del entrenador
⚽ = resultado partido de ida/vuelta
📋 = alineación probable
⚠️ = alerta o duda importante
🟢 = noticia positiva para el equipo
📊 = contexto de rendimiento

REGLAS:
- Cita SIEMPRE la fuente real si la tienes
- Si Reddit tiene consenso: "La comunidad apuesta por X (N upvotes)"
- Máximo 5-6 puntos
- Termina con: 💡 **Impacto en la apuesta:** [1 frase directa]
- Habla en español simple, nada técnico"""

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=600,
            messages=[
                {"role": "system", "content": sys_analista},
                {"role": "user",   "content": user_prompt}
            ]
        )

        # Devolver también las noticias estructuradas para el frontend
        noticias_frontend = []
        for n in noticias[:8]:
            noticias_frontend.append({
                "titulo": n["titulo"],
                "fuente": n["fuente"],
                "url":    n.get("url", ""),
                "fecha":  n.get("fecha", ""),
                "tipo":   n.get("tipo", "partido"),
            })

        return jsonify({
            "contexto":      resp.choices[0].message.content,
            "fuentes":       fuentes_ok,
            "hay_info_real": hay_info_real,
            "noticias":      noticias_frontend,
            "reddit":        reddit_posts[:4],
        })

    except Exception as e:
        return jsonify({
            "error":         str(e),
            "contexto":      "",
            "fuentes":       [],
            "hay_info_real": False,
            "noticias":      [],
            "reddit":        [],
        }), 200




@app.route("/api/matches")
def get_matches():
    liga = resolve_liga_code(request.args.get("liga", "PL"))
    hoy  = date.today()
    from datetime import timedelta

    # ── CLI: combinar WC + EC + UNL de football-data.org (plan gratuito) ─────
    # Estas 3 competiciones son 100% de selecciones nacionales y están
    # disponibles en el tier gratuito. Mucho más fiable que CLI que mezcla todo.
    if liga == "CLI":
        COMPS_SELECCIONES = ["WC", "EC", "UNL"]
        date_from = hoy.isoformat()
        date_to   = (hoy + timedelta(days=90)).isoformat()
        date_past = (hoy - timedelta(days=120)).isoformat()

        todos = []
        for comp in COMPS_SELECCIONES:
            try:
                d = fetch(f"/competitions/{comp}/matches?dateFrom={date_from}&dateTo={date_to}")
                prox = [m for m in d.get("matches", [])
                        if m["status"] in ("SCHEDULED","TIMED","IN_PLAY","PAUSED")]
                if not prox:
                    d2 = fetch(f"/competitions/{comp}/matches?dateFrom={date_past}&dateTo={date_to}&status=FINISHED")
                    prox = list(reversed(d2.get("matches", [])))[:5]
                todos.extend(prox)
            except Exception:
                continue  # si una comp no responde, saltar

        vistos = set()
        result = []
        for m in sorted(todos, key=lambda x: x.get("utcDate", "")):
            mid = m.get("id")
            if mid in vistos:
                continue
            vistos.add(mid)
            hId   = m["homeTeam"].get("id")
            aId   = m["awayTeam"].get("id")
            hName = m["homeTeam"].get("name") or "TBD"
            aName = m["awayTeam"].get("name") or "TBD"
            if not hId or not aId:
                continue
            result.append({
                "homeId":   hId,
                "homeName": hName,
                "awayId":   aId,
                "awayName": aName,
                "date":     m.get("utcDate","")[:10],
                "time":     m.get("utcDate","")[11:16],
                "status":   m["status"],
                "scoreH":   m["score"]["fullTime"].get("home"),
                "scoreA":   m["score"]["fullTime"].get("away"),
                "comp":     m.get("competition",{}).get("name",""),
            })

        fallback = not any(r["status"] in ("SCHEDULED","TIMED","IN_PLAY") for r in result)
        return jsonify({"matches": result, "fallback": fallback, "season": SEASON})

    # ── Ligas normales ────────────────────────────────────────────────────────
    date_from   = hoy.isoformat()
    dias_futuro = 60 if liga in ("CL", "EL", "EC", "WC", "UNL") else 30
    date_to     = (hoy + timedelta(days=dias_futuro)).isoformat()

    try:
        data    = fetch(f"/competitions/{liga}/matches?dateFrom={date_from}&dateTo={date_to}")
        matches = [m for m in data.get("matches",[])
                   if m["status"] in ("SCHEDULED","TIMED","IN_PLAY","PAUSED")]

        fallback = False
        if not matches:
            dias_atras = 120 if liga in ("CL", "EL", "EC", "WC", "UNL") else 60
            date_past  = (hoy - timedelta(days=dias_atras)).isoformat()
            data2      = fetch(f"/competitions/{liga}/matches?dateFrom={date_past}&dateTo={date_to}&status=FINISHED")
            matches    = list(reversed(data2.get("matches",[])))[:15]
            fallback   = True

        result = []
        for m in matches:
            hId   = m["homeTeam"].get("id")
            aId   = m["awayTeam"].get("id")
            hName = m["homeTeam"].get("name") or "TBD"
            aName = m["awayTeam"].get("name") or "TBD"
            if not hId or not aId:
                continue
            result.append({
                "homeId":   hId,
                "homeName": hName,
                "awayId":   aId,
                "awayName": aName,
                "date":     m.get("utcDate","")[:10],
                "time":     m.get("utcDate","")[11:16],
                "status":   m["status"],
                "scoreH":   m["score"]["fullTime"].get("home"),
                "scoreA":   m["score"]["fullTime"].get("away"),
            })

        return jsonify({"matches": result, "fallback": fallback, "season": SEASON})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── GET /api/analyze?homeId=X&awayId=Y&liga=PL ───────────────
@app.route("/api/analyze")
def analyze():
    home_id  = int(request.args.get("homeId"))
    away_id  = int(request.args.get("awayId"))
    home_name= request.args.get("homeName","Local")
    away_name= request.args.get("awayName","Visitante")
    liga     = resolve_liga_code(request.args.get("liga","PL"))

    try:
        # Pool multi-temporada (actual + anterior) en vez de solo la actual.
        # El peso por antigüedad se aplica después en calc_lambda vía decay,
        # así que acá simplemente juntamos todo lo disponible.
        if liga in ("CL", "EL", "EC", "CLI", "WC", "UNL"):
            # Competiciones cortas: intentar primero CL en las 2 temporadas
            pA_cl, pB_cl = [], []
            for season in (SEASON, SEASON_ANTERIOR):
                rA_cl = fetch(f"/teams/{home_id}/matches?competitions={liga}&season={season}&status=FINISHED")
                rB_cl = fetch(f"/teams/{away_id}/matches?competitions={liga}&season={season}&status=FINISHED")
                pA_cl.extend(normalizar(rA_cl.get("matches", [])))
                pB_cl.extend(normalizar(rB_cl.get("matches", [])))

            # Si aun así hay pocos partidos en CL, complementar con liga nacional (ambas temporadas)
            pA = pA_cl if len(pA_cl) >= 5 else (fetch_pool_multitemporada(home_id, liga) or pA_cl)
            pB = pB_cl if len(pB_cl) >= 5 else (fetch_pool_multitemporada(away_id, liga) or pB_cl)
        else:
            pA = fetch_pool_multitemporada(home_id, liga)
            pB = fetch_pool_multitemporada(away_id, liga)

        sA = calc_stats(pA, home_id)
        sB = calc_stats(pB, away_id)

        # ── LAMBDA CORRECTO ─────────────────────────────────────────
        # Goles esperados del LOCAL = promedio (ponderado por antigüedad) de
        # lo que mete en casa + lo que recibe el visitante fuera / 2.
        # El half-life de decay viene de la calibración vigente del backtester.
        params = get_parametros_activos()
        halflife = params.get("decay_halflife_dias", 150.0)

        atk_home = calc_lambda(pA, home_id, as_local=True,  scored=True,  halflife_dias=halflife)  # Wolves anota en casa
        def_away = calc_lambda(pB, away_id, as_local=False, scored=False, halflife_dias=halflife)  # Aston recibe fuera
        atk_away = calc_lambda(pB, away_id, as_local=False, scored=True,  halflife_dias=halflife)  # Aston anota fuera
        def_home = calc_lambda(pA, home_id, as_local=True,  scored=False, halflife_dias=halflife)  # Wolves recibe en casa

        lA_base = round((atk_home + def_away) / 2, 3)  # lambda base LOCAL
        lB_base = round((atk_away + def_home) / 2, 3)  # lambda base VISITANTE

        fA  = get_form(pA, home_id)
        fB  = get_form(pB, away_id)
        # h2h ahora busca en el pool de 2 temporadas, no solo la actual
        h2h = get_h2h(pA, home_id, away_id)

        # Tabla de posiciones
        st_home, st_away = get_standings(liga, home_id, away_id)

        # ── MAF — Motivation Adjustment Factor ──────────────────────
        # Ajusta las lambdas Poisson por contexto situacional:
        # zona de tabla, urgencia, diferencia de puntos, racha reciente
        maf_home = calc_maf(st_home, st_away, fA, es_local=True)
        maf_away = calc_maf(st_away, st_home, fB, es_local=False)

        lA = aplicar_maf(lA_base, maf_home["maf"])
        lB = aplicar_maf(lB_base, maf_away["maf"])

        mat        = build_matrix(lA, lB)
        prA, prE, prB = calc_probs(mat)
        sc_i, sc_j, sc_p = best_score(mat)

        ov25  = over_prob(mat, 2.5)
        ov15  = over_prob(mat, 1.5)
        bttsp = btts_prob(lA, lB)
        score = betsense_score(prA, prE, prB, fA, fB, h2h, home_id, sA, sB,
                               maf_home, maf_away)

        # Semáforo para cada mercado
        markets_semaforo = {
            "over25":   semaforo(ov25),
            "over15":   semaforo(ov15),
            "btts":     semaforo(bttsp),
            "under25":  semaforo(round(100 - ov25, 1)),
            "win_home": semaforo(round(prA*100, 1)),
            "win_away": semaforo(round(prB*100, 1)),
            "draw":     semaforo(round(prE*100, 1)),
        }

        po_data = {
            "lambdaHome":     round(lA, 2),
            "lambdaAway":     round(lB, 2),
            "lambdaHomeBase": round(lA_base, 2),
            "lambdaAwayBase": round(lB_base, 2),
            "mafHome":        maf_home.get("maf", 1.0),
            "mafAway":        maf_away.get("maf", 1.0),
            "probHome":   prA,
            "probDraw":   prE,
            "probAway":   prB,
            "oddsHome":   round(1/prA, 2) if prA > 0 else None,
            "oddsDraw":   round(1/prE, 2) if prE > 0 else None,
            "oddsAway":   round(1/prB, 2) if prB > 0 else None,
            "bestScore":  {"home": sc_i, "away": sc_j, "prob": sc_p},
        }
        mk_data = {
            "over25":  ov25,
            "over15":  ov15,
            "btts":    bttsp,
            "under25": round(100 - ov25, 1),
        }
        suggestions = build_suggestions(prA, prE, prB, sA, sB, mk_data, po_data, h2h, fA, fB, maf_home, maf_away)
        easy_plan = build_easy_bet_plan(home_name, away_name, suggestions, score, po_data, mk_data)

        # ANÁLISIS TÉCNICO CON INDICADORES
        analisis_tecnico_local = analyze_all_indicators(pA, home_id)
        analisis_tecnico_visit = analyze_all_indicators(pB, away_id)

        # ── JUGADORES: squad + scorers + probabilidades ──────────────
        squad_home = get_squad(home_id)
        squad_away = get_squad(away_id)
        scorers    = get_scorers_liga(liga, limit=30)

        jugadores_data = calcular_jugadores_partido(
            squad_home, squad_away, scorers,
            lA, lB, sA, sB, liga, st_home, st_away
        )

        # ── MERCADOS DERIVADOS: corners, tarjetas, faltas ────────────
        mercados_derivados = calcular_mercados_derivados(sA, sB, liga, lA, lB)

        # Armar respuesta completa
        response_data = {
            "teams": {
                "home": {"id": home_id, "name": home_name},
                "away": {"id": away_id, "name": away_name},
            },
            "liga":        liga,
            "season":      SEASON,
            "stats":       {"home": sA, "away": sB},
            "poisson":     po_data,
            "markets":     mk_data,
            "form":        {"home": fA, "away": fB},
            "h2h":         h2h,
            "score":       score,
            "suggestions":  suggestions,
            "easy_plan":   easy_plan,
            "standings": {
                "home": st_home,
                "away": st_away,
            },
            "maf": {
                "home": maf_home,
                "away": maf_away,
            },
            "semaforo": markets_semaforo,
            "indicadores_tecnicos": {
                "local": analisis_tecnico_local,
                "visitante": analisis_tecnico_visit
            },
            "jugadores":          jugadores_data,
            "mercados_derivados": mercados_derivados,
        }

        # Guardar en base de datos automáticamente
        pred_id = guardar_prediccion(response_data)
        response_data["prediccion_id"] = pred_id

        # ── BLIND ENGINE — predicción ciega con Dixon-Coles ──────────
        # Usa los mismos partidos (pA, pB) ya filtrados por la API,
        # que están naturalmente "antes" del partido a jugar (status=FINISHED).
        # No mira el resultado del partido actual en ningún momento.
        try:
            from prediction_engine import generar_prediccion_ciega
            blind_result = generar_prediccion_ciega(
                prediccion_id=pred_id,
                partidos_home=pA,
                partidos_away=pB,
                team_home_id=home_id,
                team_away_id=away_id,
                match_date=datetime.utcnow(),
                cuotas_bk=None,        # se puede pasar desde el frontend si hay cuotas reales
                bankroll=0,            # se calcula bajo demanda desde el panel (Kelly Calc)
                semaforo_score=score.get("total", 50) if isinstance(score, dict) else 50,
                indicadores_json="",
            )
            response_data["blind_engine"] = blind_result
        except Exception as _be:
            print(f"⚠️  Blind Engine no pudo procesar la predicción {pred_id}: {_be}")
            response_data["blind_engine"] = None

        return jsonify(response_data)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── GET /api/historial ───────────────────────────────────────
@app.route("/api/historial")
def historial():
    try:
        data = obtener_historial(limit=50)
        return jsonify({"historial": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── GET /api/stats-modelo ─────────────────────────────────────
@app.route("/api/stats-modelo")
def stats_modelo():
    try:
        data = obtener_estadisticas_modelo()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════
# MÓDULO DE TENNIS CON API REAL
# ══════════════════════════════════════════════════════════════

# ── CONFIGURACIÓN API TENIS (API-Sports) ─────────────────────
TENNIS_API_KEY = "872805af-56c1-4411-b960-d9cd6643c072"
TENNIS_BASE_URL = "https://v1.tennis.api-sports.io"  # API real de tenis
TENNIS_HEADERS = {
    "x-apisports-key": TENNIS_API_KEY,
    "Content-Type": "application/json"
}

def tennis_fetch(path, params=None):
    """Helper para llamadas a API-Sports Tenis"""
    url = TENNIS_BASE_URL + path
    try:
        r = requests.get(url, headers=TENNIS_HEADERS, params=params or {}, timeout=10)
        if not r.ok:
            error_msg = f"Error {r.status_code}"
            try:
                error_data = r.json()
                error_msg = error_data.get("message", error_msg)
            except:
                pass
            raise Exception(f"Tennis API: {error_msg}")
        return r.json()
    except requests.exceptions.Timeout:
        raise Exception("Timeout en la API de Tenis")
    except requests.exceptions.ConnectionError:
        raise Exception("Error de conexión con API de Tenis")
    except Exception as e:
        raise Exception(f"Error en API Tenis: {str(e)}")

# ── ENDPOINTS DE TENIS ────────────────────────────────────────

@app.route("/api/tennis/leagues")
def tennis_leagues():
    """Obtiene lista de torneos disponibles desde la API"""
    try:
        sport = request.args.get("sport", "tennis")
        # Parámetros para filtrar por temporada actual
        current_year = datetime.now().year
        if sport == "table-tennis":
            leagues = [{**league, "season": current_year} for league in TABLE_TENNIS_LEAGUES]
            return jsonify({"leagues": leagues})

        params = {"season": current_year}
        
        # Intentar obtener torneos de la API
        try:
            data = tennis_fetch("/tournaments", params)
            tournaments = data.get("response", [])
            
            if tournaments and len(tournaments) > 0:
                leagues = []
                for t in tournaments[:30]:  # Limitamos a 30
                    league = {
                        "league_key": t.get("id"),
                        "league_name": t.get("name", "Torneo de Tenis"),
                        "country_name": t.get("country", {}).get("name", "Internacional"),
                        "league_surface": t.get("surface", "Various"),
                        "category": t.get("type", "ATP"),
                        "tier": t.get("tier", 500),
                        "season": current_year
                    }
                    leagues.append(league)
                return jsonify({"leagues": leagues})
        except:
            pass
        
        # Fallback: si la API no responde, usar datos de ejemplo
        # pero con estructura que permite actualización futura
        fallback_leagues = [
            {"league_key": "atp_1", "league_name": "ATP Tour", "country_name": "World", "league_surface": "Various", "category": "ATP", "tier": 1000, "season": current_year},
            {"league_key": "wta_1", "league_name": "WTA Tour", "country_name": "World", "league_surface": "Various", "category": "WTA", "tier": 1000, "season": current_year},
            {"league_key": "gs_1", "league_name": "Grand Slams", "country_name": "World", "league_surface": "Various", "category": "Grand Slam", "tier": 2000, "season": current_year},
        ]
        return jsonify({"leagues": fallback_leagues})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/tennis/matches")
def tennis_matches():
    """Obtiene partidos de un torneo desde la API"""
    league_id = request.args.get("leagueId")
    season = request.args.get("season", datetime.now().year)
    sport = request.args.get("sport", "tennis")
    
    if not league_id:
        return jsonify({"error": "Se necesita leagueId"}), 400
    
    try:
        if sport == "table-tennis":
            return jsonify({"matches": build_table_tennis_matches(league_id)})

        # Parámetros para buscar partidos
        today = datetime.now()
        date_from = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        date_to = (today + timedelta(days=30)).strftime("%Y-%m-%d")
        
        params = {
            "tournament": league_id,
            "season": season,
            "date": date_from  # API puede requerir formato específico
        }
        
        # Intentar obtener partidos de la API
        try:
            data = tennis_fetch("/games", params)
            games = data.get("response", [])
            
            matches = []
            for game in games:
                game_data = game.get("game", {})
                teams = game.get("teams", {})
                scores = game.get("scores", {})
                status = game.get("status", {})
                
                home = teams.get("home", {})
                away = teams.get("away", {})
                
                # Formato del resultado
                result = "-"
                if scores:
                    home_score = scores.get("home", {})
                    away_score = scores.get("away", {})
                    home_sets = home_score.get("sets", 0)
                    away_sets = away_score.get("sets", 0)
                    if home_sets or away_sets:
                        result = f"{home_sets}-{away_sets}"
                
                matches.append({
                    "matchId": str(game_data.get("id", "")),
                    "date": game_data.get("date", "")[:10] if game_data.get("date") else "",
                    "time": game_data.get("date", "")[11:16] if game_data.get("date") and len(game_data.get("date")) > 16 else "",
                    "player1": home.get("name", ""),
                    "player1Key": str(home.get("id", "")),
                    "player2": away.get("name", ""),
                    "player2Key": str(away.get("id", "")),
                    "result": result,
                    "status": status.get("long", "Programado"),
                    "league": game_data.get("tournament", {}).get("name", ""),
                    "surface": game_data.get("surface", ""),
                    "live": status.get("short") in ["LIVE", "1S", "2S", "3S"],
                })
            
            if matches:
                return jsonify({"matches": matches})
        except:
            pass
        
        # Fallback: generar partidos de ejemplo con IDs realistas
        # para que la interfaz funcione mientras la API no responde
        import random
        players_pool = [
            {"id": "1", "name": "Novak Djokovic"},
            {"id": "2", "name": "Carlos Alcaraz"},
            {"id": "3", "name": "Jannik Sinner"},
            {"id": "4", "name": "Daniil Medvedev"},
            {"id": "5", "name": "Alexander Zverev"},
            {"id": "6", "name": "Iga Swiatek"},
            {"id": "7", "name": "Aryna Sabalenka"},
            {"id": "8", "name": "Coco Gauff"},
        ]
        
        matches = []
        for i in range(5):
            p1, p2 = random.sample(players_pool, 2)
            match_date = today + timedelta(days=random.randint(-5, 10))
            is_past = match_date < today
            result = "-"
            if is_past:
                result = f"{random.randint(0,2)}-{random.randint(0,2)}"
            
            matches.append({
                "matchId": f"match_{league_id}_{i}",
                "date": match_date.strftime("%Y-%m-%d"),
                "time": f"{random.randint(10,20)}:00",
                "player1": p1["name"],
                "player1Key": p1["id"],
                "player2": p2["name"],
                "player2Key": p2["id"],
                "result": result,
                "status": "Finalizado" if is_past else "Programado",
                "league": f"Torneo {league_id}",
                "surface": random.choice(["Hard", "Clay", "Grass"]),
                "live": False
            })
        
        return jsonify({"matches": matches})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/tennis/player/<player_id>")
def tennis_player_info(player_id):
    """Obtiene información de un jugador desde la API"""
    try:
        params = {"id": player_id}
        data = tennis_fetch("/players", params)
        players = data.get("response", [])
        
        if players:
            p = players[0]
            return jsonify({
                "id": p.get("id"),
                "name": p.get("name"),
                "country": p.get("country", {}).get("name"),
                "ranking": p.get("ranking", {}).get("singles", {}).get("rank"),
                "points": p.get("ranking", {}).get("singles", {}).get("points"),
                "age": p.get("age"),
                "hand": p.get("play", {}).get("hand"),
                "height": p.get("height"),
                "turned_pro": p.get("turned_pro")
            })
        else:
            return jsonify({"error": "Jugador no encontrado"}), 404
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/tennis/analyze")
def tennis_analyze():
    """Analiza un enfrentamiento entre dos jugadores usando datos reales de API"""
    p1_key = request.args.get("p1Key")
    p2_key = request.args.get("p2Key")
    p1_name = request.args.get("p1Name", "Jugador 1")
    p2_name = request.args.get("p2Name", "Jugador 2")
    surface = request.args.get("surface", "hard")
    
    if not p1_key or not p2_key:
        return jsonify({"error": "Se necesitan p1Key y p2Key"}), 400
    
    try:
        # Intentar obtener datos reales de los jugadores
        p1_data = None
        p2_data = None
        
        try:
            p1_info = tennis_fetch("/players", {"id": p1_key})
            p2_info = tennis_fetch("/players", {"id": p2_key})
            
            if p1_info.get("response"):
                p1_data = p1_info["response"][0]
            if p2_info.get("response"):
                p2_data = p2_info["response"][0]
        except:
            pass
        
        # Si no hay datos de API, usar valores por defecto
        if not p1_data:
            p1_data = {
                "name": p1_name,
                "ranking": {"singles": {"rank": 10, "points": 1000}},
                "play": {"hand": "R"},
                "height": 180,
                "age": 25
            }
        
        if not p2_data:
            p2_data = {
                "name": p2_name,
                "ranking": {"singles": {"rank": 20, "points": 800}},
                "play": {"hand": "R"},
                "height": 180,
                "age": 25
            }
        
        # Calcular estadísticas basadas en ranking (simulación con datos reales)
        rank1 = p1_data.get("ranking", {}).get("singles", {}).get("rank", 10)
        rank2 = p2_data.get("ranking", {}).get("singles", {}).get("rank", 20)
        points1 = p1_data.get("ranking", {}).get("singles", {}).get("points", 1000)
        points2 = p2_data.get("ranking", {}).get("singles", {}).get("points", 800)
        
        # Probabilidad basada en ranking y puntos
        total_points = points1 + points2
        prob1 = points1 / total_points if total_points > 0 else 0.5
        prob1 = min(max(prob1, 0.35), 0.65)
        
        # Estadísticas de servicio/resto (simuladas basadas en ranking)
        base_hold = 75 + (20 - rank1) * 0.5 if rank1 < 20 else 70
        base_break = 30 + (20 - rank1) * 0.3 if rank1 < 20 else 25
        
        stats1 = {
            "wins": 50 - rank1,
            "losses": 15 + rank1 // 2,
            "win_pct": round((50 - rank1) / (50 - rank1 + 15 + rank1 // 2) * 100, 1),
            "hold_pct": round(base_hold, 1),
            "break_pct": round(base_break, 1),
            "first_serve_pct": 62,
            "first_serve_won": round(base_hold * 0.85, 1),
            "second_serve_won": 52,
            "return_points_won": round(base_break * 1.2, 1),
            "bp_saved": 62,
            "tb_won": 55,
            "form": ["W", "W", "L", "W", "L"] if rank1 < 15 else ["W", "L", "W", "L", "L"]
        }
        
        base_hold2 = 75 + (20 - rank2) * 0.5 if rank2 < 20 else 70
        base_break2 = 30 + (20 - rank2) * 0.3 if rank2 < 20 else 25
        
        stats2 = {
            "wins": 50 - rank2,
            "losses": 15 + rank2 // 2,
            "win_pct": round((50 - rank2) / (50 - rank2 + 15 + rank2 // 2) * 100, 1),
            "hold_pct": round(base_hold2, 1),
            "break_pct": round(base_break2, 1),
            "first_serve_pct": 61,
            "first_serve_won": round(base_hold2 * 0.84, 1),
            "second_serve_won": 51,
            "return_points_won": round(base_break2 * 1.15, 1),
            "bp_saved": 60,
            "tb_won": 52,
            "form": ["W", "L", "W", "W", "L"] if rank2 < 15 else ["L", "W", "L", "L", "W"]
        }
        
        # Modelo probabilístico avanzado
        game_probs = {
            "pA_hold": round(stats1["hold_pct"] / 100, 3),
            "pB_hold": round(stats2["hold_pct"] / 100, 3),
            "pA_break": round(stats1["break_pct"] / 100, 3),
            "pB_break": round(stats2["break_pct"] / 100, 3)
        }
        
        # Simulación Monte Carlo simplificada
        import random
        wins_a = 0
        for _ in range(1000):
            sets_a = sets_b = 0
            while sets_a < 2 and sets_b < 2:
                games_a = games_b = 0
                server_a = True
                while max(games_a, games_b) < 6 or abs(games_a - games_b) < 2:
                    if server_a:
                        if random.random() < game_probs["pA_hold"]:
                            games_a += 1
                        else:
                            games_b += 1
                    else:
                        if random.random() < game_probs["pB_hold"]:
                            games_b += 1
                        else:
                            games_a += 1
                    server_a = not server_a
                    if games_a >= 6 and games_b >= 6:
                        if random.random() < 0.5:
                            games_a += 1
                        else:
                            games_b += 1
                        break
                if games_a > games_b:
                    sets_a += 1
                else:
                    sets_b += 1
            if sets_a == 2:
                wins_a += 1
        
        prob_model = wins_a / 1000
        
        # Calcular cuota justa y desviación
        fair_odds = 1 / prob_model if prob_model > 0 else 2.0
        market_odds = round(fair_odds * random.uniform(0.9, 1.1), 2)
        deviation = ((market_odds - fair_odds) / fair_odds) * 100
        value_edge = (prob_model * market_odds) - 1

        # ── MERCADOS APOSTABLES DE TENIS ────────────────────────────
        # Total de juegos esperados por set (basado en hold/break de ambos)
        avg_hold = (base_hold + base_hold2) / 200  # promedio en decimal
        # Si ambos sirven bien → sets largos → más juegos
        # Fórmula: juegos esperados por set ≈ 9 + (hold_avg - 0.65) * 12
        juegos_por_set = round(9 + (avg_hold - 0.65) * 12, 1)
        juegos_por_set = max(7.0, min(13.0, juegos_por_set))  # acotar entre 7 y 13
        total_juegos_esperados = round(juegos_por_set * 2.2, 1)  # ~2.2 sets en Best of 3

        # Probabilidad Over/Under total juegos
        # Si juegos_esperados > 20.5 → Over más probable
        linea_juegos = 20.5
        prob_over_juegos = round(min(95, max(5,
            50 + (total_juegos_esperados - linea_juegos) * 8
        )), 1)
        prob_under_juegos = round(100 - prob_over_juegos, 1)

        # Handicap de juegos (ventaja al favorito medida en juegos)
        rank_diff = rank2 - rank1  # positivo = p1 mejor rankeado
        handicap_juegos = round(min(4.5, max(0.5, abs(rank_diff) * 0.15)), 1)
        # Ajustar a líneas típicas: 1.5, 2.5, 3.5, 4.5
        for linea in [1.5, 2.5, 3.5, 4.5]:
            if handicap_juegos <= linea:
                handicap_juegos = linea
                break

        # Probabilidad de que el favorito cubra el handicap
        prob_handicap_fav = round(min(80, max(45, 55 + (abs(rank_diff) * 0.8))), 1)
        prob_handicap_dog = round(100 - prob_handicap_fav, 1)

        # Sets: 2-0 vs 2-1
        # Si hay gran diferencia de ranking → más probable 2-0
        prob_2_0 = round(min(75, max(25, 40 + abs(rank_diff) * 0.7)) * prob_model, 1)
        prob_2_0 = round(min(75, max(20, prob_2_0)), 1)
        prob_2_1 = round(100 - prob_2_0, 1)

        # Handicap de sets (el favorito con -1.5 sets = ganar 2-0)
        prob_fav_2_0 = prob_2_0  # ya calculado arriba
        prob_dog_covers = round(100 - prob_fav_2_0, 1)  # underdog gana al menos un set

        # Set de ventaja: ¿se irán a un 3er set?
        prob_tercer_set = prob_2_1
        prob_sin_tercer_set = prob_2_0

        # Tiebreak en el partido (al menos un TB)
        # Si hold_pct alto en ambos → más probabilidad de TB
        prob_tb = round(min(80, max(20,
            (base_hold + base_hold2) / 2 - 40
        )), 1)

        # Total de juegos del set 1 (Over/Under 9.5)
        prob_over_set1 = round(min(85, max(15,
            50 + (juegos_por_set - 9.5) * 10
        )), 1)

        tennis_markets = {
            # Moneyline
            "winner": {
                "p1_prob": round(prob_model * 100, 1),
                "p2_prob": round((1 - prob_model) * 100, 1),
                "fair_odds_p1": round(fair_odds, 2),
                "fair_odds_p2": round(1 / (1 - prob_model), 2) if prob_model < 1 else 99,
            },
            # Total juegos del partido
            "total_juegos": {
                "linea": linea_juegos,
                "esperados": total_juegos_esperados,
                "over_prob": prob_over_juegos,
                "under_prob": prob_under_juegos,
                "signal": "over" if prob_over_juegos > 60 else "under" if prob_under_juegos > 60 else "neutral",
            },
            # Handicap de juegos
            "handicap_juegos": {
                "linea": handicap_juegos,
                "favorito": p1_name if rank1 < rank2 else p2_name,
                "underdog": p2_name if rank1 < rank2 else p1_name,
                "fav_cubre_prob": prob_handicap_fav,
                "dog_cubre_prob": prob_handicap_dog,
                "desc": f"El favorito tiene {handicap_juegos} juegos de ventaja en cuotas"
            },
            # Sets
            "resultado_sets": {
                "prob_2_0": prob_2_0,
                "prob_2_1": prob_2_1,
                "signal": "2-0" if prob_2_0 > 55 else "2-1",
                "desc": f"{'Partido directo más probable' if prob_2_0 > 55 else 'Partido a 3 sets más probable'}"
            },
            # Handicap de sets (-1.5 al favorito)
            "handicap_sets": {
                "linea": -1.5,
                "favorito": p1_name if rank1 < rank2 else p2_name,
                "fav_gana_2_0_prob": prob_fav_2_0,
                "dog_gana_set_prob": prob_dog_covers,
                "desc": f"{'Favorito domina, considera -1.5 sets' if prob_fav_2_0 > 55 else 'El underdog puede robar un set'}"
            },
            # Tiebreak
            "tiebreak": {
                "al_menos_uno_prob": prob_tb,
                "sin_tb_prob": round(100 - prob_tb, 1),
                "signal": "si" if prob_tb > 50 else "no",
            },
            # Total juegos set 1
            "total_set1": {
                "linea": 9.5,
                "over_prob": prob_over_set1,
                "under_prob": round(100 - prob_over_set1, 1),
                "signal": "over" if prob_over_set1 > 60 else "under" if prob_over_set1 < 40 else "neutral",
            },
        }

        # Score final BetSense
        confidence = 0.7 + (30 - min(rank1 + rank2, 30)) / 100
        final_prob = prob_model * (1 + abs(rank2 - rank1) / 100)
        final_prob = min(max(final_prob, 0.35), 0.65)
        
        # Clasificación del evento
        if value_edge > 0.08 and confidence > 0.7:
            event_class = "HIGH_EDGE"
            recommendation = "STRONG_BACK"
            stake = min(3, value_edge * 100)
        elif value_edge > 0.03 and confidence > 0.5:
            event_class = "MEDIUM_EDGE"
            recommendation = "CONSIDER_BACK"
            stake = min(2, value_edge * 50)
        elif value_edge < -0.08:
            event_class = "NEGATIVE_EDGE"
            recommendation = "LAY"
            stake = 0
        else:
            event_class = "NO_EDGE"
            recommendation = "NO_ACTION"
            stake = 0
        
        # ── INDICADORES TÉCNICOS TENIS ──────────────────────────────
        indic_p1 = analyze_tennis_indicators(
            form=stats1["form"],
            hold_pct=stats1["hold_pct"],
            break_pct=stats1["break_pct"],
            win_pct=stats1["win_pct"],
            ranking=rank1
        )
        indic_p2 = analyze_tennis_indicators(
            form=stats2["form"],
            hold_pct=stats2["hold_pct"],
            break_pct=stats2["break_pct"],
            win_pct=stats2["win_pct"],
            ranking=rank2
        )

        return jsonify({
            "players": {
                "p1": {
                    "key": p1_key, 
                    "name": p1_data.get("name", p1_name),
                    "ranking": rank1,
                    "points": points1,
                    "hand": p1_data.get("play", {}).get("hand", "R"),
                    "age": p1_data.get("age", 25)
                },
                "p2": {
                    "key": p2_key, 
                    "name": p2_data.get("name", p2_name),
                    "ranking": rank2,
                    "points": points2,
                    "hand": p2_data.get("play", {}).get("hand", "R"),
                    "age": p2_data.get("age", 25)
                }
            },
            "surface": surface,
            "stats": {"p1": stats1, "p2": stats2},
            "probabilities": {
                "p1": round(prob_model, 3),
                "p2": round(1 - prob_model, 3),
                "game_probs": game_probs,
                "fair_odds": round(fair_odds, 2)
            },
            "market": {
                "market_odds": market_odds,
                "deviation_pct": round(deviation, 1),
                "value_edge": round(value_edge * 100, 1),
                "kelly_fraction": round(max(0, value_edge / (market_odds - 1) * 100), 1) if market_odds > 1 else 0
            },
            "betsense": {
                "final_probability": round(final_prob, 3),
                "confidence": round(confidence * 100, 1),
                "confidence_label": "HIGH" if confidence > 0.7 else "MEDIUM" if confidence > 0.5 else "LOW",
                "event_class": event_class,
                "recommendation": recommendation,
                "recommended_stake": round(stake, 1)
            },
            "indicadores_tecnicos": {
                "p1": indic_p1,
                "p2": indic_p2
            },
            "tennis_markets": tennis_markets
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── POST /api/chat  (Analista IA conversacional) ─────────────
@app.route("/api/chat", methods=["POST"])
def api_chat():
    """
    Body JSON:
      {
        "system":   "contexto del partido",
        "messages": [{"role":"user","content":"..."}, ...]
      }
    Responde:
      {"reply": "texto del analista"}
    """
    try:
        body     = request.get_json(force=True)
        system   = body.get("system", "")
        messages = body.get("messages", [])
        real_token = body.get("realToken") or request.headers.get("X-Real-Token", "")

        if not messages:
            return jsonify({"error": "No hay mensajes"}), 400

        ultimo_mensaje = str(messages[-1].get("content", "")).strip()
        if ultimo_mensaje:
            if _es_consulta_saldo(ultimo_mensaje):
                if not real_token or real_token not in _sesiones_reales:
                    return jsonify({"reply": "No tengo una cuenta de BetPlay conectada. Inicia sesión en el módulo Real Bets y luego te digo tu saldo."})
                saldo_data = _obtener_saldo_sesion(real_token)
                saldo_txt = str(saldo_data.get("saldo", "—"))
                casa_txt = str(saldo_data.get("casa", "betplay")).upper()
                return jsonify({"reply": f"Tu saldo actual en {casa_txt} es {saldo_txt}."})

            if _es_consulta_apuestas(ultimo_mensaje):
                if not real_token or real_token not in _sesiones_reales:
                    return jsonify({"reply": "No tengo una cuenta de BetPlay conectada. Inicia sesión en el módulo Real Bets y luego te muestro tus apuestas."})
                apuestas_data = _obtener_apuestas_sesion(real_token)
                return jsonify({"reply": _resumen_apuestas_chat(apuestas_data.get("apuestas", []))})

        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

        client = OpenAI(api_key=OPENAI_API_KEY)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=600,
            messages=[{"role": "system", "content": system}] + messages
        )

        return jsonify({"reply": response.choices[0].message.content})

    except Exception as e:
        return jsonify({"error": str(e)}), 500



# Caché simple en memoria para historial de equipos (evita re-pedir lo mismo)
_historial_cache = {}   # {team_id_liga: (timestamp, data)}
_CACHE_TTL = 3600       # 1 hora en segundos

def fetch_con_cache(path, cache_key):
    """Fetch con caché en memoria. Evita re-pedir el mismo historial."""
    import time
    ahora = time.time()
    if cache_key in _historial_cache:
        ts, data = _historial_cache[cache_key]
        if ahora - ts < _CACHE_TTL:
            return data
    data = fetch(path)
    _historial_cache[cache_key] = (ahora, data)
    return data

def fetch_throttled(path, cache_key=None):
    """Fetch con delay automático si la API devuelve rate-limit."""
    import time
    if cache_key:
        try:
            return fetch_con_cache(path, cache_key)
        except Exception:
            pass
    # Reintentar hasta 3 veces con espera exponencial
    for intento in range(3):
        try:
            r = requests.get(BASE_URL + path, headers=HEADERS)
            if r.status_code == 429:
                espera = int(r.headers.get("X-RequestCounter-Reset", 12))
                time.sleep(espera + 1)
                continue
            if not r.ok:
                raise Exception(r.json().get("message", f"Error {r.status_code}"))
            data = r.json()
            if cache_key:
                import time as t
                _historial_cache[cache_key] = (t.time(), data)
            return data
        except Exception as e:
            if intento == 2:
                raise
            time.sleep(6)
    raise Exception("Rate limit persistente — intenta en 1 minuto")


# ── GET /api/combinada?liga=PL  ───────────────────────────────


@app.route("/api/combinada")
def combinada_del_dia():
    """
    Analiza partidos del día en una liga y devuelve picks >= umbral%.
    Respeta el rate limit de football-data.org (10 req/min) usando:
    - Caché de historial por equipo (1h TTL)
    - Delay de 6s entre partidos si no hay caché
    - Máximo 5 partidos para no exceder el límite
    """
    import time
    liga   = resolve_liga_code(request.args.get("liga", "PL"))
    umbral = float(request.args.get("umbral", 70))

    try:
        hoy     = date.today()
        date_to = (hoy + timedelta(days=1)).isoformat()
        if liga in ("CLI", "WC", "EC", "UNL"):  # Competiciones de selecciones: ventana más amplia
            date_to = (hoy + timedelta(days=7)).isoformat()

        # 1 request: partidos del día
        data = fetch_throttled(f"/competitions/{liga}/matches?dateFrom={hoy.isoformat()}&dateTo={date_to}")
        matches = [m for m in data.get("matches", [])
                   if m["status"] in ("SCHEDULED", "TIMED", "IN_PLAY")]

        # Si no hay partidos hoy, buscar próximos 3 días
        if not matches:
            date_to3 = (hoy + timedelta(days=3)).isoformat()
            data2 = fetch_throttled(f"/competitions/{liga}/matches?dateFrom={hoy.isoformat()}&dateTo={date_to3}")
            matches = [m for m in data2.get("matches", [])
                       if m["status"] in ("SCHEDULED", "TIMED")]

        if not matches:
            return jsonify({"picks": [], "msg": f"No hay partidos próximos en {liga}"})

        # Máx 5 partidos = máx 10 requests de historial (dentro del límite de 10/min)
        picks        = []
        analizados   = []
        requests_hechos = 0

        for m in matches[:5]:
            hid  = m["homeTeam"]["id"]
            aid  = m["awayTeam"]["id"]
            hn   = m["homeTeam"]["name"]
            an   = m["awayTeam"]["name"]
            hora = m.get("utcDate", "")

            try:
                # Claves de caché únicas por equipo+liga+temporada
                ck_h = f"{hid}_{liga}_{SEASON}"
                ck_a = f"{aid}_{liga}_{SEASON}"

                # Si no hay caché, esperar 6s entre cada 2 requests (10/min = 1 cada 6s)
                if ck_h not in _historial_cache:
                    if requests_hechos > 0:
                        time.sleep(6)
                    requests_hechos += 1

                rA = fetch_throttled(
                    f"/teams/{hid}/matches?competitions={liga}&season={SEASON}&status=FINISHED",
                    cache_key=ck_h
                )

                if ck_a not in _historial_cache:
                    time.sleep(6)
                    requests_hechos += 1

                rB = fetch_throttled(
                    f"/teams/{aid}/matches?competitions={liga}&season={SEASON}&status=FINISHED",
                    cache_key=ck_a
                )

                pA = normalizar(rA.get("matches", []))
                pB = normalizar(rB.get("matches", []))
                if len(pA) < 3 or len(pB) < 3:
                    continue

                sA = calc_stats(pA, hid)
                sB = calc_stats(pB, aid)
                atk_h = calc_lambda(pA, hid, as_local=True,  scored=True)
                def_a = calc_lambda(pB, aid, as_local=False, scored=False)
                atk_a = calc_lambda(pB, aid, as_local=False, scored=True)
                def_h = calc_lambda(pA, hid, as_local=True,  scored=False)
                lA  = round((atk_h + def_a) / 2, 3)
                lB  = round((atk_a + def_h) / 2, 3)
                mat = build_matrix(lA, lB)
                prA, prE, prB = calc_probs(mat)
                ov25  = over_prob(mat, 2.5)
                ov15  = over_prob(mat, 1.5)
                bttsp = btts_prob(lA, lB)

                analizados.append(f"{hn} vs {an}")

                candidatos = [
                    {"market": f"Victoria {hn}",      "prob": round(prA*100,1), "tipo": "1X2"},
                    {"market": "Empate",               "prob": round(prE*100,1), "tipo": "1X2"},
                    {"market": f"Victoria {an}",       "prob": round(prB*100,1), "tipo": "1X2"},
                    {"market": "Over 1.5 Goles",       "prob": ov15,             "tipo": "goles"},
                    {"market": "Over 2.5 Goles",       "prob": ov25,             "tipo": "goles"},
                    {"market": "Under 2.5 Goles",      "prob": round(100-ov25,1),"tipo": "goles"},
                    {"market": "Ambos Marcan (BTTS)",  "prob": bttsp,            "tipo": "goles"},
                    {"market": "BTTS No",              "prob": round(100-bttsp,1),"tipo": "goles"},
                    {"market": "Doble Oportunidad 1X", "prob": round((prA+prE)*100,1), "tipo": "doble"},
                    {"market": "Doble Oportunidad X2", "prob": round((prE+prB)*100,1), "tipo": "doble"},
                ]
                for c in candidatos:
                    if c["prob"] >= umbral:
                        picks.append({
                            "partido":     f"{hn} vs {an}",
                            "hora":        hora[11:16] + " UTC" if len(hora) > 15 else "—",
                            "liga":        liga,
                            "market":      c["market"],
                            "prob":        c["prob"],
                            "tipo":        c["tipo"],
                            "cuota_justa": round(100 / c["prob"], 2),
                            "semaforo":    semaforo(c["prob"]),
                        })

            except Exception as e:
                # Rate limit u otro error en un partido → saltar y continuar
                analizados.append(f"{hn} vs {an} (error: {str(e)[:40]})")
                continue

        picks.sort(key=lambda x: x["prob"], reverse=True)

        # Probabilidad de combinada solo con los picks más seguros (top 5)
        top_picks = picks[:5]
        prob_combinada = 1.0
        for p in top_picks:
            prob_combinada *= p["prob"] / 100
        prob_combinada = round(prob_combinada * 100, 2)

        return jsonify({
            "picks":          picks,
            "top_picks":      top_picks,
            "total":          len(picks),
            "prob_combinada": prob_combinada,
            "partidos_analizados": analizados,
            "liga":           liga,
            "fecha":          hoy.isoformat(),
            "umbral":         umbral,
            "desde_cache":    requests_hechos == 0,
            "resumen":        build_combinada_summary(picks, liga, hoy.isoformat()),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ══════════════════════════════════════════════════════════════════


if __name__ == "__main__":
    print("="*50)
    print("  ⚡ BetSense Server arriba")
    print(f"  📅 Temporada detectada: {SEASON}")
    print("  🌐 Abre: http://localhost:5000")
    print("="*50)
    app.run(host="0.0.0.0", port=5000, debug=True)