"""
Módulo de gestión de cartera de inversión - StockLab V2.0

Sistema completo de gestión de patrimonio con soporte para:
- Operaciones de compra/venta de acciones
- Movimientos de efectivo (ingresos, retiradas)
- Dividendos y comisiones
- Cálculo automático de liquidez y patrimonio total
- Historial completo de operaciones
"""

import logging
from database.connection import get_connection, query_all, query_val
import streamlit as st
from datetime import datetime
from typing import Optional
from services.auth_service import (
    get_current_user,
    get_current_user_id,
    is_cloud_user_authenticated,
    supabase_rest,
)

logger = logging.getLogger(__name__)


def _mapa_empresas_catalogo() -> tuple[dict, dict]:
    """Devuelve (por_id, por_ticker) del catálogo global `empresas`."""
    rows = query_all("SELECT id, ticker, nombre, sector, mercado FROM empresas") or []
    por_id = {int(r['id']): r for r in rows if r.get('id') is not None}
    por_ticker = {str(r['ticker']).strip().upper(): r for r in rows if r.get('ticker')}
    return por_id, por_ticker


def _cargar_ops_cartera_raw(tipo_cartera: Optional[str] = None) -> list[dict]:
    """
    Carga operaciones de cartera desde Supabase (aisladas por RLS para el usuario actual)
    en modo nube, o desde SQLite en modo local, con el mismo esquema de diccionario.
    """
    if is_cloud_user_authenticated():
        user = get_current_user()
        params = {
            "user_id": f"eq.{user['id']}",
            "order": "fecha.asc,id.asc",
        }
        if tipo_cartera:
            params["tipo_cartera"] = f"eq.{tipo_cartera.strip().upper()}"
        ok, rows = supabase_rest("GET", "cartera", params=params)
        if not ok or not isinstance(rows, list):
            return []
        _, por_ticker = _mapa_empresas_catalogo()
        resultado = []
        for r in rows:
            tkr = str(r.get("ticker") or "").strip().upper()
            emp = por_ticker.get(tkr) if tkr else None
            tc = str(r.get("tipo_cartera") or "SIMULADA").strip().upper()
            notas_norm = _normalizar_notas_cartera(r.get("notas"), tc)
            resultado.append({
                "id": int(r["id"]),
                "empresa_id": int(emp["id"]) if emp else None,
                "ticker": tkr or (emp["ticker"] if emp else None),
                "empresa_nombre": r.get("empresa_nombre") or (emp["nombre"] if emp else None),
                "nombre": r.get("empresa_nombre") or (emp["nombre"] if emp else None),
                "sector": (emp.get("sector") if emp else None) or "Desconocido",
                "mercado": emp.get("mercado") if emp else None,
                "tipo": str(r.get("tipo") or "").upper(),
                "tipo_cartera": tc,
                "cantidad": float(r.get("cantidad") or 0.0),
                "precio_unitario": float(r.get("precio_unitario") or 0.0),
                "importe_total": float(r.get("importe_total") or 0.0),
                "comision": float(r.get("comision") or 0.0),
                "fecha": str(r.get("fecha") or "")[:10],
                "notas": notas_norm,
                "created_at": r.get("created_at"),
            })
        return resultado

    sql = """
        SELECT c.id, c.empresa_id, e.ticker, e.nombre AS empresa_nombre, e.nombre AS nombre,
               COALESCE(e.sector, 'Desconocido') AS sector, e.mercado,
               c.tipo, c.cantidad, c.precio_unitario, c.importe_total,
               c.comision, c.fecha, c.notas, c.created_at
        FROM cartera c
        LEFT JOIN empresas e ON c.empresa_id = e.id
    """
    params_sql = []
    if tipo_cartera:
        sql += " WHERE c.notas LIKE ?"
        params_sql.append(f"%[{tipo_cartera.strip().upper()}]%")
    sql += " ORDER BY c.fecha ASC, c.id ASC"
    return query_all(sql, tuple(params_sql))


def _invalidar_cache_cartera():
    """
    Invalida selectivamente las cachés de cartera y consultas SQL tras escrituras.
    NO toca cachés de datos de mercado (precios, fundamentales) para evitar recargas innecesarias.
    """
    funciones_cartera = [
        query_all,
        query_val,
        _obtener_operaciones_cached,
        _calcular_posiciones_abiertas_cached,
        _calcular_posiciones_abiertas_filtradas_cached,
        _calcular_liquidez_filtrada_cached,
        _calcular_patrimonio_total_filtrado_cached,
        _calcular_desglose_resultados_filtrado_cached,
        _obtener_historial_patrimonio_cached,
        _obtener_distribucion_sectores_filtrada_cached,
        _obtener_distribucion_empresas_filtrada_cached,
    ]
    for fn in funciones_cartera:
        try:
            fn.clear()
        except Exception:
            pass


def _normalizar_notas_cartera(notas: Optional[str], tipo_cartera: str) -> str:
    """
    Garantiza que el campo notas contenga única e inequívocamente el prefijo
    '[REAL]' o '[SIMULADA]' correspondiente a `tipo_cartera`.

    Elimina etiquetas anteriores o contradictorias para evitar duplicados
    ('[REAL] [REAL]') o desclasificaciones ('[REAL] [SIMULADA]').
    """
    cartera_upper = (tipo_cartera or "SIMULADA").strip().upper()
    tag_requerido = f"[{cartera_upper}]" if cartera_upper in ["REAL", "SIMULADA"] else "[SIMULADA]"

    texto_limpio = (notas or "").strip()
    # Eliminar cualquier tag previo de [REAL] o [SIMULADA] para evitar duplicados o contradicciones
    texto_limpio = texto_limpio.replace("[REAL]", "").replace("[SIMULADA]", "").strip()

    if texto_limpio:
        return f"{tag_requerido} {texto_limpio}"
    return tag_requerido


def _simular_ledger_cartera(ops_list, validar_empresas: Optional[set] = None, devolver_min_liq: bool = False):
    """
    Reproduce cronológicamente el libro de operaciones de una cartera y verifica:
      - Ninguna VENTA de las empresas en `validar_empresas` (o todas si es None)
        supera las acciones disponibles acumuladas en su fecha.
      - Evalúa además el saldo mínimo de liquidez al cierre de cada fecha (permite
        ingreso + compra el mismo día sin falsos bloqueos, pero detecta descubiertos
        intermedios en fechas anteriores).
      - Devuelve `(liquidez_final, mensaje_error_o_None)` o, si `devolver_min_liq=True`,
        `(liquidez_final, min_liq_diaria, fecha_min_liq, mensaje_error_o_None)`.
    """
    liq_s = 0.0
    min_liq = 0.0
    fecha_min = None
    acc_s = {}

    # Agrupar por fecha manteniendo orden cronológico para evaluar liquidez al cierre de cada día
    ops_por_fecha = {}
    for o in ops_list:
        f_o = str(o.get('fecha') or '')[:10]
        ops_por_fecha.setdefault(f_o, []).append(o)

    for f_dia in sorted(ops_por_fecha.keys()):
        # Dentro del mismo día procesamos primero entradas/compras y luego ventas por id original
        for o in ops_por_fecha[f_dia]:
            t_o = str(o['tipo'] or '').upper()
            c_o = float(o['cantidad'] or 0.0)
            p_o = float(o['precio_unitario'] or 0.0)
            imp_o = float(o['importe_total'] or 0.0)
            com_o = float(o['comision'] or 0.0)
            eid_o = o['empresa_id']
            if t_o == 'INGRESO_EFECTIVO':
                liq_s += imp_o
            elif t_o == 'RETIRADA_EFECTIVO':
                liq_s -= imp_o
            elif t_o == 'DIVIDENDO':
                liq_s += imp_o
            elif t_o == 'COMISION':
                liq_s -= imp_o
            elif t_o == 'COMPRA':
                liq_s -= (c_o * p_o) + com_o
                if eid_o is not None:
                    acc_s[eid_o] = acc_s.get(eid_o, 0.0) + c_o
            elif t_o == 'VENTA':
                if eid_o is not None:
                    disp = acc_s.get(eid_o, 0.0)
                    debe_validar = (validar_empresas is None) or (eid_o in validar_empresas)
                    if debe_validar and c_o > disp + 0.001:
                        err_m = f"La venta de {c_o:.4f} acciones supera las disponibles en esa fecha ({disp:.4f})."
                        if devolver_min_liq:
                            return None, None, None, err_m
                        return None, err_m
                    acc_s[eid_o] = max(0.0, disp - c_o)
                liq_s += (c_o * p_o) - com_o

        if liq_s < min_liq - 1e-9:
            min_liq = liq_s
            fecha_min = f_dia

    if devolver_min_liq:
        return liq_s, min_liq, fecha_min, None
    return liq_s, None


