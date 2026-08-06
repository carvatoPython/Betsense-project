"""
community.py — BetSense
Blueprint de la sección "Comunidad" (MVP: buscar partido cerca).

Completamente separado del motor de predicción — no importa nada de
model_core.py ni de betAI.py, solo usa database.py. Se registra en
betAI.py igual que auth_bp / rangos_bp / wallet_bp:

    from community import community_bp
    app.register_blueprint(community_bp)

⚠️ INTEGRACIÓN CON AUTH: no tengo a la vista tu auth.py, así que estos
endpoints reciben `usuario_id` explícito en el body/query en vez de
sacarlo de la sesión. Reemplazá `_usuario_id_actual(request)` por lo
que uses hoy en auth.py (ej. un decorador @login_required que setea
request.usuario_id, o leer el JWT/cookie de sesión) — es el único
punto que hay que tocar para conectarlo de verdad.

Endpoints:
    POST /api/community/perfil                    → crear/actualizar perfil de jugador
    GET  /api/community/perfil/<usuario_id>        → ver un perfil
    POST /api/community/partidos                   → publicar un picado
    GET  /api/community/partidos?ciudad=X&nivel=Y  → buscar partidos cerca
    POST /api/community/partidos/<id>/inscribirse  → confirmar cupo
    POST /api/community/partidos/<id>/cerrar        → organizador marca asistencia
    POST /api/community/calificaciones              → calificar (Nivel 1)
    GET  /api/community/reputacion/<usuario_id>     → ver reputación agregada
"""

from datetime import datetime

from flask import Blueprint, jsonify, request

from database import (
    guardar_perfil_jugador, obtener_perfil_jugador, crear_partido_comunidad,
    buscar_partidos_cerca, inscribirse_partido, cerrar_partido,
    registrar_calificacion, obtener_reputacion_jugador,
)

community_bp = Blueprint("community", __name__, url_prefix="/api/community")


def _usuario_id_actual(req):
    """
    ⚠️ PLACEHOLDER — reemplazar por la extracción real del usuario logueado
    (sesión/JWT) que ya use auth.py. Por ahora acepta usuario_id en el body
    o query string para poder probar los endpoints de una.
    """
    data = req.get_json(silent=True) or {}
    uid = data.get("usuario_id") or req.args.get("usuario_id")
    return int(uid) if uid else None


@community_bp.route("/perfil", methods=["POST"])
def api_guardar_perfil():
    usuario_id = _usuario_id_actual(request)
    if not usuario_id:
        return jsonify({"ok": False, "error": "usuario_id requerido"}), 400

    data = request.get_json(silent=True) or {}
    try:
        perfil = guardar_perfil_jugador(
            usuario_id,
            posicion=data.get("posicion"),
            pierna_habil=data.get("pierna_habil"),
            nivel=data.get("nivel"),
            ciudad=data.get("ciudad"),
            lat=data.get("lat"),
            lng=data.get("lng"),
            radio_km=data.get("radio_km"),
            disponibilidad=data.get("disponibilidad"),
            busca=data.get("busca"),
        )
        return jsonify({"ok": True, "perfil": perfil})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@community_bp.route("/perfil/<int:usuario_id>", methods=["GET"])
def api_obtener_perfil(usuario_id):
    perfil = obtener_perfil_jugador(usuario_id)
    if not perfil:
        return jsonify({"ok": False, "error": "Perfil no encontrado"}), 404
    return jsonify({"ok": True, "perfil": perfil})


