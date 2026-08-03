"""
database.py — BetSense
Maneja toda la conexión y operaciones con SQLite (para pruebas).
Cambia a PostgreSQL en producción.
"""

from sqlalchemy import (
    create_engine, Column, Integer, Float, String,
    DateTime, Text, Boolean, ForeignKey, UniqueConstraint, Index
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
import os

# ── CONEXIÓN ─────────────────────────────────────────────────
# Usar SQLite para pruebas (más fácil, no requiere servidor)
# El archivo betsense.db se creará automáticamente
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///betsense.db")

# Si quieres usar PostgreSQL más adelante, cambia a:
# DATABASE_URL = "postgresql://postgres:101106@localhost:5432/betsense"

engine = create_engine(
    DATABASE_URL, 
    echo=False, 
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)
Session = sessionmaker(bind=engine)
Base = declarative_base()


# ══════════════════════════════════════════════════════════════
# MODELOS DE AUTENTICACIÓN (para auth.py)
# ══════════════════════════════════════════════════════════════

class Usuario(Base):
    """Usuarios registrados en BetSense."""
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(30), unique=True, nullable=False)
    email = Column(String(120), unique=True, nullable=False)
    password_hash = Column(String(128), nullable=False)
    nombre = Column(String(80))
    avatar_url = Column(String(300))
    bio = Column(Text)
    deporte_fav = Column(String(50), default="football")
    equipo_fav = Column(String(80))
    bankroll_ini = Column(Float, default=0.0)
    creado = Column(DateTime, default=datetime.utcnow)
    activo = Column(Boolean, default=True)

    # Relaciones
    perfil_stats = relationship("PerfilStats", back_populates="usuario", uselist=False)
    picks = relationship("Pick", back_populates="autor", cascade="all, delete-orphan")
    siguiendo = relationship("Follow", foreign_keys="Follow.follower_id", back_populates="follower", cascade="all, delete-orphan")
    seguidores = relationship("Follow", foreign_keys="Follow.following_id", back_populates="following", cascade="all, delete-orphan")
    sesiones = relationship("Sesion", back_populates="usuario", cascade="all, delete-orphan")


class PerfilStats(Base):
    """Estadísticas públicas del tipster."""
    __tablename__ = "perfil_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), unique=True, nullable=False)
    total_picks = Column(Integer, default=0)
    picks_ganados = Column(Integer, default=0)
    picks_perdidos = Column(Integer, default=0)
    roi_porcentaje = Column(Float, default=0.0)
    racha_actual = Column(Integer, default=0)
    mejor_racha = Column(Integer, default=0)
    nivel = Column(String(20), default="rookie")
    puntos = Column(Integer, default=1000)
    actualizado = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    usuario = relationship("Usuario", back_populates="perfil_stats")


