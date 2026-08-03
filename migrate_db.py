"""
migrate_db.py — BetSense
=========================
Agrega las tablas del Blind Engine a la DB existente.
Ejecutar UNA sola vez: python migrate_db.py

No modifica ni borra ninguna tabla existente.
Solo hace CREATE TABLE IF NOT EXISTS.
"""

import os
from sqlalchemy import create_engine, text, inspect

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///betsense.db")
engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)


def migrar():
    print("🔧 BetSense — Migración Blind Engine")
    print(f"   Base de datos: {DATABASE_URL}\n")

    inspector = inspect(engine)
    tablas_existentes = inspector.get_table_names()
    print(f"   Tablas actuales: {tablas_existentes}\n")

    with engine.connect() as conn:

        # ── predicciones_ext ─────────────────────────────────
        if "predicciones_ext" not in tablas_existentes:
            conn.execute(text("""
                CREATE TABLE predicciones_ext (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    prediccion_id       INTEGER NOT NULL UNIQUE REFERENCES predicciones(id),
                    match_date          DATETIME,
                    cutoff_date         DATETIME,

                    -- Dixon-Coles
                    rho                 REAL,
                    time_decay_xi       REAL DEFAULT 0.002,
                    lambda_h_dc         REAL,
                    lambda_a_dc         REAL,
                    prob_h_dc           REAL,
                    prob_d_dc           REAL,
                    prob_a_dc           REAL,

                    -- Cuotas bookmaker
                    bk_cuota_h          REAL,
                    bk_cuota_d          REAL,
                    bk_cuota_a          REAL,
                    bk_source           TEXT,
                    bk_prob_h           REAL,
                    bk_prob_d           REAL,
                    bk_prob_a           REAL,

                    -- Value Edge
                    edge_h              REAL,
                    edge_d              REAL,
                    edge_a              REAL,
                    mejor_value         TEXT,
                    mejor_edge          REAL,

                    -- Kelly
                    kelly_stake         REAL,
                    kelly_frac          REAL DEFAULT 0.25,
                    bankroll_ref        REAL,
                    stake_sugerido      REAL,

                    -- Evaluación post-partido
                    rps                 REAL,
                    brier               REAL,
                    log_loss            REAL,
                    outcome_real        TEXT,
                    outcome_pred        TEXT,
                    acerto_1x2          INTEGER,
                    acerto_score        INTEGER,
                    delta_goles         REAL,

                    -- Contexto
                    semaforo_score      INTEGER,
                    indicadores_json    TEXT
                )
            """))
            print("   ✅ Creada tabla: predicciones_ext")
        else:
            print("   ℹ️  Ya existe: predicciones_ext")

        # ── cuotas_mercado ────────────────────────────────────
        if "cuotas_mercado" not in tablas_existentes:
            conn.execute(text("""
                CREATE TABLE cuotas_mercado (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    prediccion_id   INTEGER NOT NULL REFERENCES predicciones(id),
                    timestamp       DATETIME,
                    mercado         TEXT,
                    seleccion       TEXT,
                    cuota_bk        REAL,
                    prob_modelo     REAL,
                    prob_bk         REAL,
                    edge            REAL,
                    value_bet       INTEGER DEFAULT 0,
                    resultado       INTEGER
                )
            """))
            print("   ✅ Creada tabla: cuotas_mercado")
        else:
            print("   ℹ️  Ya existe: cuotas_mercado")

        conn.commit()

    # ── Verificar columnas faltantes en predicciones ──────────
    # (por si la DB fue creada con una versión antigua)
    cols_predicciones = [c["name"] for c in inspector.get_columns("predicciones")]
    columnas_nuevas = {
        "resultado_real_h": "INTEGER",
        "resultado_real_a": "INTEGER",
        "acertado": "INTEGER",
    }
    with engine.connect() as conn:
        for col, tipo in columnas_nuevas.items():
            if col not in cols_predicciones:
                conn.execute(text(
                    f"ALTER TABLE predicciones ADD COLUMN {col} {tipo}"
                ))
                print(f"   ✅ Columna agregada a predicciones: {col}")
                conn.commit()

    print("\n✅ Migración completada. Tablas actualizadas:")
    inspector2 = inspect(engine)
    for t in inspector2.get_table_names():
        cols = inspector2.get_columns(t)
        print(f"   - {t} ({len(cols)} columnas)")


if __name__ == "__main__":
    migrar()