def agregar_operacion(
    empresa_id: int = None,
    tipo: str = "COMPRA",
    cantidad: float = 0.0,
    precio_unitario: float = 0.0,
    importe_total: float = 0.0,
    fecha: str = None,
    comision: float = 0.0,
    notas: str = None,
    tipo_cartera: str = None
) -> bool:
    """
    Registra una nueva operación en la cartera.
    
    Args:
        empresa_id: ID de la empresa (None para movimientos de efectivo)
        tipo: COMPRA, VENTA, DIVIDENDO, INGRESO_EFECTIVO, RETIRADA_EFECTIVO, COMISION
        cantidad: Número de acciones (para COMPRA/VENTA)
        precio_unitario: Precio por acción (para COMPRA/VENTA)
        importe_total: Importe total (para DIVIDENDO/INGRESO_EFECTIVO/RETIRADA_EFECTIVO/COMISION)
        fecha: Fecha de la operación (YYYY-MM-DD). Si None, usa hoy
        comision: Comisión de la operación
        notas: Notas opcionales
        tipo_cartera: Opcional 'REAL' o 'SIMULADA' para fijar el destino patrimonial
    
    Returns:
        True si se insertó correctamente, False en caso de error
    """
    if fecha is None:
        fecha = datetime.now().strftime("%Y-%m-%d")
    
    tipo_upper = (tipo or "").upper()

    # Identificar tipo de cartera activa
    if not tipo_cartera:
        if notas and "[REAL]" in notas:
            tipo_cartera = "REAL"
        elif notas and "[SIMULADA]" in notas:
            tipo_cartera = "SIMULADA"
        elif hasattr(st, "session_state") and "tipo_cartera_activa" in st.session_state:
            tipo_cartera = st.session_state.get("tipo_cartera_activa", "SIMULADA")
        else:
            tipo_cartera = "SIMULADA"

    # Blindar e inyectar determinísticamente la etiqueta [REAL] o [SIMULADA] en notas
    notas = _normalizar_notas_cartera(notas, tipo_cartera)

    # Validar liquidez disponible para operaciones que consumen efectivo
    if tipo_upper in ['COMPRA', 'RETIRADA_EFECTIVO', 'COMISION']:
        if tipo_upper == 'COMPRA':
            coste_requerido = (cantidad * precio_unitario) + comision
        else:
            coste_requerido = (importe_total if importe_total > 0 else (cantidad * precio_unitario)) + comision

        liquidez_disponible = calcular_liquidez_filtrada(tipo_cartera)

        if coste_requerido > (liquidez_disponible + 0.001):
            msg = (
                f"Liquidez disponible insuficiente ({liquidez_disponible:,.2f} € disponibles vs {coste_requerido:,.2f} € requeridos).\n\n"
                f"Por favor, realice un Ingreso de Efectivo previo en su cartera ({tipo_cartera}) para poder completar esta compra u operación."
            )
            logger.warning(msg)
            try:
                if hasattr(st, "session_state"):
                    st.session_state["cartera_last_error"] = msg
                st.error(
                    f"**Liquidez disponible insuficiente** ({liquidez_disponible:,.2f} € disponibles vs {coste_requerido:,.2f} € requeridos).\n\n"
                    f"Por favor, realice un **Ingreso de Efectivo** previo en su cartera ({tipo_cartera}) para poder completar esta compra u operación."
                )
            except Exception:
                pass
            return False

        # Validar también que la operación no genere descubierto en su fecha (operaciones retrodatadas)
        ops_exist_l = _cargar_ops_cartera_raw(tipo_cartera)
        _, min_liq_antes, _, _ = _simular_ledger_cartera(ops_exist_l, validar_empresas=set(), devolver_min_liq=True)
        op_nueva_l = {
            'id': 10**12,
            'empresa_id': empresa_id,
            'tipo': tipo_upper,
            'cantidad': cantidad,
            'precio_unitario': precio_unitario,
            'importe_total': importe_total if importe_total > 0 else (cantidad * precio_unitario),
            'comision': comision,
            'fecha': fecha,
        }
        ops_sim_l = list(ops_exist_l) + [op_nueva_l]
        ops_sim_l.sort(key=lambda r: (str(r.get('fecha') or ''), int(r.get('id') or 0)))
        _, min_liq_despues, f_min_l, _ = _simular_ledger_cartera(ops_sim_l, validar_empresas=set(), devolver_min_liq=True)
        if min_liq_despues is not None and min_liq_despues < -0.01 and (min_liq_antes is None or min_liq_despues < min_liq_antes - 0.01):
            msg = (
                f"Liquidez insuficiente en la fecha {f_min_l or fecha} "
                f"(saldo resultante ese día: {min_liq_despues:,.2f} €). "
                f"Revisa la fecha de la operación o realiza un ingreso previo."
            )
            logger.warning(msg)
            try:
                if hasattr(st, "session_state"):
                    st.session_state["cartera_last_error"] = msg
                st.error(f"**Operación no permitida:** {msg}")
            except Exception:
                pass
            return False

    # Validar tenencia cronológica suficiente para operaciones de VENTA
    # (impide ventas en corto y ventas retrodatadas anteriores a la compra)
    if tipo_upper == 'VENTA':
        if not empresa_id or cantidad <= 0:
            msg = f"Datos inválidos para venta en cartera {tipo_cartera}."
            logger.warning(msg)
            try:
                if hasattr(st, "session_state"):
                    st.session_state["cartera_last_error"] = msg
                st.error(f"**Operación no permitida:** {msg}")
            except Exception:
                pass
            return False

        ops_exist = _cargar_ops_cartera_raw(tipo_cartera)
        op_nueva_sim = {
            'id': 10**12,
            'empresa_id': empresa_id,
            'tipo': 'VENTA',
            'cantidad': cantidad,
            'precio_unitario': precio_unitario,
            'importe_total': importe_total if importe_total > 0 else (cantidad * precio_unitario),
            'comision': comision,
            'fecha': fecha,
        }
        ops_sim = list(ops_exist) + [op_nueva_sim]
        ops_sim.sort(key=lambda r: (str(r.get('fecha') or ''), int(r.get('id') or 0)))
        _, err_venta = _simular_ledger_cartera(ops_sim, validar_empresas={empresa_id})
        if err_venta:
            msg = f"Acciones insuficientes en cartera {tipo_cartera}: {err_venta}"
            logger.warning(msg)
            try:
                if hasattr(st, "session_state"):
                    st.session_state["cartera_last_error"] = msg
                st.error(f"**Operación no permitida:** {msg}")
            except Exception:
                pass
            return False

    # Calcular importe_total si no se proporciona (para COMPRA/VENTA)
    if tipo_upper in ['COMPRA', 'VENTA'] and importe_total == 0:
        importe_total = cantidad * precio_unitario

    if is_cloud_user_authenticated():
        user = get_current_user()
        por_id, _ = _mapa_empresas_catalogo()
        emp_info = por_id.get(int(empresa_id)) if empresa_id is not None else None
        payload = {
            "user_id": user["id"],
            "tipo_cartera": tipo_cartera,
            "ticker": emp_info["ticker"] if emp_info else None,
            "empresa_nombre": emp_info["nombre"] if emp_info else None,
            "tipo": tipo_upper,
            "cantidad": float(cantidad or 0.0),
            "precio_unitario": float(precio_unitario or 0.0),
            "importe_total": float(importe_total or 0.0),
            "comision": float(comision or 0.0),
            "fecha": str(fecha)[:10],
            "notas": notas,
        }
        ok, err = supabase_rest("POST", "cartera", payload=payload, prefer="return=minimal")
        if ok:
            _invalidar_cache_cartera()
            return True
        logger.error(f"Error al registrar operación en Supabase: {err}")
        try:
            st.error(f"Error al registrar operación: {err}")
        except Exception:
            pass
        return False

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO cartera (empresa_id, tipo, cantidad, precio_unitario, 
                               importe_total, comision, fecha, notas)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (empresa_id, tipo.upper(), cantidad, precio_unitario, 
              importe_total, comision, fecha, notas))
        conn.commit()
        _invalidar_cache_cartera()
        return True
    except Exception as e:
        logger.error(f"Error al registrar operación: {e}")
        try:
            st.error(f"Error al registrar operación: {e}")
        except Exception:
            pass
        return False
    finally:
        conn.close()


