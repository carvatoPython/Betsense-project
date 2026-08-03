"""
rangos.py — BetSense
Sistema de rangos automáticos + leaderboard mensual con recompensas JP.

CÓMO INTEGRAR:
1. Importar en betAI.py:
       from rangos import rangos_bp, calcular_rango, distribuir_premios_mensuales
2. Registrar el blueprint:
       app.register_blueprint(rangos_bp)
3. El resto es automático.
"""

from flask import Blueprint, jsonify, request, g
from sqlalchemy import func, extract
from datetime import datetime, date
from calendar import monthrange
import os

from database import Base, Session, Usuario, PerfilStats, Pick
from auth import requiere_auth, JWT_SECRET
import jwt

rangos_bp = Blueprint("rangos", __name__, url_prefix="/api/auth")


# ══════════════════════════════════════════════════════════════
# CONFIGURACIÓN DE RANGOS
# ══════════════════════════════════════════════════════════════

RANGOS = [
    # (nombre_interno, label_frontend, min_picks, min_roi, icono)
    ("leyenda",  "Leyenda",  200, 15.0, "🔴"),
    ("elite",    "Elite",    100, 10.0, "🟡"),
    ("experto",  "Experto",   50,  5.0, "🟣"),
    ("tipster",  "Tipster",   10,  0.0, "🔵"),
    ("rookie",   "Rookie",     0,  0.0, "⚪"),
]

# Premios JP por posición en el leaderboard mensual (top 30)
PREMIOS_MENSUALES = {
    1:  5000,
    2:  2500,
    3:  1500,
    4:  1000,
    5:   800,
    6:   700,
    7:   600,
    8:   500,
    9:   400,
    10:  350,
    # Posiciones 11–20: 200 JP cada una
    **{i: 200 for i in range(11, 21)},
    # Posiciones 21–30: 100 JP cada una
    **{i: 100 for i in range(21, 31)},
}


# ══════════════════════════════════════════════════════════════
# LÓGICA DE RANGOS
# ══════════════════════════════════════════════════════════════

def calcular_rango(total_picks: int, roi: float) -> tuple[str, str]:
    """
    Devuelve (nombre_interno, label_frontend) del rango correspondiente.
    Los rangos NUNCA bajan — la función calcula el máximo alcanzado.
    """
    for nombre, label, min_picks, min_roi, _ in RANGOS:
        if total_picks >= min_picks and roi >= min_roi:
            return nombre, label
    return "rookie", "Rookie"


def actualizar_rango_usuario(session, usuario_id: int) -> str:
    """
    Recalcula y persiste el rango de un usuario.
    Devuelve el nuevo nombre de rango.
    """
    stats = session.query(PerfilStats).filter_by(usuario_id=usuario_id).first()
    if not stats:
        return "rookie"

    nombre, _ = calcular_rango(stats.total_picks, stats.roi_porcentaje)

    # Nunca bajar de rango: solo actualizar si el nuevo rango es mejor
    orden = {r[0]: i for i, r in enumerate(RANGOS)}
    rango_actual = stats.nivel or "rookie"
    if orden.get(nombre, 99) < orden.get(rango_actual, 99):
        stats.nivel = nombre
        session.flush()

    return stats.nivel


