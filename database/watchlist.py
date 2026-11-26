"""
Funciones de gestión de la Watchlist para StockLab.

Proporciona operaciones CRUD para la tabla watchlist en SQLite,
permitiendo añadir, eliminar y consultar empresas en seguimiento.
"""

import pandas as pd
from datetime import datetime
from .connection import get_connection
from services.auth_service import (
    get_current_user,
    is_cloud_user_authenticated,
    supabase_rest,
)


def agregar_a_watchlist(ticker: str, precio_actual: float, motivo: str = "", precio_objetivo: float = None, tipo_objetivo: str = "COMPRA_EN_CAIDA"):
    """
    Añade una empresa a la watchlist o actualiza su objetivo/motivo si ya existía,
    preservando su fecha_adicion y precio_adicion originales para no perder el histórico.
    """
    ticker_norm = str(ticker or "").strip().upper()
    if not ticker_norm:
        return False

    if is_cloud_user_authenticated():
        user = get_current_user()
        uid = user["id"]
        ok_q, rows = supabase_rest(
            "GET",
            "watchlist",
            params={"user_id": f"eq.{uid}", "ticker": f"eq.{ticker_norm}", "limit": 1},
        )
        if ok_q and isinstance(rows, list) and rows:
            existente = rows[0]
            payload = {
                "precio_objetivo": precio_objetivo,
                "motivo": motivo,
                "tipo_objetivo": tipo_objetivo,
            }
            p_add = existente.get("precio_adicion")
            if p_add is None or float(p_add) <= 0:
                payload["precio_adicion"] = float(precio_actual)
            ok_u, _ = supabase_rest(
                "PATCH",
                "watchlist",
                params={"user_id": f"eq.{uid}", "ticker": f"eq.{ticker_norm}"},
                payload=payload,
                prefer="return=minimal",
            )
            return ok_u
        payload_ins = {
            "user_id": uid,
            "ticker": ticker_norm,
            "fecha_adicion": datetime.now().strftime("%Y-%m-%d"),
            "precio_adicion": float(precio_actual),
            "precio_objetivo": precio_objetivo,
            "motivo": motivo,
            "tipo_objetivo": tipo_objetivo,
        }
        ok_i, _ = supabase_rest("POST", "watchlist", payload=payload_ins, prefer="return=minimal")
        return ok_i

    conn = get_connection()
    try:
        cursor = conn.cursor()
        existente = cursor.execute(
            "SELECT ticker, precio_adicion FROM watchlist WHERE UPPER(ticker) = ?",
            (ticker_norm,),
        ).fetchone()

        if existente:
            precio_add_actual = existente[1]
            if precio_add_actual is None or float(precio_add_actual) <= 0:
                cursor.execute(
                    """
                    UPDATE watchlist
                    SET precio_adicion = ?, precio_objetivo = ?, motivo = ?, tipo_objetivo = ?
                    WHERE UPPER(ticker) = ?
                    """,
                    (float(precio_actual), precio_objetivo, motivo, tipo_objetivo, ticker_norm),
                )
            else:
                cursor.execute(
                    """
                    UPDATE watchlist
                    SET precio_objetivo = ?, motivo = ?, tipo_objetivo = ?
                    WHERE UPPER(ticker) = ?
                    """,
                    (precio_objetivo, motivo, tipo_objetivo, ticker_norm),
                )
        else:
            fecha_hoy = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                """
                INSERT INTO watchlist
                (ticker, fecha_adicion, precio_adicion, precio_objetivo, motivo, tipo_objetivo)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ticker_norm, fecha_hoy, float(precio_actual), precio_objetivo, motivo, tipo_objetivo),
            )

        conn.commit()
        return True
    except Exception as e:
        raise e
    finally:
        conn.close()


def actualizar_watchlist(ticker: str, precio_objetivo: float = None, tipo_objetivo: str = "COMPRA_EN_CAIDA", motivo: str = "") -> bool:
    """
    Actualiza precio objetivo, estrategia y motivo de un activo existente en la watchlist
    preservando intactos su fecha_adicion y precio_adicion originales.
    """
    ticker_norm = str(ticker or "").strip().upper()
    if not ticker_norm:
        return False

    if is_cloud_user_authenticated():
        user = get_current_user()
        ok, _ = supabase_rest(
            "PATCH",
            "watchlist",
            params={"user_id": f"eq.{user['id']}", "ticker": f"eq.{ticker_norm}"},
            payload={
                "precio_objetivo": precio_objetivo,
                "tipo_objetivo": tipo_objetivo,
                "motivo": motivo,
            },
            prefer="return=minimal",
        )
        return ok

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE watchlist
            SET precio_objetivo = ?, tipo_objetivo = ?, motivo = ?
            WHERE UPPER(ticker) = ?
            """,
            (precio_objetivo, tipo_objetivo, motivo, ticker_norm),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        print(f"Error al actualizar watchlist: {e}")
        return False
    finally:
        conn.close()


