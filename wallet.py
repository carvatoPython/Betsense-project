# wallet.py — Sistema de billetera interna y escrow de picks para BetSense
# Arquitectura: SQLite independiente (wallet.db), Blueprint Flask /api/wallet/*
# Operaciones: depósito manual, retiro manual, seguir pick (escrow), liquidar pick
#
# Flujo de dinero:
#   Depósito   → admin aprueba → saldo usuario sube
#   Seguir pick → saldo baja → EscrowEntry creado (PENDING)
#   Pick resuelto → EscrowEntry liquidada → saldo sube/baja según resultado
#   Retiro     → usuario solicita → admin aprueba → saldo baja
#
# Comisión del tipster: 10% (rango < Experto) o 20% (Experto/Elite/Leyenda)
# La comisión sale de las ganancias del apostador, no del capital apostado.

import sqlite3, os, functools
from datetime import datetime
from flask import Blueprint, request, jsonify, g

# ── Configuración ──────────────────────────────────────────────────────────────
WALLET_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wallet.db")

# Rangos que cobran comisión mayor
RANGOS_PREMIUM = {"experto", "elite", "leyenda"}

wallet_bp = Blueprint("wallet", __name__)


# ── Conexión DB ────────────────────────────────────────────────────────────────
def get_wdb():
    """Obtiene conexión SQLite con row_factory para dicts."""
    conn = sqlite3.connect(WALLET_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_wallet_db():
    """Crea tablas si no existen. Llamar al iniciar la app."""
    with get_wdb() as db:
        db.executescript("""
        -- Billetera por usuario (saldo disponible en COP)
        CREATE TABLE IF NOT EXISTS wallets (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT    NOT NULL,
            saldo       REAL    NOT NULL DEFAULT 0.0,
            saldo_bloq  REAL    NOT NULL DEFAULT 0.0,  -- en escrow activo
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- Movimientos (depósitos, retiros, ganancias, comisiones, escrow)
        CREATE TABLE IF NOT EXISTS movimientos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            tipo        TEXT    NOT NULL,  -- DEPOSITO|RETIRO|ESCROW_OUT|ESCROW_IN|COMISION|DEVOLUCION
            monto       REAL    NOT NULL,  -- positivo = entra, negativo = sale
            descripcion TEXT,
            ref_id      INTEGER,           -- FK a escrow_entries si aplica
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- Solicitudes de depósito / retiro (aprobación manual)
        CREATE TABLE IF NOT EXISTS solicitudes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            username    TEXT    NOT NULL,
            tipo        TEXT    NOT NULL,  -- DEPOSITO | RETIRO
            monto       REAL    NOT NULL,
            metodo      TEXT,              -- nequi | transferencia | efectivo
            referencia  TEXT,             -- número de comprobante
            estado      TEXT    NOT NULL DEFAULT 'PENDIENTE',  -- PENDIENTE|APROBADA|RECHAZADA
            nota_admin  TEXT,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- Escrow: cuando un usuario sigue el pick de otro
        CREATE TABLE IF NOT EXISTS escrow_entries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            pick_id         INTEGER NOT NULL,        -- ID del pick social (tabla picks en auth.db)
            seguidor_id     INTEGER NOT NULL,        -- quien apuesta
            seguidor_user   TEXT    NOT NULL,
            tipster_id      INTEGER NOT NULL,        -- quien publicó el pick
            tipster_user    TEXT    NOT NULL,
            tipster_rango   TEXT    NOT NULL DEFAULT 'rookie',
            monto           REAL    NOT NULL,        -- COP apostados
            cuota           REAL    NOT NULL,        -- cuota del pick
            ganancia_bruta  REAL    GENERATED ALWAYS AS (ROUND(monto * cuota - monto, 2)) VIRTUAL,
            comision_pct    REAL    NOT NULL,        -- 10 o 20 según rango
            estado          TEXT    NOT NULL DEFAULT 'PENDING',  -- PENDING|WON|LOST|VOID
            coupon_ref      TEXT,                    -- referencia Kambi para liquidación automática
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_mov_user    ON movimientos(user_id);
        CREATE INDEX IF NOT EXISTS idx_escrow_pick ON escrow_entries(pick_id);
        CREATE INDEX IF NOT EXISTS idx_escrow_seg  ON escrow_entries(seguidor_id);
        CREATE INDEX IF NOT EXISTS idx_escrow_tip  ON escrow_entries(tipster_id);
        CREATE INDEX IF NOT EXISTS idx_sol_user    ON solicitudes(user_id);
        """)
    print("[wallet] DB inicializada →", WALLET_DB)


# ── Helpers ────────────────────────────────────────────────────────────────────
def _get_or_create_wallet(db, user_id: int, username: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        db.execute(
            "INSERT INTO wallets(user_id, username, saldo, saldo_bloq) VALUES(?,?,0,0)",
            (user_id, username)
        )
        db.commit()
        row = db.execute("SELECT * FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    return row


def _registrar_movimiento(db, user_id: int, tipo: str, monto: float,
                           descripcion: str = "", ref_id: int = None):
    db.execute(
        "INSERT INTO movimientos(user_id, tipo, monto, descripcion, ref_id) VALUES(?,?,?,?,?)",
        (user_id, tipo, monto, descripcion, ref_id)
    )


def _comision_pct(rango: str) -> float:
    """Retorna el porcentaje de comisión según rango del tipster."""
    return 20.0 if rango.lower() in RANGOS_PREMIUM else 10.0


# Función pública para liquidar desde kambi_place_bet.py
def liquidar_escrow(pick_id: int, resultado: str, coupon_ref: str = "") -> dict:
    """
    Liquida todos los escrows PENDING de un pick.
    Llamado automáticamente desde kambi_place_bet cuando Kambi confirma WON/LOST.

    resultado: 'WON' | 'LOST' | 'VOID'
    Retorna dict con resumen de liquidaciones.
    """
    resultado = resultado.upper()
    if resultado not in ("WON", "LOST", "VOID"):
        raise ValueError(f"Resultado inválido: {resultado}")

    with get_wdb() as db:
        escrows = db.execute(
            "SELECT * FROM escrow_entries WHERE pick_id=? AND estado='PENDING'",
            (pick_id,)
        ).fetchall()

        if not escrows:
            return {"ok": False, "msg": "No hay escrows pendientes", "liquidaciones": []}

        liquidaciones = []

        for e in escrows:
            e = dict(e)
            monto          = e["monto"]
            cuota          = e["cuota"]
            comision_pct   = e["comision_pct"]
            ganancia_bruta = round(monto * cuota - monto, 2)
            comision       = round(ganancia_bruta * comision_pct / 100, 2)
            ganancia_neta  = round(ganancia_bruta - comision, 2)

            # Liberar saldo_bloq
            db.execute(
                "UPDATE wallets SET saldo_bloq=saldo_bloq-?, updated_at=datetime('now') WHERE user_id=?",
                (monto, e["seguidor_id"])
            )

            if resultado == "WON":
                retorno = round(monto + ganancia_neta, 2)
                db.execute(
                    "UPDATE wallets SET saldo=saldo+?, updated_at=datetime('now') WHERE user_id=?",
                    (retorno, e["seguidor_id"])
                )
                _registrar_movimiento(
                    db, e["seguidor_id"], "ESCROW_IN", retorno,
                    f"Pick #{pick_id} GANADO · capital + ganancia neta · ref:{coupon_ref}",
                    e["id"]
                )
                _get_or_create_wallet(db, e["tipster_id"], e["tipster_user"])
                db.execute(
                    "UPDATE wallets SET saldo=saldo+?, updated_at=datetime('now') WHERE user_id=?",
                    (comision, e["tipster_id"])
                )
                _registrar_movimiento(
                    db, e["tipster_id"], "COMISION", comision,
                    f"Comisión {comision_pct:.0f}% pick #{pick_id} ganado por @{e['seguidor_user']}",
                    e["id"]
                )
                liquidaciones.append({
                    "escrow_id":     e["id"],
                    "seguidor":      e["seguidor_user"],
                    "tipster":       e["tipster_user"],
                    "monto":         monto,
                    "ganancia_neta": ganancia_neta,
                    "comision":      comision,
                    "retorno":       retorno,
                })

            elif resultado == "LOST":
                _registrar_movimiento(
                    db, e["seguidor_id"], "ESCROW_IN", 0,
                    f"Pick #{pick_id} PERDIDO · ${monto:,.0f} COP perdidos · ref:{coupon_ref}",
                    e["id"]
                )
                liquidaciones.append({
                    "escrow_id": e["id"],
                    "seguidor":  e["seguidor_user"],
                    "tipster":   e["tipster_user"],
                    "monto":     monto,
                    "perdida":   monto,
                })

            elif resultado == "VOID":
                db.execute(
                    "UPDATE wallets SET saldo=saldo+?, updated_at=datetime('now') WHERE user_id=?",
                    (monto, e["seguidor_id"])
                )
                _registrar_movimiento(
                    db, e["seguidor_id"], "DEVOLUCION", monto,
                    f"Pick #{pick_id} ANULADO · devolución íntegra",
                    e["id"]
                )
                liquidaciones.append({
                    "escrow_id":  e["id"],
                    "seguidor":   e["seguidor_user"],
                    "devolucion": monto,
                })

            db.execute(
                "UPDATE escrow_entries SET estado=?, coupon_ref=?, updated_at=datetime('now') WHERE id=?",
                (resultado, coupon_ref, e["id"])
            )

        db.commit()

    return {"ok": True, "liquidaciones": liquidaciones, "total": len(liquidaciones)}


# ── Auth helper (reutiliza el JWT del sistema existente) ───────────────────────
def _auth_required(f):
    """Decorator: extrae user_id y username del JWT de auth.py."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        from auth import verificar_token  # import diferido para evitar circular
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Token requerido"}), 401
        token = auth_header[7:]
        payload = verificar_token(token)
        if not payload:
            return jsonify({"error": "Token inválido o expirado"}), 401
        g.user_id  = payload["user_id"]
        g.username = payload["username"]
        g.rango    = payload.get("nivel", "rookie")
        return f(*args, **kwargs)
    return wrapper


def _admin_required(f):
    """Decorator: solo usuarios con rango 'admin' o username 'admin'."""
    @functools.wraps(f)
    @_auth_required
    def wrapper(*args, **kwargs):
        if g.username.lower() != "admin" and g.rango.lower() != "admin":
            return jsonify({"error": "Acceso denegado"}), 403
        return f(*args, **kwargs)
    return wrapper


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS USUARIO
# ══════════════════════════════════════════════════════════════════════════════

# ── GET /api/wallet/saldo ──────────────────────────────────────────────────────
@wallet_bp.route("/api/wallet/saldo", methods=["GET"])
@_auth_required
def get_saldo():
    """Retorna saldo disponible, bloqueado en escrow e historial reciente."""
    with get_wdb() as db:
        w = _get_or_create_wallet(db, g.user_id, g.username)
        movs = db.execute(
            """SELECT tipo, monto, descripcion, created_at
               FROM movimientos WHERE user_id=?
               ORDER BY id DESC LIMIT 20""",
            (g.user_id,)
        ).fetchall()
        escrows_act = db.execute(
            """SELECT e.id, e.pick_id, e.tipster_user, e.monto, e.cuota,
                      e.comision_pct, e.estado, e.created_at
               FROM escrow_entries e
               WHERE e.seguidor_id=? AND e.estado='PENDING'
               ORDER BY e.id DESC""",
            (g.user_id,)
        ).fetchall()

    return jsonify({
        "saldo":       round(w["saldo"], 2),
        "saldo_bloq":  round(w["saldo_bloq"], 2),
        "saldo_total": round(w["saldo"] + w["saldo_bloq"], 2),
        "movimientos": [dict(m) for m in movs],
        "escrows_activos": [dict(e) for e in escrows_act],
    })


# ── POST /api/wallet/solicitar ─────────────────────────────────────────────────
@wallet_bp.route("/api/wallet/solicitar", methods=["POST"])
@_auth_required
def solicitar():
    """Crea una solicitud de depósito o retiro (aprobación manual por admin)."""
    data   = request.get_json() or {}
    tipo   = data.get("tipo", "").upper()
    monto  = float(data.get("monto", 0))
    metodo = data.get("metodo", "nequi")
    ref    = data.get("referencia", "")

    if tipo not in ("DEPOSITO", "RETIRO"):
        return jsonify({"error": "tipo debe ser DEPOSITO o RETIRO"}), 400
    if monto <= 0:
        return jsonify({"error": "Monto debe ser positivo"}), 400
    if monto > 200_000:
        return jsonify({"error": "Límite beta: máximo $200.000 COP por operación"}), 400

    with get_wdb() as db:
        if tipo == "RETIRO":
            w = _get_or_create_wallet(db, g.user_id, g.username)
            if w["saldo"] < monto:
                return jsonify({"error": f"Saldo insuficiente (disponible: ${w['saldo']:,.0f})"}), 400

        db.execute(
            """INSERT INTO solicitudes(user_id, username, tipo, monto, metodo, referencia)
               VALUES(?,?,?,?,?,?)""",
            (g.user_id, g.username, tipo, monto, metodo, ref)
        )
        db.commit()
        sol_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    return jsonify({
        "ok": True,
        "solicitud_id": sol_id,
        "mensaje": f"Solicitud de {tipo.lower()} por ${monto:,.0f} COP recibida."
    })


# ── POST /api/wallet/seguir_pick ───────────────────────────────────────────────
@wallet_bp.route("/api/wallet/seguir_pick", methods=["POST"])
@_auth_required
def seguir_pick():
    """
    El usuario apuesta `monto` COP al pick de otro tipster.
    El monto queda en escrow (saldo_bloq) hasta que el pick se resuelva.
    """
    data    = request.get_json() or {}
    pick_id = int(data.get("pick_id", 0))
    monto   = float(data.get("monto", 0))

    if pick_id <= 0:
        return jsonify({"error": "pick_id inválido"}), 400
    if monto < 1_000:
        return jsonify({"error": "Monto mínimo: $1.000 COP"}), 400
    if monto > 200_000:
        return jsonify({"error": "Límite beta: máximo $200.000 COP por pick"}), 400

    try:
        from auth import get_pick_by_id
        pick = get_pick_by_id(pick_id)
    except ImportError:
        auth_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth.db")
        with sqlite3.connect(auth_db_path) as adb:
            adb.row_factory = sqlite3.Row
            pick = adb.execute("SELECT * FROM picks WHERE id=?", (pick_id,)).fetchone()
            if pick:
                pick = dict(pick)

    if not pick:
        return jsonify({"error": "Pick no encontrado"}), 404
    if pick.get("resultado") and pick["resultado"] != "PENDING":
        return jsonify({"error": "Este pick ya fue resuelto"}), 400
    if pick.get("user_id") == g.user_id:
        return jsonify({"error": "No puedes seguir tu propio pick"}), 400

    tipster_id   = pick.get("user_id") or pick.get("usuario_id")
    tipster_user = pick.get("username") or pick.get("usuario") or "tipster"
    cuota        = float(pick.get("cuota") or 1.5)

    tipster_rango = "rookie"
    try:
        auth_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth.db")
        with sqlite3.connect(auth_db_path) as adb:
            adb.row_factory = sqlite3.Row
            u = adb.execute("SELECT nivel FROM users WHERE id=?", (tipster_id,)).fetchone()
            if u:
                tipster_rango = u["nivel"] or "rookie"
    except Exception:
        pass

    comision_pct = _comision_pct(tipster_rango)

    with get_wdb() as db:
        w = _get_or_create_wallet(db, g.user_id, g.username)
        if w["saldo"] < monto:
            return jsonify({"error": f"Saldo insuficiente. Disponible: ${w['saldo']:,.0f} COP"}), 400

        existente = db.execute(
            "SELECT id FROM escrow_entries WHERE pick_id=? AND seguidor_id=? AND estado='PENDING'",
            (pick_id, g.user_id)
        ).fetchone()
        if existente:
            return jsonify({"error": "Ya estás siguiendo este pick"}), 400

        db.execute(
            "UPDATE wallets SET saldo=saldo-?, saldo_bloq=saldo_bloq+?, updated_at=datetime('now') WHERE user_id=?",
            (monto, monto, g.user_id)
        )
        db.execute(
            """INSERT INTO escrow_entries
               (pick_id, seguidor_id, seguidor_user, tipster_id, tipster_user,
                tipster_rango, monto, cuota, comision_pct)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (pick_id, g.user_id, g.username, tipster_id, tipster_user,
             tipster_rango, monto, cuota, comision_pct)
        )
        escrow_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        _registrar_movimiento(
            db, g.user_id, "ESCROW_OUT", -monto,
            f"Siguiendo pick #{pick_id} de @{tipster_user} · cuota {cuota}",
            escrow_id
        )
        db.commit()

    ganancia_potencial = round(monto * cuota - monto, 2)
    comision_est       = round(ganancia_potencial * comision_pct / 100, 2)

    return jsonify({
        "ok":                  True,
        "escrow_id":           escrow_id,
        "monto_bloqueado":     monto,
        "ganancia_potencial":  ganancia_potencial,
        "comision_tipster_pct": comision_pct,
        "comision_tipster_est": comision_est,
        "ganancia_neta_est":   round(ganancia_potencial - comision_est, 2),
        "mensaje": f"${monto:,.0f} COP en escrow. Potencial: ${ganancia_potencial:,.0f} (menos {comision_pct:.0f}% al tipster)"
    })


# ── GET /api/wallet/escrows ────────────────────────────────────────────────────
@wallet_bp.route("/api/wallet/escrows", methods=["GET"])
@_auth_required
def mis_escrows():
    """Lista todos los escrows del usuario (activos e históricos)."""
    with get_wdb() as db:
        rows = db.execute(
            """SELECT e.*,
                      ROUND(e.monto * e.cuota - e.monto, 2) as ganancia_bruta_calc
               FROM escrow_entries e
               WHERE e.seguidor_id=? OR e.tipster_id=?
               ORDER BY e.id DESC LIMIT 50""",
            (g.user_id, g.user_id)
        ).fetchall()
    return jsonify({"escrows": [dict(r) for r in rows]})


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS ADMIN
# ══════════════════════════════════════════════════════════════════════════════

# ── POST /api/wallet/admin/deposito_directo ────────────────────────────────────
@wallet_bp.route("/api/wallet/admin/deposito_directo", methods=["POST"])
@_admin_required
def admin_deposito_directo():
    """
    Acredita saldo directamente a un usuario sin pasar por solicitud.
    Útil para pruebas y carga inicial de la demo.

    Body: { username, monto, nota }

    Ejemplo para prueba:
      POST /api/wallet/admin/deposito_directo
      { "username": "tu_usuario", "monto": 5000, "nota": "Carga inicial demo" }
    """
    data     = request.get_json() or {}
    username = data.get("username", "").strip()
    monto    = float(data.get("monto", 0))
    nota     = data.get("nota", "Depósito directo admin")

    if not username:
        return jsonify({"error": "username requerido"}), 400
    if monto <= 0:
        return jsonify({"error": "monto debe ser positivo"}), 400
    if monto > 500_000:
        return jsonify({"error": "Máximo $500.000 COP por depósito directo"}), 400

    # Buscar user_id en auth.db
    auth_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth.db")
    try:
        with sqlite3.connect(auth_db_path) as adb:
            adb.row_factory = sqlite3.Row
            u = adb.execute(
                "SELECT id, username FROM users WHERE username=?", (username,)
            ).fetchone()
    except Exception as ex:
        return jsonify({"error": f"No se pudo acceder a auth.db: {ex}"}), 500

    if not u:
        return jsonify({"error": f"Usuario '{username}' no encontrado"}), 404

    user_id = u["id"]

    with get_wdb() as db:
        _get_or_create_wallet(db, user_id, username)
        db.execute(
            "UPDATE wallets SET saldo=saldo+?, updated_at=datetime('now') WHERE user_id=?",
            (monto, user_id)
        )
        _registrar_movimiento(db, user_id, "DEPOSITO", monto, nota)
        db.commit()
        w = db.execute("SELECT saldo, saldo_bloq FROM wallets WHERE user_id=?", (user_id,)).fetchone()

    return jsonify({
        "ok":          True,
        "username":    username,
        "depositado":  monto,
        "saldo_nuevo": round(w["saldo"], 2),
        "nota":        nota,
        "mensaje":     f"✅ ${monto:,.0f} COP acreditados a @{username}. Saldo actual: ${w['saldo']:,.0f} COP",
    })


# ── GET /api/wallet/admin/solicitudes ──────────────────────────────────────────
@wallet_bp.route("/api/wallet/admin/solicitudes", methods=["GET"])
@_admin_required
def admin_solicitudes():
    estado = request.args.get("estado", "PENDIENTE")
    with get_wdb() as db:
        rows = db.execute(
            "SELECT * FROM solicitudes WHERE estado=? ORDER BY id DESC",
            (estado,)
        ).fetchall()
    return jsonify({"solicitudes": [dict(r) for r in rows]})


# ── POST /api/wallet/admin/aprobar ─────────────────────────────────────────────
@wallet_bp.route("/api/wallet/admin/aprobar", methods=["POST"])
@_admin_required
def admin_aprobar():
    """
    Aprueba o rechaza una solicitud de depósito/retiro.
    Body: { solicitud_id, accion: 'APROBAR'|'RECHAZAR', nota }
    """
    data   = request.get_json() or {}
    sol_id = int(data.get("solicitud_id", 0))
    accion = data.get("accion", "").upper()
    nota   = data.get("nota", "")

    if accion not in ("APROBAR", "RECHAZAR"):
        return jsonify({"error": "accion debe ser APROBAR o RECHAZAR"}), 400

    with get_wdb() as db:
        sol = db.execute("SELECT * FROM solicitudes WHERE id=?", (sol_id,)).fetchone()
        if not sol:
            return jsonify({"error": "Solicitud no encontrada"}), 404
        if sol["estado"] != "PENDIENTE":
            return jsonify({"error": f"Solicitud ya fue {sol['estado']}"}), 400

        if accion == "APROBAR":
            w = _get_or_create_wallet(db, sol["user_id"], sol["username"])

            if sol["tipo"] == "DEPOSITO":
                db.execute(
                    "UPDATE wallets SET saldo=saldo+?, updated_at=datetime('now') WHERE user_id=?",
                    (sol["monto"], sol["user_id"])
                )
                _registrar_movimiento(
                    db, sol["user_id"], "DEPOSITO", sol["monto"],
                    f"Depósito aprobado vía {sol['metodo']} · ref: {sol['referencia']}"
                )

            elif sol["tipo"] == "RETIRO":
                if w["saldo"] < sol["monto"]:
                    return jsonify({"error": "Saldo insuficiente para aprobar retiro"}), 400
                db.execute(
                    "UPDATE wallets SET saldo=saldo-?, updated_at=datetime('now') WHERE user_id=?",
                    (sol["monto"], sol["user_id"])
                )
                _registrar_movimiento(
                    db, sol["user_id"], "RETIRO", -sol["monto"],
                    f"Retiro aprobado vía {sol['metodo']}"
                )

        nuevo_estado = "APROBADA" if accion == "APROBAR" else "RECHAZADA"
        db.execute(
            "UPDATE solicitudes SET estado=?, nota_admin=?, updated_at=datetime('now') WHERE id=?",
            (nuevo_estado, nota, sol_id)
        )
        db.commit()

    return jsonify({
        "ok":          True,
        "solicitud_id": sol_id,
        "estado":      nuevo_estado,
        "mensaje":     f"Solicitud {sol_id} → {nuevo_estado}"
    })


# ── POST /api/wallet/admin/liquidar_pick ───────────────────────────────────────
@wallet_bp.route("/api/wallet/admin/liquidar_pick", methods=["POST"])
@_admin_required
def admin_liquidar_pick():
    """
    Resuelve todos los escrows de un pick (liquidación manual).
    Body: { pick_id, resultado: 'WON'|'LOST'|'VOID' }
    Para liquidación automática usar la función liquidar_escrow() directamente.
    """
    data      = request.get_json() or {}
    pick_id   = int(data.get("pick_id", 0))
    resultado = data.get("resultado", "").upper()

    if resultado not in ("WON", "LOST", "VOID"):
        return jsonify({"error": "resultado debe ser WON, LOST o VOID"}), 400

    resultado_liq = liquidar_escrow(pick_id, resultado)

    if not resultado_liq["ok"]:
        return jsonify({"error": resultado_liq["msg"]}), 404

    return jsonify({
        "ok":            True,
        "pick_id":       pick_id,
        "resultado":     resultado,
        "liquidaciones": resultado_liq["liquidaciones"],
        "total_escrows": resultado_liq["total"],
    })


# ── GET /api/wallet/admin/dashboard ───────────────────────────────────────────
@wallet_bp.route("/api/wallet/admin/dashboard", methods=["GET"])
@_admin_required
def admin_dashboard():
    """Resumen general de la billetera para el admin."""
    with get_wdb() as db:
        total_saldos = db.execute(
            "SELECT COALESCE(SUM(saldo),0) as t, COALESCE(SUM(saldo_bloq),0) as b FROM wallets"
        ).fetchone()
        total_escrows_pend = db.execute(
            "SELECT COUNT(*) as c, COALESCE(SUM(monto),0) as m FROM escrow_entries WHERE estado='PENDING'"
        ).fetchone()
        sol_pend = db.execute(
            "SELECT COUNT(*) as c FROM solicitudes WHERE estado='PENDIENTE'"
        ).fetchone()
        wallets = db.execute(
            "SELECT username, saldo, saldo_bloq, updated_at FROM wallets ORDER BY saldo DESC"
        ).fetchall()
        ultimos_mov = db.execute(
            """SELECT m.user_id, w.username, m.tipo, m.monto, m.descripcion, m.created_at
               FROM movimientos m JOIN wallets w ON m.user_id=w.user_id
               ORDER BY m.id DESC LIMIT 30"""
        ).fetchall()

    return jsonify({
        "total_saldo_disponible": round(total_saldos["t"], 2),
        "total_saldo_bloqueado":  round(total_saldos["b"], 2),
        "escrows_pendientes":     total_escrows_pend["c"],
        "monto_en_escrow":        round(total_escrows_pend["m"], 2),
        "solicitudes_pendientes": sol_pend["c"],
        "wallets":                [dict(w) for w in wallets],
        "ultimos_movimientos":    [dict(m) for m in ultimos_mov],
    })