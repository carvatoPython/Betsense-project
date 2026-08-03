"""
backtest_engine.py — BetSense
Backtester ciego / motor de auto-calibración interna.

Este módulo NO es parte del flujo que ve el usuario final y NO toca
betsense.html — es una herramienta interna para vos y Jeyson.

Qué hace:
  1. REFRESH — descarga en bloque partidos históricos FINISHED de las
     5 ligas que soporta BetSense, usando /competitions/{liga}/matches
     ?season=X (1 request = 1 temporada completa, no equipo por equipo).
     Los cachea en la tabla partidos_historicos vía database.py.
  2. AUTOTUNE — para cada partido "objetivo" (temporadas de evaluación),
     reconstruye el pool de partidos ANTERIORES a esa fecha (nunca
     futuros — anti data-leakage), corre el mismo pipeline de
     model_core.py que usa betAI.py en producción, genera una
     predicción a ciegas (ignora el resultado real al predecir), y la
     compara contra el resultado que sí ocurrió (RPS + Brier).
     Prueba un grid de (decay_halflife_dias, maf_peso) y se queda con
     la combinación que minimiza el RPS promedio. La guarda en
     parametros_activos — eso es lo que betAI.py lee en producción.
     Cada combinación probada queda logueada en backtest_runs.

Uso:
    python backtest_engine.py refresh    # descarga/actualiza la caché
    python backtest_engine.py autotune   # corre el grid search sobre la caché local
    python backtest_engine.py run        # refresh + autotune

Nota sobre alcance: el cuello de botella real es la API, no el cómputo.
/competitions/{liga}/matches?season=X trae la temporada completa en un
solo request, así que 5 ligas × 3 temporadas = 15 requests para tener
~3800 partidos en caché. El grid search después corre 100% local sobre
esa caché — se puede repetir tantas veces como quieras sin gastar más
cupo de API.
"""

import sys
import time
from datetime import datetime
from itertools import product

import requests

from database import (
    init_db, guardar_partidos_historicos, obtener_partidos_historicos,
    guardar_parametros_activos, guardar_backtest_run, obtener_parametros_activos,
)
from model_core import (
    calc_lambda, build_matrix, calc_probs, get_form, get_h2h, calc_maf, aplicar_maf,
)

API_KEY  = "347e282989fc4fab9569c77f0da86527"
BASE_URL = "https://api.football-data.org/v4"
HEADERS  = {"X-Auth-Token": API_KEY}

# Las 5 ligas que ya soporta BetSense
LIGAS_BACKTEST = ["PL", "PD", "BL1", "SA", "FL1"]


def _temporada_actual() -> int:
    hoy = datetime.now().date()
    return hoy.year if hoy.month >= 8 else hoy.year - 1


SEASON_ACTUAL = _temporada_actual()
# 3 temporadas en caché: las 2 más viejas sirven de "lookback" para que
# los partidos de las últimas 2 temporadas ya tengan historial detrás.
SEASONS_CACHE = [str(SEASON_ACTUAL - i) for i in (2, 1, 0)]
# Solo se EVALÚA (se mide RPS) sobre las últimas 2 — la más vieja es puro contexto.
SEASONS_EVAL  = [str(SEASON_ACTUAL - i) for i in (1, 0)]

# Grid search — rango discutido con Carlos en el diseño
GRID_HALFLIFE = [60, 90, 120, 150, 180, 270, 365]
GRID_MAF_PESO = [0.0, 0.5, 1.0, 1.5, 2.0]

# Mínimo de partidos previos en caché para intentar predecir un partido
# (si un equipo tiene menos, el pool es demasiado ruidoso — se salta).
MIN_PARTIDOS_PREVIOS = 4


# ══════════════════════════════════════════════════════════════
# 1. REFRESH — descarga en bloque + caché local
# ══════════════════════════════════════════════════════════════