def eliminar_de_watchlist(ticker: str):
    """
    Elimina una empresa de la watchlist (insensible a mayúsculas/espacios).
    """
    ticker_norm = str(ticker or "").strip().upper()
    if not ticker_norm:
        return False

    if is_cloud_user_authenticated():
        user = get_current_user()
        ok, _ = supabase_rest(
            "DELETE",
            "watchlist",
            params={"user_id": f"eq.{user['id']}", "ticker": f"eq.{ticker_norm}"},
            prefer="return=minimal",
        )
        return ok

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM watchlist WHERE UPPER(ticker) = ?", (ticker_norm,))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        print(f"Error al eliminar de watchlist: {e}")
        return False
    finally:
        conn.close()


def obtener_watchlist() -> pd.DataFrame:
    """
    Obtiene todas las empresas de la watchlist como DataFrame.
    """
    if is_cloud_user_authenticated():
        user = get_current_user()
        ok, rows = supabase_rest(
            "GET",
            "watchlist",
            params={
                "user_id": f"eq.{user['id']}",
                "select": "ticker,fecha_adicion,precio_adicion,precio_objetivo,motivo,tipo_objetivo",
                "order": "fecha_adicion.desc",
            },
        )
        if ok and isinstance(rows, list) and rows:
            return pd.DataFrame(rows)
        return pd.DataFrame(columns=[
            "ticker", "fecha_adicion", "precio_adicion",
            "precio_objetivo", "motivo", "tipo_objetivo",
        ])

    conn = get_connection()
    try:
        query = """
            SELECT 
                ticker,
                fecha_adicion,
                precio_adicion,
                precio_objetivo,
                motivo,
                tipo_objetivo
            FROM watchlist
            ORDER BY fecha_adicion DESC
        """
        df = pd.read_sql_query(query, conn)
        return df
    except Exception as e:
        print(f"Error al obtener watchlist: {e}")
        return pd.DataFrame()
    finally:
        conn.close()


def existe_en_watchlist(ticker: str) -> bool:
    """
    Verifica si un ticker ya está en la watchlist (insensible a mayúsculas/espacios).
    """
    ticker_norm = str(ticker or "").strip().upper()
    if not ticker_norm:
        return False

    if is_cloud_user_authenticated():
        user = get_current_user()
        ok, rows = supabase_rest(
            "GET",
            "watchlist",
            params={"user_id": f"eq.{user['id']}", "ticker": f"eq.{ticker_norm}", "select": "ticker", "limit": 1},
        )
        return bool(ok and isinstance(rows, list) and len(rows) > 0)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM watchlist WHERE UPPER(ticker) = ?", (ticker_norm,))
        count = cursor.fetchone()[0]
        return count > 0
    except Exception as e:
        print(f"Error al verificar watchlist: {e}")
        return False
    finally:
        conn.close()
