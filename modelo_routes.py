"""
modelo_routes.py — BetSense
============================
Blueprint de Flask con los endpoints que consume panel_rendimiento.html.

Cómo integrar en tu app principal (donde tengas `app = Flask(__name__)`):

    from modelo_routes import modelo_bp
    app.register_blueprint(modelo_bp)

Eso agrega automáticamente:
    GET  /api/modelo/rendimiento
    GET  /api/modelo/historial?limit=50
    POST /api/modelo/resultado
"""

from flask import Blueprint, jsonify, request
from datetime import datetime

from prediction_engine import (
    obtener_rendimiento_modelo,
    registrar_resultado_real,
    PrediccionExtendida,
)
try:
    from database import Session as DBSession
except ImportError:
    DBSession = None


modelo_bp = Blueprint("modelo_bp", __name__)


# ══════════════════════════════════════════════════════════════
# GET /api/modelo/rendimiento
# ══════════════════════════════════════════════════════════════
@modelo_bp.route("/api/modelo/rendimiento", methods=["GET"])
def api_modelo_rendimiento():
    """
    Devuelve las métricas agregadas: RPS promedio, precisión 1X2,
    Brier score, rating, y datos de calibración para el gráfico.
    """
    try:
        stats = obtener_rendimiento_modelo(limit=200)
        return jsonify(stats), 200
    except Exception as e:
        return jsonify({"disponible": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════
# GET /api/modelo/historial
# ══════════════════════════════════════════════════════════════
@modelo_bp.route("/api/modelo/historial", methods=["GET"])
def api_modelo_historial():
    """
    Devuelve el historial completo de predicciones (evaluadas y pendientes)
    para la tabla del frontend.
    """
    if DBSession is None:
        return jsonify({"predicciones": []}), 200

    limit = request.args.get("limit", 50, type=int)
    session = DBSession()
    try:
        # Traer las predicciones extendidas más recientes
        ext_rows = (
            session.query(PrediccionExtendida)
            .order_by(PrediccionExtendida.cutoff_date.desc())
            .limit(limit)
            .all()
        )

        resultado = []
        for ext in ext_rows:
            # Intentar traer datos del partido desde Prediccion base
            nombre_partido = None
            score_real = None
            try:
                from database import Prediccion
                base = session.query(Prediccion).filter_by(id=ext.prediccion_id).first()
                if base:
                    home = getattr(base, "equipo_local", None) or getattr(base, "home_team", None)
                    away = getattr(base, "equipo_visitante", None) or getattr(base, "away_team", None)
                    if home and away:
                        nombre_partido = f"{home} vs {away}"
                    gh = getattr(base, "resultado_real_h", None)
                    ga = getattr(base, "resultado_real_a", None)
                    if gh is not None and ga is not None:
                        score_real = f"{gh}-{ga}"
            except Exception:
                pass

            resultado.append({
                "prediccion_id": ext.prediccion_id,
                "partido": nombre_partido,
                "cutoff_date": ext.cutoff_date.isoformat() if ext.cutoff_date else None,
                "match_date": ext.match_date.isoformat() if ext.match_date else None,
                "lambda_h_dc": ext.lambda_h_dc,
                "lambda_a_dc": ext.lambda_a_dc,
                "prob_h_dc": ext.prob_h_dc,
                "prob_d_dc": ext.prob_d_dc,
                "prob_a_dc": ext.prob_a_dc,
                "outcome_pred": ext.outcome_pred,
                "outcome_real": ext.outcome_real,
                "score_real": score_real,
                "acerto_1x2": ext.acerto_1x2,
                "rps": ext.rps,
                "brier": ext.brier,
                "log_loss": ext.log_loss,
                "mejor_value": ext.mejor_value,
                "mejor_edge": ext.mejor_edge,
                "hay_value": bool(ext.mejor_edge and ext.mejor_edge > 0.03),
                "stake_sugerido": ext.stake_sugerido,
                "bk_cuota_h": ext.bk_cuota_h,
                "bk_cuota_d": ext.bk_cuota_d,
                "bk_cuota_a": ext.bk_cuota_a,
            })

        return jsonify({"predicciones": resultado, "total": len(resultado)}), 200

    except Exception as e:
        return jsonify({"predicciones": [], "error": str(e)}), 500
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════
# POST /api/modelo/resultado
# ══════════════════════════════════════════════════════════════
@modelo_bp.route("/api/modelo/resultado", methods=["POST"])
def api_modelo_resultado():
    """
    Registra el resultado real de un partido y dispara la evaluación
    (RPS, Brier, Log Loss, diagnóstico de delta).

    Body esperado:
        { "prediccion_id": 42, "goles_h": 2, "goles_a": 1 }
    """
    data = request.get_json(force=True, silent=True) or {}

    pred_id = data.get("prediccion_id")
    goles_h = data.get("goles_h")
    goles_a = data.get("goles_a")

    if pred_id is None or goles_h is None or goles_a is None:
        return jsonify({"error": "Faltan campos: prediccion_id, goles_h, goles_a"}), 400

    try:
        pred_id = int(pred_id)
        goles_h = int(goles_h)
        goles_a = int(goles_a)
    except (ValueError, TypeError):
        return jsonify({"error": "Los campos deben ser numéricos"}), 400

    if goles_h < 0 or goles_a < 0:
        return jsonify({"error": "Los goles no pueden ser negativos"}), 400

    resultado = registrar_resultado_real(pred_id, goles_h, goles_a)

    if "error" in resultado:
        return jsonify(resultado), 404

    return jsonify(resultado), 200


# ══════════════════════════════════════════════════════════════
# GET /api/modelo/pendientes (opcional, usado internamente por el panel
# pero también útil como endpoint propio)
# ══════════════════════════════════════════════════════════════
@modelo_bp.route("/api/modelo/pendientes", methods=["GET"])
def api_modelo_pendientes():
    """Devuelve solo las predicciones sin resultado registrado aún."""
    if DBSession is None:
        return jsonify({"pendientes": []}), 200

    session = DBSession()
    try:
        rows = (
            session.query(PrediccionExtendida)
            .filter(PrediccionExtendida.outcome_real.is_(None))
            .order_by(PrediccionExtendida.cutoff_date.desc())
            .all()
        )
        pendientes = [{
            "prediccion_id": r.prediccion_id,
            "cutoff_date": r.cutoff_date.isoformat() if r.cutoff_date else None,
            "outcome_pred": r.outcome_pred,
            "prob_h_dc": r.prob_h_dc,
            "prob_d_dc": r.prob_d_dc,
            "prob_a_dc": r.prob_a_dc,
        } for r in rows]
        return jsonify({"pendientes": pendientes}), 200
    except Exception as e:
        return jsonify({"pendientes": [], "error": str(e)}), 500
    finally:
        session.close()