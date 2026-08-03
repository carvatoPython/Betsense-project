"""
indicators.py — BetSense
========================
15 indicadores deportivos inspirados en análisis financiero.
Calculados con los datos reales disponibles: goles anotados/recibidos,
resultados (G/E/P) y fechas. Sin xG ni tiros (API gratuita no los da).
"""

import math
import statistics
from typing import Optional


def _goles_marcados(partidos, team_id):
    return [p["gH"] if p["homeId"] == team_id else p["gA"] for p in partidos]

def _goles_recibidos(partidos, team_id):
    return [p["gA"] if p["homeId"] == team_id else p["gH"] for p in partidos]

def _puntos(partidos, team_id):
    pts = []
    for p in partidos:
        gf = p["gH"] if p["homeId"] == team_id else p["gA"]
        gc = p["gA"] if p["homeId"] == team_id else p["gH"]
        pts.append(3 if gf > gc else 1 if gf == gc else 0)
    return pts

def _mean(lst, fallback=0.0):
    return statistics.mean(lst) if lst else fallback

def _std(lst, fallback=0.0):
    return statistics.stdev(lst) if len(lst) >= 2 else fallback

def f(v, d=2):
    return round(v, d)


# ── 1. GMI — Goal Momentum Indicator ────────────────────────
def calc_gmi(partidos, team_id):
    goles = _goles_marcados(partidos, team_id)
    if len(goles) < 5:
        return {"available": False, "reason": "Menos de 5 partidos"}
    avg5  = _mean(goles[-5:])
    avg15 = _mean(goles[-15:] if len(goles) >= 15 else goles)
    v = f(avg5 - avg15)
    if v > 0.3:
        c, l, t = "green",  "Momentum positivo", "El equipo marca cada vez más. Buena señal para Over/BTTS."
    elif v > -0.3:
        c, l, t = "yellow", "Momentum neutral",  "Ritmo goleador constante. Sin señal especial."
    else:
        c, l, t = "red",    "Momentum negativo", "El equipo marca cada vez menos. Riesgo en mercados de goles."
    return {"available": True, "value": v, "avg5": f(avg5), "avg15": f(avg15),
            "color": c, "label": l, "tip": t, "chart": [f(g) for g in goles[-10:]]}


# ── 2. GSI — Goal Scoring Indicator (proxy xGP) ─────────────
def calc_gsi(partidos, team_id):
    goles = _goles_marcados(partidos, team_id)
    if len(goles) < 5:
        return {"available": False, "reason": "Menos de 5 partidos"}
    avg_t = _mean(goles, 1.0)
    avg5  = _mean(goles[-5:], 1.0)
    v = f(avg5 / avg_t if avg_t > 0 else 1.0)
    if v >= 1.2:
        c, l, t = "green",  "Presión ofensiva alta",   f"Genera {v}x su presión habitual. Excelente para Over/BTTS."
    elif v >= 0.8:
        c, l, t = "yellow", "Presión ofensiva normal", f"Rendimiento en su media ({v}x). Sin señal especial."
    else:
        c, l, t = "red",    "Presión ofensiva baja",   f"Solo {v}x su presión habitual. Ataque flojo en últimas jornadas."
    return {"available": True, "value": v, "avg5": f(avg5), "avg_season": f(avg_t),
            "color": c, "label": l, "tip": t, "chart": [f(g) for g in goles[-10:]]}


# ── 3. DSI — Defensive Stability Index ──────────────────────
def calc_dsi(partidos, team_id):
    gc = _goles_recibidos(partidos, team_id)
    if len(gc) < 5:
        return {"available": False, "reason": "Menos de 5 partidos"}
    avg5 = _mean(gc[-5:], 1.0)
    v = f(1.0 / avg5 if avg5 > 0 else 3.0)
    if v >= 1.5:
        c, l, t = "green",  "Defensa muy sólida",   "Muy difícil hacerle gol. Ideal para Under o portería a cero."
    elif v >= 0.7:
        c, l, t = "yellow", "Defensa aceptable",    "Recibe goles dentro de lo normal."
    else:
        c, l, t = "red",    "Defensa vulnerable",   "Recibe muchos goles. El rival tiene buenas opciones."
    return {"available": True, "value": v, "avg_gc": f(avg5),
            "color": c, "label": l, "tip": t, "chart": [f(g) for g in gc[-10:]]}


# ── 4. GDR — Goal Dominance Ratio (proxy SDR) ───────────────
def calc_gdr(partidos, team_id):
    gf = _goles_marcados(partidos, team_id)
    gc = _goles_recibidos(partidos, team_id)
    if len(gf) < 5:
        return {"available": False, "reason": "Menos de 5 partidos"}
    gf5, gc5 = sum(gf[-5:]), sum(gc[-5:])
    total = gf5 + gc5
    v = f(gf5 / total if total > 0 else 0.5)
    if v >= 0.60:
        c, l, t = "green",  "Domina el marcador", f"{int(v*100)}% de los goles en sus partidos. Equipo que lleva el juego."
    elif v >= 0.40:
        c, l, t = "yellow", "Partido equilibrado", f"{int(v*100)}/{int((1-v)*100)} en goles. Ninguno domina."
    else:
        c, l, t = "red",    "Equipo dominado",     f"Solo {int(v*100)}% de los goles. El rival controla más."
    return {"available": True, "value": v, "gf5": gf5, "gc5": gc5,
            "color": c, "label": l, "tip": t}


# ── 5. FSI — Form Strength Index ────────────────────────────
def calc_fsi(partidos, team_id):
    pts = _puntos(partidos, team_id)
    if len(pts) < 3:
        return {"available": False, "reason": "Menos de 3 partidos"}
    pts5 = pts[-5:] if len(pts) >= 5 else pts
    total, maxp = sum(pts5), len(pts5) * 3
    v = f(total / maxp)
    if v >= 0.60:
        c, l, t = "green",  "Excelente forma", f"{total}/{maxp} pts. Equipo en muy buen momento."
    elif v >= 0.30:
        c, l, t = "yellow", "Forma regular",   f"{total}/{maxp} pts. Forma ni buena ni mala."
    else:
        c, l, t = "red",    "Mala forma",      f"{total}/{maxp} pts. Resultados muy pobres recientemente."
    form = ["W" if p==3 else "D" if p==1 else "L" for p in pts5]
    return {"available": True, "value": v, "pts_recent": total, "pts_max": maxp,
            "color": c, "label": l, "tip": t, "form": form, "chart": _puntos(partidos, team_id)[-10:]}