@st.cache_data(ttl=30, show_spinner=False)
def _obtener_operaciones_cached(user_id: str, empresa_id: int = None, tipo: str = None,
                                fecha_inicio: str = None, fecha_fin: str = None):
    ops = _cargar_ops_cartera_raw()
    filtradas = []
    tipo_u = tipo.upper() if tipo else None
    for op in ops:
        if empresa_id and op.get("empresa_id") != empresa_id:
            continue
        if tipo_u and str(op.get("tipo") or "").upper() != tipo_u:
            continue
        f_op = str(op.get("fecha") or "")[:10]
        if fecha_inicio and f_op < str(fecha_inicio)[:10]:
            continue
        if fecha_fin and f_op > str(fecha_fin)[:10]:
            continue
        filtradas.append(op)
    filtradas.sort(key=lambda r: (str(r.get("fecha") or ""), int(r.get("id") or 0)), reverse=True)
    return filtradas


def obtener_operaciones(empresa_id: int = None, tipo: str = None,
                        fecha_inicio: str = None, fecha_fin: str = None):
    """Obtiene operaciones del usuario actual con filtros opcionales (caché aislada por user_id)."""
    return _obtener_operaciones_cached(
        get_current_user_id(),
        empresa_id=empresa_id,
        tipo=tipo,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
    )


def calcular_liquidez():
    """
    Calcula la liquidez actual sumando todos los movimientos de efectivo.
    """
    operaciones = _cargar_ops_cartera_raw()
    
    if not operaciones:
        return 0.0
    
    liquidez = 0.0
    
    for op in operaciones:
        if op['tipo'] == 'INGRESO_EFECTIVO':
            liquidez += op['importe_total']
        elif op['tipo'] == 'RETIRADA_EFECTIVO':
            liquidez -= op['importe_total']
        elif op['tipo'] == 'COMPRA':
            coste_total = (op['cantidad'] * op['precio_unitario']) + op['comision']
            liquidez -= coste_total
        elif op['tipo'] == 'VENTA':
            ingreso_neto = (op['cantidad'] * op['precio_unitario']) - op['comision']
            liquidez += ingreso_neto
        elif op['tipo'] == 'DIVIDENDO':
            liquidez += op['importe_total']
        elif op['tipo'] == 'COMISION':
            liquidez -= op['importe_total']
    
    return round(liquidez, 2)


def calcular_capital_invertido():
    """
    Calcula el capital actualmente invertido en acciones.
    
    Returns:
        Float con el capital invertido
    """
    posiciones = calcular_posiciones_abiertas()
    
    if not posiciones:
        return 0.0
    
    return sum(pos['coste_total'] for pos in posiciones)


def calcular_patrimonio_total():
    """
    Calcula el patrimonio total (liquidez + valor actual de posiciones).
    
    Returns:
        Dict con desglose del patrimonio
    """
    import market_data
    
    liquidez = calcular_liquidez()
    posiciones = calcular_posiciones_abiertas()
    
    valor_posiciones = 0.0
    coste_total = 0.0
    
    for pos in posiciones:
        coste_total += pos['coste_total']
        try:
            datos = market_data.get_stock_data(pos['ticker'])
            precio_actual = datos.get('precio_actual', 0) if datos else 0
            if precio_actual and precio_actual > 0:
                valor_posiciones += pos['acciones'] * precio_actual
            else:
                valor_posiciones += pos['coste_total']
        except Exception:
            valor_posiciones += pos['coste_total']
    
    patrimonio_total = liquidez + valor_posiciones
    rentabilidad_absoluta = valor_posiciones - coste_total
    rentabilidad_pct = ((valor_posiciones - coste_total) / coste_total * 100) if coste_total > 0 else 0.0
    
    return {
        'liquidez': liquidez,
        'valor_posiciones': round(valor_posiciones, 2),
        'patrimonio_total': round(patrimonio_total, 2),
        'capital_invertido': round(coste_total, 2),
        'rentabilidad_absoluta': round(rentabilidad_absoluta, 2),
        'rentabilidad_pct': round(rentabilidad_pct, 2),
        'num_posiciones': len(posiciones)
    }


