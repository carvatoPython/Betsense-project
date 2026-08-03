"""
model_core.py — BetSense
Funciones puras del motor de predicción (Poisson + MAF + decay), sin
dependencias de Flask ni de la API externa. Las importan tanto betAI.py
(producción, vía endpoints Flask) como backtest_engine.py (backtester
ciego offline) — así queda garantizado que el backtest evalúa EXACTAMENTE
la misma matemática que corre en producción, no una reimplementación
paralela que se puede desalinear con el tiempo.

get_parametros_activos() es la única función acá con efecto lateral (lee
la tabla parametros_activos), y se cachea con TTL corto. calc_lambda y
aplicar_maf la usan solo como default cuando no se les pasa el parámetro
explícito — el backtester siempre pasa sus propios candidatos del grid
search, así que nunca dependen de la caché en ese contexto.
"""

import math
import time as t
from datetime import datetime

import numpy as np

from database import obtener_parametros_activos


# ── CALIBRACIÓN — parámetros vigentes leídos del backtester ──────────
_params_cache = {"data": None, "ts": 0}
_PARAMS_TTL_SEG = 300  # 5 min


def get_parametros_activos() -> dict:
    ahora = t.time()
    if _params_cache["data"] is None or (ahora - _params_cache["ts"]) > _PARAMS_TTL_SEG:
        try:
            _params_cache["data"] = obtener_parametros_activos()
        except Exception as e:
            print(f"⚠️  No se pudo leer parametros_activos, uso defaults: {e}")
            _params_cache["data"] = {"decay_halflife_dias": 150.0, "maf_peso": 1.0}
        _params_cache["ts"] = ahora
    return _params_cache["data"]


def peso_decay(fecha_partido, fecha_referencia=None, halflife_dias=150.0):
    """
    Peso exponencial por antigüedad: peso = 0.5 ** (dias_transcurridos / halflife_dias).
    fecha_referencia es "hoy" en producción, o la fecha del partido a predecir
    durante el backtest (para no filtrar por fecha futura respecto al partido real).
    """
    if fecha_referencia is None:
        fecha_referencia = datetime.now()
    if isinstance(fecha_partido, str):
        fecha_partido = datetime.fromisoformat(fecha_partido.replace("Z", "+00:00")).replace(tzinfo=None)
    if isinstance(fecha_referencia, str):
        fecha_referencia = datetime.fromisoformat(fecha_referencia.replace("Z", "+00:00")).replace(tzinfo=None)
    dias = (fecha_referencia - fecha_partido).total_seconds() / 86400.0
    if dias < 0:
        dias = 0  # nunca pesar partidos "futuros" respecto a la referencia (anti data-leakage)
    if halflife_dias <= 0:
        halflife_dias = 150.0
    return 0.5 ** (dias / halflife_dias)


def poisson_pmf(k, lam):
    """PMF de Poisson sin depender de scipy."""
    if lam < 0:
        return 0.0
    k = int(k)
    if k < 0:
        return 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def calc_stats(partidos, team_id):
    G = E = P = GF = GC = 0
    for p in partidos:
        loc = p["homeId"] == team_id
        gf = p["gH"] if loc else p["gA"]
        gc = p["gA"] if loc else p["gH"]
        GF += gf; GC += gc
        if gf > gc: G += 1
        elif gf == gc: E += 1
        else: P += 1
    T = G + E + P or 1
    over25 = sum(1 for p in partidos if p["gH"] + p["gA"] > 2.5)
    under25 = sum(1 for p in partidos if p["gH"] + p["gA"] < 2.5)
    btts = sum(1 for p in partidos if p["gH"] > 0 and p["gA"] > 0)
    cs = sum(1 for p in partidos if (p["gA"] if p["homeId"] == team_id else p["gH"]) == 0)
    return {
        "G": G, "E": E, "P": P, "T": T,
        "GF": GF, "GC": GC,
        "pgf": round(GF / T, 2),
        "pgc": round(GC / T, 2),
        "winPct": round(G / T * 100, 1),
        "over25": round(over25 / T * 100, 1),
        "under25": round(under25 / T * 100, 1),
        "btts": round(btts / T * 100, 1),
        "cs": round(cs / T * 100, 1),
    }


