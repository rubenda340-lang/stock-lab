"""
Módulo de conexión y gestión de la base de datos SQLite de StockLab.

Contiene la conexión centralizada y la función initialize_database()
que crea todas las tablas automáticamente al arrancar la aplicación.
"""

import logging
import sqlite3
import os
import streamlit as st

logger = logging.getLogger(__name__)

# Ruta absoluta al archivo de base de datos (junto al proyecto)
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "stocklab.db")


def get_connection():
    """
    Retorna una conexión a la base de datos SQLite.
    Activa row_factory para acceder a columnas por nombre,
    habilita claves foráneas, establece busy_timeout a 30000 ms (30 s)
    y activa journal_mode=WAL de forma idempotente.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
    except Exception as e:
        logger.warning("No se pudo activar PRAGMA journal_mode=WAL: %s", e)
    return conn


@st.cache_data(ttl=60, show_spinner=False)
def query_all(sql, params=None):
    """
    Ejecuta una consulta y devuelve los resultados como lista de diccionarios.
    Gestiona apertura y cierre seguro de la conexión.
    Caché de 60 segundos para evitar consultas repetidas durante navegación rápida.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql, params or ())
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


@st.cache_data(ttl=60, show_spinner=False)
def query_val(sql, params=None):
    """
    Ejecuta una consulta y devuelve el valor de la primera columna
    de la primera fila. Útil para SELECT COUNT(*), MAX(), etc.
    Retorna None si no hay resultados.
    Caché de 60 segundos para optimizar rendimiento.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql, params or ())
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def initialize_database():
    """
    Crea todas las tablas de la aplicación si no existen.
    Se invoca automáticamente al arrancar la aplicación.

    Tablas creadas:
        - mercados
        - empresas
        - watchlist
        - cartera
        - notas
        - diario
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # ── Tabla: mercados (planificada para futuras vistas) ────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mercados (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre      TEXT    NOT NULL UNIQUE,
                pais        TEXT,
                divisa      TEXT,
                descripcion TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Tabla: empresas ──────────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS empresas (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre      TEXT    NOT NULL,
                ticker      TEXT    NOT NULL UNIQUE,
                mercado     TEXT,
                sector      TEXT,
                es_ibex35   INTEGER DEFAULT 0,
                activa      INTEGER DEFAULT 1,
                descripcion TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Tabla: watchlist ─────────────────────────────────────────────
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                ticker          TEXT PRIMARY KEY,
                fecha_adicion   TEXT    NOT NULL,
                precio_adicion  REAL    NOT NULL,
                precio_objetivo REAL,
                motivo          TEXT,
                tipo_objetivo   TEXT    DEFAULT 'COMPRA_EN_CAIDA'
            )
        """)

        # ── Tabla: cartera ───────────────────────────────────────────────
        # ACTUALIZACIÓN V2.0: Ampliar tipos de operación y añadir liquidez
        
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cartera (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                empresa_id      INTEGER,
                tipo            TEXT    NOT NULL CHECK(tipo IN ('COMPRA', 'VENTA', 'DIVIDENDO', 'INGRESO_EFECTIVO', 'RETIRADA_EFECTIVO', 'COMISION')),
                cantidad        REAL    DEFAULT 0,
                precio_unitario REAL    DEFAULT 0,
                importe_total   REAL    DEFAULT 0,
                comision        REAL    DEFAULT 0,
                fecha           TEXT    NOT NULL,
                notas           TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (empresa_id) REFERENCES empresas(id)
                    ON DELETE CASCADE
            )
        """)
        
        # Crear índice para mejorar rendimiento en consultas frecuentes
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_cartera_fecha 
            ON cartera(fecha DESC)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_cartera_tipo 
            ON cartera(tipo)
        """)

        # ── Tabla: notas (planificada para futuras vistas) ───────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notas (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                empresa_id  INTEGER,
                titulo      TEXT    NOT NULL,
                contenido   TEXT,
                categoria   TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (empresa_id) REFERENCES empresas(id)
                    ON DELETE SET NULL
            )
        """)

        # ── Tabla: diario ────────────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS diario (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha           TEXT    NOT NULL,
                titulo          TEXT    NOT NULL,
                contenido       TEXT,
                sentimiento     TEXT    CHECK(sentimiento IN ('ALCISTA', 'BAJISTA', 'NEUTRAL')),
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Tabla: eventos ──────────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS eventos (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT    NOT NULL,
                empresa         TEXT    NOT NULL,
                evento          TEXT    NOT NULL,
                fecha           TEXT    NOT NULL,
                importancia     TEXT    NOT NULL DEFAULT 'media',
                tipo            TEXT    NOT NULL,
                nivel_confianza INTEGER NOT NULL DEFAULT 70,
                ex_dividend_date TEXT,
                UNIQUE(ticker, evento, fecha) ON CONFLICT REPLACE
            )
        """)

        # Añadir columnas a tabla existente si no existen (migración)
        for col, ddl in [
            ("nivel_confianza", "INTEGER NOT NULL DEFAULT 70"),
            ("ex_dividend_date", "TEXT"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE eventos ADD COLUMN {col} {ddl}")
            except Exception:
                pass  # La columna ya existe

        # ── Tabla: ranking_estado ─────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ranking_estado (
                ticker          TEXT PRIMARY KEY,
                posicion        INTEGER NOT NULL,
                score           REAL NOT NULL,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Tabla: ia_consumo_diario (Tokens y peticiones diarias por modelo)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ia_consumo_diario (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha             TEXT NOT NULL,
                modelo            TEXT NOT NULL,
                tokens_prompt     INTEGER DEFAULT 0,
                tokens_respuesta  INTEGER DEFAULT 0,
                total_tokens      INTEGER DEFAULT 0,
                timestamp         DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_ia_consumo_fecha_modelo
            ON ia_consumo_diario(fecha, modelo)
        """)

        # ── Tabla: ia_cuota_agotada (Registro de cuotas diarias alcanzadas en Google)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ia_cuota_agotada (
                fecha             TEXT NOT NULL,
                modelo            TEXT NOT NULL,
                motivo            TEXT,
                timestamp         DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (fecha, modelo)
            )
        """)

        conn.commit()
    finally:
        conn.close()

    # Poblar datos iniciales
    seed_companies()


def seed_companies():
    """
    Inserta automáticamente el catálogo oficial de empresas del BME / Mercado Continuo español.

    Estrategia de inserción segura:
        - Usa INSERT OR IGNORE basado en el campo ticker (UNIQUE).
        - Si el script se ejecuta múltiples veces, NO duplica registros.
        - Los IDs existentes vinculados a operaciones en la tabla cartera
          permanecen intactos y funcionales.
        - Las empresas nuevas se añaden sin afectar a las ya existentes.

    Catálogo: IBEX 35 + Exclusiones/Suspendidas + Mercado Continuo.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # ── Limpieza selectiva de tickers inválidos / erróneos ─────────
        # Elimina registros residuales de ejecuciones anteriores que causan HTTP 404
        tickers_invalidos = (
            'BMARCH.MC', 'BCO.MC', 'CFR.MC', 'COP.MC', 'DOG.MC', 'EENER.MC',
            'ERC.MC', 'EUS.MC', 'FAC.MC', 'GMP.MC', 'GLB.MC', 'GRL.MC',
            'PRISA.MC', 'SAP.MC', 'HOLA.MC', 'LNE.MC',
            'MTX.MC', 'NHH.MC', 'NOR.MC', 'NYES.MC', 'QUO.MC', 'UNA.MC',
            'SEN.MC', 'SNI.MC', 'UE.MC', 'VIT.MC',
            'GCO.MC', 'INYP.MC'
        )
        cursor.execute(f"DELETE FROM empresas WHERE ticker IN {tickers_invalidos}")
        deleted = cursor.rowcount
        if deleted > 0:
            print(f"[DB] Limpieza DB: {deleted} tickers invalidos eliminados.")
        conn.commit()

        empresas_oficiales = [
            # --- IBEX 35 (35 Componentes) ---
            {"ticker": "ANA.MC", "nombre": "Acciona", "sector": "Construcción e Infraestructuras", "es_ibex35": 1, "activa": 1},
            {"ticker": "ANE.MC", "nombre": "Acciona Energía", "sector": "Energías Renovables", "es_ibex35": 1, "activa": 1},
            {"ticker": "ACX.MC", "nombre": "Acerinox", "sector": "Siderurgia y Metalúrgico", "es_ibex35": 1, "activa": 1},
            {"ticker": "ACS.MC", "nombre": "ACS", "sector": "Construcción e Infraestructuras", "es_ibex35": 1, "activa": 1},
            {"ticker": "AENA.MC", "nombre": "Aena", "sector": "Transporte y Turismo", "es_ibex35": 1, "activa": 1},
            {"ticker": "AIR.MC", "nombre": "Airbus SE", "sector": "Industrial y Aeroespacial", "es_ibex35": 0, "activa": 1},
            {"ticker": "AMS.MC", "nombre": "Amadeus", "sector": "Tecnología", "es_ibex35": 1, "activa": 1},
            {"ticker": "MTS.MC", "nombre": "ArcelorMittal", "sector": "Siderurgia y Metalúrgico", "es_ibex35": 1, "activa": 1},
            {"ticker": "SAN.MC", "nombre": "Banco Santander", "sector": "Financiero y Banca", "es_ibex35": 1, "activa": 1},
            {"ticker": "SAB.MC", "nombre": "Banco Sabadell", "sector": "Financiero y Banca", "es_ibex35": 1, "activa": 1},
            {"ticker": "BKT.MC", "nombre": "Bankinter", "sector": "Financiero y Banca", "es_ibex35": 1, "activa": 1},
            {"ticker": "BBVA.MC", "nombre": "BBVA", "sector": "Financiero y Banca", "es_ibex35": 1, "activa": 1},
            {"ticker": "CABK.MC", "nombre": "CaixaBank", "sector": "Financiero y Banca", "es_ibex35": 1, "activa": 1},
            {"ticker": "CLNX.MC", "nombre": "Cellnex", "sector": "Telecomunicaciones", "es_ibex35": 1, "activa": 1},
            {"ticker": "COL.MC", "nombre": "Colonial SFL", "sector": "Inmobiliario (SOCIMI)", "es_ibex35": 1, "activa": 1},
            {"ticker": "ENG.MC", "nombre": "Enagás", "sector": "Energía y Utilities", "es_ibex35": 1, "activa": 1},
            {"ticker": "ELE.MC", "nombre": "Endesa", "sector": "Energía y Utilities", "es_ibex35": 1, "activa": 1},
            {"ticker": "FER.MC", "nombre": "Ferrovial NV", "sector": "Construcción e Infraestructuras", "es_ibex35": 1, "activa": 1},
            {"ticker": "FDR.MC", "nombre": "Fluidra", "sector": "Industrial", "es_ibex35": 1, "activa": 1},
            {"ticker": "GRF.MC", "nombre": "Grifols Cl.A", "sector": "Farmacéutico y Salud", "es_ibex35": 1, "activa": 1},
            {"ticker": "IAG.MC", "nombre": "IAG", "sector": "Transporte y Turismo", "es_ibex35": 1, "activa": 1},
            {"ticker": "IBE.MC", "nombre": "Iberdrola", "sector": "Energía y Utilities", "es_ibex35": 1, "activa": 1},
            {"ticker": "ITX.MC", "nombre": "Inditex", "sector": "Consumo y Retail", "es_ibex35": 1, "activa": 1},
            {"ticker": "IDR.MC", "nombre": "Indra A", "sector": "Tecnología y Defensa", "es_ibex35": 1, "activa": 1},
            {"ticker": "LOG.MC", "nombre": "Logista", "sector": "Logística y Distribución", "es_ibex35": 1, "activa": 1},
            {"ticker": "MAP.MC", "nombre": "Mapfre", "sector": "Seguros", "es_ibex35": 1, "activa": 1},
            {"ticker": "MEL.MC", "nombre": "Meliá Hotels", "sector": "Transporte y Turismo", "es_ibex35": 0, "activa": 1},
            {"ticker": "MRL.MC", "nombre": "Merlin", "sector": "Inmobiliario (SOCIMI)", "es_ibex35": 1, "activa": 1},
            {"ticker": "NTGY.MC", "nombre": "Naturgy", "sector": "Energía y Utilities", "es_ibex35": 1, "activa": 1},
            {"ticker": "PUIG.MC", "nombre": "Puig Brands", "sector": "Consumo y Lujo", "es_ibex35": 1, "activa": 1},
            {"ticker": "RED.MC", "nombre": "Redeia", "sector": "Energía y Utilities", "es_ibex35": 1, "activa": 1},
            {"ticker": "REP.MC", "nombre": "Repsol", "sector": "Petróleo y Gas", "es_ibex35": 1, "activa": 1},
            {"ticker": "ROVI.MC", "nombre": "Rovi", "sector": "Farmacéutico y Salud", "es_ibex35": 1, "activa": 1},
            {"ticker": "SCYR.MC", "nombre": "Sacyr", "sector": "Construcción e Infraestructuras", "es_ibex35": 1, "activa": 1},
            {"ticker": "SLR.MC", "nombre": "Solaria", "sector": "Energías Renovables", "es_ibex35": 1, "activa": 1},
            {"ticker": "TEF.MC", "nombre": "Telefónica", "sector": "Telecomunicaciones", "es_ibex35": 1, "activa": 1},
            {"ticker": "UNI.MC", "nombre": "Unicaja", "sector": "Financiero y Banca", "es_ibex35": 1, "activa": 1},
            # --- EXCLUSIONES O SUSPENDIDAS (activa=0) ---
            {"ticker": "CAM.MC", "nombre": "CAM", "sector": "Financiero (Suspendida)", "es_ibex35": 0, "activa": 0},
            {"ticker": "CLEOP.MC", "nombre": "Cleop", "sector": "Construcción (Suspendida)", "es_ibex35": 0, "activa": 0},
            {"ticker": "CIR.MC", "nombre": "Cirsa", "sector": "Entretenimiento (No Cotiza)", "es_ibex35": 0, "activa": 0},
            {"ticker": "HBX.MC", "nombre": "HBX Group", "sector": "Turismo (No Cotiza)", "es_ibex35": 0, "activa": 0},
            {"ticker": "INV.MC", "nombre": "Innovative", "sector": "Tecnología (Sin Datos)", "es_ibex35": 0, "activa": 0},
            {"ticker": "TSK.MC", "nombre": "TSK", "sector": "Ingeniería (No Cotiza)", "es_ibex35": 0, "activa": 0}
        ]

        # ── Extensión: Mercado Continuo Activo ──────────────────────────
        empresas_oficiales.extend([
            {"ticker": "ADZ.MC", "nombre": "Adolfo Dguez", "sector": "Moda y Retail", "es_ibex35": 0, "activa": 1},
            {"ticker": "AEDAS.MC", "nombre": "Aedas", "sector": "Inmobiliario y Promoción", "es_ibex35": 0, "activa": 1},
            {"ticker": "AI.MC", "nombre": "Airtificial", "sector": "Tecnología e IA", "es_ibex35": 0, "activa": 1},
            {"ticker": "ALNT.MC", "nombre": "Alantra", "sector": "Financiero y Carteras", "es_ibex35": 0, "activa": 1},
            {"ticker": "ALM.MC", "nombre": "Almirall", "sector": "Farmacéutico y Salud", "es_ibex35": 0, "activa": 1},
            {"ticker": "AMP.MC", "nombre": "Amper", "sector": "Tecnología e Ingeniería", "es_ibex35": 0, "activa": 1},
            {"ticker": "EAT.MC", "nombre": "Amrest", "sector": "Hostelería y Restauración", "es_ibex35": 0, "activa": 1},
            {"ticker": "APAM.MC", "nombre": "Aperam", "sector": "Siderurgia y Metalúrgico", "es_ibex35": 0, "activa": 1},
            {"ticker": "ARM.MC", "nombre": "Árima", "sector": "Inmobiliario (SOCIMI)", "es_ibex35": 0, "activa": 1},
            {"ticker": "ART.MC", "nombre": "Arteche", "sector": "Industrial y Energía", "es_ibex35": 0, "activa": 1},
            {"ticker": "A3M.MC", "nombre": "Atresmedia", "sector": "Medios y Entretenimiento", "es_ibex35": 0, "activa": 1},
            {"ticker": "ATRY.MC", "nombre": "Atrys Health", "sector": "Biotecnología y Salud", "es_ibex35": 0, "activa": 1},
            {"ticker": "ADX.MC", "nombre": "Audax Renov.", "sector": "Energías Renovables", "es_ibex35": 0, "activa": 1},
            {"ticker": "CAF.MC", "nombre": "Aux.Ferrocar", "sector": "Industrial y Transporte", "es_ibex35": 0, "activa": 1},
            {"ticker": "AZK.MC", "nombre": "Azkoyen", "sector": "Industrial y Maquinaria", "es_ibex35": 0, "activa": 1},
            {"ticker": "CBAV.MC", "nombre": "Baviera", "sector": "Farmacéutico y Salud", "es_ibex35": 0, "activa": 1},
            {"ticker": "BKY.MC", "nombre": "Berkeley", "sector": "Minería y Recursos", "es_ibex35": 0, "activa": 1},
            {"ticker": "RIO.MC", "nombre": "Bo.Riojanas", "sector": "Alimentación y Bodegas", "es_ibex35": 0, "activa": 1},
            {"ticker": "CASH.MC", "nombre": "Cash", "sector": "Servicios de Seguridad", "es_ibex35": 0, "activa": 1},
            {"ticker": "CCEP.MC", "nombre": "CCEP", "sector": "Consumo y Bebidas", "es_ibex35": 0, "activa": 1},
            {"ticker": "CEV.MC", "nombre": "Cevasa", "sector": "Inversión y Holding", "es_ibex35": 0, "activa": 1},
            {"ticker": "CIE.MC", "nombre": "Cie Automot.", "sector": "Automoción e Industrial", "es_ibex35": 0, "activa": 1},
            {"ticker": "COXG.MC", "nombre": "Cox Group", "sector": "Energía y Utilities", "es_ibex35": 0, "activa": 1},
            {"ticker": "MDF.MC", "nombre": "D.Felguera", "sector": "Ingeniería y Construcción", "es_ibex35": 0, "activa": 1},
            {"ticker": "OLE.MC", "nombre": "Deoleo", "sector": "Alimentación y Aceites", "es_ibex35": 0, "activa": 1},
            {"ticker": "DESA.MC", "nombre": "Desa", "sector": "Industrial", "es_ibex35": 0, "activa": 1},
            {"ticker": "DIA.MC", "nombre": "Dia", "sector": "Distribución Alimentaria", "es_ibex35": 0, "activa": 1},
            {"ticker": "DOM.MC", "nombre": "Dominion", "sector": "Servicios y Tecnología", "es_ibex35": 0, "activa": 1},
            {"ticker": "EBRO.MC", "nombre": "Ebro Foods", "sector": "Consumo y Retail", "es_ibex35": 0, "activa": 1},
            {"ticker": "ENER.MC", "nombre": "Ecoener", "sector": "Energías Renovables", "es_ibex35": 0, "activa": 1},
            {"ticker": "EDR.MC", "nombre": "eDreams", "sector": "Transporte y Turismo", "es_ibex35": 0, "activa": 1},
            {"ticker": "ENO.MC", "nombre": "Elecnor", "sector": "Ingeniería y Energía", "es_ibex35": 0, "activa": 1},
            {"ticker": "ENC.MC", "nombre": "Ence", "sector": "Industrial y Recursos", "es_ibex35": 0, "activa": 1},
            {"ticker": "ECR.MC", "nombre": "Ercros", "sector": "Químico", "es_ibex35": 0, "activa": 1},
            {"ticker": "EZE.MC", "nombre": "Ezentis", "sector": "Telecomunicaciones e Infraestructuras", "es_ibex35": 0, "activa": 1},
            {"ticker": "FAE.MC", "nombre": "Faes Farma", "sector": "Farmacéutico y Salud", "es_ibex35": 0, "activa": 1},
            {"ticker": "FCC.MC", "nombre": "FCC", "sector": "Construcción y Servicios Públicos", "es_ibex35": 0, "activa": 1},
            {"ticker": "GAM.MC", "nombre": "G.A.M.", "sector": "Industrial y Servicios", "es_ibex35": 0, "activa": 1},
            {"ticker": "GEST.MC", "nombre": "Gestamp", "sector": "Automoción e Industrial", "es_ibex35": 0, "activa": 1},
            {"ticker": "GRE.MC", "nombre": "Grenergy", "sector": "Energías Renovables", "es_ibex35": 0, "activa": 1},
            {"ticker": "GRF-P.MC", "nombre": "Grifols Cl.B", "sector": "Farmacéutico y Salud", "es_ibex35": 0, "activa": 1},
            {"ticker": "IBG.MC", "nombre": "Iberpapel", "sector": "Forestal y Papel", "es_ibex35": 0, "activa": 1},
            {"ticker": "ISUR.MC", "nombre": "Inm.Del Sur", "sector": "Inmobiliario y Promoción", "es_ibex35": 0, "activa": 1},
            {"ticker": "IMC.MC", "nombre": "Inmocemento", "sector": "Materiales de Construcción", "es_ibex35": 0, "activa": 1},
            {"ticker": "IZER.MC", "nombre": "Izertis", "sector": "Consultoría Tecnológica", "es_ibex35": 0, "activa": 1},
            {"ticker": "LIB.MC", "nombre": "Libertas 7", "sector": "Inversión y Holding", "es_ibex35": 0, "activa": 1},
            {"ticker": "LDA.MC", "nombre": "Linea Direct", "sector": "Seguros", "es_ibex35": 0, "activa": 1},
            {"ticker": "LGT.MC", "nombre": "Lingotes Esp", "sector": "Industrial y Fundición", "es_ibex35": 0, "activa": 1},
            {"ticker": "MVC.MC", "nombre": "Metrovacesa", "sector": "Inmobiliario y Promoción", "es_ibex35": 0, "activa": 1},
            {"ticker": "MFEA.MC", "nombre": "MFE", "sector": "Medios y Entretenimiento", "es_ibex35": 0, "activa": 1},
            {"ticker": "MCM.MC", "nombre": "Miquel Cost.", "sector": "Forestal y Papel", "es_ibex35": 0, "activa": 1},
            {"ticker": "MTB.MC", "nombre": "Montebalito", "sector": "Inmobiliario y Promoción", "es_ibex35": 0, "activa": 1},
            {"ticker": "NTH.MC", "nombre": "Naturhouse", "sector": "Salud y Nutrición", "es_ibex35": 0, "activa": 1},
            {"ticker": "HOME.MC", "nombre": "Neinor", "sector": "Inmobiliario y Promoción", "es_ibex35": 0, "activa": 1},
            {"ticker": "NXT.MC", "nombre": "Nextil", "sector": "Textil e Industrial", "es_ibex35": 0, "activa": 1},
            {"ticker": "NEA.MC", "nombre": "Nico.Correa", "sector": "Industrial y Maquinaria", "es_ibex35": 0, "activa": 1},
            {"ticker": "NYE.MC", "nombre": "Nyesa", "sector": "Inmobiliario y Promoción", "es_ibex35": 0, "activa": 1},
            {"ticker": "OHLA.MC", "nombre": "Ohla", "sector": "Construcción e Infraestructuras", "es_ibex35": 0, "activa": 1},
            {"ticker": "ORY.MC", "nombre": "Oryzon", "sector": "Biotecnología y Salud", "es_ibex35": 0, "activa": 1},
            {"ticker": "PVA.MC", "nombre": "Pescanova", "sector": "Alimentación y Pesca", "es_ibex35": 0, "activa": 1},
            {"ticker": "PHM.MC", "nombre": "Pharma Mar", "sector": "Biotecnología y Salud", "es_ibex35": 0, "activa": 1},
            {"ticker": "PRM.MC", "nombre": "Prim", "sector": "Suministros Médicos", "es_ibex35": 0, "activa": 1},
            {"ticker": "PRS.MC", "nombre": "Prisa", "sector": "Medios y Entretenimiento", "es_ibex35": 0, "activa": 1},
            {"ticker": "PSG.MC", "nombre": "Prosegur", "sector": "Servicios de Seguridad", "es_ibex35": 0, "activa": 1},
            {"ticker": "RLIA.MC", "nombre": "Realia", "sector": "Inmobiliario y Promoción", "es_ibex35": 0, "activa": 1},
            {"ticker": "RJF.MC", "nombre": "Reig Jofre", "sector": "Farmacéutico y Salud", "es_ibex35": 0, "activa": 1},
            {"ticker": "R4.MC", "nombre": "Renta 4", "sector": "Financiero y Banca", "es_ibex35": 0, "activa": 1},
            {"ticker": "REN.MC", "nombre": "Renta Corp.", "sector": "Inmobiliario y Promoción", "es_ibex35": 0, "activa": 1},
            {"ticker": "GSJ.MC", "nombre": "San Jose", "sector": "Construcción e Infraestructuras", "es_ibex35": 0, "activa": 1},
            {"ticker": "SOL.MC", "nombre": "Soltec", "sector": "Energías Renovables", "es_ibex35": 0, "activa": 1},
            {"ticker": "SQRL.MC", "nombre": "Squirrel", "sector": "Medios y Comunicación", "es_ibex35": 0, "activa": 1},
            {"ticker": "TLGO.MC", "nombre": "Talgo", "sector": "Industrial y Transporte", "es_ibex35": 0, "activa": 1},
            {"ticker": "TRE.MC", "nombre": "Tec.Reunidas", "sector": "Ingeniería y Energía", "es_ibex35": 0, "activa": 1},
            {"ticker": "TUB.MC", "nombre": "Tubacex", "sector": "Industrial y Metalúrgico", "es_ibex35": 0, "activa": 1},
            {"ticker": "TRG.MC", "nombre": "Tubos Reuni.", "sector": "Industrial y Metalúrgico", "es_ibex35": 0, "activa": 1},
            {"ticker": "UBS.MC", "nombre": "Urbas", "sector": "Inmobiliario y Construcción", "es_ibex35": 0, "activa": 1},
            {"ticker": "VID.MC", "nombre": "Vidrala", "sector": "Industrial y Envases", "es_ibex35": 0, "activa": 1},
            {"ticker": "VIS.MC", "nombre": "Viscofan", "sector": "Consumo Defensivo", "es_ibex35": 0, "activa": 1},
            {"ticker": "VOC.MC", "nombre": "Vocento", "sector": "Medios y Entretenimiento", "es_ibex35": 0, "activa": 1}
        ])

        # ── Inserción segura en SQLite ───────────────────────────────────
        # INSERT OR IGNORE evita duplicados por ticker (UNIQUE)
        # Los IDs existentes vinculados a cartera permanecen intactos
        for emp in empresas_oficiales:
            cursor.execute("""
                INSERT OR IGNORE INTO empresas (ticker, nombre, sector, es_ibex35, mercado, activa)
                VALUES (?, ?, ?, ?, 'BME', ?)
            """, (emp['ticker'], emp['nombre'], emp['sector'], emp['es_ibex35'], emp['activa']))

        inserted = cursor.rowcount
        if inserted > 0:
            print(f"[OK] Seeding: {inserted} nuevas empresas insertadas en el catalogo BME.")

        conn.commit()
    except Exception as e:
        print(f"[ERROR] Error en seed_companies: {e}")
        conn.rollback()
    finally:
        conn.close()


# Alias retrocompatible — app.py importa init_db()
init_db = initialize_database
