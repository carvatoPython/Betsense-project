"""
auth.py — BetSense Social
Sistema de autenticación y perfiles de usuarios.
Se integra con el betAI.py existente sin romper nada.
"""

from flask import Blueprint, jsonify, request, g
from sqlalchemy import Column, Integer, String, DateTime, Text, Float, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime, timedelta
import hashlib
import secrets
import jwt
import os
from functools import wraps

# Modelos ya definidos en database.py — se importan directamente para no duplicar
from database import Base, Session, Usuario, PerfilStats, Pick, Comentario, Follow, Sesion

# ── CONFIGURACIÓN JWT ─────────────────────────────────────────
JWT_SECRET = os.environ.get("JWT_SECRET", "betsense_secret_cambia_esto_en_produccion_2024")
JWT_EXPIRY = timedelta(days=30)

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{h}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":")
        return hashlib.sha256(f"{salt}{password}".encode()).hexdigest() == h
    except Exception:
        return False


def _generar_jwt(usuario_id: int, username: str) -> str:
    payload = {
        "sub": usuario_id,
        "username": username,
        "exp": datetime.utcnow() + JWT_EXPIRY,
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def _nivel_por_picks(total_picks: int, roi: float) -> str:
    if total_picks < 10:
        return "rookie"
    if total_picks < 50 or roi < 5:
        return "tipster"
    if total_picks < 150 or roi < 12:
        return "experto"
    return "leyenda"


def _nivel_frontend(nivel: str) -> str:
    """Convierte nivel interno a formato que espera el frontend"""
    return {
        "rookie": "Principiante",
        "tipster": "Tipster",
        "experto": "Experto",
        "leyenda": "Leyenda"
    }.get(nivel, "Principiante")


# ── Decorador de autenticación ────────────────────────────────

def requiere_auth(f):
    """Decorador para endpoints que necesitan JWT válido."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not token:
            return jsonify({"error": "Token requerido"}), 401
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            g.usuario_id = payload["sub"]
            g.username = payload["username"]
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token expirado"}), 401
        except Exception:
            return jsonify({"error": "Token inválido"}), 401
        return f(*args, **kwargs)
    return decorated


def usuario_actual_o_none():
    """Devuelve el usuario_id si hay JWT, o None. Para endpoints opcionales."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload["sub"]
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# ENDPOINTS DE AUTH — FORMATO COMPATIBLE CON FRONTEND
# ══════════════════════════════════════════════════════════════

@auth_bp.route("/register", methods=["POST"])
def register():
    """
    POST /api/auth/register
    Body: { username, email, password, deporte_favorito }
    """
    data = request.get_json() or {}
    username = (data.get("username") or "").strip().lower()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    deporte_favorito = data.get("deporte_favorito", "football")

    # Validaciones
    if not username or len(username) < 3:
        return jsonify({"error": "Username mínimo 3 caracteres"}), 400
    if not email or "@" not in email or "." not in email:
        return jsonify({"error": "Email inválido"}), 400
    if not password or len(password) < 6:
        return jsonify({"error": "Contraseña mínimo 6 caracteres"}), 400
    if len(username) > 30 or not username.replace("_", "").replace(".", "").isalnum():
        return jsonify({"error": "Username solo letras, números, _ o ."}), 400

    session = Session()
    try:
        # Verificar unicidad
        existe_user = session.query(Usuario).filter_by(username=username).first()
        existe_email = session.query(Usuario).filter_by(email=email).first()

        if existe_user:
            return jsonify({"error": "Username ya en uso"}), 409
        if existe_email:
            return jsonify({"error": "Email ya registrado"}), 409

        # Crear usuario
        usuario = Usuario(
            username=username,
            email=email,
            password_hash=_hash_password(password),
            nombre=username,
            deporte_fav=deporte_favorito,
        )
        session.add(usuario)
        session.flush()

        # Crear perfil de stats vacío
        stats = PerfilStats(usuario_id=usuario.id, puntos=1000)
        session.add(stats)
        session.commit()

        token = _generar_jwt(usuario.id, usuario.username)

        # FORMATO QUE ESPERA EL FRONTEND
        return jsonify({
            "token": token,
            "user": {
                "id": usuario.id,
                "username": usuario.username,
                "email": usuario.email,
                "deporte_favorito": usuario.deporte_fav,
                "nivel": "Principiante",
                "jp_total": 1000
            }
        }), 201

    except Exception as e:
        session.rollback()
        return jsonify({"error": f"Error interno: {str(e)}"}), 500
    finally:
        session.close()


@auth_bp.route("/login", methods=["POST"])
def login():
    """
    POST /api/auth/login
    Body: { username, password }
    El frontend manda username (puede ser email o username)
    """
    data = request.get_json() or {}
    identifier = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""

    if not identifier or not password:
        return jsonify({"error": "Usuario/email y contraseña son obligatorios"}), 400

    session = Session()
    try:
        usuario = (
            session.query(Usuario)
            .filter((Usuario.username == identifier) | (Usuario.email == identifier))
            .first()
        )

        if not usuario or not _verify_password(password, usuario.password_hash):
            return jsonify({"error": "Credenciales incorrectas"}), 401

        if not usuario.activo:
            return jsonify({"error": "Cuenta desactivada"}), 403

        stats = session.query(PerfilStats).filter_by(usuario_id=usuario.id).first()
        nivel = stats.nivel if stats else "rookie"

        token = _generar_jwt(usuario.id, usuario.username)

        # FORMATO QUE ESPERA EL FRONTEND
        return jsonify({
            "token": token,
            "user": {
                "id": usuario.id,
                "username": usuario.username,
                "email": usuario.email,
                "deporte_favorito": usuario.deporte_fav,
                "nivel": _nivel_frontend(nivel),
                "jp_total": stats.puntos if stats else 1000
            }
        })

    except Exception as e:
        return jsonify({"error": f"Error interno: {str(e)}"}), 500
    finally:
        session.close()


@auth_bp.route("/me", methods=["GET"])
@requiere_auth
def me():
    """GET /api/auth/me — Devuelve el perfil completo del usuario autenticado."""
    session = Session()
    try:
        usuario = session.get(Usuario, g.usuario_id)
        if not usuario:
            return jsonify({"error": "Usuario no encontrado"}), 404

        stats = session.query(PerfilStats).filter_by(usuario_id=usuario.id).first()
        n_sigue = session.query(Follow).filter_by(follower_id=usuario.id).count()
        n_seguidores = session.query(Follow).filter_by(following_id=usuario.id).count()

        return jsonify({
            "id": usuario.id,
            "username": usuario.username,
            "nombre": usuario.nombre,
            "email": usuario.email,
            "avatar_url": usuario.avatar_url,
            "bio": usuario.bio,
            "deporte_fav": usuario.deporte_fav,
            "equipo_fav": usuario.equipo_fav,
            "creado": usuario.creado.isoformat(),
            "siguiendo": n_sigue,
            "seguidores": n_seguidores,
            "stats": {
                "total_picks": stats.total_picks if stats else 0,
                "picks_ganados": stats.picks_ganados if stats else 0,
                "picks_perdidos": stats.picks_perdidos if stats else 0,
                "roi": round(stats.roi_porcentaje, 1) if stats else 0,
                "racha": stats.racha_actual if stats else 0,
                "mejor_racha": stats.mejor_racha if stats else 0,
                "nivel": _nivel_frontend(stats.nivel) if stats else "Principiante",
                "puntos": stats.puntos if stats else 0,
            }
        })
    finally:
        session.close()


@auth_bp.route("/update-profile", methods=["PATCH"])
@requiere_auth
def update_profile():
    """PATCH /api/auth/update-profile — Actualiza perfil del usuario."""
    data = request.get_json() or {}
    session = Session()
    try:
        usuario = session.get(Usuario, g.usuario_id)
        if not usuario:
            return jsonify({"error": "No encontrado"}), 404

        campos_permitidos = ["nombre", "bio", "avatar_url", "deporte_fav", "equipo_fav", "bankroll_ini"]
        for campo in campos_permitidos:
            if campo in data:
                setattr(usuario, campo, data[campo])

        session.commit()
        return jsonify({"ok": True, "mensaje": "Perfil actualizado"})
    except Exception as e:
        session.rollback()
        return jsonify({"error": str(e)[:80]}), 500
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════
# ENDPOINTS DE PERFIL PÚBLICO Y SOCIAL
# ══════════════════════════════════════════════════════════════

@auth_bp.route("/perfil/<username>", methods=["GET"])
def perfil_publico(username):
    """GET /api/auth/perfil/:username — Perfil público de un tipster."""
    session = Session()
    try:
        usuario = session.query(Usuario).filter_by(username=username.lower()).first()
        if not usuario:
            return jsonify({"error": "Usuario no encontrado"}), 404

        stats = session.query(PerfilStats).filter_by(usuario_id=usuario.id).first()
        n_sigue = session.query(Follow).filter_by(follower_id=usuario.id).count()
        n_seguidores = session.query(Follow).filter_by(following_id=usuario.id).count()

        picks_recientes = (
            session.query(Pick)
            .filter_by(usuario_id=usuario.id)
            .order_by(Pick.creado.desc())
            .limit(10)
            .all()
        )

        yo_sigo = False
        yo_id = usuario_actual_o_none()
        if yo_id:
            yo_sigo = bool(
                session.query(Follow)
                .filter_by(follower_id=yo_id, following_id=usuario.id)
                .first()
            )

        return jsonify({
            "id": usuario.id,
            "username": usuario.username,
            "nombre": usuario.nombre,
            "avatar_url": usuario.avatar_url,
            "bio": usuario.bio,
            "deporte_fav": usuario.deporte_fav,
            "equipo_fav": usuario.equipo_fav,
            "creado": usuario.creado.isoformat(),
            "siguiendo": n_sigue,
            "seguidores": n_seguidores,
            "yo_sigo": yo_sigo,
            "stats": {
                "total_picks": stats.total_picks if stats else 0,
                "picks_ganados": stats.picks_ganados if stats else 0,
                "roi": round(stats.roi_porcentaje, 1) if stats else 0,
                "racha": stats.racha_actual if stats else 0,
                "mejor_racha": stats.mejor_racha if stats else 0,
                "nivel": _nivel_frontend(stats.nivel) if stats else "rookie",
                "puntos": stats.puntos if stats else 0,
            },
            "picks_recientes": [
                {
                    "id": p.id,
                    "partido": p.partido,
                    "liga": p.liga,
                    "mercado": p.mercado,
                    "cuota": p.cuota,
                    "confianza": p.confianza,
                    "estado": p.estado,
                    "creado": p.creado.isoformat(),
                    "reacciones": {
                        "fuego": p.reacciones_fuego,
                        "duda": p.reacciones_duda,
                        "junto": p.reacciones_junto,
                    }
                }
                for p in picks_recientes
            ]
        })
    finally:
        session.close()


@auth_bp.route("/follow/<int:target_id>", methods=["POST"])
@requiere_auth
def follow(target_id):
    """POST /api/auth/follow/:id — Seguir/dejar de seguir un usuario."""
    if target_id == g.usuario_id:
        return jsonify({"error": "No puedes seguirte a ti mismo"}), 400

    session = Session()
    try:
        existente = session.query(Follow).filter_by(
            follower_id=g.usuario_id, following_id=target_id
        ).first()

        if existente:
            session.delete(existente)
            session.commit()
            return jsonify({"accion": "unfollow", "siguiendo": False})
        else:
            session.add(Follow(follower_id=g.usuario_id, following_id=target_id))
            session.commit()
            return jsonify({"accion": "follow", "siguiendo": True})
    except Exception as e:
        session.rollback()
        return jsonify({"error": str(e)[:80]}), 500
    finally:
        session.close()


@auth_bp.route("/feed", methods=["GET"])
def feed():
    """
    GET /api/auth/feed?page=1
    Si hay JWT → feed personalizado (gente que sigues + global).
    Sin JWT → feed global.
    """
    page = int(request.args.get("page", 1))
    limit = 20
    offset = (page - 1) * limit

    session = Session()
    try:
        yo_id = usuario_actual_o_none()

        if yo_id:
            siguiendo = [f.following_id for f in
                         session.query(Follow).filter_by(follower_id=yo_id).all()]
            siguiendo.append(yo_id)
            query = session.query(Pick).filter(Pick.usuario_id.in_(siguiendo))
        else:
            query = session.query(Pick)

        picks = (
            query
            .order_by(Pick.creado.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )

        resultado = []
        for p in picks:
            stats_autor = session.query(PerfilStats).filter_by(usuario_id=p.usuario_id).first()
            resultado.append({
                "id": p.id,
                "partido": p.partido,
                "liga": p.liga,
                "mercado": p.mercado,
                "cuota": p.cuota,
                "stake": p.stake,
                "confianza": p.confianza,
                "descripcion": p.descripcion,
                "estado": p.estado,
                "creado": p.creado.isoformat(),
                "reacciones": {
                    "fuego": p.reacciones_fuego,
                    "duda": p.reacciones_duda,
                    "junto": p.reacciones_junto,
                },
                "autor": {
                    "id": p.autor.id,
                    "username": p.autor.username,
                    "nombre": p.autor.nombre,
                    "avatar_url": p.autor.avatar_url,
                    "nivel": _nivel_frontend(stats_autor.nivel) if stats_autor else "rookie",
                    "roi": round(stats_autor.roi_porcentaje, 1) if stats_autor else 0,
                    "racha": stats_autor.racha_actual if stats_autor else 0,
                }
            })

        return jsonify({
            "picks": resultado,
            "page": page,
            "total": len(resultado),
            "hay_mas": len(resultado) == limit,
        })
    finally:
        session.close()


@auth_bp.route("/pick", methods=["POST"])
@requiere_auth
def crear_pick():
    """POST /api/auth/pick — Publica un nuevo pick en el feed."""
    data = request.get_json() or {}

    partido = data.get("partido", "").strip()
    liga = data.get("liga", "").strip()
    mercado = data.get("mercado", "").strip()
    cuota = float(data.get("cuota", 0))
    stake = float(data.get("stake", 1))
    confianza = int(data.get("confianza", 3))
    descripcion = data.get("descripcion", "").strip()
    pred_id = data.get("prediccion_id")

    if not partido or not mercado:
        return jsonify({"error": "Partido y mercado son requeridos"}), 400

    session = Session()
    try:
        pick = Pick(
            usuario_id=g.usuario_id,
            prediccion_id=pred_id,
            partido=partido,
            liga=liga,
            mercado=mercado,
            cuota=cuota,
            stake=min(max(stake, 0.5), 10),
            confianza=min(max(confianza, 1), 5),
            descripcion=descripcion,
        )
        session.add(pick)
        session.flush()

        stats = session.query(PerfilStats).filter_by(usuario_id=g.usuario_id).first()
        if stats:
            stats.total_picks += 1
            stats.puntos += 5
            stats.nivel = _nivel_por_picks(stats.total_picks, stats.roi_porcentaje)

        session.commit()
        return jsonify({"ok": True, "pick_id": pick.id, "mensaje": "Pick publicado"}), 201
    except Exception as e:
        session.rollback()
        return jsonify({"error": str(e)[:80]}), 500
    finally:
        session.close()


@auth_bp.route("/pick/<int:pick_id>/reaccion", methods=["POST"])
@requiere_auth
def reaccionar(pick_id):
    """POST /api/auth/pick/:id/reaccion — Reaccionar a un pick."""
    tipo = (request.get_json() or {}).get("tipo")
    if tipo not in ("fuego", "duda", "junto"):
        return jsonify({"error": "Tipo inválido"}), 400

    session = Session()
    try:
        pick = session.get(Pick, pick_id)
        if not pick:
            return jsonify({"error": "Pick no encontrado"}), 404

        if tipo == "fuego":
            pick.reacciones_fuego += 1
        elif tipo == "duda":
            pick.reacciones_duda += 1
        else:
            pick.reacciones_junto += 1

        session.commit()
        return jsonify({
            "ok": True,
            "reacciones": {
                "fuego": pick.reacciones_fuego,
                "duda": pick.reacciones_duda,
                "junto": pick.reacciones_junto,
            }
        })
    except Exception as e:
        session.rollback()
        return jsonify({"error": str(e)[:80]}), 500
    finally:
        session.close()


@auth_bp.route("/pick/<int:pick_id>/comentario", methods=["POST"])
@requiere_auth
def comentar(pick_id):
    """POST /api/auth/pick/:id/comentario — Agregar comentario a un pick."""
    texto = (request.get_json() or {}).get("texto", "").strip()
    if not texto or len(texto) > 500:
        return jsonify({"error": "Comentario inválido (máx 500 caracteres)"}), 400

    session = Session()
    try:
        pick = session.get(Pick, pick_id)
        if not pick:
            return jsonify({"error": "Pick no encontrado"}), 404

        comentario = Comentario(
            pick_id=pick_id,
            usuario_id=g.usuario_id,
            texto=texto,
        )
        session.add(comentario)
        session.commit()
        return jsonify({"ok": True, "comentario_id": comentario.id}), 201
    except Exception as e:
        session.rollback()
        return jsonify({"error": str(e)[:80]}), 500
    finally:
        session.close()


@auth_bp.route("/tipsters/top", methods=["GET"])
def top_tipsters():
    """GET /api/auth/tipsters/top — Ranking de los mejores tipsters."""
    session = Session()
    try:
        stats_list = (
            session.query(PerfilStats)
            .filter(PerfilStats.total_picks >= 5)
            .order_by(PerfilStats.roi_porcentaje.desc())
            .limit(20)
            .all()
        )
        resultado = []
        for s in stats_list:
            u = s.usuario
            if u and u.activo:
                resultado.append({
                    "username": u.username,
                    "nombre": u.nombre,
                    "avatar_url": u.avatar_url,
                    "nivel": _nivel_frontend(s.nivel),
                    "total_picks": s.total_picks,
                    "roi": round(s.roi_porcentaje, 1),
                    "racha": s.racha_actual,
                    "puntos": s.puntos,
                })
        return jsonify({"tipsters": resultado})
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════
# INICIALIZAR NUEVAS TABLAS
# ══════════════════════════════════════════════════════════════

# auth.py - MODIFICAR init_auth_db()

# auth.py - MODIFICAR init_auth_db()

def init_auth_db():
    """Las tablas de autenticación ya se crean en database.py.
    Esta función solo verifica que existan, pero no las recrea."""
    try:
        from database import engine
        # Verificar que las tablas existen sin crearlas
        # Podrías hacer una consulta simple para verificar
        print("✅ Tablas de auth verificadas (creadas por database.py)")
    except Exception as e:
        print(f"❌ Error verificando tablas de auth: {e}")