# ── 6. GTO — Goal Trend Oscillator (inspirado en RSI) ───────
def calc_gto(partidos, team_id):
    goles = _goles_marcados(partidos, team_id)
    if len(goles) < 6:
        return {"available": False, "reason": "Menos de 6 partidos"}
    avg_t = _mean(goles[:-5], 1.0)
    avg5  = _mean(goles[-5:],  1.0)
    v = f(avg5 / avg_t if avg_t > 0 else 1.0)
    if v >= 1.3:
        c, l, t = "green",  "Sobre-rendimiento",   f"Anota {v}x su media. El ataque está por encima de su nivel normal."
    elif v >= 0.7:
        c, l, t = "yellow", "Rendimiento esperado", f"Goles en línea con su promedio ({v}x)."
    else:
        c, l, t = "red",    "Sub-rendimiento",     f"Solo {v}x su media. El ataque rinde por debajo de lo esperado."
    return {"available": True, "value": v, "avg5": f(avg5), "avg_season": f(avg_t),
            "color": c, "label": l, "tip": t, "chart": [f(g) for g in goles[-10:]]}


# ── 7. DCI — Defensive Consistency Index (proxy DRR) ────────
def calc_dci(partidos, team_id):
    gc = _goles_recibidos(partidos, team_id)
    if len(gc) < 5:
        return {"available": False, "reason": "Menos de 5 partidos"}
    gc5 = gc[-5:]
    cs = sum(1 for g in gc5 if g == 0)
    v = f(cs / len(gc5))
    if v >= 0.4:
        c, l, t = "green",  "Portería sólida",     f"{cs}/5 partidos sin recibir. Buena opción para Under o portería a cero."
    elif v >= 0.2:
        c, l, t = "yellow", "Defensa moderada",    f"{cs}/5 partidos sin recibir. Nivel defensivo aceptable."
    else:
        c, l, t = "red",    "Portería permeable",  f"Solo {cs}/5 partidos sin recibir. Defensa con problemas."
    return {"available": True, "value": v, "clean_sheets": cs,
            "color": c, "label": l, "tip": t,
            "chart": [1 if g == 0 else 0 for g in gc[-10:]]}


# ── 8. MVI — Match Volatility Indicator ─────────────────────
def calc_mvi(partidos, team_id):
    goles = _goles_marcados(partidos, team_id)
    if len(goles) < 5:
        return {"available": False, "reason": "Menos de 5 partidos"}
    g10 = goles[-10:] if len(goles) >= 10 else goles
    v = f(_std(g10))
    if v <= 0.70:
        c, l, t = "green",  "Muy predecible",   "Resultados muy regulares. Fácil de analizar y apostar con confianza."
    elif v <= 1.20:
        c, l, t = "yellow", "Volatilidad media", "Resultados con cierta variación. Riesgo moderado."
    else:
        c, l, t = "red",    "Muy impredecible",  "Sus resultados cambian mucho. Alto riesgo en cualquier apuesta."
    return {"available": True, "value": v,
            "color": c, "label": l, "tip": t, "chart": [f(g) for g in g10]}


# ── 9. SPI — Scoring Probability Index ──────────────────────
def calc_spi(partidos, team_id):
    goles = _goles_marcados(partidos, team_id)
    if len(goles) < 5:
        return {"available": False, "reason": "Menos de 5 partidos"}
    con_gol = sum(1 for g in goles if g > 0)
    v = f(con_gol / len(goles))
    if v >= 0.75:
        c, l, t = "green",  "Casi siempre marca",       f"Anota en el {int(v*100)}% de sus partidos. Excelente para BTTS u Over 0.5."
    elif v >= 0.50:
        c, l, t = "yellow", "Marca con regularidad",    f"Anota en el {int(v*100)}% de partidos. Frecuencia normal."
    else:
        c, l, t = "red",    "Poca frecuencia goleadora", f"Solo anota en el {int(v*100)}% de partidos. Riesgo en mercados de goles."
    return {"available": True, "value": v, "con_gol": con_gol, "total": len(goles),
            "color": c, "label": l, "tip": t}


# ── 10. AER — Attack Efficiency Rate ────────────────────────
def calc_aer(partidos, team_id):
    goles = _goles_marcados(partidos, team_id)
    if not goles:
        return {"available": False, "reason": "Sin partidos"}
    avg_g = _mean(goles, 1.0)
    v = f(min(avg_g / 3.0, 1.0))
    if v >= 0.50:
        c, l, t = "green",  "Ataque muy eficiente",   f"~{f(avg_g)} goles/partido. Ataque productivo."
    elif v >= 0.27:
        c, l, t = "yellow", "Eficiencia normal",      f"~{f(avg_g)} goles/partido. Rendimiento estándar."
    else:
        c, l, t = "red",    "Ataque poco eficiente",  f"Solo ~{f(avg_g)} goles/partido. Bajo rendimiento ofensivo."
    return {"available": True, "value": v, "avg_goles": f(avg_g),
            "color": c, "label": l, "tip": t, "chart": [f(g) for g in goles[-10:]]}


# ── 11. DRI — Defensive Resilience Index (proxy DRR) ────────
def calc_dri(partidos, team_id):
    gc = _goles_recibidos(partidos, team_id)
    if len(gc) < 5:
        return {"available": False, "reason": "Menos de 5 partidos"}
    avg_gc = _mean(gc[-10:] if len(gc) >= 10 else gc)
    v = f(1.0 / (1.0 + avg_gc))
    if v >= 0.55:
        c, l, t = "green",  "Alta resistencia",   f"Solo ~{f(avg_gc)} goles recibidos/partido. Defensa difícil de superar."
    elif v >= 0.38:
        c, l, t = "yellow", "Resistencia normal", f"~{f(avg_gc)} goles recibidos/partido. Nivel estándar."
    else:
        c, l, t = "red",    "Baja resistencia",   f"~{f(avg_gc)} goles recibidos/partido. Defensa fácil de vulnerar."
    return {"available": True, "value": v, "avg_gc": f(avg_gc),
            "color": c, "label": l, "tip": t, "chart": [f(g) for g in gc[-10:]]}