def fetch_temporada_liga(liga: str, season: str) -> list:
    """1 request = 1 temporada completa de 1 liga."""
    url = f"{BASE_URL}/competitions/{liga}/matches?season={season}&status=FINISHED"
    r = requests.get(url, headers=HEADERS)
    if not r.ok:
        print(f"⚠️  {liga} {season}: HTTP {r.status_code} — {r.text[:150]}")
        return []
    data = r.json()
    partidos = []
    for m in data.get("matches", []):
        gh = m.get("score", {}).get("fullTime", {}).get("home")
        ga = m.get("score", {}).get("fullTime", {}).get("away")
        if gh is None or ga is None:
            continue
        partidos.append({
            "match_id": m["id"],
            "liga": liga,
            "season": str(season),
            "fecha": datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).replace(tzinfo=None),
            "home_id": m["homeTeam"]["id"],
            "away_id": m["awayTeam"]["id"],
            "home_name": m["homeTeam"].get("name", "") or "",
            "away_name": m["awayTeam"].get("name", "") or "",
            "gh": gh,
            "ga": ga,
        })
    return partidos


def refresh_cache():
    """
    Descarga/actualiza la caché local. Respeta el rate limit gratuito de
    football-data.org (~10 req/min) con pausas conservadoras.
    """
    total_nuevos = 0
    requests_hechos = 0
    for liga in LIGAS_BACKTEST:
        for season in SEASONS_CACHE:
            partidos = fetch_temporada_liga(liga, season)
            if partidos:
                nuevos = guardar_partidos_historicos(partidos)
                total_nuevos += nuevos
                print(f"✅ {liga} {season}: {len(partidos)} partidos en API ({nuevos} nuevos en caché)")
            requests_hechos += 1
            # 10/min → dejamos margen: 8 requests y pausa larga, resto pausa corta
            if requests_hechos % 8 == 0:
                time.sleep(65)
            else:
                time.sleep(6.5)
    print(f"\n📦 Caché actualizada: {total_nuevos} partidos nuevos en total.")
    return total_nuevos


# ══════════════════════════════════════════════════════════════
# 2. RECONSTRUCCIÓN DE ESTADO "ANTES DEL PARTIDO"
# ══════════════════════════════════════════════════════════════

def _tabla_a_fecha(cache_liga_season: list, fecha_ref: datetime) -> dict:
    """
    Reconstruye la tabla de posiciones tal como se veía ANTES de fecha_ref,
    usando solo los partidos de esa liga/temporada ya jugados a esa fecha.
    Evita tener que pedir standings históricos a la API (no existen en el
    plan gratuito) — se calcula gratis a partir de la caché.
    """
    stats = {}  # team_id -> {pts, played, gf, gc}
    for p in cache_liga_season:
        if p["date"] >= fecha_ref:
            continue
        hid, aid = p["homeId"], p["awayId"]
        for tid in (hid, aid):
            stats.setdefault(tid, {"pts": 0, "played": 0, "gf": 0, "gc": 0})
        gh, ga = p["gH"], p["gA"]
        stats[hid]["played"] += 1
        stats[aid]["played"] += 1
        stats[hid]["gf"] += gh; stats[hid]["gc"] += ga
        stats[aid]["gf"] += ga; stats[aid]["gc"] += gh
        if gh > ga:
            stats[hid]["pts"] += 3
        elif gh < ga:
            stats[aid]["pts"] += 3
        else:
            stats[hid]["pts"] += 1
            stats[aid]["pts"] += 1

    tabla = sorted(stats.items(), key=lambda kv: (-kv[1]["pts"], -(kv[1]["gf"] - kv[1]["gc"])))
    total = len(tabla)
    posiciones = {}
    for i, (tid, s) in enumerate(tabla, start=1):
        posiciones[tid] = {"position": i, "points": s["pts"], "played": s["played"], "total": total}
    return posiciones


def _pool_equipo(cache_liga: list, team_id: int, fecha_ref: datetime) -> list:
    """Partidos de un equipo estrictamente anteriores a fecha_ref (across las temporadas cacheadas)."""
    return [p for p in cache_liga if p["date"] < fecha_ref and (p["homeId"] == team_id or p["awayId"] == team_id)]


# ══════════════════════════════════════════════════════════════
# 3. MÉTRICAS — RPS / Brier
# ══════════════════════════════════════════════════════════════