@st.cache_data(ttl=60, show_spinner=False)
def _calcular_posiciones_abiertas_cached(user_id: str):
    operaciones = [
        op for op in _cargar_ops_cartera_raw()
        if op.get("empresa_id") is not None and op.get("tipo") in ("COMPRA", "VENTA")
    ]
    
    if not operaciones:
        return []
    
    posiciones = {}
    
    for op in operaciones:
        eid = op['empresa_id']
        
        if eid not in posiciones:
            posiciones[eid] = {
                'empresa_id': eid,
                'ticker': op['ticker'],
                'nombre': op['nombre'],
                'sector': op.get('sector', 'Desconocido'),
                'acciones': 0.0,
                'coste_acumulado': 0.0,
                'primera_compra': None,
                'comisiones': 0.0,
                'notas': ''
            }
        
        pos = posiciones[eid]
        
        if op['tipo'] == 'COMPRA':
            pos['coste_acumulado'] += op['cantidad'] * op['precio_unitario']
            pos['acciones'] += op['cantidad']
            pos['comisiones'] += op.get('comision') or 0.0
            
            if pos['primera_compra'] is None:
                pos['primera_compra'] = op['fecha']
                
        elif op['tipo'] == 'VENTA':
            if pos['acciones'] > 0:
                acciones_prev = pos['acciones']
                coste_por_accion = pos['coste_acumulado'] / acciones_prev
                coste_venta = op['cantidad'] * coste_por_accion
                
                pos['acciones'] -= op['cantidad']
                pos['coste_acumulado'] -= coste_venta
                if pos['acciones'] <= 0.001:
                    # Posición cerrada: reiniciar acumuladores y metadatos para futuras reaperturas
                    pos['acciones'] = 0.0
                    pos['coste_acumulado'] = 0.0
                    pos['comisiones'] = 0.0
                    pos['primera_compra'] = None
                    pos['notas'] = ''
                else:
                    frac_restante = max(0.0, pos['acciones'] / acciones_prev)
                    pos['comisiones'] *= frac_restante
    
    resultado = []
    for pos in posiciones.values():
        if pos['acciones'] > 0.001:
            pos['precio_medio'] = pos['coste_acumulado'] / pos['acciones']
            pos['coste_total'] = pos['coste_acumulado']
            resultado.append(pos)
    
    return resultado


def calcular_posiciones_abiertas():
    """Calcula las posiciones abiertas del usuario actual (caché aislada por user_id)."""
    return _calcular_posiciones_abiertas_cached(get_current_user_id())


def obtener_resumen_cartera():
    """
    Obtiene métricas resumen completas de la cartera.
    
    Returns:
        Diccionario con todas las métricas principales
    """
    return calcular_patrimonio_total()


@st.cache_data(ttl=180, show_spinner=False)
def _obtener_historial_patrimonio_cached(user_id: str, tipo_cartera: str = "SIMULADA"):
    import market_data

    operaciones = _cargar_ops_cartera_raw(tipo_cartera)

    if not operaciones:
        return []

    # Precargar mapa empresa_id -> ticker y series históricas de cierre para valoración real diaria
    eids_unicos = sorted({op['empresa_id'] for op in operaciones if op.get('empresa_id')})
    eid_to_ticker = {}
    if eids_unicos:
        placeholders = ",".join("?" for _ in eids_unicos)
        rows_emp = query_all(f"SELECT id, ticker FROM empresas WHERE id IN ({placeholders})", tuple(eids_unicos))
        eid_to_ticker = {r['id']: r['ticker'] for r in rows_emp}

    import pandas as _pd
    hist_closes_by_ticker = {}
    spot_by_ticker = {}
    hoy_str = datetime.now().strftime("%Y-%m-%d")
    fecha_min_ops = min(str(op['fecha'])[:10] for op in operaciones)
    try:
        dias_rango = (datetime.now() - datetime.strptime(fecha_min_ops, "%Y-%m-%d")).days
    except Exception:
        dias_rango = 365
    api_period = "5y" if dias_rango > 365 else ("1y" if dias_rango > 150 else "6mo")

    for tk in set(eid_to_ticker.values()):
        try:
            df_tk = market_data.get_price_history(tk, period=api_period, interval="1d")
            if df_tk is not None and not df_tk.empty and 'Close' in df_tk.columns:
                s_close = df_tk['Close'].dropna()
                if not s_close.empty:
                    idx_norm = _pd.to_datetime(s_close.index)
                    if getattr(idx_norm, "tz", None) is not None:
                        idx_norm = idx_norm.tz_localize(None)
                    s_close = _pd.Series(s_close.values, index=idx_norm.normalize()).sort_index()
                    hist_closes_by_ticker[tk] = s_close
        except Exception:
            pass
        try:
            d_spot = market_data.get_stock_data(tk) or {}
            if d_spot.get('precio_actual') and float(d_spot['precio_actual']) > 0:
                spot_by_ticker[tk] = float(d_spot['precio_actual'])
        except Exception:
            pass

    def _precio_en_fecha(ticker: str, fecha_str: str, coste_fallback: float) -> float:
        if fecha_str[:10] >= hoy_str and ticker in spot_by_ticker:
            return spot_by_ticker[ticker]
        serie = hist_closes_by_ticker.get(ticker)
        if serie is not None and not serie.empty:
            try:
                dt_obj = _pd.Timestamp(fecha_str[:10])
                sub = serie.loc[:dt_obj]
                if not sub.empty:
                    return float(sub.iloc[-1])
            except Exception:
                pass
        return coste_fallback

    # Agrupar operaciones por fecha YYYY-MM-DD
    ops_por_fecha = {}
    for op in operaciones:
        fecha = str(op['fecha'])[:10]
        ops_por_fecha.setdefault(fecha, []).append(op)

    fechas_ordenadas = sorted(ops_por_fecha.keys())
    year_actual = datetime.now().year
    cierre_ano_prev = f"{year_actual - 1}-12-31"

    # Construir malla temporal real: fechas de operación + todas las sesiones bursátiles disponibles
    # desde la primera operación hasta hoy (evita rectas planas en 1M/6M/YTD/Todo).
    fechas_set = set(fechas_ordenadas)
    if fechas_ordenadas:
        f_inicio = fechas_ordenadas[0]
        if f_inicio < cierre_ano_prev < hoy_str:
            fechas_set.add(cierre_ano_prev)
        if fechas_ordenadas[-1] < hoy_str:
            fechas_set.add(hoy_str)
        for serie in hist_closes_by_ticker.values():
            for dt_idx in serie.index:
                ds = dt_idx.strftime("%Y-%m-%d")
                if f_inicio <= ds <= hoy_str:
                    fechas_set.add(ds)
    fechas_evaluacion = sorted(fechas_set)

    historial = []
    liquidez = 0.0
    capital_aportado = 0.0
    posiciones = {}

    for fecha in fechas_evaluacion:
        ops_dia = ops_por_fecha.get(fecha, [])

        for op in ops_dia:
            t_op = str(op['tipo'] or '').upper()
            imp = float(op.get('importe_total') or 0.0)
            cant = float(op.get('cantidad') or 0.0)
            prec = float(op.get('precio_unitario') or 0.0)
            com = float(op.get('comision') or 0.0)
            if t_op == 'INGRESO_EFECTIVO':
                liquidez += imp
                capital_aportado += imp
            elif t_op == 'RETIRADA_EFECTIVO':
                liquidez -= imp
                capital_aportado -= imp
            elif t_op == 'COMPRA' and op['empresa_id']:
                eid = op['empresa_id']
                if eid not in posiciones:
                    posiciones[eid] = {'acciones': 0.0, 'coste': 0.0}
                posiciones[eid]['coste'] += cant * prec
                posiciones[eid]['acciones'] += cant
                liquidez -= (cant * prec) + com
            elif t_op == 'VENTA' and op['empresa_id']:
                eid = op['empresa_id']
                if eid in posiciones and posiciones[eid]['acciones'] > 0:
                    acciones_prev = posiciones[eid]['acciones']
                    coste_por_accion = posiciones[eid]['coste'] / acciones_prev
                    posiciones[eid]['coste'] = max(0.0, posiciones[eid]['coste'] - (cant * coste_por_accion))
                    posiciones[eid]['acciones'] = max(0.0, acciones_prev - cant)
                    if posiciones[eid]['acciones'] <= 0.001:
                        posiciones[eid]['acciones'] = 0.0
                        posiciones[eid]['coste'] = 0.0
                liquidez += (cant * prec) - com
            elif t_op == 'DIVIDENDO':
                liquidez += imp
            elif t_op == 'COMISION':
                liquidez -= imp

        valor_posiciones = 0.0
        for eid, pos in posiciones.items():
            if pos['acciones'] > 0.001:
                ticker = eid_to_ticker.get(eid)
                precio_medio_fb = pos['coste'] / pos['acciones'] if pos['acciones'] > 0 else 0.0
                if ticker:
                    precio_f = _precio_en_fecha(ticker, fecha, precio_medio_fb)
                    valor_posiciones += pos['acciones'] * precio_f
                else:
                    valor_posiciones += pos['coste']

        patrimonio = liquidez + valor_posiciones

        historial.append({
            'fecha': fecha,
            'patrimonio': round(patrimonio, 2),
            'liquidez': round(liquidez, 2),
            'valor_posiciones': round(valor_posiciones, 2),
            'capital_aportado': round(capital_aportado, 2),
        })

    return historial