# ── 12. OPI — Offensive Pressure Index ──────────────────────
def calc_opi(gsi, gto, spi):
    if not all(d.get("available") for d in [gsi, gto, spi]):
        return {"available": False, "reason": "Componentes no disponibles"}
    gsi_n = min(gsi["value"] / 2.0, 1.0)
    gto_n = min(gto["value"] / 2.0, 1.0)
    v = f(gsi_n * 0.40 + gto_n * 0.30 + spi["value"] * 0.30)
    if v >= 0.60:
        c, l, t = "green",  "Alta presión ofensiva",   "El equipo ataca con intensidad. Buen momento para Over/BTTS."
    elif v >= 0.35:
        c, l, t = "yellow", "Presión ofensiva normal", "Nivel de ataque estándar."
    else:
        c, l, t = "red",    "Baja presión ofensiva",   "El ataque está flojo. Riesgo en mercados de goles a favor."
    return {"available": True, "value": v,
            "components": {"gsi": gsi["value"], "gto": gto["value"], "spi": spi["value"]},
            "color": c, "label": l, "tip": t}


# ── 13. DPI — Defensive Pressure Index ──────────────────────
def calc_dpi(dsi, dci, dri):
    if not all(d.get("available") for d in [dsi, dci, dri]):
        return {"available": False, "reason": "Componentes no disponibles"}
    dsi_n = min(1.0 / (dsi["value"] * 2 + 0.01), 1.0)
    dci_n = 1.0 - dci["value"]
    dri_n = 1.0 - dri["value"]
    v = f(dsi_n * 0.40 + dci_n * 0.30 + dri_n * 0.30)
    if v >= 0.60:
        c, l, t = "red",    "Defensa bajo alta presión",  "La defensa es vulnerable. El rival tiene buenas opciones."
    elif v >= 0.35:
        c, l, t = "yellow", "Presión defensiva moderada", "Exposición defensiva normal."
    else:
        c, l, t = "green",  "Defensa sólida",             "La defensa está muy sólida. Buena para Under o portería a cero."
    return {"available": True, "value": v,
            "components": {"dsi": dsi["value"], "dci": dci["value"], "dri": dri["value"]},
            "color": c, "label": l, "tip": t}


# ── 14. CR — Consistency Rating ─────────────────────────────
def calc_cr(partidos, team_id):
    pts = _puntos(partidos, team_id)
    if len(pts) < 5:
        return {"available": False, "reason": "Menos de 5 partidos"}
    std = _std(pts[-10:] if len(pts) >= 10 else pts)
    v = f(1.0 / (std + 0.1))
    if v >= 1.5:
        c, l, t = "green",  "Muy consistente",   "Resultados muy regulares. Predecible y fácil de analizar."
    elif v >= 0.7:
        c, l, t = "yellow", "Consistencia media", "Alternancia de buenos y malos resultados. Riesgo moderado."
    else:
        c, l, t = "red",    "Muy inconsistente",  "Resultados erráticos. Alto riesgo en cualquier apuesta."
    return {"available": True, "value": v, "std": f(std),
            "color": c, "label": l, "tip": t, "chart": pts[-10:]}


# ── 15. DCS — Dominance Composite Score ─────────────────────
def calc_dcs(opi, dpi, fsi, gdr):
    comps = [d for d in [opi, fsi, gdr] if d.get("available")]
    if not comps:
        return {"available": False, "reason": "Insuficientes indicadores"}
    if dpi.get("available"):
        v = f((opi.get("value",0.5) + (1.0 - dpi["value"]) + fsi.get("value",0.5) + gdr.get("value",0.5)) / 4.0)
    else:
        vals = [d["value"] for d in comps]
        v = f(sum(vals) / len(vals))
    if v >= 0.60:
        c, l, t = "green",  "Equipo dominante",   "La mayoría de indicadores positivos. Buen momento para apostar a favor."
    elif v >= 0.38:
        c, l, t = "yellow", "Equipo equilibrado", "Indicadores mixtos. Incertidumbre moderada."
    else:
        c, l, t = "red",    "Equipo en crisis",   "Mayoría de indicadores negativos. Alto riesgo apostando a su favor."
    return {"available": True, "value": v,
            "color": c, "label": l, "tip": t}


# ─────────────────────────────────────────────────────────────
#  FUNCIÓN PRINCIPAL — FÚTBOL
# ─────────────────────────────────────────────────────────────
def analyze_all_indicators(partidos: list, team_id: int) -> dict:
    if not partidos or len(partidos) < 3:
        return {"available": False, "reason": "Sin suficientes partidos históricos"}

    p = sorted(partidos, key=lambda x: x.get("date", ""))

    gmi = calc_gmi(p, team_id)
    gsi = calc_gsi(p, team_id)
    dsi = calc_dsi(p, team_id)
    gdr = calc_gdr(p, team_id)
    fsi = calc_fsi(p, team_id)
    gto = calc_gto(p, team_id)
    dci = calc_dci(p, team_id)
    mvi = calc_mvi(p, team_id)
    spi = calc_spi(p, team_id)
    aer = calc_aer(p, team_id)
    dri = calc_dri(p, team_id)
    cr  = calc_cr(p, team_id)
    opi = calc_opi(gsi, gto, spi)
    dpi = calc_dpi(dsi, dci, dri)
    dcs = calc_dcs(opi, dpi, fsi, gdr)

    all_ind = [gmi, gsi, dsi, gdr, fsi, gto, dci, mvi, spi, aer, dri, opi, dpi, cr, dcs]
    bull  = sum(1 for i in all_ind if i.get("available") and i.get("color") == "green")
    bear  = sum(1 for i in all_ind if i.get("available") and i.get("color") == "red")
    avail = sum(1 for i in all_ind if i.get("available"))
    score = round((bull / avail * 100) if avail else 50)

    if score >= 65:
        sem_c, sem_l = "green",  "Momento positivo"
    elif score >= 40:
        sem_c, sem_l = "yellow", "Señales mixtas"
    else:
        sem_c, sem_l = "red",    "Momento negativo"

    return {
        "available": True,
        "gmi": gmi, "gsi": gsi, "dsi": dsi, "gdr": gdr,
        "fsi": fsi, "gto": gto, "dci": dci, "mvi": mvi,
        "spi": spi, "aer": aer, "dri": dri, "cr":  cr,
        "opi": opi, "dpi": dpi, "dcs": dcs,
        "semaforo": {
            "score": score, "bull": bull, "bear": bear,
            "total": avail, "color": sem_c, "label": sem_l,
        }
    }