def calc_lambda(partidos, team_id, as_local, scored=True, fecha_referencia=None, halflife_dias=None):
    """
    scored=True  → goles ANOTADOS por el equipo
    scored=False → goles RECIBIDOS por el equipo
    as_local=True  → solo partidos jugados en casa
    as_local=False → solo partidos jugados de visitante

    Promedio ponderado por antigüedad (no plano). Esto es lo que resuelve
    el arranque de temporada: partidos viejos siguen contando, solo que
    con menos peso, en vez de que el pool quede vacío y todo colapse a 1.0.
    fecha_referencia / halflife_dias permiten reusar esta misma función
    tanto en producción (referencia=hoy) como en el backtester (referencia=
    fecha del partido a predecir, para no filtrar futuro).
    """
    if halflife_dias is None:
        halflife_dias = get_parametros_activos().get("decay_halflife_dias", 150.0)

    goles, pesos = [], []
    for p in partidos:
        es_local = p["homeId"] == team_id
        if as_local and not es_local:
            continue
        if not as_local and es_local:
            continue
        g = (p["gH"] if es_local else p["gA"]) if scored else (p["gA"] if es_local else p["gH"])
        w = peso_decay(p["date"], fecha_referencia, halflife_dias)
        goles.append(g)
        pesos.append(w)

    if not goles or sum(pesos) <= 0:
        return 1.0
    return round(float(np.average(goles, weights=pesos)), 3)


def build_matrix(lA, lB, mx=7):
    m = []
    for i in range(mx + 1):
        row = []
        for j in range(mx + 1):
            row.append(float(poisson_pmf(i, lA) * poisson_pmf(j, lB)))
        m.append(row)
    return m


def calc_probs(matrix):
    pA = pE = pB = 0.0
    for i, row in enumerate(matrix):
        for j, v in enumerate(row):
            if i > j: pA += v
            elif i == j: pE += v
            else: pB += v
    return round(pA, 4), round(pE, 4), round(pB, 4)


def best_score(matrix):
    best_v, bi, bj = 0, 0, 0
    for i, row in enumerate(matrix):
        for j, v in enumerate(row):
            if v > best_v:
                best_v, bi, bj = v, i, j
    return bi, bj, round(best_v, 4)


def get_form(partidos, team_id):
    sorted_p = sorted(partidos, key=lambda x: x["date"], reverse=True)[:5]
    result = []
    for p in reversed(sorted_p):
        gf = p["gH"] if p["homeId"] == team_id else p["gA"]
        gc = p["gA"] if p["homeId"] == team_id else p["gH"]
        result.append("W" if gf > gc else "D" if gf == gc else "L")
    return result


def get_h2h(partidos_a, id_a, id_b):
    h2h = [p for p in partidos_a if p["homeId"] == id_b or p["awayId"] == id_b]
    return sorted(h2h, key=lambda x: x["date"], reverse=True)[:5]


def over_prob(matrix, threshold):
    total = 0.0
    for i, row in enumerate(matrix):
        for j, v in enumerate(row):
            if i + j > threshold:
                total += v
    return round(total * 100, 1)


def btts_prob(lA, lB):
    p_no_a = math.exp(-lA)
    p_no_b = math.exp(-lB)
    return round((1 - p_no_a) * (1 - p_no_b) * 100, 1)


def _zona_equipo(pos: int, total: int) -> str:
    if total <= 0:
        return "media"
    if pos <= 0:
        return "media"
    pct = pos / total
    if pos >= total - 2:
        return "descenso"
    if pct >= 0.75:
        return "descenso_zona"
    if pos == 1:
        return "campeon"
    if pct <= 0.10:
        return "europa_top"
    if pct <= 0.20:
        return "europa"
    return "media"