@community_bp.route("/partidos", methods=["POST"])
def api_crear_partido():
    organizador_id = _usuario_id_actual(request)
    if not organizador_id:
        return jsonify({"ok": False, "error": "usuario_id requerido"}), 400

    data = request.get_json(silent=True) or {}
    campos_requeridos = ("titulo", "ciudad", "fecha_hora", "cupos_totales")
    faltantes = [c for c in campos_requeridos if not data.get(c)]
    if faltantes:
        return jsonify({"ok": False, "error": f"Faltan campos: {', '.join(faltantes)}"}), 400

    try:
        fecha_hora = datetime.fromisoformat(data["fecha_hora"])
    except ValueError:
        return jsonify({"ok": False, "error": "fecha_hora inválida, usar formato ISO 8601"}), 400

    try:
        partido_id = crear_partido_comunidad(
            organizador_id=organizador_id,
            titulo=data["titulo"],
            ciudad=data["ciudad"],
            fecha_hora=fecha_hora,
            cupos_totales=int(data["cupos_totales"]),
            ubicacion_texto=data.get("ubicacion_texto"),
            lat=data.get("lat"),
            lng=data.get("lng"),
            nivel_requerido=data.get("nivel_requerido", "cualquiera"),
            costo=float(data.get("costo", 0.0)),
        )
        return jsonify({"ok": True, "partido_id": partido_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@community_bp.route("/partidos", methods=["GET"])
def api_buscar_partidos():
    ciudad = request.args.get("ciudad")
    if not ciudad:
        return jsonify({"ok": False, "error": "ciudad requerida"}), 400
    nivel = request.args.get("nivel")
    partidos = buscar_partidos_cerca(ciudad=ciudad, nivel=nivel)
    return jsonify({"ok": True, "partidos": partidos})


@community_bp.route("/partidos/<int:partido_id>/inscribirse", methods=["POST"])
def api_inscribirse(partido_id):
    jugador_id = _usuario_id_actual(request)
    if not jugador_id:
        return jsonify({"ok": False, "error": "usuario_id requerido"}), 400
    resultado = inscribirse_partido(partido_id, jugador_id)
    return jsonify(resultado), (200 if resultado.get("ok") else 400)


@community_bp.route("/partidos/<int:partido_id>/cerrar", methods=["POST"])
def api_cerrar_partido(partido_id):
    """El organizador pasa lista: quiénes de los inscritos sí llegaron a jugar."""
    data = request.get_json(silent=True) or {}
    ids_asistieron = data.get("ids_asistieron", [])
    if not isinstance(ids_asistieron, list):
        return jsonify({"ok": False, "error": "ids_asistieron debe ser una lista"}), 400
    resultado = cerrar_partido(partido_id, ids_asistieron)
    return jsonify(resultado), (200 if resultado.get("ok") else 400)


@community_bp.route("/calificaciones", methods=["POST"])
def api_calificar():
    calificador_id = _usuario_id_actual(request)
    if not calificador_id:
        return jsonify({"ok": False, "error": "usuario_id requerido"}), 400

    data = request.get_json(silent=True) or {}
    campos_requeridos = ("partido_id", "calificado_id", "trabajo_equipo",
                          "respeto", "puntualidad", "nivel", "volveria_jugar")
    faltantes = [c for c in campos_requeridos if data.get(c) is None]
    if faltantes:
        return jsonify({"ok": False, "error": f"Faltan campos: {', '.join(faltantes)}"}), 400

    for campo in ("trabajo_equipo", "respeto", "puntualidad", "nivel"):
        if not (1 <= int(data[campo]) <= 5):
            return jsonify({"ok": False, "error": f"{campo} debe estar entre 1 y 5"}), 400

    resultado = registrar_calificacion(
        partido_id=int(data["partido_id"]),
        calificador_id=calificador_id,
        calificado_id=int(data["calificado_id"]),
        trabajo_equipo=int(data["trabajo_equipo"]),
        respeto=int(data["respeto"]),
        puntualidad=int(data["puntualidad"]),
        nivel=int(data["nivel"]),
        volveria_jugar=bool(data["volveria_jugar"]),
    )
    return jsonify(resultado), (200 if resultado.get("ok") else 400)


@community_bp.route("/reputacion/<int:usuario_id>", methods=["GET"])
def api_reputacion(usuario_id):
    return jsonify({"ok": True, "reputacion": obtener_reputacion_jugador(usuario_id)})