def obtener_historial_patrimonio(tipo_cartera: str = "SIMULADA"):
    """Historial diario de patrimonio del usuario actual (caché aislada por user_id)."""
    return _obtener_historial_patrimonio_cached(get_current_user_id(), tipo_cartera)


@st.cache_data(ttl=300, show_spinner=False)
def _obtener_distribucion_sectores_filtrada_cached(user_id: str, tipo_cartera: str = "SIMULADA"):
    posiciones = _calcular_posiciones_abiertas_filtradas_cached(user_id, tipo_cartera)
    
    if not posiciones:
        return []
    
    # Obtener precios en batch para eficiencia
    tickers = [pos['ticker'] for pos in posiciones]
    precios_batch = obtener_precios_actuales_batch(tickers)
    
    sectores = {}
    valor_total = 0.0
    
    for pos in posiciones:
        precio_actual = precios_batch.get(pos['ticker'], 0)
        valor = pos['acciones'] * precio_actual if precio_actual > 0 else pos['coste_total']
        
        sector = pos.get('sector', 'Otros')
        if sector not in sectores:
            sectores[sector] = 0.0
        sectores[sector] += valor
        valor_total += valor
    
    resultado = []
    for sector, valor in sectores.items():
        pct = (valor / valor_total * 100) if valor_total > 0 else 0
        resultado.append({
            'sector': sector,
            'valor': round(valor, 2),
            'porcentaje': round(pct, 1)
        })
    
    resultado.sort(key=lambda x: x['porcentaje'], reverse=True)
    return resultado


def obtener_distribucion_sectores_filtrada(tipo_cartera: str = "SIMULADA"):
    """Distribución por sectores del usuario actual (caché aislada por user_id)."""
    return _obtener_distribucion_sectores_filtrada_cached(get_current_user_id(), tipo_cartera)


@st.cache_data(ttl=300, show_spinner=False)
def _obtener_distribucion_empresas_filtrada_cached(user_id: str, tipo_cartera: str = "SIMULADA"):
    posiciones = _calcular_posiciones_abiertas_filtradas_cached(user_id, tipo_cartera)
    
    if not posiciones:
        return []
    
    # Obtener precios en batch para eficiencia
    tickers = [pos['ticker'] for pos in posiciones]
    precios_batch = obtener_precios_actuales_batch(tickers)
    
    empresas = []
    valor_total = 0.0
    
    for pos in posiciones:
        precio_actual = precios_batch.get(pos['ticker'], 0)
        valor = pos['acciones'] * precio_actual if precio_actual > 0 else pos['coste_total']
        
        empresas.append({
            'ticker': pos['ticker'],
            'nombre': pos['nombre'],
            'valor': round(valor, 2),
            'sector': pos.get('sector', 'Otros')
        })
        valor_total += valor
    
    for emp in empresas:
        emp['porcentaje'] = round((emp['valor'] / valor_total * 100) if valor_total > 0 else 0, 1)
    
    empresas.sort(key=lambda x: x['porcentaje'], reverse=True)
    return empresas


def obtener_distribucion_empresas_filtrada(tipo_cartera: str = "SIMULADA"):
    """Distribución por empresas del usuario actual (caché aislada por user_id)."""
    return _obtener_distribucion_empresas_filtrada_cached(get_current_user_id(), tipo_cartera)