def rps_1x2(prA: float, prE: float, prB: float, outcome: str) -> float:
    """
    Ranked Probability Score para 1X2, ordenado [Local, Empate, Visitante]
    (ordinal por diferencia de gol). outcome ∈ {"1","X","2"}.
    """
    actual = {"1": [1, 0, 0], "X": [0, 1, 0], "2": [0, 0, 1]}[outcome]
    pred = [prA, prE, prB]
    cp = [pred[0], pred[0] + pred[1]]           # cumsum predicho (sin el último, siempre 1)
    ca = [actual[0], actual[0] + actual[1]]      # cumsum real
    return round(0.5 * ((cp[0] - ca[0]) ** 2 + (cp[1] - ca[1]) ** 2), 5)


def brier_1x2(prA: float, prE: float, prB: float, outcome: str) -> float:
    """Brier score multiclase normalizado (0 a ~0.67)."""
    actual = {"1": [1, 0, 0], "X": [0, 1, 0], "2": [0, 0, 1]}[outcome]
    pred = [prA, prE, prB]
    return round(sum((p - a) ** 2 for p, a in zip(pred, actual)) / 3, 5)


# ══════════════════════════════════════════════════════════════
# 4. PREDICCIÓN CIEGA SOBRE UN PARTIDO HISTÓRICO
# ══════════════════════════════════════════════════════════════

def predecir_ciego(match: dict, cache_liga: list, halflife_dias: float, maf_peso: float):
    """
    Reconstruye el estado "antes del partido" y corre el mismo pipeline
    Poisson que betAI.py, IGNORANDO el resultado real de match. Devuelve
    (prA, prE, prB) o None si no hay suficiente historial para confiar
    en la predicción.
    """
    fecha_ref = match["date"]
    home_id, away_id = match["homeId"], match["awayId"]

    pA = _pool_equipo(cache_liga, home_id, fecha_ref)
    pB = _pool_equipo(cache_liga, away_id, fecha_ref)
    if len(pA) < MIN_PARTIDOS_PREVIOS or len(pB) < MIN_PARTIDOS_PREVIOS:
        return None

    atk_home = calc_lambda(pA, home_id, as_local=True,  scored=True,  fecha_referencia=fecha_ref, halflife_dias=halflife_dias)
    def_away = calc_lambda(pB, away_id, as_local=False, scored=False, fecha_referencia=fecha_ref, halflife_dias=halflife_dias)
    atk_away = calc_lambda(pB, away_id, as_local=False, scored=True,  fecha_referencia=fecha_ref, halflife_dias=halflife_dias)
    def_home = calc_lambda(pA, home_id, as_local=True,  scored=False, fecha_referencia=fecha_ref, halflife_dias=halflife_dias)

    lA_base = (atk_home + def_away) / 2
    lB_base = (atk_away + def_home) / 2

    fA = get_form(pA, home_id)
    fB = get_form(pB, away_id)

    tabla = _tabla_a_fecha([p for p in cache_liga if p["season"] == match["season"]], fecha_ref)
    st_home = tabla.get(home_id)
    st_away = tabla.get(away_id)

    maf_home = calc_maf(st_home, st_away, fA, es_local=True)
    maf_away = calc_maf(st_away, st_home, fB, es_local=False)

    lA = aplicar_maf(lA_base, maf_home["maf"], maf_peso=maf_peso)
    lB = aplicar_maf(lB_base, maf_away["maf"], maf_peso=maf_peso)

    mat = build_matrix(lA, lB)
    prA, prE, prB = calc_probs(mat)
    return prA, prE, prB


# ══════════════════════════════════════════════════════════════
# 5. GRID SEARCH / AUTO-TUNE
# ══════════════════════════════════════════════════════════════

def _outcome(gh, ga):
    if gh > ga: return "1"
    if gh < ga: return "2"
    return "X"