def calc_maf(
    st: dict,           # standings del equipo (puede ser None)
    rival_st: dict,      # standings del rival (puede ser None)
    forma: list,         # últimos 5 resultados ["W","D","L",...]
    es_local: bool,       # ¿juega de local?
) -> dict:
    """
    Calcula el MAF para un equipo.
    Retorna el factor final y el desglose explicativo.
    """

    if not st:
        return {
            "maf": 1.0,
            "zona": "desconocida",
            "zona_label": "Sin datos de tabla",
            "urgencia": 1.0,
            "gap": 1.0,
            "racha": 1.0,
            "alertas": [],
            "resumen": "Sin datos de posición en tabla para ajuste situacional.",
            "disponible": False,
        }

    pos = st.get("position", 10)
    pts = st.get("points", 30)
    played = st.get("played", 20)
    total = st.get("total", 20)
    zona = _zona_equipo(pos, total)

    jornadas_totales = 38
    restantes = max(0, jornadas_totales - played)

    zona_map = {
        "descenso": 1.28,
        "descenso_zona": 1.15,
        "europa": 1.05,
        "europa_top": 1.08,
        "campeon": 0.88,
        "media": 1.00,
    }
    zona_labels = {
        "descenso": f"Zona de descenso directo (#{pos})",
        "descenso_zona": f"Zona de playoff descenso (#{pos})",
        "europa": f"Zona Europa (#{pos})",
        "europa_top": f"Zona Champions (#{pos})",
        "campeon": f"Líder / campeón (#{pos})",
        "media": f"Zona media (#{pos})",
    }
    zona_factor = zona_map.get(zona, 1.0)
    zona_label = zona_labels.get(zona, f"Posición #{pos}")

    urgencia = 1.0
    if restantes <= 5 and zona in ("descenso", "descenso_zona"):
        urgencia = 1.20
    elif restantes <= 10 and zona in ("descenso", "descenso_zona"):
        urgencia = 1.12
    elif restantes <= 5 and zona in ("europa_top", "europa"):
        urgencia = 1.10
    elif restantes <= 5 and zona == "campeon":
        urgencia = 0.90

    gap_factor = 1.0
    if rival_st:
        rival_pts = rival_st.get("points", pts)
        diff_pts = rival_pts - pts

        if diff_pts >= 20:
            gap_factor = 1.12
        elif diff_pts >= 10:
            gap_factor = 1.06
        elif diff_pts <= -20:
            gap_factor = 0.95
        elif diff_pts <= -10:
            gap_factor = 0.98

    racha_factor = 1.0
    if forma:
        ultimas5 = forma[-5:]
        wins = ultimas5.count("W")
        losses = ultimas5.count("L")
        draws = ultimas5.count("D")

        if wins == 0 and losses >= 4:
            racha_factor = 1.12
        elif wins == 0 and losses >= 3:
            racha_factor = 1.07
        elif wins >= 4:
            racha_factor = 1.06
        elif wins == 5:
            racha_factor = 1.10
        elif losses == 0 and draws <= 1:
            racha_factor = 1.08

    maf_raw = (
        zona_factor * 0.40 +
        urgencia * 0.25 +
        gap_factor * 0.20 +
        racha_factor * 0.15
    )
    maf = round(max(0.70, min(1.40, maf_raw)), 3)

    alertas = []

    if zona == "descenso":
        alertas.append({
            "tipo": "critica", "icono": "🚨",
            "texto": f"Equipo en zona de descenso directo (#{pos}/{total}). Saldrá a ganar a cualquier costo — los modelos estadísticos subestiman su intensidad.",
        })
    elif zona == "descenso_zona":
        alertas.append({
            "tipo": "warning", "icono": "⚠️",
            "texto": f"Equipo en zona de playoff de descenso (#{pos}/{total}). Alta presión para conseguir puntos.",
        })

    if urgencia > 1.10:
        alertas.append({
            "tipo": "critica", "icono": "⏰",
            "texto": f"Solo {restantes} jornadas restantes con la situación comprometida. Urgencia máxima.",
        })
    elif urgencia > 1.05:
        alertas.append({
            "tipo": "warning", "icono": "⏰",
            "texto": f"{restantes} jornadas restantes. La presión del calendario aumenta.",
        })

    if gap_factor > 1.08:
        diff_abs = abs(rival_st.get("points", pts) - pts) if rival_st else 0
        alertas.append({
            "tipo": "info", "icono": "⚡",
            "texto": f"El rival tiene {diff_abs} puntos más en la tabla. El equipo inferior puede jugar sin presión y sorprender.",
        })

    if racha_factor >= 1.10:
        alertas.append({
            "tipo": "critica", "icono": "📉",
            "texto": "Racha muy negativa reciente. Desesperación o cambio táctico probable — impredecible.",
        })
    elif racha_factor >= 1.06 and forma and forma.count("W") >= 4:
        alertas.append({
            "tipo": "positiva", "icono": "🔥",
            "texto": "Equipo en racha muy positiva — alta confianza y momentum.",
        })

    if zona == "campeon" and urgencia <= 0.92:
        alertas.append({
            "tipo": "info", "icono": "🏆",
            "texto": "Equipo ya campeón o matemáticamente asegurado. Posible rotación de plantel.",
        })

    impacto_pct = round((maf - 1.0) * 100, 1)
    signo = "+" if impacto_pct >= 0 else ""
    resumen = (
        f"MAF={maf} ({signo}{impacto_pct}% sobre λ base) — "
        f"Zona: {zona_label} · Urgencia: x{urgencia} · "
        f"Gap: x{gap_factor} · Racha: x{racha_factor}"
    )

    return {
        "maf": maf,
        "disponible": True,
        "zona": zona,
        "zona_label": zona_label,
        "posicion": pos,
        "total_equipos": total,
        "pts": pts,
        "restantes": restantes,
        "zona_factor": zona_factor,
        "urgencia": urgencia,
        "gap_factor": gap_factor,
        "racha_factor": racha_factor,
        "alertas": alertas,
        "resumen": resumen,
    }


def aplicar_maf(lambda_base: float, maf: float, maf_peso: float = None) -> float:
    """
    Aplica el MAF a una lambda Poisson base. maf_peso escala qué tanto
    pesa la desviación del MAF respecto a 1.0 (motivación neutra) — es el
    segundo parámetro que el backtester auto-tunea junto al decay half-life.
    maf_peso=1.0 → comportamiento original. maf_peso=0 → ignora el MAF.
    """
    if maf_peso is None:
        maf_peso = get_parametros_activos().get("maf_peso", 1.0)
    maf_efectivo = 1.0 + (maf - 1.0) * maf_peso
    return round(max(0.3, lambda_base * maf_efectivo), 3)