def actualizar_operacion(operacion_id: int, **kwargs) -> bool:
    """
    Actualiza una operación existente garantizando el blindaje de la etiqueta de cartera.
    """
    campos_permitidos = ['cantidad', 'precio_unitario', 'importe_total',
                        'comision', 'fecha', 'notas', 'tipo']

    if is_cloud_user_authenticated():
        user = get_current_user()
        todas_ops = _cargar_ops_cartera_raw()
        op_actual = next((o for o in todas_ops if o['id'] == int(operacion_id)), None)
        if not op_actual:
            return False
        tipo_cartera = (kwargs.get('tipo_cartera') or op_actual.get('tipo_cartera') or 'SIMULADA').strip().upper()
        kwargs['notas'] = _normalizar_notas_cartera(kwargs.get('notas', op_actual.get('notas') or ''), tipo_cartera)
        tipo_final = str(kwargs.get('tipo') or op_actual['tipo'] or '').upper()
        if tipo_final in ('COMPRA', 'VENTA') and 'importe_total' not in kwargs:
            if 'cantidad' in kwargs or 'precio_unitario' in kwargs:
                cant_f = float(kwargs.get('cantidad') if kwargs.get('cantidad') is not None else (op_actual['cantidad'] or 0.0))
                prec_f = float(kwargs.get('precio_unitario') if kwargs.get('precio_unitario') is not None else (op_actual['precio_unitario'] or 0.0))
                kwargs['importe_total'] = round(cant_f * prec_f, 2)

        ops_antes = [o for o in todas_ops if o.get('tipo_cartera') == tipo_cartera]
        eid_afectado = op_actual.get('empresa_id')
        empresas_a_validar = {eid_afectado} if eid_afectado is not None else set()
        liq_antes, min_liq_antes, _, _ = _simular_ledger_cartera(ops_antes, validar_empresas=set(), devolver_min_liq=True)
        op_editada = dict(op_actual)
        for campo_v in campos_permitidos:
            if campo_v in kwargs and kwargs[campo_v] is not None:
                op_editada[campo_v] = kwargs[campo_v]
        ops_despues = [op_editada if r['id'] == int(operacion_id) else dict(r) for r in ops_antes]
        ops_despues.sort(key=lambda r: (str(r.get('fecha') or ''), int(r.get('id') or 0)))
        liq_despues, min_liq_despues, f_min_edit, err_tenencia = _simular_ledger_cartera(
            ops_despues, validar_empresas=empresas_a_validar, devolver_min_liq=True
        )
        if err_tenencia:
            try:
                st.error(f"Operación no válida: {err_tenencia}")
            except Exception:
                pass
            return False
        if liq_despues is not None and liq_despues < -0.01 and (liq_antes is None or liq_despues < liq_antes - 0.01):
            try:
                st.error(f"Liquidez insuficiente tras la edición (saldo resultante: {liq_despues:,.2f} €).")
            except Exception:
                pass
            return False
        if min_liq_despues is not None and min_liq_despues < -0.01 and (min_liq_antes is None or min_liq_despues < min_liq_antes - 0.01):
            try:
                st.error(f"Liquidez negativa en fecha intermedia ({f_min_edit}): {min_liq_despues:,.2f} €.")
            except Exception:
                pass
            return False

        payload_upd = {c: kwargs[c] for c in campos_permitidos if c in kwargs and kwargs[c] is not None}
        if not payload_upd:
            return False
        ok, err = supabase_rest(
            "PATCH",
            "cartera",
            params={"id": f"eq.{int(operacion_id)}", "user_id": f"eq.{user['id']}"},
            payload=payload_upd,
            prefer="return=minimal",
        )
        if ok:
            _invalidar_cache_cartera()
            return True
        try:
            st.error(f"Error al actualizar operación: {err}")
        except Exception:
            pass
        return False

    conn = get_connection()
    try:
        cursor = conn.cursor()
        op_actual = cursor.execute(
            "SELECT id, empresa_id, tipo, cantidad, precio_unitario, importe_total, comision, fecha, notas FROM cartera WHERE id = ?",
            (operacion_id,),
        ).fetchone()
        
        if op_actual:
            notas_previas = op_actual['notas'] or ""
            tipo_cartera = kwargs.get('tipo_cartera')
            
            if not tipo_cartera:
                if 'notas' in kwargs and kwargs['notas'] and "[REAL]" in str(kwargs['notas']):
                    tipo_cartera = "REAL"
                elif 'notas' in kwargs and kwargs['notas'] and "[SIMULADA]" in str(kwargs['notas']):
                    tipo_cartera = "SIMULADA"
                elif "[REAL]" in notas_previas:
                    tipo_cartera = "REAL"
                elif "[SIMULADA]" in notas_previas:
                    tipo_cartera = "SIMULADA"
                elif hasattr(st, "session_state") and "tipo_cartera_activa" in st.session_state:
                    tipo_cartera = st.session_state.get("tipo_cartera_activa", "SIMULADA")
                else:
                    tipo_cartera = "SIMULADA"

            # Si se especificaron notas en kwargs o si es necesario re-blindar, normalizar
            nuevas_notas = kwargs.get('notas', notas_previas)
            kwargs['notas'] = _normalizar_notas_cartera(nuevas_notas, tipo_cartera)

            # Recalcular importe_total automáticamente para COMPRA/VENTA cuando cambian cantidad o precio
            tipo_final = str(kwargs.get('tipo') or op_actual['tipo'] or '').upper()
            if tipo_final in ('COMPRA', 'VENTA') and 'importe_total' not in kwargs:
                if 'cantidad' in kwargs or 'precio_unitario' in kwargs:
                    cant_f = float(kwargs.get('cantidad') if kwargs.get('cantidad') is not None else (op_actual['cantidad'] or 0.0))
                    prec_f = float(kwargs.get('precio_unitario') if kwargs.get('precio_unitario') is not None else (op_actual['precio_unitario'] or 0.0))
                    kwargs['importe_total'] = round(cant_f * prec_f, 2)

            # Validar invariantes de tenencia (sin ventas en corto) y liquidez tras la edición
            prefijo_val = f"[{tipo_cartera}]"
            filas_cartera = cursor.execute(
                """
                SELECT id, empresa_id, tipo, cantidad, precio_unitario, importe_total, comision, fecha
                FROM cartera
                WHERE notas LIKE ?
                ORDER BY fecha ASC, id ASC
                """,
                (f"%{prefijo_val}%",),
            ).fetchall()

            ops_antes = [dict(r) for r in filas_cartera]
            eid_afectado = op_actual['empresa_id']
            empresas_a_validar = {eid_afectado} if eid_afectado is not None else set()
            liq_antes, min_liq_antes, _, _ = _simular_ledger_cartera(ops_antes, validar_empresas=set(), devolver_min_liq=True)

            op_editada = dict(op_actual)
            for campo_v in campos_permitidos:
                if campo_v in kwargs and kwargs[campo_v] is not None:
                    op_editada[campo_v] = kwargs[campo_v]
            ops_despues = [op_editada if r['id'] == operacion_id else dict(r) for r in filas_cartera]
            ops_despues.sort(key=lambda r: (str(r.get('fecha') or ''), int(r.get('id') or 0)))

            liq_despues, min_liq_despues, f_min_edit, err_tenencia = _simular_ledger_cartera(
                ops_despues, validar_empresas=empresas_a_validar, devolver_min_liq=True
            )
            if err_tenencia:
                try:
                    st.error(f"Operación no válida: {err_tenencia}")
                except Exception:
                    pass
                return False
            if liq_despues is not None and liq_despues < -0.01 and (liq_antes is None or liq_despues < liq_antes - 0.01):
                try:
                    st.error(f"Liquidez insuficiente tras la edición (saldo resultante: {liq_despues:,.2f} €).")
                except Exception:
                    pass
                return False
            if min_liq_despues is not None and min_liq_despues < -0.01 and (min_liq_antes is None or min_liq_despues < min_liq_antes - 0.01):
                try:
                    st.error(f"Liquidez negativa en fecha intermedia ({f_min_edit}): {min_liq_despues:,.2f} €.")
                except Exception:
                    pass
                return False

        updates = []
        params = []
        
        for campo, valor in kwargs.items():
            if campo in campos_permitidos and valor is not None:
                updates.append(f"{campo} = ?")
                params.append(valor)
        
        if not updates:
            return False
        
        params.append(operacion_id)
        sql = f"UPDATE cartera SET {', '.join(updates)} WHERE id = ?"
        
        cursor.execute(sql, tuple(params))
        conn.commit()
        _invalidar_cache_cartera()
        return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Error al actualizar operación: {e}")
        try:
            st.error(f"Error al actualizar operación: {e}")
        except Exception:
            pass
        return False
    finally:
        conn.close()