def evaluar_combo(halflife_dias: float, maf_peso: float, dataset_por_liga: dict):
    """Corre predecir_ciego sobre TODOS los partidos objetivo y devuelve RPS/Brier promedio."""
    rps_total, brier_total, n = 0.0, 0.0, 0
    for liga, (cache_liga, objetivo) in dataset_por_liga.items():
        for match in objetivo:
            pred = predecir_ciego(match, cache_liga, halflife_dias, maf_peso)
            if pred is None:
                continue
            prA, prE, prB = pred
            outcome = _outcome(match["gH"], match["gA"])
            rps_total += rps_1x2(prA, prE, prB, outcome)
            brier_total += brier_1x2(prA, prE, prB, outcome)
            n += 1
    if n == 0:
        return None
    return {"rps": round(rps_total / n, 5), "brier": round(brier_total / n, 5), "n": n}


def autotune():
    """Grid search completo sobre la caché local. Guarda la mejor combo en parametros_activos."""
    print(f"📊 Cargando caché local (temporadas {SEASONS_CACHE})...")
    dataset_por_liga = {}
    for liga in LIGAS_BACKTEST:
        cache_liga = obtener_partidos_historicos(liga=liga, seasons=SEASONS_CACHE)
        objetivo = [p for p in cache_liga if p["season"] in SEASONS_EVAL]
        if not cache_liga:
            print(f"⚠️  {liga}: sin datos en caché — corré 'python backtest_engine.py refresh' primero.")
            continue
        dataset_por_liga[liga] = (cache_liga, objetivo)
        print(f"   {liga}: {len(cache_liga)} en caché, {len(objetivo)} a evaluar")

    total_objetivo = sum(len(v[1]) for v in dataset_por_liga.values())
    if total_objetivo == 0:
        print("❌ No hay partidos para evaluar. Corré el refresh primero.")
        return

    print(f"\n🔍 Grid search: {len(GRID_HALFLIFE)}×{len(GRID_MAF_PESO)} = "
          f"{len(GRID_HALFLIFE)*len(GRID_MAF_PESO)} combinaciones sobre ~{total_objetivo} partidos objetivo...\n")

    resultados = []
    for halflife, maf_peso in product(GRID_HALFLIFE, GRID_MAF_PESO):
        r = evaluar_combo(halflife, maf_peso, dataset_por_liga)
        if r is None:
            continue
        resultados.append((halflife, maf_peso, r))
        print(f"   half-life={halflife:>3}d  maf_peso={maf_peso:.1f}  →  "
              f"RPS={r['rps']:.5f}  Brier={r['brier']:.5f}  (n={r['n']})")

    if not resultados:
        print("❌ Ninguna combinación produjo predicciones válidas.")
        return

    resultados.sort(key=lambda x: x[2]["rps"])
    mejor_halflife, mejor_maf_peso, mejor_r = resultados[0]

    for halflife, maf_peso, r in resultados:
        es_ganador = (halflife == mejor_halflife and maf_peso == mejor_maf_peso)
        guardar_backtest_run(halflife, maf_peso, r["n"], r["rps"], r["brier"], es_ganador=es_ganador)

    actual = obtener_parametros_activos()
    print(f"\n🏆 Mejor combinación: half-life={mejor_halflife}d, maf_peso={mejor_maf_peso} "
          f"→ RPS={mejor_r['rps']:.5f} (sobre {mejor_r['n']} partidos)")
    print(f"   Config anterior: half-life={actual['decay_halflife_dias']}d, maf_peso={actual['maf_peso']} "
          f"(RPS calibración: {actual.get('rps_promedio_calibracion')})")

    if actual.get("rps_promedio_calibracion") is None or mejor_r["rps"] < actual["rps_promedio_calibracion"]:
        guardar_parametros_activos(mejor_halflife, mejor_maf_peso, mejor_r["rps"], mejor_r["n"])
        print("✅ parametros_activos actualizado — betAI.py usará esta calibración en la próxima predicción.")
    else:
        print("ℹ️  La combinación vigente ya era igual o mejor — no se actualiza nada.")


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    accion = sys.argv[1] if len(sys.argv) > 1 else "run"

    if accion == "refresh":
        refresh_cache()
    elif accion == "autotune":
        autotune()
    elif accion == "run":
        refresh_cache()
        autotune()
    else:
        print("Uso: python backtest_engine.py [refresh|autotune|run]")