# ─────────────────────────────────────────────────────────────
#  FUNCIÓN PRINCIPAL — TENIS
# ─────────────────────────────────────────────────────────────
def analyze_tennis_indicators(partidos: list, player_id: int,
                               surface=None) -> dict:
    if not partidos or len(partidos) < 3:
        return {"available": False, "reason": "Sin suficientes partidos"}
    adapted = []
    for p in partidos:
        p1_id  = p.get("p1Id") or p.get("player1Id") or 0
        sets_w = p.get("setsWon") or p.get("sets_won") or p.get("sW") or 0
        sets_l = p.get("setsLost") or p.get("sets_lost") or p.get("sL") or 0
        won = sets_w > sets_l
        adapted.append({
            "homeId": player_id if won else 0,
            "awayId": 0 if won else player_id,
            "gH": sets_w if p1_id == player_id else sets_l,
            "gA": sets_l if p1_id == player_id else sets_w,
            "date": p.get("date", ""),
        })
    return analyze_all_indicators(adapted, player_id)

import math
import statistics
from typing import List


# ═══════════════════════════════════════════════════════════════
# FIBONACCI — NÚCLEO DE PONDERACIÓN
# ═══════════════════════════════════════════════════════════════

_FIB_CACHE: dict = {}

def _fib(n: int) -> int:
    if n <= 0: return 0
    if n == 1: return 1
    if n in _FIB_CACHE: return _FIB_CACHE[n]
    a, b = 1, 1
    for _ in range(2, n):
        a, b = b, a + b
    _FIB_CACHE[n] = b
    return b

def fib_weights(n: int) -> List[float]:
    """
    n pesos Fibonacci normalizados, del más antiguo al más reciente.
    n=6 → raw [1,1,2,3,5,8] → norm [.05,.05,.10,.15,.25,.40]
    El partido MÁS RECIENTE siempre tiene el mayor peso.
    """
    raw   = [_fib(i) for i in range(1, n + 1)]
    total = sum(raw)
    return [r / total for r in raw]

def fib_mean(data: list, fallback: float = 0.0) -> float:
    """Promedio ponderado Fibonacci. Últimos elementos = mayor peso."""
    if not data:
        return fallback
    w = fib_weights(len(data))
    return sum(d * wt for d, wt in zip(data, w))

def fib_windows(data: list) -> dict:
    """
    Promedios ponderados Fibonacci en ventanas 3, 5, 8, 13.
    Retorna solo las ventanas disponibles según longitud de data.
    """
    result = {}
    for v in [3, 5, 8, 13]:
        if len(data) >= v:
            result[str(v)] = round(fib_mean(data[-v:]), 3)
    return result

def fib_trend(data: list) -> str:
    """Tendencia comparando ventana más corta vs más larga disponible."""
    w = fib_windows(data)
    if len(w) < 2:
        return "lateral"
    keys   = sorted(w.keys(), key=int)
    short  = w[keys[0]]
    long_  = w[keys[-1]]
    diff   = short - long_
    if diff > 0.15:  return "alcista"
    if diff < -0.15: return "bajista"
    return "lateral"


# ═══════════════════════════════════════════════════════════════
# HELPERS BASE
# ═══════════════════════════════════════════════════════════════

def _goles_f(partidos, team_id):
    return [p["gH"] if p["homeId"] == team_id else p["gA"] for p in partidos]

def _goles_c(partidos, team_id):
    return [p["gA"] if p["homeId"] == team_id else p["gH"] for p in partidos]

def _puntos(partidos, team_id):
    result = []
    for p in partidos:
        gf = p["gH"] if p["homeId"] == team_id else p["gA"]
        gc = p["gA"] if p["homeId"] == team_id else p["gH"]
        result.append(3 if gf > gc else 1 if gf == gc else 0)
    return result

def _mean(lst, fallback=0.0):
    return statistics.mean(lst) if lst else fallback

def _std(lst, fallback=0.0):
    return statistics.stdev(lst) if len(lst) >= 2 else fallback

def r2(v):
    try: return round(float(v), 2)
    except: return 0.0

def _na(reason="Pocos partidos"):
    return {"available": False, "reason": reason}


# ═══════════════════════════════════════════════════════════════
# LOS 15 INDICADORES — con Fibonacci como capa de ponderación
# ═══════════════════════════════════════════════════════════════

def calc_gmi(partidos, team_id):
    """GMI: FibMean(8) − FibMean(20) de goles. Momentum ofensivo."""
    goles = _goles_f(partidos, team_id)
    if len(goles) < 5:
        return _na()
    n_s = min(len(goles), 8)
    n_l = min(len(goles), 20)
    fs  = fib_mean(goles[-n_s:])
    fl  = fib_mean(goles[-n_l:])
    v   = r2(fs - fl)
    if v > 0.3:   c,l,t = "green","Momentum positivo", f"FibMean{n_s}={r2(fs)} > FibMean{n_l}={r2(fl)}. Ataque en progresión."
    elif v > -0.3:c,l,t = "yellow","Momentum neutral",  f"FibMean{n_s}≈FibMean{n_l}. Ritmo goleador estable."
    else:          c,l,t = "red","Momentum negativo",  f"FibMean{n_s}={r2(fs)} < FibMean{n_l}={r2(fl)}. Ataque en declive."
    return {"available":True,"value":v,"fib_short":r2(fs),"fib_long":r2(fl),
            "fib_windows":fib_windows(goles),"trend":fib_trend(goles),
            "color":c,"label":l,"tip":t,"chart":[r2(g) for g in goles[-10:]],
            "formula":f"FibMean({n_s}) − FibMean({n_l})"}