def eliminar_operacion(operacion_id: int) -> bool:
    """
    Elimina una operación de la cartera validando previamente la consistencia
    del libro (sin ventas huérfanas ni liquidez negativa resultante).
    """
    if is_cloud_user_authenticated():
        user = get_current_user()
        todas_ops = _cargar_ops_cartera_raw()
        op_obj = next((o for o in todas_ops if o['id'] == int(operacion_id)), None)
        if not op_obj:
            return False
        tipo_cartera = op_obj.get('tipo_cartera') or ('REAL' if '[REAL]' in (op_obj.get('notas') or '') else 'SIMULADA')
        ops_antes = [o for o in todas_ops if o.get('tipo_cartera') == tipo_cartera]
        eid_del = op_obj.get('empresa_id')
        empresas_a_validar = {eid_del} if eid_del is not None else set()
        liq_antes, min_liq_antes, _, _ = _simular_ledger_cartera(ops_antes, validar_empresas=set(), devolver_min_liq=True)
        ops_despues = [o for o in ops_antes if o['id'] != int(operacion_id)]
        liq_despues, min_liq_despues, f_min_del, err_tenencia = _simular_ledger_cartera(
            ops_despues, validar_empresas=empresas_a_validar, devolver_min_liq=True
        )
        if err_tenencia:
            try:
                st.error(f"No se puede eliminar: {err_tenencia} Elimina antes las ventas posteriores asociadas.")
            except Exception:
                pass
            return False
        if liq_despues is not None and liq_despues < -0.01 and (liq_antes is None or liq_despues < liq_antes - 0.01):
            try:
                st.error(f"No se puede eliminar: dejaría liquidez negativa ({liq_despues:,.2f} €) por operaciones posteriores.")
            except Exception:
                pass
            return False
        if min_liq_despues is not None and min_liq_despues < -0.01 and (min_liq_antes is None or min_liq_despues < min_liq_antes - 0.01):
            try:
                st.error(f"No se puede eliminar: dejaría liquidez negativa el {f_min_del} ({min_liq_despues:,.2f} €).")
            except Exception:
                pass
            return False
        ok, err = supabase_rest(
            "DELETE",
            "cartera",
            params={"id": f"eq.{int(operacion_id)}", "user_id": f"eq.{user['id']}"},
            prefer="return=minimal",
        )
        if ok:
            _invalidar_cache_cartera()
            return True
        try:
            st.error(f"Error al eliminar operación: {err}")
        except Exception:
            pass
        return False

    conn = get_connection()
    try:
        cursor = conn.cursor()
        op_obj = cursor.execute(
            "SELECT id, empresa_id, notas FROM cartera WHERE id = ?",
            (operacion_id,),
        ).fetchone()
        if not op_obj:
            return False

        notas_obj = op_obj['notas'] or ""
        tipo_cartera = "REAL" if "[REAL]" in notas_obj else "SIMULADA"
        prefijo_val = f"[{tipo_cartera}]"
        filas_cartera = cursor.execute(
            """
            SELECT id, empresa_id, tipo, cantidad, precio_unitario, importe_total, comision, fecha
            FROM cartera
            WHERE notas LIKE ?
            ORDER BY fecha ASC, id ASC
            """,
            (f"%{prefijo_val}%",),
        ).fetchall()

        ops_antes = [dict(r) for r in filas_cartera]
        eid_del = op_obj['empresa_id']
        empresas_a_validar = {eid_del} if eid_del is not None else set()
        liq_antes, min_liq_antes, _, _ = _simular_ledger_cartera(ops_antes, validar_empresas=set(), devolver_min_liq=True)
        ops_despues = [dict(r) for r in filas_cartera if r['id'] != operacion_id]
        liq_despues, min_liq_despues, f_min_del, err_tenencia = _simular_ledger_cartera(
            ops_despues, validar_empresas=empresas_a_validar, devolver_min_liq=True
        )

        if err_tenencia:
            try:
                st.error(f"No se puede eliminar: {err_tenencia} Elimina antes las ventas posteriores asociadas.")
            except Exception:
                pass
            return False
        if liq_despues is not None and liq_despues < -0.01 and (liq_antes is None or liq_despues < liq_antes - 0.01):
            try:
                st.error(f"No se puede eliminar: dejaría liquidez negativa ({liq_despues:,.2f} €) por operaciones posteriores.")
            except Exception:
                pass
            return False
        if min_liq_despues is not None and min_liq_despues < -0.01 and (min_liq_antes is None or min_liq_despues < min_liq_antes - 0.01):
            try:
                st.error(f"No se puede eliminar: dejaría liquidez negativa el {f_min_del} ({min_liq_despues:,.2f} €).")
            except Exception:
                pass
            return False

        cursor.execute("DELETE FROM cartera WHERE id = ?", (operacion_id,))
        conn.commit()
        _invalidar_cache_cartera()
        return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Error al eliminar operación: {e}")
        try:
            st.error(f"Error al eliminar operación: {e}")
        except Exception:
            pass
        return False
    finally:
        conn.close()


def obtener_empresas_disponibles():
    """
    Obtiene lista de empresas activas disponibles para operar.
    
    Returns:
        Lista de diccionarios con id, ticker, nombre
    """
    return query_all("""
        SELECT id, ticker, nombre 
        FROM empresas 
        WHERE activa = 1
        ORDER BY nombre
    """)


def obtener_empresa_por_ticker(ticker: str):
    """
    Obtiene información de una empresa por su ticker.
    
    Args:
        ticker: Ticker de la empresa
    
    Returns:
        Diccionario con datos de la empresa o None
    """
    resultados = query_all(
        "SELECT id, ticker, nombre FROM empresas WHERE ticker = ?",
        (ticker.upper(),)
    )
    return resultados[0] if resultados else None


# ── FUNCIONES OPTIMIZADAS CON FILTRO POR TIPO DE CARTERA ──────────────────

@st.cache_data(ttl=60, show_spinner=False)
def _calcular_posiciones_abiertas_filtradas_cached(user_id: str, tipo_cartera: str = "SIMULADA"):
    operaciones = [
        op for op in _cargar_ops_cartera_raw(tipo_cartera)
        if op.get("empresa_id") is not None and op.get("tipo") in ("COMPRA", "VENTA")
    ]

    if not operaciones:
        return []

    posiciones = {}

    for op in operaciones:
        eid = op['empresa_id']

        if eid not in posiciones:
            posiciones[eid] = {
                'empresa_id': eid,
                'ticker': op['ticker'],
                'nombre': op['nombre'],
                'sector': op.get('sector', 'Desconocido'),
                'mercado': op.get('mercado'),
                'acciones': 0.0,
                'coste_acumulado': 0.0,
                'primera_compra': None,
                'comisiones': 0.0,
                'notas': ''
            }

        pos = posiciones[eid]

        if op['tipo'] == 'COMPRA':
            pos['coste_acumulado'] += op['cantidad'] * op['precio_unitario']
            pos['acciones'] += op['cantidad']
            pos['comisiones'] += op['comision'] or 0.0

            nota_op = op.get('notas') or ''
            nota_limpia = nota_op.replace('[SIMULADA]', '').replace('[REAL]', '').strip()
            if nota_limpia:
                if pos['notas']:
                    pos['notas'] += " | " + nota_limpia
                else:
                    pos['notas'] = nota_limpia

            if pos['primera_compra'] is None:
                pos['primera_compra'] = op['fecha']

        elif op['tipo'] == 'VENTA':
            if pos['acciones'] > 0:
                acciones_prev = pos['acciones']
                coste_por_accion = pos['coste_acumulado'] / acciones_prev
                coste_venta = op['cantidad'] * coste_por_accion

                pos['acciones'] -= op['cantidad']
                pos['coste_acumulado'] -= coste_venta
                if pos['acciones'] <= 0.001:
                    pos['acciones'] = 0.0
                    pos['coste_acumulado'] = 0.0
                    pos['comisiones'] = 0.0
                    pos['primera_compra'] = None
                    pos['notas'] = ''
                else:
                    frac_restante = max(0.0, pos['acciones'] / acciones_prev)
                    pos['comisiones'] *= frac_restante

    resultado = []
    for pos in posiciones.values():
        if pos['acciones'] > 0.001:
            pos['precio_medio'] = pos['coste_acumulado'] / pos['acciones']
            pos['coste_total'] = pos['coste_acumulado']
            resultado.append(pos)

    return resultado


def calcular_posiciones_abiertas_filtradas(tipo_cartera: str = "SIMULADA"):
    """Posiciones abiertas del usuario actual filtradas por tipo de cartera (caché aislada por user_id)."""
    return _calcular_posiciones_abiertas_filtradas_cached(get_current_user_id(), tipo_cartera)


