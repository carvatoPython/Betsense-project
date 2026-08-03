"""
prediction_engine.py — BetSense
================================
Blind Prediction Engine + Dixon-Coles + Kelly Criterion + RPS evaluator.

Arquitectura:
  - DixonColesModel   : reemplaza el Poisson básico con corrección rho + time decay
  - BlindPredictor    : guarda predicciones ANTES del partido (cutoff temporal)
  - ResultTracker     : registra resultados reales y calcula métricas
  - KellyCalculator   : dado edge modelo vs. bookmaker, calcula staking óptimo
  - ModelEvaluator    : RPS, calibración, historial de rendimiento

Integración con database.py:
  - Usa las columnas existentes de Prediccion (prob_home, prob_draw, prob_away,
    lambda_home, lambda_away, resultado_real_h/a, acertado)
  - Agrega columnas nuevas vía PrediccionExtendida (tabla separada, mismo pred_id)
  - No modifica tablas existentes → compatibilidad total
"""

import math
import statistics
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import (
    Column, Integer, Float, String, DateTime, Boolean,
    Text, ForeignKey, create_engine
)
from sqlalchemy.orm import sessionmaker, relationship
import os


# ── Re-usar engine/Session de database.py ────────────────────
# Importación lazy para no crear ciclo de dependencias
def _get_session():
    DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///betsense.db")
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
    )
    from database import Base
    _create_extended_tables(engine, Base)
    Session = sessionmaker(bind=engine)
    return Session()


def _create_extended_tables(engine, Base):
    """Crea las tablas nuevas sin tocar las existentes."""
    PrediccionExtendida.__table__.create(bind=engine, checkfirst=True)
    CuotasMercado.__table__.create(bind=engine, checkfirst=True)


# ── Importar Base de database.py ─────────────────────────────
try:
    from database import Base, Session as DBSession
    _USE_DB = True
except ImportError:
    # Modo standalone (para tests)
    from sqlalchemy.orm import declarative_base
    Base = declarative_base()
    _USE_DB = False


# ══════════════════════════════════════════════════════════════
# NUEVAS TABLAS (extienden Prediccion sin modificarla)
# ══════════════════════════════════════════════════════════════

class PrediccionExtendida(Base):
    """
    Extiende la tabla 'predicciones' con métricas del Blind Engine.
    Relación 1-a-1 con Prediccion via prediccion_id.
    """
    __tablename__ = "predicciones_ext"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    prediccion_id  = Column(Integer, ForeignKey("predicciones.id"), unique=True, nullable=False)

    # ── Datos del partido (para contexto en el histórico) ─────
    match_date     = Column(DateTime, nullable=True)   # fecha real del partido
    cutoff_date    = Column(DateTime, default=datetime.utcnow)  # cuando se generó la predicción

    # ── Dixon-Coles (mejora sobre Poisson básico) ─────────────
    rho            = Column(Float, nullable=True)       # corrección de dependencia low-scoring
    time_decay_xi  = Column(Float, default=0.002)       # parámetro de decaimiento temporal
    lambda_h_dc    = Column(Float, nullable=True)       # lambda home ajustado DC
    lambda_a_dc    = Column(Float, nullable=True)       # lambda away ajustado DC
    prob_h_dc      = Column(Float, nullable=True)       # P(home) con corrección DC
    prob_d_dc      = Column(Float, nullable=True)       # P(draw) con corrección DC
    prob_a_dc      = Column(Float, nullable=True)       # P(away) con corrección DC

    # ── Cuotas del bookmaker (para calcular value) ────────────
    bk_cuota_h     = Column(Float, nullable=True)
    bk_cuota_d     = Column(Float, nullable=True)
    bk_cuota_a     = Column(Float, nullable=True)
    bk_source      = Column(String(50), nullable=True)  # ej. "betplay", "rushbet"

    # ── Probabilidades implícitas del bookmaker (sin margen) ──
    bk_prob_h      = Column(Float, nullable=True)
    bk_prob_d      = Column(Float, nullable=True)
    bk_prob_a      = Column(Float, nullable=True)

    # ── Value Edge (modelo vs. bookmaker) ────────────────────
    edge_h         = Column(Float, nullable=True)  # prob_modelo - prob_bk (home)
    edge_d         = Column(Float, nullable=True)
    edge_a         = Column(Float, nullable=True)
    mejor_value    = Column(String(10), nullable=True)  # "1", "X", "2" o None
    mejor_edge     = Column(Float, nullable=True)

    # ── Kelly Criterion ───────────────────────────────────────
    kelly_stake    = Column(Float, nullable=True)   # fracción óptima del bankroll
    kelly_frac     = Column(Float, default=0.25)    # fracción Kelly a usar (25% = conservador)
    bankroll_ref   = Column(Float, nullable=True)   # bankroll al momento de la predicción
    stake_sugerido = Column(Float, nullable=True)   # kelly_stake * kelly_frac * bankroll_ref

    # ── Evaluación post-partido ───────────────────────────────
    rps            = Column(Float, nullable=True)   # Ranked Probability Score (↓ mejor)
    brier          = Column(Float, nullable=True)   # Brier Score
    log_loss       = Column(Float, nullable=True)   # Log Loss
    outcome_real   = Column(String(5), nullable=True)  # "1", "X", "2"
    outcome_pred   = Column(String(5), nullable=True)  # resultado predicho (mayor prob)
    acerto_1x2     = Column(Boolean, nullable=True)
    acerto_score   = Column(Boolean, nullable=True)
    delta_goles    = Column(Integer, nullable=True)  # diferencia goles reales vs esperados

    # ── Indicadores activos en el momento de la predicción ────
    semaforo_score = Column(Integer, nullable=True)   # score del semáforo global
    indicadores_json = Column(Text, nullable=True)    # JSON con los 17 indicadores