def cerrar_pick(pick_id: int, gano: bool) -> dict:
    """
    Cierra un pick (ganado/perdido) y actualiza las estadísticas del tipster:
    - picks_ganados / picks_perdidos
    - ROI aproximado
    - racha_actual y mejor_racha
    - nivel/rango
    - JP (puntos) como recompensa
    """
    session = Session()
    try:
        pick = session.get(Pick, pick_id)
        if not pick:
            return {"error": "Pick no encontrado"}
        if pick.estado != "abierto":
            return {"error": "Pick ya está cerrado"}

        # Cerrar el pick
        pick.estado  = "ganado" if gano else "perdido"
        pick.cerrado = datetime.utcnow()
        session.flush()

        # Actualizar stats
        stats = session.query(PerfilStats).filter_by(usuario_id=pick.usuario_id).first()
        if not stats:
            session.commit()
            return {"ok": True}

        cuota  = pick.cuota or 1.0
        stake  = pick.stake or 1.0

        if gano:
            stats.picks_ganados += 1
            stats.racha_actual  += 1
            if stats.racha_actual > stats.mejor_racha:
                stats.mejor_racha = stats.racha_actual
            # JP ganados: stake × cuota redondeado
            jp_reward = max(10, round(stake * cuota * 10))
            stats.puntos += jp_reward
        else:
            stats.picks_perdidos += 1
            stats.racha_actual    = 0
            jp_reward             = 0

        # Recalcular ROI simplificado
        # ROI = (ganado - invertido) / invertido × 100
        total_apostado = (stats.picks_ganados + stats.picks_perdidos) * stake
        total_ganado   = stats.picks_ganados * cuota * stake
        if total_apostado > 0:
            stats.roi_porcentaje = round((total_ganado - total_apostado) / total_apostado * 100, 2)

        stats.actualizado = datetime.utcnow()

        # Recalcular rango
        nuevo_rango = actualizar_rango_usuario(session, pick.usuario_id)

        session.commit()

        return {
            "ok":          True,
            "pick_id":     pick_id,
            "estado":      pick.estado,
            "jp_reward":   jp_reward,
            "nuevo_rango": nuevo_rango,
            "racha":       stats.racha_actual,
            "roi":         stats.roi_porcentaje,
        }

    except Exception as e:
        session.rollback()
        return {"error": str(e)[:120]}
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════
# LEADERBOARD MENSUAL
# ══════════════════════════════════════════════════════════════

def _picks_mes_query(session, year: int, month: int):
    """Query de picks cerrados en un mes/año específico."""
    return (
        session.query(Pick)
        .filter(
            Pick.estado.in_(["ganado", "perdido"]),
            extract("year",  Pick.cerrado) == year,
            extract("month", Pick.cerrado) == month,
        )
    )


def calcular_leaderboard_mensual(year: int = None, month: int = None, limit: int = 30) -> list:
    """
    Calcula el ranking mensual de tipsters por aciertos.
    Devuelve lista ordenada con premios calculados.
    """
    hoy = date.today()
    year  = year  or hoy.year
    month = month or hoy.month

    session = Session()
    try:
        picks_mes = _picks_mes_query(session, year, month).all()

        if not picks_mes:
            return []

        # Agrupar por usuario
        stats_por_user: dict[int, dict] = {}
        for p in picks_mes:
            uid = p.usuario_id
            if uid not in stats_por_user:
                stats_por_user[uid] = {"ganados": 0, "total": 0}
            stats_por_user[uid]["total"]  += 1
            if p.estado == "ganado":
                stats_por_user[uid]["ganados"] += 1

        # Ordenar: primero por ganados, desempate por total
        ranking_ids = sorted(
            stats_por_user.keys(),
            key=lambda uid: (-stats_por_user[uid]["ganados"], stats_por_user[uid]["total"])
        )[:limit]

        resultado = []
        for pos, uid in enumerate(ranking_ids, start=1):
            usuario = session.get(Usuario, uid)
            if not usuario or not usuario.activo:
                continue

            perfil = session.query(PerfilStats).filter_by(usuario_id=uid).first()
            datos  = stats_por_user[uid]
            pct    = round(datos["ganados"] / datos["total"] * 100) if datos["total"] > 0 else 0
            premio = PREMIOS_MENSUALES.get(pos, 0)

            resultado.append({
                "posicion":      pos,
                "usuario_id":    uid,
                "username":      usuario.username,
                "nombre":        usuario.nombre,
                "nivel":         (perfil.nivel if perfil else "rookie"),
                "picks_mes":     datos["total"],
                "picks_ganados": datos["ganados"],
                "pct_acierto":   pct,
                "roi":           round(perfil.roi_porcentaje, 1) if perfil else 0,
                "jp_premio":     premio,
            })

        return resultado

    finally:
        session.close()