def calc_gsi(partidos, team_id):
    """GSI: FibMean(5) / avg_temporada. Proxy de presión ofensiva (xGP)."""
    goles = _goles_f(partidos, team_id)
    if len(goles) < 5:
        return _na()
    avg_t = _mean(goles, 1.0)
    fib5  = fib_mean(goles[-5:])
    v = r2(fib5 / avg_t if avg_t > 0 else 1.0)
    if v >= 1.2:   c,l,t = "green","Presión ofensiva alta",   f"FibMean5={r2(fib5)} ({v}x media). Ataque activo."
    elif v >= 0.8: c,l,t = "yellow","Presión ofensiva normal", f"FibMean5={r2(fib5)} ({v}x media). Nivel habitual."
    else:           c,l,t = "red","Presión ofensiva baja",   f"FibMean5={r2(fib5)} ({v}x media). Por debajo de su nivel."
    return {"available":True,"value":v,"fib5":r2(fib5),"avg_season":r2(avg_t),
            "color":c,"label":l,"tip":t,"chart":[r2(g) for g in goles[-10:]],
            "formula":"FibMean(5) / avg_temporada"}

def calc_dsi(partidos, team_id):
    """DSI: 1 / FibMean(goles_concedidos5). Solidez defensiva."""
    gc = _goles_c(partidos, team_id)
    if len(gc) < 5:
        return _na()
    fib5 = fib_mean(gc[-5:])
    v = r2(1.0 / fib5 if fib5 > 0 else 3.0)
    if v >= 1.5:   c,l,t = "green","Defensa muy sólida",  f"FibMean concedidos={r2(fib5)}/p. Muy difícil superarla."
    elif v >= 0.7: c,l,t = "yellow","Defensa aceptable",   f"FibMean concedidos={r2(fib5)}/p. Nivel estándar."
    else:           c,l,t = "red","Defensa vulnerable",  f"FibMean concedidos={r2(fib5)}/p. Recibe muchos goles."
    return {"available":True,"value":v,"fib_gc":r2(fib5),
            "color":c,"label":l,"tip":t,"chart":[r2(g) for g in gc[-10:]],
            "formula":"1 / FibMean(goles_concedidos5)"}

def calc_gdr(partidos, team_id):
    """GDR: goles_favor5 / goles_totales5. Dominancia en el marcador."""
    gf = _goles_f(partidos, team_id)
    gc = _goles_c(partidos, team_id)
    if len(gf) < 5:
        return _na()
    gf5 = sum(gf[-5:]); gc5 = sum(gc[-5:]); tot = gf5 + gc5
    v = r2(gf5 / tot if tot > 0 else 0.5)
    if v >= 0.60:   c,l,t = "green","Domina el marcador",  f"{int(v*100)}% de los goles son suyos."
    elif v >= 0.40: c,l,t = "yellow","Equilibrio en goles", f"{int(v*100)}/{int((1-v)*100)} distribución de goles."
    else:            c,l,t = "red","Dominado en goles",   f"Solo {int(v*100)}% de los goles. El rival marca más."
    return {"available":True,"value":v,"gf5":gf5,"gc5":gc5,
            "color":c,"label":l,"tip":t,"formula":"goles_favor5 / goles_totales5"}

def calc_fsi(partidos, team_id):
    """FSI: FibMean(puntos5) / 3.0. Forma reciente ponderada Fibonacci."""
    pts = _puntos(partidos, team_id)
    if len(pts) < 3:
        return _na()
    n     = min(len(pts), 5)
    pts_n = pts[-n:]
    fib_p = fib_mean(pts_n)
    simp  = _mean(pts_n)
    v     = r2(fib_p / 3.0)
    if v >= 0.60:   c,l,t = "green","Excelente forma", f"FibPts={r2(fib_p)}/3 (el último resultado pesa más). Gran momento."
    elif v >= 0.30: c,l,t = "yellow","Forma regular",   f"FibPts={r2(fib_p)}/3. Resultados mixtos recientes."
    else:            c,l,t = "red","Mala forma",      f"FibPts={r2(fib_p)}/3. Los últimos partidos son los peores."
    form = ["W" if p==3 else "D" if p==1 else "L" for p in pts_n]
    return {"available":True,"value":v,"fib_pts":r2(fib_p),"simple_avg":r2(simp),
            "fib_windows":fib_windows(pts),"form":form,"pts_recent":pts_n,
            "color":c,"label":l,"tip":t,"chart":pts[-10:],
            "formula":"FibMean(puntos5) / 3.0"}

def calc_gto(partidos, team_id):
    """GTO: FibMean(5) / avg_temporada de goles. Oscilador de tendencia."""
    goles = _goles_f(partidos, team_id)
    if len(goles) < 6:
        return _na()
    avg_t = _mean(goles, 1.0)
    fib5  = fib_mean(goles[-5:])
    v = r2(fib5 / avg_t if avg_t > 0 else 1.0)
    if v >= 1.3:   c,l,t = "green","Sobre-rendimiento",    f"FibMean5={r2(fib5)} vs media={r2(avg_t)} ({v}x). Ataque disparado."
    elif v >= 0.7: c,l,t = "yellow","Rendimiento esperado", f"FibMean5={r2(fib5)} vs media={r2(avg_t)} ({v}x). En su nivel."
    else:           c,l,t = "red","Sub-rendimiento",      f"FibMean5={r2(fib5)} vs media={r2(avg_t)} ({v}x). Por debajo."
    return {"available":True,"value":v,"fib5":r2(fib5),"avg_season":r2(avg_t),
            "fib_windows":fib_windows(goles),"trend":fib_trend(goles),
            "color":c,"label":l,"tip":t,"chart":[r2(g) for g in goles[-10:]],
            "formula":"FibMean(goles5) / avg_temporada"}

def calc_dci(partidos, team_id):
    """DCI: clean_sheets / 5. Consistencia defensiva."""
    gc = _goles_c(partidos, team_id)
    if len(gc) < 5:
        return _na()
    gc5 = gc[-5:]; cs = sum(1 for g in gc5 if g == 0)
    v = r2(cs / len(gc5))
    if v >= 0.4:   c,l,t = "green","Portería sólida",    f"{cs}/5 partidos sin recibir. Buena para Under/portería a cero."
    elif v >= 0.2: c,l,t = "yellow","Defensa moderada",   f"{cs}/5 partidos sin recibir. Nivel aceptable."
    else:           c,l,t = "red","Portería permeable", f"Solo {cs}/5 sin recibir. Defensa con problemas."
    return {"available":True,"value":v,"clean_sheets":cs,
            "color":c,"label":l,"tip":t,
            "chart":[1 if g==0 else 0 for g in gc[-10:]],"formula":"clean_sheets/5"}