@st.cache_data(ttl=60, show_spinner=False)
def _calcular_liquidez_filtrada_cached(user_id: str, tipo_cartera: str = "SIMULADA"):
    operaciones = _cargar_ops_cartera_raw(tipo_cartera)

    if not operaciones:
        return 0.0

    liquidez = 0.0

    for op in operaciones:
        if op['tipo'] == 'INGRESO_EFECTIVO':
            liquidez += op['importe_total']
        elif op['tipo'] == 'RETIRADA_EFECTIVO':
            liquidez -= op['importe_total']
        elif op['tipo'] == 'COMPRA':
            coste_operacion = (op['cantidad'] * op['precio_unitario']) + op['comision']
            liquidez -= coste_operacion
        elif op['tipo'] == 'VENTA':
            ingreso_operacion = (op['cantidad'] * op['precio_unitario']) - op['comision']
            liquidez += ingreso_operacion
        elif op['tipo'] == 'DIVIDENDO':
            liquidez += op['importe_total']
        elif op['tipo'] == 'COMISION':
            liquidez -= op['importe_total']

    return round(liquidez, 2)


def calcular_liquidez_filtrada(tipo_cartera: str = "SIMULADA"):
    """Liquidez disponible del usuario actual (caché aislada por user_id)."""
    return _calcular_liquidez_filtrada_cached(get_current_user_id(), tipo_cartera)


@st.cache_data(ttl=60, show_spinner=False)
def _calcular_patrimonio_total_filtrado_cached(user_id: str, tipo_cartera: str = "SIMULADA"):
    liquidez = _calcular_liquidez_filtrada_cached(user_id, tipo_cartera)
    posiciones = _calcular_posiciones_abiertas_filtradas_cached(user_id, tipo_cartera)

    tickers = [pos['ticker'] for pos in posiciones]
    precios_batch = obtener_precios_actuales_batch(tickers) if tickers else {}

    valor_posiciones = 0.0
    coste_total = 0.0

    for pos in posiciones:
        coste_con_com = float(pos['coste_total']) + float(pos.get('comisiones') or 0.0)
        coste_total += coste_con_com
        precio_actual = float(precios_batch.get(pos['ticker'], 0) or 0)
        if precio_actual > 0:
            valor_posiciones += pos['acciones'] * precio_actual
        else:
            valor_posiciones += coste_con_com

    patrimonio_total = liquidez + valor_posiciones
    rentabilidad_absoluta = valor_posiciones - coste_total
    rentabilidad_pct = ((valor_posiciones - coste_total) / coste_total * 100) if coste_total > 0 else 0.0

    return {
        'liquidez': liquidez,
        'valor_posiciones': round(valor_posiciones, 2),
        'patrimonio_total': round(patrimonio_total, 2),
        'capital_invertido': round(coste_total, 2),
        'rentabilidad_absoluta': round(rentabilidad_absoluta, 2),
        'rentabilidad_pct': round(rentabilidad_pct, 2),
        'num_posiciones': len(posiciones)
    }


def calcular_patrimonio_total_filtrado(tipo_cartera: str = "SIMULADA"):
    """Patrimonio total del usuario actual (caché aislada por user_id)."""
    return _calcular_patrimonio_total_filtrado_cached(get_current_user_id(), tipo_cartera)


@st.cache_data(ttl=60, show_spinner=False)
def _calcular_desglose_resultados_filtrado_cached(user_id: str, tipo_cartera: str = "SIMULADA") -> dict:
    ops = _cargar_ops_cartera_raw(tipo_cartera)

    capital_aportado = 0.0
    dividendos = 0.0
    comisiones_totales = 0.0
    plusvalia_realizada = 0.0
    estado_pos = {}

    for op in ops:
        t_op = str(op.get('tipo') or '').upper()
        eid = op.get('empresa_id')
        cant = float(op.get('cantidad') or 0.0)
        prec = float(op.get('precio_unitario') or 0.0)
        imp = float(op.get('importe_total') or 0.0)
        com = float(op.get('comision') or 0.0)

        if t_op == 'INGRESO_EFECTIVO':
            capital_aportado += imp
        elif t_op == 'RETIRADA_EFECTIVO':
            capital_aportado -= imp
        elif t_op == 'DIVIDENDO':
            dividendos += imp
        elif t_op == 'COMISION':
            comisiones_totales += imp
        elif t_op == 'COMPRA' and eid is not None:
            comisiones_totales += com
            st_p = estado_pos.setdefault(eid, {'acc': 0.0, 'coste': 0.0, 'com': 0.0})
            st_p['acc'] += cant
            st_p['coste'] += cant * prec
            st_p['com'] += com
        elif t_op == 'VENTA' and eid is not None:
            comisiones_totales += com
            st_p = estado_pos.get(eid)
            if st_p and st_p['acc'] > 0:
                acc_prev = st_p['acc']
                cant_eff = min(cant, acc_prev)
                coste_u = st_p['coste'] / acc_prev
                com_u = st_p['com'] / acc_prev
                coste_vendido = (cant_eff * coste_u) + (cant_eff * com_u)
                ingreso_neto = (cant * prec) - com
                plusvalia_realizada += ingreso_neto - coste_vendido

                st_p['acc'] = max(0.0, acc_prev - cant)
                st_p['coste'] = max(0.0, st_p['coste'] - (cant_eff * coste_u))
                st_p['com'] = max(0.0, st_p['com'] - (cant_eff * com_u))
                if st_p['acc'] <= 0.001:
                    st_p['acc'] = 0.0
                    st_p['coste'] = 0.0
                    st_p['com'] = 0.0
            else:
                plusvalia_realizada += (cant * prec) - com

    posiciones = _calcular_posiciones_abiertas_filtradas_cached(user_id, tipo_cartera)
    tickers = [p['ticker'] for p in posiciones]
    precios_batch = obtener_precios_actuales_batch(tickers) if tickers else {}
    plusvalia_latente = 0.0
    for pos in posiciones:
        p_act = float(precios_batch.get(pos['ticker'], 0) or 0)
        if p_act > 0:
            coste_c = pos['coste_total'] + float(pos.get('comisiones') or 0.0)
            plusvalia_latente += (pos['acciones'] * p_act) - coste_c

    return {
        'capital_aportado': round(capital_aportado, 2),
        'plusvalia_latente': round(plusvalia_latente, 2),
        'plusvalia_realizada': round(plusvalia_realizada, 2),
        'dividendos_cobrados': round(dividendos, 2),
        'comisiones_totales': round(comisiones_totales, 2),
    }


def calcular_desglose_resultados_filtrado(tipo_cartera: str = "SIMULADA") -> dict:
    """Desglose económico completo del usuario actual (caché aislada por user_id)."""
    return _calcular_desglose_resultados_filtrado_cached(get_current_user_id(), tipo_cartera)


@st.cache_data(ttl=300, show_spinner=False)
def obtener_precios_actuales_batch(tickers: list):
    """
    Obtiene precios actuales para múltiples tickers en batch con caché.
    
    Args:
        tickers: Lista de tickers
    
    Returns:
        Diccionario {ticker: precio_actual}
    """
    import market_data
    
    precios = {}
    for ticker in tickers:
        try:
            datos = market_data.get_stock_data(ticker)
            precios[ticker] = datos.get('precio_actual', 0) if datos else 0
        except Exception:
            precios[ticker] = 0
    
    return precios