def distribuir_premios_mensuales(year: int = None, month: int = None) -> dict:
    """
    Distribuye JP a los top 30 del mes.
    Llama esta función UNA vez al final del mes (cron job o manual).
    Devuelve resumen de la distribución.
    """
    hoy   = date.today()
    year  = year  or hoy.year
    month = month or hoy.month

    ranking = calcular_leaderboard_mensual(year, month, limit=30)
    if not ranking:
        return {"ok": False, "msg": "Sin picks este mes", "distribuidos": 0}

    session = Session()
    total_jp = 0
    distribuidos = 0

    try:
        for entry in ranking:
            if entry["jp_premio"] <= 0:
                continue
            stats = session.query(PerfilStats).filter_by(usuario_id=entry["usuario_id"]).first()
            if stats:
                stats.puntos += entry["jp_premio"]
                total_jp     += entry["jp_premio"]
                distribuidos += 1
                print(f"  🏆 #{entry['posicion']} @{entry['username']} → +{entry['jp_premio']} JP")

        session.commit()
        return {
            "ok":          True,
            "mes":         f"{month}/{year}",
            "distribuidos": distribuidos,
            "total_jp":    total_jp,
            "top1":        ranking[0]["username"] if ranking else None,
        }

    except Exception as e:
        session.rollback()
        return {"ok": False, "msg": str(e)[:120], "distribuidos": 0}
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════

@rangos_bp.route("/leaderboard/mensual", methods=["GET"])
def leaderboard_mensual():
    """
    GET /api/auth/leaderboard/mensual?limit=30&year=2025&month=6
    Devuelve el ranking del mes actual (o el especificado).
    """
    limit = min(int(request.args.get("limit", 30)), 30)
    year  = request.args.get("year",  type=int)
    month = request.args.get("month", type=int)

    ranking = calcular_leaderboard_mensual(year, month, limit)

    hoy = date.today()
    y   = year  or hoy.year
    m   = month or hoy.month
    dias_en_mes  = monthrange(y, m)[1]
    fin_mes      = date(y, m, dias_en_mes)
    dias_restantes = (fin_mes - hoy).days if fin_mes >= hoy else 0

    return jsonify({
        "ranking":         ranking,
        "mes":             m,
        "year":            y,
        "dias_restantes":  dias_restantes,
        "total_participantes": len(ranking),
    })


@rangos_bp.route("/pick/<int:pick_id>/cerrar", methods=["POST"])
@requiere_auth
def cerrar_pick_endpoint(pick_id):
    """
    POST /api/auth/pick/:id/cerrar
    Body: { "gano": true | false }
    Solo el autor del pick puede cerrarlo.
    """
    gano = (request.get_json() or {}).get("gano")
    if gano is None:
        return jsonify({"error": "Campo 'gano' requerido (true/false)"}), 400

    # Verificar que el pick pertenece al usuario
    session = Session()
    try:
        pick = session.get(Pick, pick_id)
        if not pick:
            return jsonify({"error": "Pick no encontrado"}), 404
        if pick.usuario_id != g.usuario_id:
            return jsonify({"error": "No autorizado"}), 403
    finally:
        session.close()

    resultado = cerrar_pick(pick_id, bool(gano))
    if "error" in resultado:
        return jsonify(resultado), 400
    return jsonify(resultado)


@rangos_bp.route("/mi-rango", methods=["GET"])
@requiere_auth
def mi_rango():
    """GET /api/auth/mi-rango — Devuelve el rango y stats del usuario autenticado."""
    session = Session()
    try:
        stats = session.query(PerfilStats).filter_by(usuario_id=g.usuario_id).first()
        if not stats:
            return jsonify({"rango": "rookie", "label": "Rookie", "icono": "⚪"})

        nombre, label = calcular_rango(stats.total_picks, stats.roi_porcentaje)

        # Próximo rango
        orden = [r[0] for r in RANGOS]
        idx   = orden.index(nombre)
        if idx > 0:
            prox_nombre, prox_label, prox_picks, prox_roi, _ = RANGOS[idx - 1]
            faltan_picks = max(0, prox_picks - stats.total_picks)
            faltan_roi   = max(0.0, prox_roi - stats.roi_porcentaje)
        else:
            prox_label   = None
            faltan_picks = 0
            faltan_roi   = 0.0

        icono = next(r[4] for r in RANGOS if r[0] == nombre)

        return jsonify({
            "rango":         nombre,
            "label":         label,
            "icono":         icono,
            "total_picks":   stats.total_picks,
            "picks_ganados": stats.picks_ganados,
            "roi":           round(stats.roi_porcentaje, 2),
            "racha":         stats.racha_actual,
            "mejor_racha":   stats.mejor_racha,
            "puntos_jp":     stats.puntos,
            "proximo_rango": prox_label,
            "faltan_picks":  faltan_picks,
            "faltan_roi":    round(faltan_roi, 1),
        })
    finally:
        session.close()