def calc_mvi(partidos, team_id):
    """MVI: σ(goles_últimos10). Volatilidad = imprevisibilidad."""
    goles = _goles_f(partidos, team_id)
    if len(goles) < 5:
        return _na()
    g10 = goles[-10:] if len(goles) >= 10 else goles
    v   = r2(_std(g10))
    if v <= 0.70:   c,l,t = "green","Muy predecible",    "Resultados muy regulares. Alta confianza en Over/Under histórico."
    elif v <= 1.20: c,l,t = "yellow","Volatilidad media", "Cierta variación. Riesgo moderado."
    else:            c,l,t = "red","Muy impredecible",  "Sus resultados cambian mucho. Alto riesgo en cualquier mercado."
    return {"available":True,"value":v,"color":c,"label":l,"tip":t,
            "chart":[r2(g) for g in g10],"formula":"σ(goles_últimos10)"}

def calc_spi(partidos, team_id):
    """SPI: partidos_con_gol / total. Probabilidad empírica de marcar."""
    goles = _goles_f(partidos, team_id)
    if len(goles) < 5:
        return _na()
    cg = sum(1 for g in goles if g > 0)
    v  = r2(cg / len(goles))
    if v >= 0.75:   c,l,t = "green","Casi siempre marca",       f"Anota en el {int(v*100)}% de partidos. Excelente para BTTS."
    elif v >= 0.50: c,l,t = "yellow","Marca con regularidad",    f"Anota en el {int(v*100)}% de partidos."
    else:            c,l,t = "red","Poca frecuencia goleadora", f"Anota solo en el {int(v*100)}% de partidos."
    return {"available":True,"value":v,"con_gol":cg,"total":len(goles),
            "color":c,"label":l,"tip":t,"formula":"partidos_con_gol/total"}

def calc_aer(partidos, team_id):
    """AER: FibMean(todos_goles) / 3.0. Eficiencia ofensiva ponderada."""
    goles = _goles_f(partidos, team_id)
    if not goles:
        return _na()
    fib_all = fib_mean(goles)
    v = r2(min(fib_all / 3.0, 1.0))
    if v >= 0.50:   c,l,t = "green","Ataque muy eficiente",  f"FibMean={r2(fib_all)} goles/p. Producción alta."
    elif v >= 0.27: c,l,t = "yellow","Eficiencia normal",     f"FibMean={r2(fib_all)} goles/p. Estándar."
    else:            c,l,t = "red","Ataque poco eficiente", f"FibMean={r2(fib_all)} goles/p. Bajo rendimiento."
    return {"available":True,"value":v,"fib_goles":r2(fib_all),
            "color":c,"label":l,"tip":t,"chart":[r2(g) for g in goles[-10:]],
            "formula":"FibMean(todos_goles) / 3.0"}

def calc_dri(partidos, team_id):
    """DRI: 1 / (1 + FibMean(goles_recibidos)). Resiliencia defensiva."""
    gc = _goles_c(partidos, team_id)
    if len(gc) < 5:
        return _na()
    n = min(len(gc), 10)
    fib_gc = fib_mean(gc[-n:])
    v = r2(1.0 / (1.0 + fib_gc))
    if v >= 0.55:   c,l,t = "green","Alta resistencia",   f"FibMean recibidos={r2(fib_gc)}/p. Portería difícil de batir."
    elif v >= 0.38: c,l,t = "yellow","Resistencia normal", f"FibMean recibidos={r2(fib_gc)}/p. Nivel estándar."
    else:            c,l,t = "red","Baja resistencia",   f"FibMean recibidos={r2(fib_gc)}/p. Fácil de vulnerar."
    return {"available":True,"value":v,"fib_gc":r2(fib_gc),
            "color":c,"label":l,"tip":t,"chart":[r2(g) for g in gc[-10:]],
            "formula":"1 / (1 + FibMean(goles_recibidos))"}

def calc_opi(gsi, gto, spi):
    """OPI: GSI×(8/16) + GTO×(5/16) + SPI×(3/16). Pesos Fibonacci 8:5:3."""
    if not all(d.get("available") for d in [gsi, gto, spi]):
        return _na("Componentes no disponibles")
    w8,w5,w3 = 8/16, 5/16, 3/16
    gsi_n = min(gsi["value"] / 2.0, 1.0)
    gto_n = min(gto["value"] / 2.0, 1.0)
    v = r2(gsi_n*w8 + gto_n*w5 + spi["value"]*w3)
    if v >= 0.60:   c,l,t = "green","Alta presión ofensiva",   f"OPI={v} (pesos Fib 8:5:3). Ataque intenso. Favorable para Over/BTTS."
    elif v >= 0.35: c,l,t = "yellow","Presión ofensiva normal", f"OPI={v}. Nivel de ataque estándar."
    else:            c,l,t = "red","Baja presión ofensiva",   f"OPI={v}. Ataque flojo. Riesgo en mercados de goles."
    return {"available":True,"value":v,
            "components":{"gsi":gsi["value"],"gto":gto["value"],"spi":spi["value"]},
            "fib_weights":{"gsi":round(w8,3),"gto":round(w5,3),"spi":round(w3,3)},
            "color":c,"label":l,"tip":t,"formula":"GSI×(8/16) + GTO×(5/16) + SPI×(3/16)"}

def calc_dpi(dsi, dci, dri):
    """DPI: DSI_inv×(8/16) + DCI_inv×(5/16) + DRI_inv×(3/16). Pesos Fibonacci."""
    if not all(d.get("available") for d in [dsi, dci, dri]):
        return _na("Componentes no disponibles")
    w8,w5,w3 = 8/16, 5/16, 3/16
    dsi_n = min(1.0 / (dsi["value"]*2 + 0.01), 1.0)
    dci_n = 1.0 - dci["value"]
    dri_n = 1.0 - dri["value"]
    v = r2(dsi_n*w8 + dci_n*w5 + dri_n*w3)
    if v >= 0.60:   c,l,t = "red","Defensa bajo alta presión",  f"DPI={v}. Vulnerabilidad importante. El rival tiene opciones."
    elif v >= 0.35: c,l,t = "yellow","Presión defensiva moderada", f"DPI={v}. Exposición defensiva normal."
    else:            c,l,t = "green","Defensa sólida",             f"DPI={v}. Muy resistente. Favorable para Under."
    return {"available":True,"value":v,
            "components":{"dsi":dsi["value"],"dci":dci["value"],"dri":dri["value"]},
            "fib_weights":{"dsi":round(w8,3),"dci":round(w5,3),"dri":round(w3,3)},
            "color":c,"label":l,"tip":t,"formula":"DSI_inv×(8/16) + DCI_inv×(5/16) + DRI_inv×(3/16)"}