class CuotasMercado(Base):
    """
    Snapshot de cuotas en el momento de la predicción.
    Para mercados alternativos: Over/Under, BTTS, etc.
    """
    __tablename__ = "cuotas_mercado"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    prediccion_id  = Column(Integer, ForeignKey("predicciones.id"), nullable=False)
    timestamp      = Column(DateTime, default=datetime.utcnow)
    mercado        = Column(String(50))   # "1x2", "over25", "btts", "over15", "under25"
    seleccion      = Column(String(20))   # "home", "draw", "away", "over", "under", "si", "no"
    cuota_bk       = Column(Float)
    prob_modelo    = Column(Float)
    prob_bk        = Column(Float)        # implícita sin margen
    edge           = Column(Float)        # prob_modelo - prob_bk
    value_bet      = Column(Boolean, default=False)  # edge > threshold
    resultado      = Column(Boolean, nullable=True)  # True=ganó, False=perdió, None=pendiente


# ══════════════════════════════════════════════════════════════
# 1. DIXON-COLES MODEL
# ══════════════════════════════════════════════════════════════

class DixonColesModel:
    """
    Mejora el Poisson básico con:
    1. Corrección rho para marcadores bajos (0-0, 1-0, 0-1, 1-1)
    2. Time decay exponencial (partidos recientes pesan más)
    3. Separación ataque/defensa casa vs. visita

    Paper original: Dixon & Coles (1997) - Journal of Royal Statistical Society
    """

    def __init__(self, xi: float = 0.002):
        """
        xi: parámetro de time decay. 0.002 ≈ partidos de hace 1 año
            pesan ~50% menos que partidos recientes. Recomendado: 0.001–0.004.
        """
        self.xi = xi

    def _tau(self, x: int, y: int, lambda_h: float, lambda_a: float, rho: float) -> float:
        """
        Función de corrección Dixon-Coles para marcadores bajos.
        Ajusta la dependencia entre goles de ambos equipos.
        """
        if x == 0 and y == 0:
            return 1 - lambda_h * lambda_a * rho
        elif x == 1 and y == 0:
            return 1 + lambda_a * rho
        elif x == 0 and y == 1:
            return 1 + lambda_h * rho
        elif x == 1 and y == 1:
            return 1 - rho
        else:
            return 1.0

    def _poisson_prob(self, k: int, lam: float) -> float:
        """P(X=k) con distribución de Poisson."""
        if lam <= 0:
            return 1.0 if k == 0 else 0.0
        try:
            return (lam ** k) * math.exp(-lam) / math.factorial(k)
        except (OverflowError, ValueError):
            return 0.0

    def _time_weight(self, days_ago: float) -> float:
        """Peso exponencial: w = e^(-xi * days_ago)"""
        return math.exp(-self.xi * days_ago)

    def calcular_lambdas_con_decay(
        self,
        partidos_home: list,
        partidos_away: list,
        team_home_id: int,
        team_away_id: int,
        fecha_partido: Optional[datetime] = None
    ) -> dict:
        """
        Calcula lambda_H y lambda_A considerando:
        - Ataque en casa vs. visita (diferenciado)
        - Time decay exponencial
        - Fortaleza relativa del rival

        partidos_home: lista de dicts con {date, gH, gA, homeId, awayId}
        partidos_away: misma estructura
        """
        fecha_ref = fecha_partido or datetime.utcnow()

        def _parse_date(d):
            if isinstance(d, datetime):
                return d
            if isinstance(d, str):
                for fmt in ("%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        return datetime.strptime(d[:19], fmt[:len(d)])
                    except ValueError:
                        continue
            return fecha_ref - timedelta(days=180)  # fallback

        # ── Goles del home como local ─────────────────────────
        gf_home_local, gc_home_local, weights_h = [], [], []
        for p in partidos_home:
            if p.get("homeId") != team_home_id:
                continue
            d = _parse_date(p.get("date", ""))
            w = self._time_weight((fecha_ref - d).days)
            gf_home_local.append(p["gH"] * w)
            gc_home_local.append(p["gA"] * w)
            weights_h.append(w)

        # ── Goles del away como visitante ─────────────────────
        gf_away_visit, gc_away_visit, weights_a = [], [], []
        for p in partidos_away:
            if p.get("awayId") != team_away_id:
                continue
            d = _parse_date(p.get("date", ""))
            w = self._time_weight((fecha_ref - d).days)
            gf_away_visit.append(p["gA"] * w)
            gc_away_visit.append(p["gH"] * w)
            weights_a.append(w)

        # ── Promedios ponderados ──────────────────────────────
        def _wavg(vals, ws):
            if not ws or sum(ws) == 0:
                return 1.2
            return sum(vals) / sum(ws)

        ataque_h   = _wavg(gf_home_local, weights_h)   # goles que mete home en casa
        defensa_h  = _wavg(gc_home_local, weights_h)   # goles que recibe home en casa
        ataque_a   = _wavg(gf_away_visit, weights_a)   # goles que mete away de visitante
        defensa_a  = _wavg(gc_away_visit, weights_a)   # goles que recibe away de visitante

        # ── Lambdas cruzadas ──────────────────────────────────
        # lambda_H = ataque_home_local * defensa_away_visitante
        # lambda_A = ataque_away_visitante * defensa_home_local
        lambda_h = max(ataque_h * defensa_a, 0.3)
        lambda_a = max(ataque_a * defensa_h, 0.3)

        return {
            "lambda_h": round(lambda_h, 4),
            "lambda_a": round(lambda_a, 4),
            "ataque_h": round(ataque_h, 4),
            "defensa_h": round(defensa_h, 4),
            "ataque_a": round(ataque_a, 4),
            "defensa_a": round(defensa_a, 4),
            "n_partidos_h": len(weights_h),
            "n_partidos_a": len(weights_a),
        }

    def calcular_probabilidades_dc(
        self,
        lambda_h: float,
        lambda_a: float,
        rho: float = -0.1,
        max_goles: int = 8
    ) -> dict:
        """
        Genera la distribución completa de marcadores con corrección Dixon-Coles.

        rho: parámetro de dependencia. Típicamente negativo (-0.1 a -0.2).
             Negativo = los goles del local y visitante están ligeramente
             correlacionados negativamente (un equipo marca → el otro menos).
        max_goles: cortar en este número para cálculo (8 es suficiente).
        """
        prob_home = 0.0
        prob_draw = 0.0
        prob_away = 0.0
        score_matrix = {}  # (i, j) → probabilidad

        for i in range(max_goles + 1):
            for j in range(max_goles + 1):
                p_ij = (
                    self._tau(i, j, lambda_h, lambda_a, rho)
                    * self._poisson_prob(i, lambda_h)
                    * self._poisson_prob(j, lambda_a)
                )
                score_matrix[(i, j)] = max(p_ij, 0.0)

                if i > j:
                    prob_home += p_ij
                elif i == j:
                    prob_draw += p_ij
                else:
                    prob_away += p_ij

        # Normalizar (rho puede introducir leve desbalance)
        total = prob_home + prob_draw + prob_away
        if total > 0:
            prob_home /= total
            prob_draw /= total
            prob_away /= total

        # Marcador más probable
        best_score = max(score_matrix, key=score_matrix.get)
        best_prob  = score_matrix[best_score]

        # Over/Under y BTTS
        over25 = sum(v for (i, j), v in score_matrix.items() if i + j > 2)
        over15 = sum(v for (i, j), v in score_matrix.items() if i + j > 1)
        under25 = 1 - over25
        btts   = sum(v for (i, j), v in score_matrix.items() if i > 0 and j > 0)

        return {
            "prob_home": round(prob_home, 4),
            "prob_draw": round(prob_draw, 4),
            "prob_away": round(prob_away, 4),
            "over25":    round(over25, 4),
            "over15":    round(over15, 4),
            "under25":   round(under25, 4),
            "btts":      round(btts, 4),
            "best_score": {"h": best_score[0], "a": best_score[1], "prob": round(best_prob, 4)},
            "score_matrix": {f"{i}-{j}": round(v, 5) for (i, j), v in score_matrix.items() if v > 0.005},
            "rho_used": rho,
        }

    def estimar_rho(self, partidos_historicos: list) -> float:
        """
        Estima rho empíricamente contando cuántos 0-0, 1-0, 0-1, 1-1
        ocurrieron vs. lo que predice Poisson independiente.

        Con pocos datos, devuelve -0.1 (valor estándar Dixon-Coles).
        """
        if len(partidos_historicos) < 20:
            return -0.1

        # Promedios globales
        todos_h = [p["gH"] for p in partidos_historicos]
        todos_a = [p["gA"] for p in partidos_historicos]
        avg_h = statistics.mean(todos_h) if todos_h else 1.2
        avg_a = statistics.mean(todos_a) if todos_a else 1.0

        # Conteo real de marcadores bajos
        n = len(partidos_historicos)
        c00_real = sum(1 for p in partidos_historicos if p["gH"] == 0 and p["gA"] == 0) / n
        c10_real = sum(1 for p in partidos_historicos if p["gH"] == 1 and p["gA"] == 0) / n
        c01_real = sum(1 for p in partidos_historicos if p["gH"] == 0 and p["gA"] == 1) / n
        c11_real = sum(1 for p in partidos_historicos if p["gH"] == 1 and p["gA"] == 1) / n

        # Predicción Poisson independiente
        p0h = math.exp(-avg_h)
        p1h = avg_h * math.exp(-avg_h)
        p0a = math.exp(-avg_a)
        p1a = avg_a * math.exp(-avg_a)

        c00_pois = p0h * p0a
        # Estimar rho por el desvío en 0-0
        if c00_pois > 0:
            rho_est = (c00_real - c00_pois) / (avg_h * avg_a * c00_pois)
        else:
            rho_est = -0.1

        # Clip para valores razonables
        return max(min(rho_est, 0.1), -0.3)


# ══════════════════════════════════════════════════════════════
# 2. BLIND PREDICTOR (el "amigo que no vio el partido")
# ══════════════════════════════════════════════════════════════

class BlindPredictor:
    """
    Guarda una predicción ANTES del partido usando solo datos
    con fecha anterior al cutoff. Nunca mira datos del partido mismo.

    Flujo:
        1. blind_predict(pred_id, partidos_H, partidos_A, match_date, cuotas_bk)
        2. (partido ocurre)
        3. registrar_resultado(pred_id, goles_h, goles_a)
        → el sistema calcula RPS, Brier, Log Loss y actualiza la ext.
    """

    def __init__(self):
        self.dc = DixonColesModel(xi=0.002)
        self.kelly = KellyCalculator()
        self.evaluator = ModelEvaluator()

    def blind_predict(
        self,
        prediccion_id: int,
        partidos_home: list,
        partidos_away: list,
        team_home_id: int,
        team_away_id: int,
        match_date: Optional[datetime] = None,
        cuotas_bk: Optional[dict] = None,
        bankroll: float = 0.0,
        semaforo_score: int = 50,
        indicadores_json: str = "",
    ) -> dict:
        """
        Genera predicción ciega y la guarda en predicciones_ext.

        cuotas_bk: dict con keys "cuota_h", "cuota_d", "cuota_a", "source"
        Devuelve dict con toda la predicción + value betting info.
        """
        cutoff = match_date or datetime.utcnow()

        # ── Filtrar partidos ANTERIORES al cutoff ─────────────
        def _antes_del_cutoff(partidos):
            resultado = []
            for p in partidos:
                fecha_p = self._parse_date(p.get("date", ""), cutoff)
                if fecha_p < cutoff:
                    resultado.append(p)
            return resultado

        ph_filtered = _antes_del_cutoff(partidos_home)
        pa_filtered = _antes_del_cutoff(partidos_away)

        # ── Dixon-Coles ───────────────────────────────────────
        lambdas = self.dc.calcular_lambdas_con_decay(
            ph_filtered, pa_filtered,
            team_home_id, team_away_id,
            fecha_partido=cutoff
        )
        rho = self.dc.estimar_rho(ph_filtered + pa_filtered)
        probs = self.dc.calcular_probabilidades_dc(
            lambdas["lambda_h"], lambdas["lambda_a"], rho=rho
        )

        resultado = {
            "prediccion_id": prediccion_id,
            "cutoff_date": cutoff.isoformat(),
            "lambda_h_dc": lambdas["lambda_h"],
            "lambda_a_dc": lambdas["lambda_a"],
            "rho": rho,
            "prob_h_dc": probs["prob_home"],
            "prob_d_dc": probs["prob_draw"],
            "prob_a_dc": probs["prob_away"],
            "over25_dc": probs["over25"],
            "over15_dc": probs["over15"],
            "under25_dc": probs["under25"],
            "btts_dc": probs["btts"],
            "best_score_dc": probs["best_score"],
            "n_partidos_h": lambdas["n_partidos_h"],
            "n_partidos_a": lambdas["n_partidos_a"],
        }

        # ── Value Betting (si hay cuotas del bookmaker) ───────
        if cuotas_bk:
            value = self.kelly.calcular_value(
                prob_modelo_h=probs["prob_home"],
                prob_modelo_d=probs["prob_draw"],
                prob_modelo_a=probs["prob_away"],
                cuota_h=cuotas_bk.get("cuota_h"),
                cuota_d=cuotas_bk.get("cuota_d"),
                cuota_a=cuotas_bk.get("cuota_a"),
            )
            resultado.update(value)

            if bankroll > 0 and value.get("mejor_edge", 0) > 0:
                stake = self.kelly.calcular_stake(
                    prob_modelo=value["mejor_prob_modelo"],
                    cuota=value["mejor_cuota"],
                    bankroll=bankroll,
                    fraccion=0.25,  # Kelly fraccional conservador
                )
                resultado["kelly_stake"] = stake["kelly_pleno"]
                resultado["stake_sugerido"] = stake["stake_final"]
                resultado["bankroll_ref"] = bankroll

        # ── Guardar en DB ─────────────────────────────────────
        self._guardar_ext(prediccion_id, resultado, match_date,
                          semaforo_score, indicadores_json, cuotas_bk)

        return resultado

    def registrar_resultado(
        self,
        prediccion_id: int,
        goles_h: int,
        goles_a: int,
    ) -> dict:
        """
        Registra el resultado real y calcula todas las métricas de evaluación.
        Llama después de que el partido ocurrió.
        """
        # Obtener predicción extendida
        try:
            session = DBSession() if _USE_DB else None
        except Exception:
            return {"error": "No hay sesión DB disponible"}

        try:
            ext = session.query(PrediccionExtendida)\
                         .filter_by(prediccion_id=prediccion_id).first()
            if not ext:
                return {"error": f"No hay predicción ext para ID {prediccion_id}"}

            # Determinar resultado
            if goles_h > goles_a:
                outcome = "1"
            elif goles_h == goles_a:
                outcome = "X"
            else:
                outcome = "2"

            probs = {
                "1": ext.prob_h_dc or 0.33,
                "X": ext.prob_d_dc or 0.33,
                "2": ext.prob_a_dc or 0.33,
            }
            pred_outcome = max(probs, key=probs.get)

            # ── Métricas ──────────────────────────────────────
            rps   = self.evaluator.calcular_rps(probs["1"], probs["X"], probs["2"], outcome)
            brier = self.evaluator.calcular_brier(probs["1"], probs["X"], probs["2"], outcome)
            ll    = self.evaluator.calcular_log_loss(probs["1"], probs["X"], probs["2"], outcome)

            # ── Actualizar ext ────────────────────────────────
            ext.outcome_real  = outcome
            ext.outcome_pred  = pred_outcome
            ext.acerto_1x2    = (outcome == pred_outcome)
            ext.rps           = rps
            ext.brier         = brier
            ext.log_loss      = ll
            ext.delta_goles   = abs(goles_h - goles_a) - abs(
                (ext.lambda_h_dc or 1.2) - (ext.lambda_a_dc or 1.0)
            )
            session.commit()

            delta_analisis = self._analizar_delta(
                pred_outcome, outcome, probs,
                ext.lambda_h_dc, ext.lambda_a_dc,
                goles_h, goles_a, ext.semaforo_score
            )

            return {
                "prediccion_id": prediccion_id,
                "resultado_real": f"{goles_h}-{goles_a}",
                "outcome_real": outcome,
                "outcome_pred": pred_outcome,
                "acerto": outcome == pred_outcome,
                "rps": round(rps, 4),
                "brier": round(brier, 4),
                "log_loss": round(ll, 4),
                "analisis_delta": delta_analisis,
            }
        finally:
            session.close()

    def _analizar_delta(
        self, pred, real, probs, lh, la, gh, ga, semaforo
    ) -> str:
        """
        Genera un texto de diagnóstico: ¿por qué falló el modelo?
        Esto es el corazón del 'amigo que no vio el partido'.
        """
        lines = []
        if pred == real:
            lines.append(f"✅ Acertó el resultado ({real}).")
        else:
            lines.append(f"❌ Predijo {pred} ({round(probs[pred]*100,1)}%) pero fue {real}.")

        # Goles esperados vs. reales
        goles_esp = round((lh or 1.2) + (la or 1.0), 1)
        goles_real = gh + ga
        diff_goles = goles_real - goles_esp
        if abs(diff_goles) > 1.5:
            dir = "más" if diff_goles > 0 else "menos"
            lines.append(f"⚠️  El partido tuvo {dir} goles de lo esperado "
                         f"({goles_real} real vs {goles_esp} esperados).")

        # Semáforo
        if semaforo:
            if semaforo >= 65 and real in ("X", "2"):
                lines.append("📊 Semáforo estaba verde pero el favorito no ganó — "
                             "revisar si el indicador sobrevalora la forma reciente.")
            elif semaforo <= 35 and real == "1":
                lines.append("📊 Semáforo estaba rojo pero ganó el local — "
                             "posible partido con factor contextual no capturado.")

        return " | ".join(lines)

    def _parse_date(self, d, fallback):
        if isinstance(d, datetime):
            return d
        if isinstance(d, str):
            for fmt in ("%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%d"):
                try:
                    return datetime.strptime(d[:10], "%Y-%m-%d")
                except ValueError:
                    continue
        return fallback - timedelta(days=180)

    def _guardar_ext(self, pred_id, resultado, match_date,
                     semaforo_score, indicadores_json, cuotas_bk):
        """Persiste PrediccionExtendida en la DB."""
        if not _USE_DB:
            return
        try:
            session = DBSession()
            # Evitar duplicado
            existente = session.query(PrediccionExtendida)\
                               .filter_by(prediccion_id=pred_id).first()
            if existente:
                session.close()
                return

            ext = PrediccionExtendida(
                prediccion_id  = pred_id,
                match_date     = match_date,
                cutoff_date    = datetime.utcnow(),
                lambda_h_dc    = resultado.get("lambda_h_dc"),
                lambda_a_dc    = resultado.get("lambda_a_dc"),
                rho            = resultado.get("rho"),
                prob_h_dc      = resultado.get("prob_h_dc"),
                prob_d_dc      = resultado.get("prob_d_dc"),
                prob_a_dc      = resultado.get("prob_a_dc"),
                bk_cuota_h     = cuotas_bk.get("cuota_h") if cuotas_bk else None,
                bk_cuota_d     = cuotas_bk.get("cuota_d") if cuotas_bk else None,
                bk_cuota_a     = cuotas_bk.get("cuota_a") if cuotas_bk else None,
                bk_source      = cuotas_bk.get("source") if cuotas_bk else None,
                bk_prob_h      = resultado.get("bk_prob_h"),
                bk_prob_d      = resultado.get("bk_prob_d"),
                bk_prob_a      = resultado.get("bk_prob_a"),
                edge_h         = resultado.get("edge_h"),
                edge_d         = resultado.get("edge_d"),
                edge_a         = resultado.get("edge_a"),
                mejor_value    = resultado.get("mejor_value"),
                mejor_edge     = resultado.get("mejor_edge"),
                kelly_stake    = resultado.get("kelly_stake"),
                bankroll_ref   = resultado.get("bankroll_ref"),
                stake_sugerido = resultado.get("stake_sugerido"),
                semaforo_score = semaforo_score,
                indicadores_json = indicadores_json,
            )
            session.add(ext)
            session.commit()
            print(f"✅ BlindPredictor: predicción ext guardada → ID {pred_id}")
        except Exception as e:
            print(f"❌ BlindPredictor._guardar_ext: {e}")
        finally:
            session.close()


# ══════════════════════════════════════════════════════════════
# 3. KELLY CALCULATOR
# ══════════════════════════════════════════════════════════════

class KellyCalculator:
    """
    Implementa el Kelly Criterion para apuestas deportivas.

    Fórmula: f* = (b*p - q) / b
        b = cuota_decimal - 1  (ganancia neta por unidad apostada)
        p = probabilidad modelo de ganar
        q = 1 - p

    Kelly pleno es agresivo. Usar Kelly Fraccional (25-50%) para
    mayor estabilidad (paper Springer 2026, 5 ligas europeas).
    """

    def calcular_kelly(
        self, prob: float, cuota: float
    ) -> float:
        """
        Devuelve la fracción óptima del bankroll (0 a 1).
        Si no hay edge positivo, devuelve 0 (no apostar).
        """
        if prob <= 0 or cuota <= 1:
            return 0.0
        b = cuota - 1.0
        q = 1.0 - prob
        f = (b * prob - q) / b
        return max(round(f, 4), 0.0)

    def calcular_stake(
        self,
        prob_modelo: float,
        cuota: float,
        bankroll: float,
        fraccion: float = 0.25
    ) -> dict:
        """
        Calcula el stake recomendado.
        fraccion: 0.25 = Kelly al 25% (recomendado para modelos con ruido)
        """
        kelly_pleno = self.calcular_kelly(prob_modelo, cuota)
        stake_kelly = kelly_pleno * fraccion * bankroll

        prob_bk = 1 / cuota if cuota > 0 else 0.5
        edge = prob_modelo - prob_bk

        return {
            "kelly_pleno": round(kelly_pleno, 4),
            "kelly_fraccional": round(kelly_pleno * fraccion, 4),
            "fraccion_usada": fraccion,
            "bankroll": bankroll,
            "stake_final": round(stake_kelly, 2),
            "edge_pct": round(edge * 100, 2),
            "roi_esperado": round(edge * 100, 2),
            "recomendacion": (
                "✅ APOSTAR" if edge > 0.03
                else "⚠️ EDGE MÍNIMO" if edge > 0
                else "🚫 NO APOSTAR — sin edge"
            ),
        }

    def calcular_value(
        self,
        prob_modelo_h: float,
        prob_modelo_d: float,
        prob_modelo_a: float,
        cuota_h: Optional[float],
        cuota_d: Optional[float],
        cuota_a: Optional[float],
    ) -> dict:
        """
        Compara las probabilidades del modelo contra las implícitas
        del bookmaker. Detecta qué mercado tiene más value.
        """
        resultado = {}

        # Eliminar margen del bookmaker (overround)
        cuotas_validas = [c for c in [cuota_h, cuota_d, cuota_a] if c and c > 1]
        if len(cuotas_validas) == 3:
            overround = sum(1/c for c in cuotas_validas)
            bk_prob_h = (1/cuota_h) / overround if cuota_h else None
            bk_prob_d = (1/cuota_d) / overround if cuota_d else None
            bk_prob_a = (1/cuota_a) / overround if cuota_a else None
        else:
            bk_prob_h = 1/cuota_h if cuota_h and cuota_h > 1 else None
            bk_prob_d = 1/cuota_d if cuota_d and cuota_d > 1 else None
            bk_prob_a = 1/cuota_a if cuota_a and cuota_a > 1 else None

        resultado["bk_prob_h"] = round(bk_prob_h, 4) if bk_prob_h else None
        resultado["bk_prob_d"] = round(bk_prob_d, 4) if bk_prob_d else None
        resultado["bk_prob_a"] = round(bk_prob_a, 4) if bk_prob_a else None

        # Edges
        edge_h = (prob_modelo_h - bk_prob_h) if bk_prob_h else None
        edge_d = (prob_modelo_d - bk_prob_d) if bk_prob_d else None
        edge_a = (prob_modelo_a - bk_prob_a) if bk_prob_a else None

        resultado["edge_h"] = round(edge_h, 4) if edge_h is not None else None
        resultado["edge_d"] = round(edge_d, 4) if edge_d is not None else None
        resultado["edge_a"] = round(edge_a, 4) if edge_a is not None else None

        # Mejor value
        edges = {"1": (edge_h, prob_modelo_h, cuota_h),
                 "X": (edge_d, prob_modelo_d, cuota_d),
                 "2": (edge_a, prob_modelo_a, cuota_a)}
        mejor = None
        mejor_edge_val = 0.0
        for key, (edge, prob, cuota) in edges.items():
            if edge is not None and edge > mejor_edge_val:
                mejor_edge_val = edge
                mejor = key
                mejor_prob = prob
                mejor_cuota = cuota

        resultado["mejor_value"] = mejor
        resultado["mejor_edge"] = round(mejor_edge_val, 4) if mejor else 0.0
        resultado["mejor_prob_modelo"] = mejor_prob if mejor else None
        resultado["mejor_cuota"] = mejor_cuota if mejor else None
        resultado["hay_value"] = mejor_edge_val > 0.03  # threshold mínimo 3%

        return resultado


# ══════════════════════════════════════════════════════════════
# 4. MODEL EVALUATOR (RPS, Brier, Log Loss, Calibración)
# ══════════════════════════════════════════════════════════════

class ModelEvaluator:
    """
    Métricas de evaluación probabilística para modelos de predicción.

    RPS (Ranked Probability Score): métrica estándar del Soccer Prediction
        Challenge. Penaliza predicciones alejadas del resultado real.
        Rango: [0, 1]. Cuanto más bajo, mejor.

    Brier Score: error cuadrático medio de las probabilidades.

    Log Loss: penaliza fuertemente cuando el modelo asigna baja
        probabilidad al resultado que ocurrió.
    """

    def calcular_rps(
        self, p_home: float, p_draw: float, p_away: float, outcome: str
    ) -> float:
        """
        RPS para un partido de 3 outcomes (1X2).
        outcome: "1", "X", "2"

        Fórmula: RPS = (1/2) * Σ (cumP_pred[i] - cumP_real[i])²
        Orden: home, draw, away
        """
        # Vector de probabilidades predichas
        pred = [p_home, p_draw, p_away]

        # Vector de probabilidades reales (one-hot)
        if outcome == "1":
            real = [1, 0, 0]
        elif outcome == "X":
            real = [0, 1, 0]
        else:
            real = [0, 0, 1]

        # Probabilidades acumuladas
        cum_pred = [pred[0], pred[0] + pred[1], 1.0]
        cum_real = [real[0], real[0] + real[1], 1.0]

        rps = sum((cum_pred[i] - cum_real[i]) ** 2 for i in range(2)) / 2
        return rps

    def calcular_brier(
        self, p_home: float, p_draw: float, p_away: float, outcome: str
    ) -> float:
        """
        Brier Score multi-clase.
        Brier = (1/3) * Σ (p_predicha - p_real)²
        """
        pred = [p_home, p_draw, p_away]
        real = ([1, 0, 0] if outcome == "1"
                else [0, 1, 0] if outcome == "X"
                else [0, 0, 1])
        return sum((p - r) ** 2 for p, r in zip(pred, real)) / 3

    def calcular_log_loss(
        self, p_home: float, p_draw: float, p_away: float, outcome: str
    ) -> float:
        """
        Log Loss: -log(p_predicha_para_outcome_real)
        Penaliza fuertemente la confianza en el resultado incorrecto.
        """
        eps = 1e-7
        if outcome == "1":
            p = max(p_home, eps)
        elif outcome == "X":
            p = max(p_draw, eps)
        else:
            p = max(p_away, eps)
        return -math.log(p)

    def calcular_rps_promedio(self, predicciones_ext: list) -> dict:
        """
        Dado una lista de PrediccionExtendida con rps calculado,
        devuelve estadísticas agregadas del modelo.
        """
        rps_vals = [p.rps for p in predicciones_ext if p.rps is not None]
        brier_vals = [p.brier for p in predicciones_ext if p.brier is not None]
        acertos = [p.acerto_1x2 for p in predicciones_ext if p.acerto_1x2 is not None]

        if not rps_vals:
            return {"disponible": False, "n": 0}

        return {
            "disponible": True,
            "n": len(rps_vals),
            "rps_promedio": round(statistics.mean(rps_vals), 4),
            "rps_mediana": round(statistics.median(rps_vals), 4),
            "rps_std": round(statistics.stdev(rps_vals), 4) if len(rps_vals) > 1 else 0,
            "brier_promedio": round(statistics.mean(brier_vals), 4) if brier_vals else None,
            "precision_1x2": round(sum(acertos) / len(acertos) * 100, 1) if acertos else None,
            "referencia": {
                "naive_1x2": 0.2222,   # modelo que siempre predice 33/33/33
                "buen_modelo": 0.18,   # RPS < 0.18 se considera bueno
                "excelente": 0.16,     # RPS < 0.16 es competitivo con bookmakers
            },
            "rating": (
                "🟢 Excelente" if statistics.mean(rps_vals) < 0.16
                else "🟡 Bueno" if statistics.mean(rps_vals) < 0.20
                else "🔴 A mejorar"
            ),
        }

    def calcrar_calibracion(self, predicciones_ext: list) -> dict:
        """
        Curva de calibración: agrupa predicciones por rango de probabilidad
        y compara con la frecuencia real. Si el modelo está calibrado,
        cuando predice 60% deberían ocurrir ~60% de los eventos.

        Devuelve lista de buckets para graficar en el frontend.
        """
        buckets = {
            "0-10": {"pred_sum": 0, "real_sum": 0, "n": 0},
            "10-20": {"pred_sum": 0, "real_sum": 0, "n": 0},
            "20-30": {"pred_sum": 0, "real_sum": 0, "n": 0},
            "30-40": {"pred_sum": 0, "real_sum": 0, "n": 0},
            "40-50": {"pred_sum": 0, "real_sum": 0, "n": 0},
            "50-60": {"pred_sum": 0, "real_sum": 0, "n": 0},
            "60-70": {"pred_sum": 0, "real_sum": 0, "n": 0},
            "70-80": {"pred_sum": 0, "real_sum": 0, "n": 0},
            "80-90": {"pred_sum": 0, "real_sum": 0, "n": 0},
            "90-100": {"pred_sum": 0, "real_sum": 0, "n": 0},
        }

        for p in predicciones_ext:
            if p.outcome_real is None:
                continue
            for prob, outcome_key in [
                (p.prob_h_dc, "1"),
                (p.prob_d_dc, "X"),
                (p.prob_a_dc, "2"),
            ]:
                if prob is None:
                    continue
                pct = prob * 100
                for key in buckets:
                    lo, hi = map(int, key.split("-"))
                    if lo <= pct < hi:
                        buckets[key]["pred_sum"] += prob
                        buckets[key]["real_sum"] += (1 if p.outcome_real == outcome_key else 0)
                        buckets[key]["n"] += 1
                        break

        calibracion = []
        for key, vals in buckets.items():
            if vals["n"] > 0:
                calibracion.append({
                    "rango": key,
                    "prob_predicha": round(vals["pred_sum"] / vals["n"], 3),
                    "prob_real": round(vals["real_sum"] / vals["n"], 3),
                    "n": vals["n"],
                })

        return {"buckets": calibracion, "n_total": sum(v["n"] for v in buckets.values())}


# ══════════════════════════════════════════════════════════════
# 5. FUNCIONES DE ACCESO PRINCIPAL (para betAI.py)
# ══════════════════════════════════════════════════════════════

_predictor = BlindPredictor()
_evaluator = ModelEvaluator()


def generar_prediccion_ciega(
    prediccion_id: int,
    partidos_home: list,
    partidos_away: list,
    team_home_id: int,
    team_away_id: int,
    match_date: Optional[datetime] = None,
    cuotas_bk: Optional[dict] = None,
    bankroll: float = 0.0,
    semaforo_score: int = 50,
    indicadores_json: str = "",
) -> dict:
    """
    Punto de entrada principal desde betAI.py.
    Llama después de guardar_prediccion() para extender con el Blind Engine.

    Ejemplo de uso:
        pred_id = guardar_prediccion(data)
        resultado_ciego = generar_prediccion_ciega(
            prediccion_id=pred_id,
            partidos_home=home_matches,
            partidos_away=away_matches,
            team_home_id=real_madrid_id,
            team_away_id=barcelona_id,
            match_date=datetime(2025, 10, 26),
            cuotas_bk={"cuota_h": 2.10, "cuota_d": 3.40, "cuota_a": 3.50, "source": "betplay"},
            bankroll=100000,  # en pesos colombianos
            semaforo_score=analysis_data["semaforo"]["score"],
        )
    """
    return _predictor.blind_predict(
        prediccion_id=prediccion_id,
        partidos_home=partidos_home,
        partidos_away=partidos_away,
        team_home_id=team_home_id,
        team_away_id=team_away_id,
        match_date=match_date,
        cuotas_bk=cuotas_bk,
        bankroll=bankroll,
        semaforo_score=semaforo_score,
        indicadores_json=indicadores_json,
    )


def registrar_resultado_real(
    prediccion_id: int, goles_h: int, goles_a: int
) -> dict:
    """
    Registra el resultado real post-partido y calcula RPS, Brier, Log Loss.
    También actualiza la Prediccion base (acertado, resultado_real_h/a).

    Este es el momento donde el modelo "ve el partido" y se evalúa a sí mismo.
    """
    resultado_ext = _predictor.registrar_resultado(prediccion_id, goles_h, goles_a)

    # También actualizar la tabla base via database.py
    try:
        from database import actualizar_resultado
        actualizar_resultado(prediccion_id, goles_h, goles_a)
    except Exception:
        pass

    return resultado_ext


def obtener_rendimiento_modelo(limit: int = 100) -> dict:
    """
    Devuelve las estadísticas de rendimiento del Blind Engine
    para mostrar en el dashboard de BetSense.
    """
    if not _USE_DB:
        return {"disponible": False}
    try:
        session = DBSession()
        preds = session.query(PrediccionExtendida)\
                       .filter(PrediccionExtendida.rps.isnot(None))\
                       .order_by(PrediccionExtendida.cutoff_date.desc())\
                       .limit(limit).all()
        stats = _evaluator.calcular_rps_promedio(preds)
        calibracion = _evaluator.calcrar_calibracion(preds)
        stats["calibracion"] = calibracion
        return stats
    except Exception as e:
        return {"disponible": False, "error": str(e)}
    finally:
        session.close()


def calcular_kelly_rapido(
    prob_modelo: float,
    cuota_bookmaker: float,
    bankroll: float,
    fraccion: float = 0.25,
) -> dict:
    """
    Cálculo rápido de Kelly sin necesidad de predicción guardada.
    Útil para el módulo de Value Betting del frontend.

    prob_modelo: probabilidad que da el modelo (0.0 a 1.0)
    cuota_bookmaker: cuota decimal del bookmaker (ej. 2.10)
    bankroll: bankroll total en cualquier moneda
    fraccion: 0.25 por defecto (Kelly al 25%, conservador)
    """
    return _predictor.kelly.calcular_stake(prob_modelo, cuota_bookmaker, bankroll, fraccion)