class Pick(Base):
    """Un pick publicado por un usuario en el feed social."""
    __tablename__ = "picks_social"

    id = Column(Integer, primary_key=True, autoincrement=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    prediccion_id = Column(Integer, ForeignKey("predicciones.id"), nullable=True)

    partido = Column(String(150))
    liga = Column(String(20))
    mercado = Column(String(80))
    cuota = Column(Float)
    stake = Column(Float, default=1.0)
    confianza = Column(Integer, default=3)
    descripcion = Column(Text)

    estado = Column(String(15), default="abierto")  # abierto / ganado / perdido / anulado
    creado = Column(DateTime, default=datetime.utcnow)
    cerrado = Column(DateTime, nullable=True)

    reacciones_fuego = Column(Integer, default=0)
    reacciones_duda = Column(Integer, default=0)
    reacciones_junto = Column(Integer, default=0)

    autor = relationship("Usuario", back_populates="picks")
    comentarios = relationship("Comentario", back_populates="pick", cascade="all, delete-orphan")


class Comentario(Base):
    """Comentarios en los picks del feed."""
    __tablename__ = "comentarios"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pick_id = Column(Integer, ForeignKey("picks_social.id"), nullable=False)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    texto = Column(Text, nullable=False)
    creado = Column(DateTime, default=datetime.utcnow)

    pick = relationship("Pick", back_populates="comentarios")
    usuario = relationship("Usuario")


class Follow(Base):
    """Sistema de seguimiento entre usuarios."""
    __tablename__ = "follows"

    id = Column(Integer, primary_key=True, autoincrement=True)
    follower_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    following_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    creado = Column(DateTime, default=datetime.utcnow)

    # Restricción única para evitar follows duplicados
    __table_args__ = (UniqueConstraint('follower_id', 'following_id', name='unique_follow'),)

    follower = relationship("Usuario", foreign_keys=[follower_id], back_populates="siguiendo")
    following = relationship("Usuario", foreign_keys=[following_id], back_populates="seguidores")


class Sesion(Base):
    """Tokens de sesión activos (refresh tokens)."""
    __tablename__ = "sesiones"

    id = Column(Integer, primary_key=True, autoincrement=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    token = Column(String(64), unique=True, nullable=False)
    expira = Column(DateTime, nullable=False)
    creado = Column(DateTime, default=datetime.utcnow)
    ip = Column(String(45))
    user_agent = Column(String(200))

    usuario = relationship("Usuario", back_populates="sesiones")


# ══════════════════════════════════════════════════════════════
# MODELOS EXISTENTES (para betAI.py)
# ══════════════════════════════════════════════════════════════

class Equipo(Base):
    """Equipos conocidos."""
    __tablename__ = "equipos"

    id = Column(Integer, primary_key=True)  # ID de la API
    nombre = Column(String(100), nullable=False)
    liga = Column(String(10))
    pais = Column(String(50))
    creado = Column(DateTime, default=datetime.utcnow)

    predicciones_home = relationship("Prediccion", foreign_keys="Prediccion.home_id", back_populates="equipo_home")
    predicciones_away = relationship("Prediccion", foreign_keys="Prediccion.away_id", back_populates="equipo_away")


class Prediccion(Base):
    """Cada vez que el usuario analiza un partido, se guarda aquí."""
    __tablename__ = "predicciones"

    id = Column(Integer, primary_key=True, autoincrement=True)
    home_id = Column(Integer, ForeignKey("equipos.id"), nullable=False)
    away_id = Column(Integer, ForeignKey("equipos.id"), nullable=False)
    liga = Column(String(10))
    season = Column(String(6))
    fecha_anal = Column(DateTime, default=datetime.utcnow)

    # Lambdas Poisson
    lambda_home = Column(Float)
    lambda_away = Column(Float)

    # Probabilidades 1X2
    prob_home = Column(Float)
    prob_draw = Column(Float)
    prob_away = Column(Float)

    # Cuotas justas
    odds_home = Column(Float)
    odds_draw = Column(Float)
    odds_away = Column(Float)

    # Mercados
    over25 = Column(Float)
    over15 = Column(Float)
    btts = Column(Float)
    under25 = Column(Float)

    # Marcador más probable
    score_home = Column(Integer)
    score_away = Column(Integer)
    score_prob = Column(Float)

    # BetSense Score
    bs_score = Column(Integer)
    bs_label = Column(String(30))

    # Posiciones en tabla
    pos_home = Column(Integer)
    pos_away = Column(Integer)
    pts_home = Column(Integer)
    pts_away = Column(Integer)

    # Resultado real (se actualiza después del partido)
    resultado_real_h = Column(Integer, nullable=True)
    resultado_real_a = Column(Integer, nullable=True)
    acertado = Column(Boolean, nullable=True)

    # Relaciones
    equipo_home = relationship("Equipo", foreign_keys=[home_id], back_populates="predicciones_home")
    equipo_away = relationship("Equipo", foreign_keys=[away_id], back_populates="predicciones_away")
    sugerencias = relationship("Sugerencia", back_populates="prediccion", cascade="all, delete-orphan")
    historial_cuotas = relationship("HistorialCuotas", back_populates="prediccion", cascade="all, delete-orphan")
    picks = relationship("Pick", back_populates=None)  # picks_social puede referenciar predicciones


class Sugerencia(Base):
    """Las apuestas sugeridas para cada predicción."""
    __tablename__ = "sugerencias"

    id = Column(Integer, primary_key=True, autoincrement=True)
    prediccion_id = Column(Integer, ForeignKey("predicciones.id"), nullable=False)
    market = Column(String(50))
    prob = Column(Float)
    icono = Column(String(5))
    nivel = Column(String(15))

    prediccion = relationship("Prediccion", back_populates="sugerencias")


class HistorialCuotas(Base):
    """Para el Value Engine: snapshots de cuotas de casas de apuestas."""
    __tablename__ = "historial_cuotas"

    id = Column(Integer, primary_key=True, autoincrement=True)
    prediccion_id = Column(Integer, ForeignKey("predicciones.id"))
    timestamp = Column(DateTime, default=datetime.utcnow)
    bookmaker = Column(String(50))
    cuota_home = Column(Float)
    cuota_draw = Column(Float)
    cuota_away = Column(Float)
    value_home = Column(Float)
    value_draw = Column(Float)
    value_away = Column(Float)

    prediccion = relationship("Prediccion", back_populates="historial_cuotas")


# ══════════════════════════════════════════════════════════════
# MODELOS DEL BACKTESTER CIEGO (auto-calibración interna)
# ══════════════════════════════════════════════════════════════

class PartidoHistorico(Base):
    """
    Caché local de partidos FINISHED, traídos en bloque vía
    /competitions/{liga}/matches?season=X (1 request = 1 temporada completa).
    Evita re-pedir a la API en cada corrida del backtester.
    """
    __tablename__ = "partidos_historicos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, unique=True, nullable=False)  # id de football-data.org
    liga = Column(String(10), nullable=False)
    season = Column(String(6), nullable=False)
    fecha = Column(DateTime, nullable=False)
    home_id = Column(Integer, nullable=False)
    away_id = Column(Integer, nullable=False)
    home_name = Column(String(100))
    away_name = Column(String(100))
    gh = Column(Integer, nullable=False)
    ga = Column(Integer, nullable=False)
    creado = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_partidohist_fecha", "fecha"),
        Index("ix_partidohist_liga_season", "liga", "season"),
        Index("ix_partidohist_home", "home_id"),
        Index("ix_partidohist_away", "away_id"),
    )


class BacktestRun(Base):
    """
    Historial de corridas del backtester ciego: cada combinación de
    parámetros probada en el grid search, con su RPS/Brier promedio.
    """
    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    fecha = Column(DateTime, default=datetime.utcnow)
    decay_halflife_dias = Column(Float, nullable=False)
    maf_peso = Column(Float, nullable=False)
    n_partidos = Column(Integer)
    rps_promedio = Column(Float)
    brier_promedio = Column(Float)
    es_ganador = Column(Boolean, default=False)  # True si fue la mejor combo de esa corrida


class ParametrosActivos(Base):
    """
    Configuración vigente que betAI.py lee en producción. Fila única
    (id=1), actualizada por el auto-tune del backtester cuando encuentra
    una combinación mejor que la actual.
    """
    __tablename__ = "parametros_activos"

    id = Column(Integer, primary_key=True, default=1)
    decay_halflife_dias = Column(Float, default=150.0, nullable=False)
    maf_peso = Column(Float, default=1.0, nullable=False)
    rps_promedio_calibracion = Column(Float, nullable=True)
    n_partidos_calibracion = Column(Integer, nullable=True)
    actualizado = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ══════════════════════════════════════════════════════════════
# FUNCIONES DE ACCESO
# ══════════════════════════════════════════════════════════════

def init_db():
    """Crea todas las tablas si no existen."""
    try:
        Base.metadata.create_all(engine)
        print("✅ Base de datos BetSense inicializada correctamente.")
        print(f"   📁 Conexión: {DATABASE_URL}")
    except Exception as e:
        print(f"❌ Error inicializando base de datos: {e}")
        raise


def guardar_equipo(session, team_id, nombre, liga):
    """Inserta o actualiza un equipo."""
    eq = session.get(Equipo, team_id)
    if not eq:
        eq = Equipo(id=team_id, nombre=nombre, liga=liga)
        session.add(eq)
        session.flush()
    return eq


def guardar_prediccion(data: dict) -> int:
    """
    Recibe el dict completo de /api/analyze y lo persiste.
    Devuelve el ID de la predicción guardada.
    """
    session = Session()
    try:
        # Equipos
        guardar_equipo(session, data["teams"]["home"]["id"],
                       data["teams"]["home"]["name"], data["liga"])
        guardar_equipo(session, data["teams"]["away"]["id"],
                       data["teams"]["away"]["name"], data["liga"])

        po = data["poisson"]
        mk = data["markets"]
        sc = data["score"]
        st = data.get("standings", {})
        sth = st.get("home") or {}
        sta = st.get("away") or {}

        pred = Prediccion(
            home_id=data["teams"]["home"]["id"],
            away_id=data["teams"]["away"]["id"],
            liga=data["liga"],
            season=data["season"],
            lambda_home=po["lambdaHome"],
            lambda_away=po["lambdaAway"],
            prob_home=po["probHome"],
            prob_draw=po["probDraw"],
            prob_away=po["probAway"],
            odds_home=po.get("oddsHome"),
            odds_draw=po.get("oddsDraw"),
            odds_away=po.get("oddsAway"),
            over25=mk["over25"],
            over15=mk["over15"],
            btts=mk["btts"],
            under25=mk["under25"],
            score_home=po["bestScore"]["home"],
            score_away=po["bestScore"]["away"],
            score_prob=po["bestScore"]["prob"],
            bs_score=sc["total"],
            bs_label=sc["label"],
            pos_home=sth.get("position"),
            pos_away=sta.get("position"),
            pts_home=sth.get("points"),
            pts_away=sta.get("points"),
        )
        session.add(pred)
        session.flush()

        # Sugerencias
        for sug in data.get("suggestions", []):
            session.add(Sugerencia(
                prediccion_id=pred.id,
                market=sug["market"],
                prob=sug["prob"],
                icono=sug.get("icon", "🎲"),
                nivel=sug.get("level", "VARIABLE"),
            ))

        session.commit()
        print(f"✅ Predicción guardada — ID {pred.id} | {data['teams']['home']['name']} vs {data['teams']['away']['name']}")
        return pred.id

    except Exception as e:
        session.rollback()
        print(f"❌ Error guardando predicción: {e}")
        return None
    finally:
        session.close()


def obtener_historial(limit=20):
    """Devuelve las últimas predicciones guardadas."""
    session = Session()
    try:
        preds = (session.query(Prediccion)
                 .order_by(Prediccion.fecha_anal.desc())
                 .limit(limit)
                 .all())
        result = []
        for p in preds:
            result.append({
                "id": p.id,
                "home": p.equipo_home.nombre if p.equipo_home else p.home_id,
                "away": p.equipo_away.nombre if p.equipo_away else p.away_id,
                "liga": p.liga,
                "season": p.season,
                "fecha": p.fecha_anal.strftime("%Y-%m-%d %H:%M"),
                "prob_home": round(p.prob_home * 100, 1) if p.prob_home else None,
                "prob_draw": round(p.prob_draw * 100, 1) if p.prob_draw else None,
                "prob_away": round(p.prob_away * 100, 1) if p.prob_away else None,
                "bs_score": p.bs_score,
                "bs_label": p.bs_label,
                "score_pred": f"{p.score_home}–{p.score_away}",
                "acertado": p.acertado,
            })
        return result
    finally:
        session.close()


def obtener_estadisticas_modelo():
    """
    Estadísticas de rendimiento del modelo:
    cuántas predicciones ha hecho, cuántas acertó, etc.
    """
    session = Session()
    try:
        total = session.query(Prediccion).count()
        con_result = session.query(Prediccion).filter(Prediccion.acertado.isnot(None)).count()
        acertadas = session.query(Prediccion).filter(Prediccion.acertado == True).count()

        return {
            "total_predicciones": total,
            "con_resultado_real": con_result,
            "acertadas": acertadas,
            "precision": round(acertadas / con_result * 100, 1) if con_result > 0 else None,
        }
    finally:
        session.close()


def actualizar_resultado(prediccion_id: int, goles_home: int, goles_away: int) -> dict:
    """
    Registra el resultado real y calcula si el modelo acertó el 1X2.
    Devuelve dict con 'acertado' y 'detalle', o None si no existe el ID.
    """
    session = Session()
    try:
        pred = session.get(Prediccion, prediccion_id)
        if not pred:
            return None

        if goles_home > goles_away:
            outcome_real = "1"
        elif goles_home == goles_away:
            outcome_real = "X"
        else:
            outcome_real = "2"

        probs = {
            "1": pred.prob_home or 0,
            "X": pred.prob_draw or 0,
            "2": pred.prob_away or 0,
        }
        outcome_pred = max(probs, key=probs.get)
        acertado = (outcome_real == outcome_pred)

        pred.resultado_real_h = goles_home
        pred.resultado_real_a = goles_away
        pred.acertado = acertado
        session.commit()

        print(f"{'✅' if acertado else '❌'} Resultado ID {prediccion_id}: "
              f"{goles_home}–{goles_away} real | predicho={outcome_pred} | acertado={acertado}")

        return {
            "acertado": acertado,
            "outcome_real": outcome_real,
            "outcome_pred": outcome_pred,
            "detalle": (
                f"Predicho: {outcome_pred} ({round(probs[outcome_pred]*100,1)}%) | "
                f"Real: {outcome_real} ({goles_home}–{goles_away})"
            ),
        }
    except Exception as e:
        session.rollback()
        print(f"❌ Error actualizando resultado: {e}")
        return None
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════
# FUNCIONES — BACKTESTER CIEGO / CALIBRACIÓN
# ══════════════════════════════════════════════════════════════

def guardar_partidos_historicos(partidos: list) -> int:
    """
    Upsert en bloque de partidos históricos (formato ya normalizado,
    ver backtest_engine.normalizar_partido_api). Devuelve cuántos
    partidos nuevos insertó (los ya existentes se ignoran).
    """
    session = Session()
    insertados = 0
    try:
        existentes = {
            mid for (mid,) in session.query(PartidoHistorico.match_id)
            .filter(PartidoHistorico.match_id.in_([p["match_id"] for p in partidos]))
            .all()
        } if partidos else set()

        for p in partidos:
            if p["match_id"] in existentes:
                continue
            session.add(PartidoHistorico(**p))
            insertados += 1
        session.commit()
        return insertados
    except Exception as e:
        session.rollback()
        print(f"❌ Error guardando partidos históricos: {e}")
        return insertados
    finally:
        session.close()


def obtener_partidos_historicos(liga: str = None, seasons: list = None) -> list:
    """
    Trae partidos de la caché local para armar el pool de backtest.
    Filtra por liga/temporadas si se especifican. Devuelve dicts listos
    para usar (mismo shape que normalizar() en betAI.py: homeId/awayId/gH/gA/date).
    """
    session = Session()
    try:
        q = session.query(PartidoHistorico)
        if liga:
            q = q.filter(PartidoHistorico.liga == liga)
        if seasons:
            q = q.filter(PartidoHistorico.season.in_([str(s) for s in seasons]))
        rows = q.order_by(PartidoHistorico.fecha.asc()).all()
        return [{
            "matchId": r.match_id,
            "homeId": r.home_id,
            "awayId": r.away_id,
            "homeName": r.home_name,
            "awayName": r.away_name,
            "gH": r.gh,
            "gA": r.ga,
            "date": r.fecha,
            "liga": r.liga,
            "season": r.season,
        } for r in rows]
    finally:
        session.close()


def obtener_parametros_activos() -> dict:
    """
    Lee la config vigente (decay half-life, peso MAF) que betAI.py debe
    usar en producción. Si no existe todavía, crea la fila con defaults.
    """
    session = Session()
    try:
        params = session.get(ParametrosActivos, 1)
        if not params:
            params = ParametrosActivos(id=1, decay_halflife_dias=150.0, maf_peso=1.0)
            session.add(params)
            session.commit()
        return {
            "decay_halflife_dias": params.decay_halflife_dias,
            "maf_peso": params.maf_peso,
            "rps_promedio_calibracion": params.rps_promedio_calibracion,
            "n_partidos_calibracion": params.n_partidos_calibracion,
            "actualizado": params.actualizado.isoformat() if params.actualizado else None,
        }
    finally:
        session.close()


def guardar_parametros_activos(decay_halflife_dias: float, maf_peso: float,
                                 rps_promedio: float = None, n_partidos: int = None) -> None:
    """Actualiza la config vigente (llamado por el auto-tune al encontrar una mejor combo)."""
    session = Session()
    try:
        params = session.get(ParametrosActivos, 1)
        if not params:
            params = ParametrosActivos(id=1)
            session.add(params)
        params.decay_halflife_dias = decay_halflife_dias
        params.maf_peso = maf_peso
        params.rps_promedio_calibracion = rps_promedio
        params.n_partidos_calibracion = n_partidos
        session.commit()
        print(f"🎯 Parámetros activos actualizados: half-life={decay_halflife_dias}d, "
              f"maf_peso={maf_peso}, RPS={rps_promedio}")
    except Exception as e:
        session.rollback()
        print(f"❌ Error guardando parámetros activos: {e}")
    finally:
        session.close()


def guardar_backtest_run(decay_halflife_dias: float, maf_peso: float, n_partidos: int,
                           rps_promedio: float, brier_promedio: float, es_ganador: bool = False) -> None:
    """Loguea una combinación probada durante el grid search del auto-tune."""
    session = Session()
    try:
        session.add(BacktestRun(
            decay_halflife_dias=decay_halflife_dias,
            maf_peso=maf_peso,
            n_partidos=n_partidos,
            rps_promedio=rps_promedio,
            brier_promedio=brier_promedio,
            es_ganador=es_ganador,
        ))
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"❌ Error guardando backtest run: {e}")
    finally:
        session.close()


def obtener_historial_backtests(limit: int = 50) -> list:
    """Últimas corridas de auto-tune, para diagnóstico interno (no va al HTML del usuario)."""
    session = Session()
    try:
        rows = (session.query(BacktestRun)
                .order_by(BacktestRun.fecha.desc())
                .limit(limit).all())
        return [{
            "fecha": r.fecha.strftime("%Y-%m-%d %H:%M"),
            "decay_halflife_dias": r.decay_halflife_dias,
            "maf_peso": r.maf_peso,
            "n_partidos": r.n_partidos,
            "rps_promedio": r.rps_promedio,
            "brier_promedio": r.brier_promedio,
            "es_ganador": r.es_ganador,
        } for r in rows]
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════
# INICIALIZACIÓN
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    print("\n📊 Tablas creadas:")
    for table in Base.metadata.tables.keys():
        print(f"   - {table}")