@rangos_bp.route("/leaderboard/distribuir", methods=["POST"])
def distribuir_premios():
    """
    POST /api/auth/leaderboard/distribuir
    Header: X-Admin-Key: <clave_admin>
    Distribuye los JP del mes. Llamar manualmente o con cron al fin de cada mes.
    """
    admin_key = request.headers.get("X-Admin-Key", "")
    if admin_key != os.environ.get("ADMIN_KEY", "betsense_admin_2024"):
        return jsonify({"error": "No autorizado"}), 403

    year  = request.get_json().get("year")  if request.get_json() else None
    month = request.get_json().get("month") if request.get_json() else None

    resultado = distribuir_premios_mensuales(year, month)
    return jsonify(resultado)


@rangos_bp.route("/insignias/<int:usuario_id>", methods=["GET"])
def insignias_usuario(usuario_id: int):
    """
    GET /api/auth/insignias/:id — Calcula las insignias desbloqueadas de un tipster.
    Se calculan on-the-fly basadas en sus stats.
    """
    session = Session()
    try:
        stats = session.query(PerfilStats).filter_by(usuario_id=usuario_id).first()
        if not stats:
            return jsonify({"insignias": []})

        picks = session.query(Pick).filter_by(usuario_id=usuario_id).all()
        ganados_hist = [p for p in picks if p.estado == "ganado"]

        insignias = []

        # 🔥 En llamas — 5 picks ganados consecutivos (racha actual o mejor)
        if stats.mejor_racha >= 5:
            insignias.append({
                "id": "en_llamas", "emoji": "🔥", "nombre": "En Llamas",
                "desc": f"Racha de {stats.mejor_racha} picks ganados seguidos"
            })

        # ⚡ Cazador de valor — pick ganado con cuota > 3.0
        cazador = any(p.cuota and p.cuota >= 3.0 for p in ganados_hist)
        if cazador:
            insignias.append({
                "id": "cazador_valor", "emoji": "⚡", "nombre": "Cazador de Valor",
                "desc": "Pick ganado con cuota mayor a 3.0"
            })

        # 🎯 Francotirador — 10+ picks con más del 80% de acierto (mínimo 10 picks)
        if stats.total_picks >= 10:
            pct = (stats.picks_ganados / stats.total_picks) * 100
            if pct >= 80:
                insignias.append({
                    "id": "francotirador", "emoji": "🎯", "nombre": "Francotirador",
                    "desc": f"{round(pct)}% de acierto con {stats.total_picks}+ picks"
                })

        # 💎 Consistente — 50 picks publicados
        if stats.total_picks >= 50:
            insignias.append({
                "id": "consistente", "emoji": "💎", "nombre": "Consistente",
                "desc": "50+ picks publicados"
            })

        # 🚀 ROI Master — ROI positivo sostenido > 10%
        if stats.roi_porcentaje >= 10 and stats.total_picks >= 20:
            insignias.append({
                "id": "roi_master", "emoji": "🚀", "nombre": "ROI Master",
                "desc": f"ROI del {round(stats.roi_porcentaje, 1)}% con 20+ picks"
            })

        # 👑 Leyenda — máximo rango
        if stats.nivel == "leyenda":
            insignias.append({
                "id": "leyenda", "emoji": "👑", "nombre": "Leyenda",
                "desc": "Máximo rango desbloqueado"
            })

        return jsonify({
            "usuario_id": usuario_id,
            "insignias":  insignias,
            "total":      len(insignias),
        })

    finally:
        session.close()