def calc_cr(partidos, team_id):
    """CR: 1 / (σ(puntos) + 0.1). Consistencia de resultados."""
    pts = _puntos(partidos, team_id)
    if len(pts) < 5:
        return _na()
    std = _std(pts[-10:] if len(pts) >= 10 else pts)
    v   = r2(1.0 / (std + 0.1))
    if v >= 1.5:   c,l,t = "green","Muy consistente",    "Resultados muy regulares. Alta confianza para apostar."
    elif v >= 0.7: c,l,t = "yellow","Consistencia media", "Mezcla de buenos y malos resultados. Riesgo moderado."
    else:           c,l,t = "red","Muy inconsistente",  "Resultados erráticos. Alto riesgo en cualquier apuesta."
    return {"available":True,"value":v,"std":r2(std),
            "color":c,"label":l,"tip":t,"chart":pts[-10:],
            "formula":"1 / (σ(puntos) + 0.1)"}

def calc_dcs(opi, dpi, fsi, gdr, cr):
    """DCS: (OPI×8 + FSI×5 + GDR×3 + DPI_inv×2 + CR×1) / 19. Pesos Fibonacci."""
    comps = [d for d in [opi, fsi, gdr, cr] if d.get("available")]
    if not comps:
        return _na("Insuficientes indicadores")
    opi_v = opi.get("value",0.5) if opi.get("available") else 0.5
    fsi_v = fsi.get("value",0.5) if fsi.get("available") else 0.5
    gdr_v = gdr.get("value",0.5) if gdr.get("available") else 0.5
    dpi_v = (1-dpi.get("value",0.5)) if dpi.get("available") else 0.5
    cr_v  = min(cr.get("value",1)/3.0, 1.0) if cr.get("available") else 0.5
    v = r2((opi_v*8 + fsi_v*5 + gdr_v*3 + dpi_v*2 + cr_v*1) / 19)
    if v >= 0.60:   c,l,t = "green","Equipo dominante",   "Indicadores Fibonacci mayormente positivos. Buen momento para apostar."
    elif v >= 0.38: c,l,t = "yellow","Equipo equilibrado", "Indicadores mixtos. Incertidumbre moderada."
    else:            c,l,t = "red","Equipo en crisis",   "Indicadores Fibonacci negativos. Alto riesgo."
    return {"available":True,"value":v,
            "fib_weights":{"OPI":8,"FSI":5,"GDR":3,"DPI_inv":2,"CR":1},
            "components":{"opi":opi_v,"fsi":fsi_v,"gdr":gdr_v,"dpi_inv":dpi_v,"cr":cr_v},
            "color":c,"label":l,"tip":t,
            "formula":"(OPI×8 + FSI×5 + GDR×3 + DPI_inv×2 + CR×1) / 19"}


# ═══════════════════════════════════════════════════════════════
# FMI — Fibonacci Momentum Index  (indicador #16)
# FMI = FibMean(puntos_últimos8) − FibMean(puntos_últimos20)
# Equivalente al histograma MACD pero con ponderación Fibonacci.
# Positivo = equipo mejorando | Negativo = equipo empeorando
# ═══════════════════════════════════════════════════════════════
def calc_fmi(partidos, team_id):
    pts = _puntos(partidos, team_id)
    if len(pts) < 8:
        return _na("Mínimo 8 partidos para FMI")

    n_s = min(len(pts), 8)
    n_l = min(len(pts), 20)
    f8  = fib_mean(pts[-n_s:])
    f20 = fib_mean(pts[-n_l:])
    fmi = r2(f8 - f20)

    # Histograma: evolución del FMI partido a partido
    hist = []
    for i in range(max(8, len(pts) - 12), len(pts) + 1):
        sub = pts[:i]
        if len(sub) < 8: continue
        e8  = fib_mean(sub[-8:])
        e20 = fib_mean(sub[-min(20, len(sub)):])
        hist.append(r2(e8 - e20))

    if fmi > 0.4:    c,l,t = "green","FMI Positivo fuerte",  f"FibMean8={r2(f8)} >> FibMean20={r2(f20)}. Mejora sostenida y acelerada."
    elif fmi > 0.1:  c,l,t = "green","FMI Positivo",          f"FibMean8={r2(f8)} > FibMean20={r2(f20)}. Mejora progresiva reciente."
    elif fmi > -0.1: c,l,t = "yellow","FMI Neutral",            f"FibMean8≈FibMean20. Sin cambio de tendencia claro."
    elif fmi > -0.4: c,l,t = "red","FMI Negativo",           f"FibMean8={r2(f8)} < FibMean20={r2(f20)}. Tendencia bajista."
    else:             c,l,t = "red","FMI Negativo fuerte",   f"FibMean8={r2(f8)} << FibMean20={r2(f20)}. Caída sostenida."

    return {"available":True,"value":fmi,"fib8":r2(f8),"fib20":r2(f20),
            "hist":hist,"crossover":"bullish" if fmi>0.1 else "bearish" if fmi<-0.1 else "neutral",
            "color":c,"label":l,"tip":t,
            "formula":"FibMean(puntos_últimos8) − FibMean(puntos_últimos20)"}


# ═══════════════════════════════════════════════════════════════
# FPI — Fibonacci Performance Index  (indicador #17)
# FPI = (goles_fib×8 + xG_proxy_fib×5 + forma_fib×3) / 16 × 100
# Pesos Fibonacci 8:5:3 para las tres dimensiones clave.
# Escala 0–100.
# ═══════════════════════════════════════════════════════════════
def calc_fpi(partidos, team_id):
    gf  = _goles_f(partidos, team_id)
    gc  = _goles_c(partidos, team_id)
    pts = _puntos(partidos, team_id)

    if len(gf) < 5:
        return _na()

    # Componente 1: goles a favor (FibMean, normalizado 0–1)
    n1       = min(len(gf), 8)
    goles_fib = fib_mean(gf[-n1:])
    goles_n   = min(goles_fib / 3.0, 1.0)

    # Componente 2: xG proxy = ratio goles_favor en el total de goles
    n2     = min(len(gf), 5)
    gf_fib = fib_mean(gf[-n2:])
    gc_fib = fib_mean(gc[-n2:])
    xg_p   = gf_fib / (gf_fib + gc_fib) if (gf_fib + gc_fib) > 0 else 0.5

    # Componente 3: forma en puntos (FibMean, normalizado 0–1)
    n3       = min(len(pts), 8)
    forma_fib = fib_mean(pts[-n3:]) / 3.0

    # Pesos Fibonacci 8:5:3 → total 16
    fpi_raw = (goles_n * 8 + xg_p * 5 + forma_fib * 3) / 16
    fpi     = round(fpi_raw * 100, 1)

    if fpi >= 65:   c,l,t = "green","FPI Alto",   f"FPI={fpi}/100. Buena producción goleadora, presión y forma. Señal global positiva."
    elif fpi >= 40: c,l,t = "yellow","FPI Medio",  f"FPI={fpi}/100. Rendimiento equilibrado sin ventaja ni desventaja clara."
    else:            c,l,t = "red","FPI Bajo",   f"FPI={fpi}/100. Rendimiento general bajo en los tres frentes."

    return {"available":True,"value":fpi,
            "components":{"goles_n":round(goles_n,3),"xg_proxy":round(xg_p,3),"forma_n":round(forma_fib,3)},
            "fib_goles":r2(goles_fib),"fib_forma":r2(fib_mean(pts[-n3:])),
            "fib_windows_goles":fib_windows(gf),"fib_windows_forma":fib_windows(pts),
            "fib_weights":{"goles":8,"xg":5,"forma":3},
            "color":c,"label":l,"tip":t,
            "formula":"(goles_fib×8 + xG_proxy_fib×5 + forma_fib×3) / 16 × 100"}


# ═══════════════════════════════════════════════════════════════
# SEMÁFORO GLOBAL — voto ponderado Fibonacci
# ═══════════════════════════════════════════════════════════════
def _semaforo(indicadores: list) -> dict:
    # Pesos en orden: fpi, fmi, dcs, opi, fsi, gmi, gto, dsi, dpi, dri, dci, gsi, gdr, spi, aer, mvi, cr
    pesos      = [13, 8, 8, 5, 5, 3, 3, 3, 2, 2, 2, 1, 1, 1, 1, 1, 1]
    total_peso = sum(pesos)
    pos = neg = tot = 0.0
    n_ok = 0
    for i, ind in enumerate(indicadores):
        if not ind.get("available"): continue
        w = pesos[i] if i < len(pesos) else 1
        tot += w; n_ok += 1
        c = ind.get("color","yellow")
        if c == "green": pos += w
        elif c == "red":  neg += w
    if tot == 0:
        return {"score":50,"bull":0,"bear":0,"total":0,"color":"yellow","label":"Sin datos"}
    score = round((pos / tot) * 100)
    if score >= 65: sc,sl = "green","Momento positivo"
    elif score >= 40: sc,sl = "yellow","Señales mixtas"
    else: sc,sl = "red","Momento negativo"
    return {"score":score,"bull":round(pos),"bear":round(neg),
            "total":n_ok,"color":sc,"label":sl}


# ═══════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL — FÚTBOL
# ═══════════════════════════════════════════════════════════════
def analyze_all_indicators(partidos: list, team_id: int) -> dict:
    if not partidos or len(partidos) < 3:
        return {"available": False, "reason": "Sin suficientes partidos históricos"}

    p = sorted(partidos, key=lambda x: x.get("date", ""))

    gmi = calc_gmi(p, team_id)
    gsi = calc_gsi(p, team_id)
    dsi = calc_dsi(p, team_id)
    gdr = calc_gdr(p, team_id)
    fsi = calc_fsi(p, team_id)
    gto = calc_gto(p, team_id)
    dci = calc_dci(p, team_id)
    mvi = calc_mvi(p, team_id)
    spi = calc_spi(p, team_id)
    aer = calc_aer(p, team_id)
    dri = calc_dri(p, team_id)
    cr  = calc_cr(p, team_id)
    opi = calc_opi(gsi, gto, spi)
    dpi = calc_dpi(dsi, dci, dri)
    dcs = calc_dcs(opi, dpi, fsi, gdr, cr)
    fmi = calc_fmi(p, team_id)
    fpi = calc_fpi(p, team_id)

    todos = [fpi, fmi, dcs, opi, fsi, gmi, gto, dsi, dpi, dri, dci, gsi, gdr, spi, aer, mvi, cr]
    semaforo = _semaforo(todos)

    return {
        "available": True,
        "gmi": gmi, "gsi": gsi, "dsi": dsi, "gdr": gdr,
        "fsi": fsi, "gto": gto, "dci": dci, "mvi": mvi,
        "spi": spi, "aer": aer, "dri": dri, "cr":  cr,
        "opi": opi, "dpi": dpi, "dcs": dcs,
        "fmi": fmi,
        "fpi": fpi,
        "semaforo": semaforo,
        "fib_ventanas": {
            "goles":   fib_windows(_goles_f(p, team_id)),
            "forma":   fib_windows(_puntos(p, team_id)),
            "defensa": fib_windows(_goles_c(p, team_id)),
        },
    }


# ═══════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL — TENIS
# ═══════════════════════════════════════════════════════════════
def analyze_tennis_indicators(partidos: list, player_id: int,
                               surface=None) -> dict:
    if not partidos or len(partidos) < 3:
        return {"available": False, "reason": "Sin suficientes partidos"}
    adapted = []
    for p in partidos:
        p1_id  = p.get("p1Id") or p.get("player1Id") or 0
        sets_w = p.get("setsWon") or p.get("sets_won") or p.get("sW") or 0
        sets_l = p.get("setsLost") or p.get("sets_lost") or p.get("sL") or 0
        won = sets_w > sets_l
        adapted.append({
            "homeId": player_id if won else 0,
            "awayId": 0 if won else player_id,
            "gH": sets_w if p1_id == player_id else sets_l,
            "gA": sets_l if p1_id == player_id else sets_w,
            "date": p.get("date", ""),
        })
    return analyze_all_indicators(adapted, player_id)