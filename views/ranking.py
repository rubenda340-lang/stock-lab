import streamlit as st
import pandas as pd
import numpy as np
import concurrent.futures
import time
from typing import Optional
import database
import market_data
from database import (
    agregar_a_watchlist,
    eliminar_de_watchlist,
    obtener_watchlist,
    query_all,
    existe_en_watchlist,
)

from services.data_provider import (
    fetch_fundamentals_uncached,
    get_closing_variation,
    get_intraday_variation,
    get_price_batch,
)


# ═══════════════════════════════════════════════════════════════════
# 🚀 ARQUITECTURA OPTIMIZADA - MULTITHREADING + CACHÉ AGRESIVA
# ═══════════════════════════════════════════════════════════════════

@st.cache_data(ttl=43200, show_spinner=False)  # 12 h de caché
def cargar_todos_los_fundamentales_en_paralelo(lista_tickers: list) -> dict:
    """
    Descarga masiva de fundamentales en PARALELO usando ThreadPoolExecutor.
    [V7] Usa el pipeline estandarizado data_provider: Yahoo → FMP → Finnhub → fallback.
    persist="disk": sobrevive reinicios del servidor (Streamlit 1.18+).
    Los datos ausentes son None (nunca ficticios).
    """
    def consultar_un_ticker(ticker: str):
        """Consulta individual usando la cascada de fuentes V7."""
        try:
            time.sleep(0.05)  # delay para evitar rate-limiting
            return ticker, fetch_fundamentals_uncached(ticker)
        except Exception:
            return ticker, {}

    dict_fundamentales = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futuros = {executor.submit(consultar_un_ticker, t): t for t in lista_tickers}
        for futuro in concurrent.futures.as_completed(futuros):
            try:
                ticker, datos = futuro.result(timeout=25)
                dict_fundamentales[ticker] = datos
            except Exception:
                pass

    return dict_fundamentales


def descargar_toda_la_banca_lote(lista_tickers: list) -> pd.DataFrame:
    """
    Descarga masiva de TODOS los tickers en UNA única llamada de red vía data_provider.
    [V7] period='60d' para disponer de suficientes sesiones para
    SMA-20 (≥20 barras) y RSI-14 (≥15 barras).
    Delega a get_price_batch() (SSOT de precios con caché 5min y sesión camuflada).
    """
    if not lista_tickers:
        return pd.DataFrame()
    return get_price_batch(tuple(lista_tickers))


# ═══════════════════════════════════════════════════════════════════
# 🎯 STOCKLAB V7 - MOTOR DE PUNTUACIÓN CUANTITATIVA
# ═══════════════════════════════════════════════════════════════════

def _clean_float(val):
    if val is None:
        return None
    if isinstance(val, (pd.Series, np.ndarray, list)):
        if len(val) == 0:
            return None
        val = val[0]
    try:
        num = float(val)
        return None if (np.isnan(num) or np.isinf(num)) else num
    except (ValueError, TypeError):
        return None


def _normalizar_div_pct(dividend_yield_raw: Optional[float]) -> Optional[float]:
    """
    Convierte dividend_yield a porcentaje (0-100) de forma completamente segura.
    Maneja tanto escala decimal (0.0369 -> 3.69%) como porcentajes pre-escalados (3.69 -> 3.69%).
    Protege contra el bug donde un dividendo < 1% (ej: 0.85% de Grifols o 0.82% de ArcelorMittal)
    se multiplicaba erróneamente por 100 produciendo 85.0% u 82.0%.
    """
    if dividend_yield_raw is None:
        return None
    try:
        val = float(dividend_yield_raw)
        if np.isnan(val) or np.isinf(val):
            return None
        # Si viene en decimal puro (ej: 0.0369 para 3.69%, 0.0085 para 0.85%)
        if abs(val) < 0.25:
            return val * 100.0
        # Si viene >= 0.25, ya es un porcentaje (ej: 0.85% de Grifols o 3.69% de BBVA)
        return val
    except (ValueError, TypeError):
        return None


def _interp_score(val: float, puntos: list) -> float:
    """
    Interpolación lineal suave por tramos.
    `puntos` debe venir ordenado por x creciente: [(x0, y0), (x1, y1), ...].
    Elimina saltos discretos bruscos ("cliffs") y empates artificiales.
    """
    if val <= puntos[0][0]:
        return float(puntos[0][1])
    if val >= puntos[-1][0]:
        return float(puntos[-1][1])
    for i in range(len(puntos) - 1):
        x0, y0 = puntos[i]
        x1, y1 = puntos[i + 1]
        if x0 <= val <= x1:
            if x1 == x0:
                return float(y1)
            return float(y0 + ((val - x0) / (x1 - x0)) * (y1 - y0))
    return float(puntos[-1][1])


def _normalizar_ratio_pct(val_raw: Optional[float], info: Optional[dict] = None) -> Optional[float]:
    """
    Convierte ratios decimales (ROE, ROA, márgenes, crecimiento, payout) a porcentaje.
    - Si `info` proviene de data_provider (`_data_source` presente), los ratios son
      siempre decimales puros (p. ej. 1.413 -> 141.3%, -3.938 -> -393.8%).
    - Sin `_data_source` (dicts externos o tests), multiplica por 100 si |val| <= 5.0.
    """
    val = _clean_float(val_raw)
    if val is None:
        return None
    if info and info.get('_data_source'):
        return val * 100.0
    return val * 100.0 if abs(val) <= 5.0 else val


def _es_sector_financiero(sector: str) -> bool:
    """
    [V7.1] Detecta si una empresa pertenece a banca, servicios financieros o seguros,
    cuyo balance es estructuralmente diferente al de una empresa industrial.
    Excluye holdings, sociedades de inversión o carteras (ej. Libertas 7, Cevasa, Alantra).
    """
    if not sector:
        return False
    for excl in ('Holding', 'Inversi', 'Cartera', 'Investment'):
        if excl in sector:
            return False
    SECTORES_FIN = ('Financiero', 'Banca', 'Seguros')
    return any(s in sector for s in SECTORES_FIN)


def _es_banca(sector: str) -> bool:
    """Distingue banca pura de aseguradoras (las aseguradoras tienen márgenes operativos estándar)."""
    return _es_sector_financiero(sector) and ('Seguros' not in (sector or ''))


def _calcular_cobertura_fundamental(info: dict, sector: str = '') -> dict:
    """
    [V8.5] Audita la completitud de fundamentales clave según el modelo aplicable
    (Estándar: 9 métricas; Financiero: 8 métricas adaptadas sin EV/EBITDA ni D/E bancario).
    """
    info = info or {}
    es_fin = _es_sector_financiero(sector or info.get('sector') or '')

    tpe = _clean_float(info.get('trailingPE'))
    fpe = _clean_float(info.get('forwardPE'))
    tiene_per = (tpe is not None and tpe > 0) or (fpe is not None and fpe > 0)
    pb = _clean_float(info.get('priceToBook'))
    ev = _clean_float(info.get('enterpriseToEbitda'))
    mcap = _clean_float(info.get('marketCap'))

    if es_fin:
        checks = [
            ('PER', tiene_per),
            ('P/B', pb is not None and pb > 0),
            ('ROE', _clean_float(info.get('returnOnEquity')) is not None),
            ('ROA', _clean_float(info.get('returnOnAssets')) is not None),
            ('Margen Op.', _clean_float(info.get('operatingMargins')) is not None),
            ('Margen Neto', _clean_float(info.get('profitMargins')) is not None),
            ('Crec. Ingresos', _clean_float(info.get('revenueGrowth')) is not None),
            ('Market Cap', mcap is not None and mcap > 0),
        ]
    else:
        checks = [
            ('PER', tiene_per),
            ('EV/EBITDA', ev is not None and ev > 0),
            ('P/B', pb is not None and pb > 0),
            ('ROE', _clean_float(info.get('returnOnEquity')) is not None),
            ('Margen Op.', _clean_float(info.get('operatingMargins')) is not None),
            ('Margen Neto', _clean_float(info.get('profitMargins')) is not None),
            ('Crec. Ingresos', _clean_float(info.get('revenueGrowth')) is not None),
            ('Deuda/Patrimonio', _clean_float(info.get('debtToEquity')) is not None),
            ('Liquidez (CR)', _clean_float(info.get('currentRatio')) is not None),
        ]

    total = len(checks)
    disponibles = sum(1 for _, ok in checks if ok)
    faltantes = [nombre for nombre, ok in checks if not ok]
    pct = round((disponibles / total) * 100.0, 1) if total > 0 else 0.0
    nivel = 'Alta' if pct >= 85.0 else ('Media' if pct >= 60.0 else 'Baja')
    return {
        'disponibles': disponibles,
        'total': total,
        'pct': pct,
        'nivel': nivel,
        'faltantes': faltantes,
        'modelo_financiero': es_fin,
    }


def _fmt_val(val, suffix: str = '', decimals: int = 2, fallback: str = 'N/A') -> str:
    """
    [V7.5] Formatea un valor numérico de forma segura.
    Retorna `fallback` si el valor es None, NaN o infinito.
    Elimina la posibilidad de que aparezca 'nan' o 'inf' en la UI.
    """
    if val is None:
        return fallback
    try:
        if np.isnan(val) or np.isinf(val):
            return fallback
    except (TypeError, ValueError):
        return fallback
    return f"{round(val, decimals)}{suffix}"


def _calcular_calidad_empresarial(info: dict, sector: str = '') -> float:
    """
    SCORE 1 - Calidad Empresarial (Máx 100 puntos, tras normalización de base 95)
    [V8.5] Delega toda la lógica a _desglosar_calidad_detalle() — Single Source of Truth.
    """
    try:
        desglose = _desglosar_calidad_detalle(info, sector)
        bruto = (desglose['calidad_negocio'] + desglose['fortaleza_financiera'] +
                 desglose['dividendos'] + desglose['ventaja_competitiva'])
        score = bruto / 95.0 * 100.0
    except Exception as e:
        print(f"⚠️ Error calculando calidad empresarial: {e}")
        score = 0.0
    return min(100.0, max(0.0, score))


def _calcular_oportunidad_inversion(df_historico: pd.DataFrame, info: dict, variacion_diaria: float, sector: str = '') -> float:
    """
    SCORE 2 - Oportunidad de Inversión (Máx 100 puntos)
    [V8.5] Delega toda la lógica a _desglosar_oportunidad_detalle() — Single Source of Truth.
    """
    try:
        desglose = _desglosar_oportunidad_detalle(df_historico, info, variacion_diaria, sector=sector)
        score = (desglose['valoracion'] + desglose['momentum_tecnico'] +
                 desglose['momentum_fundamental'])
    except Exception as e:
        print(f"⚠️ Error calculando oportunidad de inversión: {e}")
        score = 50.0
    return min(100.0, max(0.0, score))


def calcular_score_stocklab_v7(df_historico: pd.DataFrame, info: dict, variacion_diaria: float, sector: str = '') -> dict:
    """
    Motor principal StockLab V8.5 (mantiene alias V7 para compatibilidad hacia atrás).
    
    Calcula:
    - CALIDAD (0-100)
    - OPORTUNIDAD (0-100)
    - SCORE STOCKLAB (ponderación 60% calidad + 40% oportunidad)
    """
    calidad = _calcular_calidad_empresarial(info, sector)
    oportunidad = _calcular_oportunidad_inversion(df_historico, info, variacion_diaria, sector=sector)
    
    score_final = (calidad * 0.60) + (oportunidad * 0.40)
    
    return {
        'calidad': round(calidad, 1),
        'oportunidad': round(oportunidad, 1),
        'score_final': round(score_final, 1)
    }


def calcular_score_stocklab_v8(df_historico: pd.DataFrame, info: dict, variacion_diaria: float, sector: str = '') -> dict:
    """Alias explícito para StockLab V8/V8.5."""
    return calcular_score_stocklab_v7(df_historico, info, variacion_diaria, sector)


def _desglosar_calidad_detalle(info: dict, sector: str = '') -> dict:
    """
    [V8.5] Desglosa la Calidad Empresarial en sus 4 componentes internos (base 95 pts):
    - calidad_negocio (0-40): ROE + Margen Operativo calibrado + Crecimiento ingresos (curvas suaves)
    - fortaleza_financiera (0-30): ROA + Margen Neto bancario en Banca; D/E + Liquidez en Estándar
    - dividendos (0-10): Yield suave con control de payout/yield-trap o bonus compounder
    - ventaja_competitiva (0-15): Market Cap log-suave + Foso Operativo calibrado + Moat ROA/ROE
    """
    try:
        info = info or {}
        sector_eff = sector or info.get('sector') or ''
        modelo_financiero = _es_sector_financiero(sector_eff)
        es_banca = _es_banca(sector_eff)

        roe_pct = _normalizar_ratio_pct(info.get('returnOnEquity'), info)
        roa_pct = _normalizar_ratio_pct(info.get('returnOnAssets'), info)
        op_margin_pct = _normalizar_ratio_pct(info.get('operatingMargins'), info)
        profit_margin_pct = _normalizar_ratio_pct(info.get('profitMargins'), info)
        rev_growth_pct = _normalizar_ratio_pct(info.get('revenueGrowth'), info)
        debt_equity = _clean_float(info.get('debtToEquity'))
        current_ratio = _clean_float(info.get('currentRatio'))
        p_b = _clean_float(info.get('priceToBook'))
        ajustes_aplicados = []

        # ── CALIDAD DEL NEGOCIO (Máx 40 pts) ──
        if roe_pct is None:
            score_roe = 4.0
        elif p_b is not None and p_b <= 0:
            score_roe = 2.0
            ajustes_aplicados.append('Patrimonio neto negativo (ROE limitado)')
        else:
            if (not modelo_financiero) and debt_equity is not None and debt_equity > 3.5 and roe_pct > 25.0:
                roe_eff = 25.0
                ajustes_aplicados.append(f'ROE apalancado ({roe_pct:.1f}% con D/E {debt_equity:.1f}) acotado')
            else:
                roe_eff = roe_pct
            score_roe = _interp_score(
                roe_eff,
                [(-5.0, 0.0), (0.0, 1.0), (5.0, 5.0), (10.0, 8.5), (15.0, 11.5), (22.0, 14.0), (30.0, 15.0)],
            )

        if op_margin_pct is None:
            score_margin = 4.0
            op_margin_eff = 0.0
        else:
            op_margin_eff = op_margin_pct
            if es_banca:
                score_margin = _interp_score(
                    op_margin_pct,
                    [(10.0, 1.0), (25.0, 5.0), (40.0, 8.5), (50.0, 11.5), (60.0, 14.0), (70.0, 15.0)],
                )
            else:
                score_margin = _interp_score(
                    op_margin_pct,
                    [(-5.0, 0.0), (0.0, 1.0), (5.0, 5.0), (10.0, 8.5), (15.0, 11.5), (22.0, 14.0), (30.0, 15.0)],
                )

        if rev_growth_pct is None:
            score_crecimiento = 2.5
        else:
            score_crecimiento = _interp_score(
                rev_growth_pct,
                [(-15.0, 0.0), (-5.0, 1.5), (0.0, 3.0), (5.0, 5.5), (10.0, 7.5), (18.0, 9.2), (30.0, 10.0)],
            )

        calidad_negocio = min(40.0, score_roe + score_margin + score_crecimiento)

        _hints_cn = []
        _hints_cn.append('Sin datos de ROE' if roe_pct is None else f'ROE {roe_pct:.1f}%')
        if op_margin_pct is None:
            _hints_cn.append('Sin datos de margen')
        else:
            _hints_cn.append(f'Margen Op. {op_margin_pct:.1f}%' + (' (escala banca)' if es_banca else ''))
        _hints_cn.append('Sin datos de crecimiento' if rev_growth_pct is None else f'Crec. ingresos {rev_growth_pct:+.1f}%')
        reason_calidad_negocio = ' · '.join(_hints_cn)

        # ── FORTALEZA FINANCIERA (Máx 30 pts) ──
        sin_datos_fortaleza = False
        if modelo_financiero:
            if es_banca:
                if roa_pct is not None:
                    score_ff1 = _interp_score(
                        roa_pct,
                        [(0.0, 0.0), (0.4, 4.0), (0.7, 8.0), (1.0, 11.5), (1.4, 14.5), (1.8, 16.0)],
                    )
                elif roe_pct is not None:
                    score_ff1 = _interp_score(
                        roe_pct,
                        [(0.0, 1.0), (8.0, 6.0), (12.0, 9.5), (18.0, 13.5), (25.0, 15.0)],
                    )
                else:
                    score_ff1 = 5.0

                if profit_margin_pct is not None:
                    score_ff2 = _interp_score(
                        profit_margin_pct,
                        [(10.0, 2.0), (20.0, 5.5), (30.0, 8.5), (38.0, 11.5), (45.0, 13.5), (55.0, 14.0)],
                    )
                else:
                    score_ff2 = 5.0

                sin_datos_fortaleza = (roa_pct is None and roe_pct is None and profit_margin_pct is None)
                fortaleza_financiera = min(30.0, score_ff1 + score_ff2)
                _hints_ff = []
                if roa_pct is not None:
                    _hints_ff.append(f'ROA bancario {roa_pct:.2f}%')
                elif roe_pct is not None:
                    _hints_ff.append(f'ROE bancario {roe_pct:.1f}% (sin ROA)')
                else:
                    _hints_ff.append('Sin datos de ROA/ROE')
                _hints_ff.append('Sin margen neto' if profit_margin_pct is None else f'Margen neto {profit_margin_pct:.1f}%')
                reason_fortaleza = ' · '.join(_hints_ff)
            else:
                # Aseguradoras (p. ej. Mapfre, Línea Directa)
                if debt_equity is not None:
                    score_ff1 = _interp_score(
                        debt_equity,
                        [(0.2, 16.0), (0.5, 13.5), (1.0, 10.5), (2.0, 6.0), (3.5, 2.0), (5.0, 0.0)],
                    )
                elif roa_pct is not None:
                    score_ff1 = _interp_score(roa_pct, [(0.0, 1.0), (2.0, 7.0), (4.0, 11.0), (7.0, 15.0)])
                else:
                    score_ff1 = 5.0

                if profit_margin_pct is not None:
                    score_ff2 = _interp_score(
                        profit_margin_pct,
                        [(0.0, 1.0), (4.0, 6.0), (8.0, 10.0), (12.0, 13.0), (18.0, 14.0)],
                    )
                else:
                    score_ff2 = 5.0

                sin_datos_fortaleza = (debt_equity is None and roa_pct is None and profit_margin_pct is None)
                fortaleza_financiera = min(30.0, score_ff1 + score_ff2)
                _hints_ff = []
                if debt_equity is not None:
                    _hints_ff.append(f'D/E seguros {debt_equity:.2f}')
                elif roa_pct is not None:
                    _hints_ff.append(f'ROA {roa_pct:.2f}%')
                else:
                    _hints_ff.append('Sin datos de solvencia')
                _hints_ff.append('Sin margen neto' if profit_margin_pct is None else f'Margen neto {profit_margin_pct:.1f}%')
                reason_fortaleza = ' · '.join(_hints_ff)
        else:
            if p_b is not None and p_b <= 0:
                score_deuda = 0.0
            elif debt_equity is None:
                score_deuda = 5.0
            else:
                score_deuda = _interp_score(
                    debt_equity,
                    [(0.0, 15.0), (0.4, 14.0), (0.8, 11.5), (1.5, 8.0), (2.5, 4.5), (4.0, 1.0), (6.0, 0.0)],
                )

            if current_ratio is None:
                score_current = 5.0
            else:
                score_current = _interp_score(
                    current_ratio,
                    [(0.5, 1.0), (0.8, 4.5), (1.1, 8.0), (1.5, 11.5), (2.0, 14.0), (2.8, 15.0)],
                )

            sin_datos_fortaleza = (debt_equity is None and current_ratio is None)
            fortaleza_financiera = min(30.0, score_deuda + score_current)

            if roe_pct is not None:
                if roe_pct < 5.0 and fortaleza_financiera > 16.0:
                    fortaleza_financiera = 16.0
                    ajustes_aplicados.append(f'Tope solvencia 16/30 por ROE < 5% ({roe_pct:.1f}%)')
                elif roe_pct < 10.0 and fortaleza_financiera > 23.0:
                    fortaleza_financiera = 23.0
                    ajustes_aplicados.append(f'Tope solvencia 23/30 por ROE < 10% ({roe_pct:.1f}%)')

            _hints_ff = []
            if p_b is not None and p_b <= 0:
                _hints_ff.append('Patrimonio neto negativo (0 pts deuda)')
            elif debt_equity is None:
                _hints_ff.append('Sin datos de deuda')
            else:
                _hints_ff.append(f'D/E {debt_equity:.2f}')
            _hints_ff.append('Sin datos de liquidez' if current_ratio is None else f'Current Ratio {current_ratio:.2f}')
            if roe_pct is not None and roe_pct < 5.0:
                _hints_ff.append(f'ROE bajo ({roe_pct:.1f}%) — tope 16')
            reason_fortaleza = ' · '.join(_hints_ff)

        # ── DIVIDENDOS & ASIGNACIÓN EFICIENTE (Máx 10 pts) ──
        dividend_yield_raw = _clean_float(info.get('dividendYield'))
        payout_pct = _normalizar_ratio_pct(info.get('payoutRatio'), info)
        div_pct = _normalizar_div_pct(dividend_yield_raw)

        if div_pct is not None and div_pct > 0:
            dividendos = _interp_score(
                div_pct,
                [(0.0, 0.0), (1.0, 2.5), (2.5, 5.5), (4.0, 8.0), (5.5, 9.5), (7.0, 10.0)],
            )
            if div_pct > 10.0:
                dividendos = max(0.0, dividendos - 1.5)
                ajustes_aplicados.append(f'Yield extremo ({div_pct:.1f}%) penalizado (-1.5 pts)')
            if payout_pct is not None and payout_pct > 60.0:
                pen_payout = _interp_score(payout_pct, [(60.0, 0.0), (80.0, 1.5), (100.0, 3.5), (130.0, 5.0)])
                dividendos = max(0.0, dividendos - pen_payout)
                if pen_payout >= 1.5:
                    _p_lbl = '>500%' if payout_pct > 500 else f'{payout_pct:.0f}%'
                    ajustes_aplicados.append(f'Payout exigente ({_p_lbl}, -{pen_payout:.1f} pts)')
        else:
            if roe_pct is not None and op_margin_pct is not None and roe_pct >= 10.0 and op_margin_pct >= 5.0:
                dividendos = _interp_score(roe_pct, [(10.0, 4.5), (18.0, 6.0), (28.0, 6.5)])
                ajustes_aplicados.append(f'Bonus compounder (+{dividendos:.1f} pts)')
            elif roe_pct is not None and roe_pct >= 5.0:
                dividendos = 3.0
            else:
                dividendos = 0.0

        sin_datos_dividendos = (dividend_yield_raw is None and dividendos <= 0)

        _hints_div = []
        if dividend_yield_raw is None or (div_pct is not None and div_pct <= 0):
            if roe_pct is not None and op_margin_pct is not None and roe_pct >= 10.0 and op_margin_pct >= 5.0:
                _hints_div.append('Sin dividendo — Bonus compounder (ROE alto + margen)')
            elif roe_pct is not None and roe_pct >= 5.0:
                _hints_div.append('Sin dividendo — Reinvierte con ROE aceptable')
            else:
                _hints_div.append('Sin datos de dividendo')
        else:
            _hints_div.append(f'Yield {div_pct:.2f}%')
            if payout_pct is not None:
                _p_show = '>500%' if payout_pct > 500 else f'{payout_pct:.0f}%'
                _hints_div.append(f'Payout {_p_show}')
                if payout_pct >= 100.0:
                    _hints_div.append('Payout insostenible')
                elif payout_pct >= 80.0:
                    _hints_div.append('Payout elevado')
        reason_dividendos = ' · '.join(_hints_div)

        # ── VENTAJA COMPETITIVA Y FOSO REAL (Máx 15 pts) ──
        market_cap = _clean_float(info.get('marketCap'))
        if market_cap is None or market_cap <= 0:
            score_cap = 0.0
        else:
            score_cap = _interp_score(
                float(np.log10(market_cap)),
                [(7.5, 0.0), (8.0, 1.2), (8.5, 2.8), (9.0, 4.8), (9.5, 6.5), (10.2, 7.6), (10.8, 8.0)],
            )

        if op_margin_pct is None:
            score_foso = 0.0
        elif es_banca:
            score_foso = _interp_score(op_margin_pct, [(25.0, 0.0), (40.0, 2.0), (52.0, 3.8), (62.0, 5.0)])
        else:
            score_foso = _interp_score(op_margin_pct, [(5.0, 0.0), (10.0, 1.8), (16.0, 3.5), (24.0, 5.0)])

        score_moat = 0.0
        if es_banca and roa_pct is not None:
            score_moat = _interp_score(roa_pct, [(0.8, 0.0), (1.1, 1.0), (1.4, 2.0)])
        elif (not es_banca) and roe_pct is not None and (debt_equity is None or debt_equity <= 2.5):
            score_moat = _interp_score(roe_pct, [(12.0, 0.0), (16.0, 1.0), (22.0, 2.0)])

        ventaja_competitiva = min(15.0, score_cap + score_foso + score_moat)

        _hints_vc = []
        if market_cap is None or market_cap <= 0:
            _hints_vc.append('Sin datos de Market Cap')
        elif market_cap > 3_000_000_000:
            _hints_vc.append(f'Large Cap ({market_cap/1e9:.1f}B)')
        elif market_cap > 1_000_000_000:
            _hints_vc.append(f'Mid Cap ({market_cap/1e9:.1f}B)')
        elif market_cap > 300_000_000:
            _hints_vc.append(f'Small Cap ({market_cap/1e6:.0f}M)')
        elif market_cap > 100_000_000:
            _hints_vc.append(f'Micro Cap ({market_cap/1e6:.0f}M)')
        else:
            _hints_vc.append(f'Nano Cap ({market_cap/1e6:.0f}M)')

        if op_margin_pct is None or score_foso <= 0.1:
            _hints_vc.append('Sin foso operativo relevante')
        else:
            _hints_vc.append(f'Foso Op. {op_margin_eff:.1f}%')
        if score_moat >= 0.5:
            _m_lbl = 'ROA moat' if es_banca else 'ROE moat'
            _hints_vc.append(f'{_m_lbl} +{score_moat:.1f} pts')
        reason_ventaja = ' · '.join(_hints_vc)

        return {
            'calidad_negocio':      round(calidad_negocio, 1),
            'fortaleza_financiera': round(fortaleza_financiera, 1),
            'dividendos':           round(dividendos, 1),
            'ventaja_competitiva':  round(ventaja_competitiva, 1),
            'modelo_financiero':    modelo_financiero,
            'es_banca':             es_banca,
            'sin_datos_fortaleza':  sin_datos_fortaleza,
            'sin_datos_dividendos': sin_datos_dividendos,
            'ajustes':              ajustes_aplicados,
            '_reason_calidad_negocio':      reason_calidad_negocio,
            '_reason_fortaleza_financiera': reason_fortaleza,
            '_reason_dividendos':           reason_dividendos,
            '_reason_ventaja_competitiva':  reason_ventaja,
        }
    except Exception:
        return {
            'calidad_negocio':      0.0,
            'fortaleza_financiera': 0.0,
            'dividendos':           0.0,
            'ventaja_competitiva':  0.0,
            'modelo_financiero':    False,
            'es_banca':             False,
            'sin_datos_fortaleza':  False,
            'sin_datos_dividendos': False,
            'ajustes':              [],
            '_reason_calidad_negocio':      'Error en el cálculo',
            '_reason_fortaleza_financiera': 'Error en el cálculo',
            '_reason_dividendos':           'Error en el cálculo',
            '_reason_ventaja_competitiva':  'Error en el cálculo',
        }


def _desglosar_oportunidad_detalle(df_historico: pd.DataFrame, info: dict, variacion_diaria: float, sector: str = '') -> dict:
    """
    [V8.5] Desglosa la Oportunidad de Inversión en sus 3 componentes internos (Máx 100 pts):
    - valoracion (0-40): curvas suaves; en Financiero pondera PER (20) + P/B (20) sin EV/EBITDA fantasma
    - momentum_tecnico (0-30): tendencia multi-sesión (SMA50/20 + Mom 20d + RSI-14 + cercanía Máx 52s)
    - momentum_fundamental (0-30): crecimiento ingresos + crecimiento beneficios (o margen calibrado) + rentabilidad
    """
    try:
        info = info or {}
        sector_eff = sector or info.get('sector') or ''
        modelo_financiero = _es_sector_financiero(sector_eff)
        es_banca = _es_banca(sector_eff)

        # ── VALORACIÓN (Máx 40 pts) ──
        trailing_pe = _clean_float(info.get('trailingPE'))
        forward_pe = _clean_float(info.get('forwardPE'))
        per = trailing_pe if (trailing_pe and trailing_pe > 0) else (forward_pe if (forward_pe and forward_pe > 0) else None)
        ev_ebitda = _clean_float(info.get('enterpriseToEbitda'))
        p_b = _clean_float(info.get('priceToBook'))
        p_s = _clean_float(info.get('priceToSales'))
        patrimonio_neg = (p_b is not None and p_b <= 0)
        per_atipico = (per is not None and (per < 3.0 or patrimonio_neg))

        if per is None or per <= 0:
            score_per = 0.0
        elif per_atipico:
            score_per = 5.0
        elif modelo_financiero:
            score_per = _interp_score(
                per,
                [(4.0, 18.0), (7.5, 20.0), (10.5, 16.5), (13.0, 12.5), (17.0, 7.5), (25.0, 2.5), (40.0, 0.5)],
            )
        else:
            score_per = _interp_score(
                per,
                [(3.0, 16.0), (7.0, 20.0), (12.0, 18.0), (16.0, 14.5), (21.0, 10.0), (28.0, 5.5), (40.0, 2.0), (60.0, 0.5)],
            )

        _hints_val = []
        if per is None or per <= 0:
            _hints_val.append('Sin datos de PER')
        elif per_atipico:
            _hints_val.append(f'PER {per:.1f} (Atípico)')
        else:
            _tag_per = 'Atractivo' if per < 12 else ('Razonable' if per <= 18 else ('Exigente' if per <= 26 else 'Elevado'))
            _hints_val.append(f'PER {per:.1f} ({_tag_per})')

        if modelo_financiero:
            if p_b is not None and p_b > 0:
                score_pb = _interp_score(
                    p_b,
                    [(0.6, 20.0), (0.9, 17.0), (1.2, 13.5), (1.6, 9.5), (2.2, 5.5), (3.5, 2.0), (5.0, 0.5)],
                )
                _hints_val.append(f'P/B financiero {p_b:.2f} (peso doble)')
            else:
                score_pb = 4.0
                _hints_val.append('Sin P/B financiero')
            valoracion = min(40.0, score_per + score_pb)
        else:
            if ev_ebitda is None or ev_ebitda <= 0:
                score_ebitda = 2.5
                _hints_val.append('Sin EV/EBITDA')
            else:
                score_ebitda = _interp_score(
                    ev_ebitda,
                    [(3.0, 9.5), (6.0, 10.0), (9.0, 8.0), (12.0, 5.5), (16.0, 3.0), (22.0, 1.0), (30.0, 0.0)],
                )
                _hints_val.append(f'EV/EBITDA {ev_ebitda:.1f}')

            if p_b is not None and p_b > 0:
                score_pb = _interp_score(
                    p_b,
                    [(0.6, 10.0), (1.2, 8.5), (2.0, 6.2), (3.5, 3.8), (6.0, 1.8), (12.0, 0.5)],
                )
                _hints_val.append(f'P/B {p_b:.2f}')
            elif p_b is not None and p_b <= 0:
                score_pb = 0.0
                _hints_val.append(f'P/B negativo ({p_b:.2f})')
            elif p_s is not None and p_s > 0:
                score_pb = _interp_score(p_s, [(0.5, 8.0), (1.5, 6.0), (3.0, 3.5), (6.0, 1.0)])
                _hints_val.append(f'P/S {p_s:.2f} (fallback)')
            else:
                score_pb = 0.0
                _hints_val.append('Sin P/B ni P/S')

            valoracion = min(40.0, score_per + score_ebitda + score_pb)

        reason_valoracion = ' · '.join(_hints_val)

        # ── MOMENTUM TÉCNICO MULTI-SESIÓN (Máx 30 pts) — Sin ruido intradía de 1 día ──
        momentum_tecnico = 0.0
        _hints_mt = []
        _mt_es_fallback = True

        if df_historico is not None and not df_historico.empty:
            try:
                if isinstance(df_historico.columns, pd.MultiIndex):
                    col_close = df_historico['Close']
                else:
                    col_close = df_historico['Close'] if 'Close' in df_historico.columns else pd.Series(dtype=float)
                if isinstance(col_close, pd.DataFrame):
                    col_close = col_close.iloc[:, 0]
                precios = pd.to_numeric(col_close, errors='coerce').dropna()

                if len(precios) >= 20:
                    _mt_es_fallback = False
                    precio_actual = float(precios.iloc[-1])

                    # 1) Tendencia SMA50 (o SMA20 si hay < 50 barras) — Máx 10 pts
                    if len(precios) >= 50:
                        sma_50 = float(precios.rolling(window=50).mean().iloc[-1])
                        if sma_50 > 0:
                            dist_50 = (precio_actual - sma_50) / sma_50 * 100.0
                            momentum_tecnico += _interp_score(
                                dist_50,
                                [(-12.0, 1.0), (-4.0, 3.5), (0.0, 6.0), (4.0, 8.5), (10.0, 10.0), (25.0, 8.0)],
                            )
                            _hints_mt.append(f'SMA50 {dist_50:+.1f}%')
                    else:
                        sma_20 = float(precios.rolling(window=20).mean().iloc[-1])
                        if sma_20 > 0:
                            dist_20 = (precio_actual - sma_20) / sma_20 * 100.0
                            momentum_tecnico += _interp_score(
                                dist_20,
                                [(-8.0, 1.0), (-2.0, 4.0), (0.0, 6.0), (3.0, 8.5), (8.0, 10.0), (18.0, 8.0)],
                            )
                            _hints_mt.append(f'SMA20 {dist_20:+.1f}%')

                    # 2) Momentum 20 sesiones (~1 mes bursátil) — Máx 8 pts
                    if len(precios) >= 21 and float(precios.iloc[-21]) > 0:
                        ret_20d = (precio_actual / float(precios.iloc[-21]) - 1.0) * 100.0
                        momentum_tecnico += _interp_score(
                            ret_20d,
                            [(-15.0, 0.5), (-5.0, 2.5), (0.0, 4.5), (5.0, 7.0), (12.0, 8.0), (25.0, 6.5)],
                        )
                        _hints_mt.append(f'Mom 20d {ret_20d:+.1f}%')
                    else:
                        momentum_tecnico += 4.0

                    # 3) RSI-14 equilibrado — Máx 7 pts
                    delta = precios.diff()
                    gain = float(delta.where(delta > 0, 0.0).rolling(window=14).mean().iloc[-1])
                    loss = float((-delta.where(delta < 0, 0.0)).rolling(window=14).mean().iloc[-1])
                    rsi = 100.0 - (100.0 / (1.0 + gain / loss)) if loss > 0 else (50.0 if gain == 0 else 100.0)
                    momentum_tecnico += _interp_score(
                        rsi,
                        [(15.0, 3.5), (30.0, 5.0), (45.0, 7.0), (60.0, 7.0), (72.0, 4.5), (85.0, 2.0)],
                    )
                    _rsi_tag = 'sobreventa' if rsi < 35 else ('sobrecompra' if rsi > 70 else 'neutro')
                    _hints_mt.append(f'RSI {rsi:.0f} ({_rsi_tag})')

                    # 4) Proximidad a máximos de 52 semanas — Máx 5 pts
                    if len(precios) >= 60:
                        max_52s = float(precios.tail(252).max())
                        if max_52s > 0:
                            dist_52h = (precio_actual / max_52s - 1.0) * 100.0
                            momentum_tecnico += _interp_score(
                                dist_52h,
                                [(-40.0, 0.5), (-20.0, 2.0), (-10.0, 3.8), (-3.0, 5.0), (0.0, 5.0)],
                            )
                            _hints_mt.append(f'Máx 52s {dist_52h:+.1f}%')
                    else:
                        momentum_tecnico += 2.5
            except Exception:
                pass

        if _mt_es_fallback or momentum_tecnico <= 0:
            momentum_tecnico = 15.0
            _hints_mt = ['Sin histórico suficiente — valor neutro estimado']

        momentum_tecnico = min(30.0, max(0.0, momentum_tecnico))
        reason_momentum_tecnico = ' · '.join(_hints_mt) if _hints_mt else 'Datos técnicos calculados'

        # ── MOMENTUM FUNDAMENTAL (Máx 30 pts) ──
        rev_growth_pct = _normalizar_ratio_pct(info.get('revenueGrowth'), info)
        earnings_growth_pct = _normalizar_ratio_pct(info.get('earningsGrowth'), info)
        profit_margin_pct = _normalizar_ratio_pct(info.get('profitMargins'), info)

        # 1) Crecimiento de ingresos (Máx 12 pts)
        if rev_growth_pct is None:
            score_rev = 3.5
        else:
            score_rev = _interp_score(
                rev_growth_pct,
                [(-15.0, 0.0), (-5.0, 2.0), (0.0, 4.5), (6.0, 7.5), (12.0, 10.0), (22.0, 12.0)],
            )

        # 2) Crecimiento de beneficios (Máx 10 pts) o fallback a margen neto calibrado
        if earnings_growth_pct is not None:
            score_earn = _interp_score(
                earnings_growth_pct,
                [(-30.0, 0.5), (-10.0, 2.5), (0.0, 5.0), (10.0, 7.5), (25.0, 9.5), (50.0, 10.0)],
            )
        elif profit_margin_pct is not None:
            if es_banca:
                score_earn = _interp_score(
                    profit_margin_pct,
                    [(10.0, 1.5), (25.0, 4.5), (35.0, 7.0), (45.0, 9.0), (55.0, 10.0)],
                )
            else:
                score_earn = _interp_score(
                    profit_margin_pct,
                    [(-5.0, 0.0), (0.0, 1.5), (6.0, 4.5), (12.0, 7.5), (20.0, 9.5), (28.0, 10.0)],
                )
        else:
            score_earn = 3.0

        # 3) Conversión a beneficio neto (Máx 8 pts, calibrada por sector)
        if profit_margin_pct is None:
            score_prof = 2.5
        elif es_banca:
            score_prof = _interp_score(
                profit_margin_pct,
                [(10.0, 1.5), (25.0, 4.0), (35.0, 6.0), (45.0, 7.5), (55.0, 8.0)],
            )
        else:
            score_prof = _interp_score(
                profit_margin_pct,
                [(-5.0, 0.0), (0.0, 1.5), (6.0, 4.0), (12.0, 6.2), (20.0, 7.8), (28.0, 8.0)],
            )

        momentum_fundamental = min(30.0, score_rev + score_earn + score_prof)

        _hints_mf = []
        _hints_mf.append('Sin crec. ingresos' if rev_growth_pct is None else f'Crec. ingresos {rev_growth_pct:+.1f}%')
        if earnings_growth_pct is not None:
            _eg_show = '>500%' if earnings_growth_pct > 500 else f'{earnings_growth_pct:+.1f}%'
            _hints_mf.append(f'Crec. beneficios {_eg_show}')
        _hints_mf.append('Sin margen neto' if profit_margin_pct is None else f'Margen neto {profit_margin_pct:.1f}%')
        reason_momentum_fundamental = ' · '.join(_hints_mf)

        return {
            'valoracion': round(valoracion, 1),
            'momentum_tecnico': round(momentum_tecnico, 1),
            'momentum_fundamental': round(momentum_fundamental, 1),
            '_reason_valoracion':            reason_valoracion,
            '_reason_momentum_tecnico':      reason_momentum_tecnico,
            '_reason_momentum_fundamental':  reason_momentum_fundamental,
        }
    except Exception:
        return {
            'valoracion': 0.0,
            'momentum_tecnico': 0.0,
            'momentum_fundamental': 0.0,
            '_reason_valoracion':            'Error en el cálculo',
            '_reason_momentum_tecnico':      'Error en el cálculo',
            '_reason_momentum_fundamental':  'Error en el cálculo',
        }


def generar_resumen_fundamental_v7(info: dict) -> str:
    """[V8.5] Genera resumen legible de fundamentales. Usa _normalizar_ratio_pct + _fmt_val."""
    try:
        info = info or {}
        per_raw = _clean_float(info.get('trailingPE'))
        if per_raw is None or per_raw <= 0:
            per_raw = _clean_float(info.get('forwardPE'))
        div_raw = _clean_float(info.get('dividendYield'))
        p_b_raw = _clean_float(info.get('priceToBook'))
        roe_pct = _normalizar_ratio_pct(info.get('returnOnEquity'), info)
        div_pct = _normalizar_div_pct(div_raw)

        if per_raw is not None and per_raw > 0:
            return (f"PER: {_fmt_val(per_raw, decimals=2)} | "
                    f"Div Yield: {_fmt_val(div_pct, suffix='%', decimals=2)} | "
                    f"P/B: {_fmt_val(p_b_raw, decimals=2)} | "
                    f"ROE: {_fmt_val(roe_pct, suffix='%', decimals=2)}")
        elif any(v is not None for v in (div_pct, p_b_raw, roe_pct)):
            partes = []
            if div_pct is not None:
                partes.append(f"Div Yield: {_fmt_val(div_pct, suffix='%', decimals=2)}")
            if p_b_raw is not None:
                partes.append(f"P/B: {_fmt_val(p_b_raw, decimals=2)}")
            if roe_pct is not None:
                partes.append(f"ROE: {_fmt_val(roe_pct, suffix='%', decimals=2)}")
            return " | ".join(partes)
        else:
            return "Datos fundamentales limitados"

    except (ValueError, TypeError):
        return "Datos fundamentales no disponibles"


# ── CSS PREMIUM TOTAL — RANKING + INSPECTOR ──────────────────────
CSS_RANKING = """
    /* ═══════════════════════════════════════════════════════════════════
       STOCKLAB RANKING — Finance Terminal Premium (Mi Cartera Style)
       Ultra-fluid 120 FPS design, hardware-accelerated, zero layout thrashing
       ═══════════════════════════════════════════════════════════════════ */

    /* --- Keyframe Animations --- */
    @keyframes fade-in-up {
        from { opacity: 0; transform: translateY(8px); }
        to   { opacity: 1; transform: translateY(0);    }
    }
    @keyframes led-breathe {
        0%, 100% { opacity: 0.4; }
        50%       { opacity: 0.85; }
    }

    /* --- Global Foundation --- */
    .block-container {
        padding-top: 1.6rem !important;
        padding-bottom: 3rem !important;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
    }
    ::-webkit-scrollbar { width: 5px !important; height: 5px !important; }
    ::-webkit-scrollbar-track { background: #0d121c !important; }
    ::-webkit-scrollbar-thumb { background: rgba(148, 163, 184, 0.20) !important; border-radius: 999px !important; }

    /* --- KPI METRICS (Mi Cartera Secondary KPI Card Style) --- */
    section[data-testid="stMain"] div[data-testid="stMetric"],
    section[data-testid="stMain"] [data-testid="metric-container"],
    section[data-testid="stMain"] .stMetric {
        background: linear-gradient(180deg, #131a29 0%, #101623 100%) !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 12px !important;
        padding: 12px 16px !important;
        min-height: 90px !important;
        display: flex !important;
        flex-direction: column !important;
        justify-content: center !important;
        transition: border-color 0.25s ease, box-shadow 0.25s ease !important;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.24), 0 8px 24px -8px rgba(0, 0, 0, 0.34), inset 0 1px 0 rgba(255, 255, 255, 0.04), inset 0 1px 0 rgba(255,255,255,0.01) !important;
        position: relative !important;
        overflow: hidden !important;
    }
    section[data-testid="stMain"] div[data-testid="stMetric"]::before,
    section[data-testid="stMain"] .stMetric::before {
        display: none !important;
    }
    section[data-testid="stMain"] div[data-testid="stMetric"]:hover,
    section[data-testid="stMain"] .stMetric:hover {
        border-color: rgba(56, 189, 248, 0.28) !important;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.30), 0 12px 28px -10px rgba(0, 0, 0, 0.42), inset 0 1px 0 rgba(255, 255, 255, 0.06) !important;
    }
    section[data-testid="stMain"] div[data-testid="stMetric"]:hover::before,
    section[data-testid="stMain"] .stMetric:hover::before {
        display: none !important;
    }

    section[data-testid="stMain"] div[data-testid="stMetric"] label,
    section[data-testid="stMain"] .stMetric label {
        color: #78879c !important;
        font-size: 0.68rem !important;
        font-weight: 600 !important;
        text-transform: uppercase !important;
        letter-spacing: 0.08em !important;
        font-family: 'Inter', sans-serif !important;
        margin-bottom: 4px !important;
    }

    section[data-testid="stMain"] div[data-testid="stMetric"] div[data-testid="stMetricValue"],
    section[data-testid="stMain"] .stMetric div[data-testid="stMetricValue"] {
        color: #38bdf8 !important;
        font-weight: 800 !important;
        font-size: 1.35rem !important;
        font-family: 'JetBrains Mono', monospace !important;
        letter-spacing: -0.5px !important;
        text-shadow: none !important;
    }

    /* --- Form Controls / Selectboxes / Checkboxes --- */
    div[data-testid="stHorizontalBlock"]:has(.st-key-ranking_filter_watchlist) {
        gap: 14px !important;
        align-items: flex-start !important;
    }

    section[data-testid="stMain"] div[data-testid="stSelectbox"] > div > div {
        background-color: #0d121c !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 10px !important;
        color: #cbd5e1 !important;
        font-family: 'Inter', sans-serif !important;
        font-size: 0.86rem !important;
        height: 42px !important;
        min-height: 42px !important;
        max-height: 42px !important;
        display: flex !important;
        align-items: center !important;
        transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
    }
    section[data-testid="stMain"] div[data-testid="stSelectbox"] > div > div:focus-within {
        border-color: rgba(56, 189, 248, 0.35) !important;
        box-shadow: none !important;
    }

    /* BaseWeb Selectbox Dropdown Menu Popover */
    div[data-baseweb="popover"] {
        border-radius: 12px !important;
        box-shadow: 0 16px 40px rgba(0,0,0,0.85) !important;
        overflow: hidden !important;
    }
    div[data-baseweb="menu"] {
        background-color: #111723 !important;
        border: 1px solid rgba(56, 189, 248, 0.18) !important;
        border-radius: 12px !important;
    }
    div[data-baseweb="menu"] li {
        font-family: 'Inter', sans-serif !important;
        font-size: 0.84rem !important;
        color: #cbd5e1 !important;
        transition: background 0.15s ease, color 0.15s ease !important;
    }
    div[data-baseweb="menu"] li:hover {
        background-color: rgba(56, 189, 248, 0.08) !important;
        color: #38bdf8 !important;
    }
    /* Widget label typography - 100% unified across all 4 filter columns */
    div[data-testid="stHorizontalBlock"]:has(.st-key-ranking_filter_watchlist) label[data-testid="stWidgetLabel"] {
        min-height: 1.5rem !important;
        margin-bottom: 0.25rem !important;
        display: flex !important;
        align-items: center !important;
        font-family: 'Inter', sans-serif !important;
        font-size: 0.76rem !important;
        font-weight: 600 !important;
        color: #94a3b8 !important;
        letter-spacing: 0.05em !important;
        text-transform: uppercase !important;
    }

    div[data-testid="stHorizontalBlock"]:has(.st-key-ranking_filter_watchlist) label[data-testid="stWidgetLabel"] p {
        font-family: 'Inter', sans-serif !important;
        font-size: 0.76rem !important;
        font-weight: 600 !important;
        color: #94a3b8 !important;
        letter-spacing: 0.05em !important;
        text-transform: uppercase !important;
        margin: 0 !important;
        line-height: 1.4 !important;
    }

    /* --- Watchlist Filter Chip / Toggle --- */
    .st-key-ranking_filter_watchlist {
        margin-top: 0 !important;
        display: flex !important;
        flex-direction: column !important;
        justify-content: flex-start !important;
        width: 100% !important;
    }
    .st-key-ranking_filter_watchlist,
    .st-key-ranking_filter_watchlist > div,
    .st-key-ranking_filter_watchlist div[data-testid="element-container"],
    .st-key-ranking_filter_watchlist div[data-testid="stElementContainer"],
    .st-key-ranking_filter_watchlist div[data-testid="stVerticalBlock"] {
        width: 100% !important;
        max-width: 100% !important;
        gap: 0 !important;
    }
    .st-key-ranking_filter_watchlist div[data-testid="stCheckbox"] {
        margin: 0 !important;
        background-color: #0d121c !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 10px !important;
        padding: 0 14px !important;
        height: 42px !important;
        min-height: 42px !important;
        max-height: 42px !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        box-sizing: border-box !important;
        transition: all 0.2s ease !important;
        width: 100% !important;
        min-width: 100% !important;
        cursor: pointer !important;
    }
    .st-key-ranking_filter_watchlist div[data-testid="stCheckbox"]:hover {
        border-color: rgba(245, 158, 11, 0.45) !important;
        background: rgba(245, 158, 11, 0.05) !important;
    }
    .st-key-ranking_filter_watchlist div[data-testid="stCheckbox"]:has(input:checked) {
        border-color: rgba(245, 158, 11, 0.6) !important;
        background: rgba(245, 158, 11, 0.12) !important;
        box-shadow: none !important;
    }
    .st-key-ranking_filter_watchlist div[data-testid="stCheckbox"] label {
        color: #94a3b8 !important;
        font-weight: 600 !important;
        font-size: 0.84rem !important;
        font-family: 'Inter', sans-serif !important;
        margin: 0 !important;
        cursor: pointer !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        gap: 8px !important;
        width: 100% !important;
        height: 100% !important;
    }
    .st-key-ranking_filter_watchlist div[data-testid="stCheckbox"] label p {
        margin: 0 !important;
        padding: 0 !important;
        font-size: 0.84rem !important;
        font-family: 'Inter', sans-serif !important;
        line-height: 1 !important;
    }
    .st-key-ranking_filter_watchlist div[data-testid="stCheckbox"]:has(input:checked) label span,
    .st-key-ranking_filter_watchlist div[data-testid="stCheckbox"]:has(input:checked) label p {
        color: #fbbf24 !important;
        font-weight: 700 !important;
    }
    .st-key-ranking_filter_watchlist div[data-testid="stCheckbox"] input[type="checkbox"] {
        accent-color: #f59e0b !important;
        width: 16px !important;
        height: 16px !important;
        cursor: pointer !important;
        margin: 0 !important;
    }

    /* --- THEMATIC KPI CARDS (STOCKLAB OBSIDIAN NEON) --- */
    .kpi-row {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 14px;
        margin: 14px 0 18px 0;
    }
    .kpi-card {
        background:
            radial-gradient(ellipse 85% 65% at 50% -15%, var(--kpi-glow-light), transparent 72%),
            var(--sl-surface-glass);
        border: 1px solid var(--sl-border-default);
        border-radius: var(--sl-radius-lg);
        padding: 17px 18px;
        box-shadow: var(--sl-shadow-card);
        position: relative;
        overflow: hidden;
        transition: transform 240ms var(--sl-ease), border-color 240ms var(--sl-ease), box-shadow 240ms var(--sl-ease);
    }
    .kpi-card::before {
        content: "";
        display: block;
        position: absolute;
        top: 0;
        left: 16%;
        right: 16%;
        height: 1.5px;
        background: linear-gradient(90deg, transparent, var(--kpi-color), transparent);
        opacity: 0.78;
        transition: opacity 240ms var(--sl-ease), left 240ms var(--sl-ease), right 240ms var(--sl-ease);
    }
    .kpi-card:hover {
        transform: translateY(-3px);
        border-color: color-mix(in srgb, var(--kpi-color) 46%, rgba(148, 163, 184, 0.2));
        box-shadow:
            0 8px 24px rgba(0, 0, 0, 0.40),
            0 0 26px -8px var(--kpi-glow),
            inset 0 1px 0 rgba(255, 255, 255, 0.09);
    }
    .kpi-card:hover::before {
        left: 8%;
        right: 8%;
        opacity: 1;
    }
    .kpi-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 6px;
    }
    .kpi-title {
        font-family: var(--sl-font-body);
        font-size: 0.69rem;
        font-weight: 650;
        text-transform: uppercase;
        letter-spacing: 1.3px;
        color: var(--sl-text-secondary);
    }
    .kpi-icon {
        font-size: 0.85rem;
    }
    .kpi-value-row {
        display: flex;
        align-items: baseline;
        gap: 6px;
        margin-bottom: 6px;
    }
    .kpi-num {
        font-family: var(--sl-font-mono);
        font-size: clamp(1.45rem, 2.2vw, 2rem);
        font-variant-numeric: tabular-nums;
        font-weight: 700;
        letter-spacing: -0.5px;
        color: var(--kpi-color);
        text-shadow: 0 0 20px color-mix(in srgb, var(--kpi-color) 28%, transparent);
    }
    .kpi-total {
        font-family: var(--sl-font-mono);
        font-size: 0.78rem;
        color: var(--sl-text-secondary);
        font-weight: 500;
    }
    .kpi-track {
        background: rgba(255, 255, 255, 0.04);
        border-radius: 3px;
        height: 3px;
        width: 100%;
        overflow: hidden;
        margin-top: 4px;
    }
    .kpi-bar {
        height: 100%;
        border-radius: 3px;
        background: var(--kpi-color);
        box-shadow: 0 0 6px var(--kpi-glow);
    }
    .kpi-caption {
        font-family: var(--sl-font-body);
        font-size: 0.75rem;
        color: var(--sl-text-secondary);
        margin-top: 6px;
        line-height: 1.45;
    }

    .kpi-card.empresas {
        --kpi-color: #38bdf8;
        --kpi-glow: rgba(56, 189, 248, 0.45);
        --kpi-glow-light: rgba(56, 189, 248, 0.12);
    }
    .kpi-card.score {
        --kpi-color: #fbbf24;
        --kpi-glow: rgba(251, 191, 36, 0.45);
        --kpi-glow-light: rgba(251, 191, 36, 0.12);
    }
    .kpi-card.calidad {
        --kpi-color: #34d399;
        --kpi-glow: rgba(52, 211, 153, 0.45);
        --kpi-glow-light: rgba(52, 211, 153, 0.12);
    }
    .kpi-card.oportunidad {
        --kpi-color: #22d3ee;
        --kpi-glow: rgba(34, 211, 238, 0.45);
        --kpi-glow-light: rgba(34, 211, 238, 0.12);
    }

    /* --- TWIN MARKET MOVERS CARDS --- */
    .movers-row {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 14px;
        margin-bottom: 18px;
    }
    .mover-card {
        background: var(--sl-surface-glass);
        border: 1px solid var(--sl-border-default);
        border-radius: 12px;
        padding: 12px 18px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        box-shadow: var(--sl-shadow-card);
        position: relative;
        overflow: hidden;
        transition: transform 220ms var(--sl-ease), border-color 220ms var(--sl-ease), box-shadow 220ms var(--sl-ease);
    }
    .mover-card.bull {
        border-left: 3px solid #34d399;
    }
    .mover-card.bull:hover {
        transform: translateY(-2px);
        border-color: rgba(52, 211, 153, 0.42);
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.38), 0 0 24px -8px rgba(52, 211, 153, 0.28);
    }
    .mover-card.bear {
        border-left: 3px solid #f87171;
    }
    .mover-card.bear:hover {
        transform: translateY(-2px);
        border-color: rgba(248, 113, 113, 0.42);
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.38), 0 0 24px -8px rgba(248, 113, 113, 0.28);
    }
    .mover-left {
        display: flex;
        align-items: center;
        gap: 10px;
    }
    .mover-badge-icon {
        width: 26px;
        height: 26px;
        border-radius: 7px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: 0.85rem;
    }
    .mover-card.bull .mover-badge-icon {
        background: rgba(16, 185, 129, 0.12);
        color: #34d399;
        border: 1px solid rgba(16, 185, 129, 0.25);
    }
    .mover-card.bear .mover-badge-icon {
        background: rgba(239, 68, 68, 0.12);
        color: #f87171;
        border: 1px solid rgba(239, 68, 68, 0.25);
    }
    .mover-info {
        display: flex;
        flex-direction: column;
    }
    .mover-tag {
        font-family: var(--sl-font-body);
        font-size: 0.58rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 1.2px;
    }
    .mover-card.bull .mover-tag { color: #34d399; }
    .mover-card.bear .mover-tag { color: #f87171; }
    .mover-name {
        font-family: var(--sl-font-body);
        font-size: 0.84rem;
        font-weight: 600;
        color: #e2e8f0;
    }
    .mover-ticker {
        font-family: var(--sl-font-mono);
        font-size: 0.72rem;
        color: var(--sl-text-muted);
        margin-left: 4px;
    }
    .mover-pct {
        font-family: var(--sl-font-mono);
        font-size: 1.05rem;
        font-weight: 800;
        padding: 4px 10px;
        border-radius: 6px;
    }
    .mover-card.bull .mover-pct {
        color: #34d399;
        background: rgba(16, 185, 129, 0.08);
        border: 1px solid rgba(16, 185, 129, 0.2);
    }
    .mover-card.bear .mover-pct {
        color: #f87171;
        background: rgba(239, 68, 68, 0.08);
        border: 1px solid rgba(239, 68, 68, 0.2);
    }

    /* --- Divider --- */
    .ranking-divider, hr {
        border: none !important;
        height: 1px !important;
        background: linear-gradient(90deg, transparent, rgba(148, 163, 184, 0.16), transparent) !important;
        margin: 20px 0 !important;
        width: 100% !important;
    }

    /* ════════════════════════════════════════════════════════════════
       🏆  TABLA DE CLASIFICACIÓN — FINANCE TERMINAL (MI CARTERA STYLE)
       ════════════════════════════════════════════════════════════════ */

    .table-counter {
        display: inline-flex;
        align-items: center;
        margin-left: 10px;
        padding: 2px 9px;
        border-radius: 6px;
        background: rgba(56, 189, 248, 0.08);
        border: 1px solid rgba(56, 189, 248, 0.20);
        color: #22d3ee;
        font-family: var(--sl-font-mono);
        font-size: 0.70rem;
        font-weight: 700;
        letter-spacing: 0.5px;
        vertical-align: middle;
    }

    .ranking-wrapper {
        width: 100%;
        max-height: 620px;
        overflow-y: auto;
        overflow-x: auto;
        border-radius: var(--sl-radius-lg);
        background: var(--sl-surface-glass);
        border: 1px solid var(--sl-border-default);
        box-shadow: var(--sl-shadow-card);
        position: relative;
    }
    .ranking-wrapper::-webkit-scrollbar {
        width: 6px;
        height: 6px;
    }
    .ranking-wrapper::-webkit-scrollbar-track {
        background: rgba(11, 14, 22, 0.7);
    }
    .ranking-wrapper::-webkit-scrollbar-thumb { background: rgba(148, 163, 184, 0.20) !important; border-radius: 999px !important; }
    .ranking-wrapper::-webkit-scrollbar-thumb:hover { background: rgba(56, 189, 248, 0.42) !important; }
    .ranking-wrapper::before {
        content: '';
        position: absolute;
        top: 0; left: 14%; right: 14%;
        height: 1.5px;
        background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.55), rgba(99, 102, 241, 0.38), transparent);
        opacity: 0.75;
        pointer-events: none;
        z-index: 12;
    }

    table.rnk-table {
        width: 100%;
        border-collapse: separate;
        border-spacing: 0;
        padding: 0;
        min-width: 680px;
    }

    /* Sticky Header — Terminal Style (100% opaque to prevent scroll bleed) */
    table.rnk-table thead tr {
        background: #111b2c;
    }
    table.rnk-table thead th {
        position: sticky;
        top: 0;
        z-index: 10;
        background: #111b2c !important;
        box-shadow: inset 0 -1px 0 rgba(56, 189, 248, 0.24), 0 2px 8px rgba(0, 0, 0, 0.35);
        color: var(--sl-text-secondary);
        text-transform: uppercase;
        font-size: 0.64rem;
        font-weight: 700;
        letter-spacing: 0.05em;
        padding: 11px 5px;
        white-space: nowrap;
        user-select: none;
        text-align: center;
        font-family: var(--sl-font-body);
    }
    table.rnk-table thead th:first-child { padding-left: 10px; }
    table.rnk-table thead th:last-child { padding-right: 10px; }

    /* Body rows — clean, stable, zero layout reflows */
    table.rnk-table tbody tr {
        background: transparent;
        border-bottom: 1px solid rgba(255,255,255,0.035);
        transition: background 160ms ease, border-color 160ms ease;
        cursor: default;
        content-visibility: auto;
        contain-intrinsic-size: 0 44px;
    }
    table.rnk-table tbody tr:nth-child(even) {
        background: rgba(148, 163, 184, 0.03);
    }
    table.rnk-table tbody tr:hover {
        background: rgba(56, 189, 248, 0.09) !important;
        border-bottom-color: rgba(56, 189, 248, 0.16) !important;
    }
    table.rnk-table tbody td {
        padding: 9px 5px;
        color: #9fb0c7;
        font-size: 0.80rem;
        vertical-align: middle;
        white-space: nowrap;
        font-family: var(--sl-font-body);
    }
    table.rnk-table tbody tr:hover td { color: #f1f5f9; }
    table.rnk-table tbody td:first-child {
        padding-left: 10px;
        position: relative;
    }
    table.rnk-table tbody td:last-child {
        padding-right: 10px;
    }

    /* Podio Top 1, 2, 3 LED lateral indicator */
    table.rnk-table tbody tr.rank-gold td:first-child {
        box-shadow: inset 3px 0 0 #f59e0b !important;
    }
    table.rnk-table tbody tr.rank-silver td:first-child {
        box-shadow: inset 3px 0 0 #94a3b8 !important;
    }
    table.rnk-table tbody tr.rank-bronze td:first-child {
        box-shadow: inset 3px 0 0 #b45309 !important;
    }

    /* Rank number */
    .rnk-num {
        display: inline-flex; align-items: center; justify-content: center;
        width: 24px; height: 24px; border-radius: 6px;
        font-family: var(--sl-font-mono);
        font-size: 0.75rem; font-weight: 700;
    }
    .rnk-num.gold   { background: rgba(245,158,11,0.15); color: #fbbf24; border: 1px solid rgba(245,158,11,0.3); }
    .rnk-num.silver { background: rgba(148,163,184,0.12); color: #cbd5e1; border: 1px solid rgba(148,163,184,0.25); }
    .rnk-num.bronze { background: rgba(180,83,9,0.15);   color: #d97706; border: 1px solid rgba(180,83,9,0.3); }
    .rnk-num.normal { background: rgba(255,255,255,0.03); color: var(--sl-text-muted); }

    /* Ticker badge (Mi Cartera Style) */
    .rnk-ticker {
        display: inline-flex; align-items: center;
        background: rgba(16,185,129,0.08);
        color: #34d399; padding: 2px 6px; border-radius: 6px;
        font-family: var(--sl-font-mono);
        font-size: 0.73rem; font-weight: 700;
        border: 1px solid rgba(16,185,129,0.18);
        letter-spacing: 0.3px;
    }

    /* Position change indicator */
    .chg-up   { color: #34d399; font-weight: 700; font-family: var(--sl-font-mono); font-size: 0.75rem; }
    .chg-down { color: #f87171; font-weight: 700; font-family: var(--sl-font-mono); font-size: 0.75rem; }
    .chg-flat { color: var(--sl-text-muted); font-weight: 600; font-family: var(--sl-font-mono); font-size: 0.75rem; }

    /* Company name */
    .rnk-name { display: inline-block; color: #e2e8f0; font-weight: 500; font-size: 0.81rem; max-width: 112px; overflow: hidden; text-overflow: ellipsis; vertical-align: middle; }

    /* Score badges (Refined and lightweight) */
    .score-badge {
        display: inline-flex; align-items: center; justify-content: center;
        padding: 2px 7px; border-radius: 6px;
        font-family: var(--sl-font-mono);
        font-size: 0.76rem; font-weight: 700;
        min-width: 40px;
    }
    .score-calidad {
        background: rgba(16,185,129,0.10);
        color: #34d399; border: 1px solid rgba(16,185,129,0.22);
    }
    .score-oportunidad {
        background: rgba(56, 189, 248, 0.08);
        color: #22d3ee; border: 1px solid rgba(56, 189, 248, 0.20);
    }
    .score-final {
        background: rgba(245,158,11,0.12);
        color: #fbbf24; border: 1px solid rgba(245,158,11,0.30);
        font-weight: 800;
    }

    /* Sector tag with chromatic category pills */
    .rnk-sec-pill {
        display: inline-flex;
        align-items: center;
        padding: 2px 6px;
        border-radius: 6px;
        font-size: 0.68rem;
        font-weight: 600;
        max-width: 108px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        font-family: var(--sl-font-body);
        border: 1px solid transparent;
    }
    .sec-tech    { background: rgba(56, 189, 248, 0.08); color: #38bdf8; border-color: rgba(56, 189, 248, 0.22); }
    .sec-fin     { background: rgba(251, 191, 36, 0.08); color: #fbbf24; border-color: rgba(251, 191, 36, 0.22); }
    .sec-health  { background: rgba(52, 211, 153, 0.08); color: #34d399; border-color: rgba(52, 211, 153, 0.22); }
    .sec-energy  { background: rgba(249, 115, 22, 0.08); color: #fb923c; border-color: rgba(249, 115, 22, 0.22); }
    .sec-consume { background: rgba(167, 139, 250, 0.08); color: #c084fc; border-color: rgba(167, 139, 250, 0.22); }
    .sec-default { background: rgba(148, 163, 184, 0.06); color: #94a3b8; border-color: rgba(148, 163, 184, 0.16); }

    /* Price */
    .rnk-price {
        font-family: var(--sl-font-mono);
        color: #e2e8f0; font-weight: 600; font-size: 0.85rem;
    }

    /* Var diaria */
    .var-pos {
        color: #34d399; font-weight: 700; font-family: var(--sl-font-mono);
        font-size: 0.82rem;
    }
    .var-neg {
        color: #f87171; font-weight: 700; font-family: var(--sl-font-mono);
        font-size: 0.82rem;
    }

    /* Star badge (clean, lightweight, no alpha drop-shadow) */
    .star-badge { font-size: 0.95rem; }

    /* ════════════════════════════════════════════════════════════════
       ⚡ ACCIONES RÁPIDAS DEL INSPECTOR (Estilo Terminal de Trading)
       ════════════════════════════════════════════════════════════════ */
    .st-key-inspector_actions div[data-testid="column"]:nth-child(1) button {
        background: rgba(245, 158, 11, 0.08) !important;
        border: 1px solid rgba(245, 158, 11, 0.35) !important;
        color: #fbbf24 !important;
        font-weight: 700 !important;
        border-radius: 10px !important;
        font-family: var(--sl-font-body) !important;
        transition: all 0.2s ease !important;
    }
    .st-key-inspector_actions div[data-testid="column"]:nth-child(1) button:hover {
        background: rgba(245, 158, 11, 0.18) !important;
        border-color: rgba(245, 158, 11, 0.65) !important;
        box-shadow: 0 0 18px -5px rgba(245, 158, 11, 0.45) !important;
        transform: translateY(-1px) !important;
    }
    .st-key-inspector_actions div[data-testid="column"]:nth-child(2) button {
        background: rgba(56, 189, 248, 0.08) !important;
        border: 1px solid rgba(56, 189, 248, 0.30) !important;
        color: #38bdf8 !important;
        font-weight: 700 !important;
        border-radius: 10px !important;
        font-family: var(--sl-font-body) !important;
        transition: all 200ms var(--sl-ease) !important;
    }
    .st-key-inspector_actions div[data-testid="column"]:nth-child(2) button:hover {
        background: rgba(56, 189, 248, 0.18) !important;
        border-color: rgba(56, 189, 248, 0.58) !important;
        box-shadow: 0 0 18px -5px rgba(56, 189, 248, 0.45) !important;
        transform: translateY(-1px) !important;
    }
    .st-key-inspector_actions div[data-testid="column"]:nth-child(3) button {
        background: rgba(16, 185, 129, 0.08) !important;
        border: 1px solid rgba(16, 185, 129, 0.35) !important;
        color: #34d399 !important;
        font-weight: 700 !important;
        border-radius: 10px !important;
        font-family: var(--sl-font-body) !important;
        transition: all 200ms var(--sl-ease) !important;
    }
    .st-key-inspector_actions div[data-testid="column"]:nth-child(3) button:hover {
        background: rgba(16, 185, 129, 0.18) !important;
        border-color: rgba(16, 185, 129, 0.65) !important;
        box-shadow: 0 0 18px -5px rgba(16, 185, 129, 0.45) !important;
        transform: translateY(-1px) !important;
    }

    /* ════════════════════════════════════════════════════════════════
       🔍  INSPECTOR DE ACTIVOS (MI CARTERA STYLE)
       ════════════════════════════════════════════════════════════════ */
    .inspector-shell {
        background: linear-gradient(150deg, var(--sl-bg-elevated), var(--sl-bg-surface) 65%);
        border: 1px solid var(--sl-border-default);
        border-radius: 14px;
        overflow: hidden;
        box-shadow: 0 4px 24px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.01);
        margin-top: 12px;
        margin-bottom: 80px;
        position: relative;
    }

    /* ════════════════════════════════════════════════════════════════
       🔍  INSPECTOR EMPTY STATE CANVAS (Terminal Style)
       ════════════════════════════════════════════════════════════════ */
    .insp-empty-shell {
        background: linear-gradient(160deg, rgba(17,20,32,0.65) 0%, rgba(12,15,26,0.7) 55%, rgba(9,12,20,0.75) 100%);
        border: 1px dashed rgba(148, 163, 184, 0.22);
        border-radius: 14px;
        padding: 42px 28px 36px;
        margin-top: 14px;
        margin-bottom: 120px;
        min-height: 380px;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        text-align: center;
        position: relative;
        overflow: hidden;
        box-shadow: 0 4px 24px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.02);
    }
    .insp-empty-shell::before {
        content: '';
        position: absolute;
        top: 0; left: 20%; right: 20%;
        height: 1px;
        background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.35), transparent);
    }
    .insp-empty-icon-wrap {
        width: 64px;
        height: 64px;
        border-radius: 16px;
        background: linear-gradient(135deg, rgba(56, 189, 248, 0.1) 0%, rgba(16,185,129,0.08) 100%);
        border: 1px solid rgba(56, 189, 248, 0.25);
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 1.8rem;
        margin-bottom: 16px;
        box-shadow: none;
    }
    .insp-empty-title {
        font-family: var(--sl-font-body);
        font-size: 1.15rem;
        font-weight: 700;
        color: #f1f5f9;
        letter-spacing: -0.3px;
        margin-bottom: 8px;
    }
    .insp-empty-desc {
        font-family: var(--sl-font-body);
        font-size: 0.84rem;
        color: var(--sl-text-muted);
        max-width: 580px;
        line-height: 1.55;
        margin-bottom: 24px;
    }
    .insp-empty-grid {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 14px;
        width: 100%;
        max-width: 780px;
        margin-bottom: 16px;
    }
    .insp-empty-pill {
        background: rgba(14, 18, 28, 0.7);
        border: 1px solid rgba(255, 255, 255, 0.05);
        border-radius: 10px;
        padding: 12px 14px;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 4px;
        transition: border-color 0.2s ease, transform 0.2s ease;
    }
    .insp-empty-pill:hover {
        border-color: rgba(56, 189, 248, 0.3);
        transform: translateY(-2px);
    }
    .insp-empty-pill-tag {
        font-family: var(--sl-font-body);
        font-size: 0.60rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 1.2px;
        color: #38bdf8;
    }
    .insp-empty-pill-name {
        font-family: var(--sl-font-body);
        font-size: 0.78rem;
        font-weight: 600;
        color: #cbd5e1;
    }
    .insp-empty-hint {
        font-family: var(--sl-font-mono);
        font-size: 0.72rem;
        color: var(--sl-text-muted);
        margin-top: 10px;
        display: flex;
        align-items: center;
        gap: 6px;
    }
    .inspector-shell::before {
        content: '';
        position: absolute;
        top: 0; left: 16%; right: 16%;
        height: 1px;
        background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.31), transparent);
        opacity: 0.5;
    }

    .insp-header {
        display: flex; align-items: center; justify-content: space-between;
        padding: 18px 24px;
        border-bottom: 1px solid rgba(148, 163, 184, 0.12);
        background: rgba(11,14,22,0.5);
    }
    .insp-company-name {
        font-size: 1.25rem; font-weight: 800; color: #ffffff;
        letter-spacing: -0.4px;
        font-family: var(--sl-font-body);
    }
    .insp-ticker-pill {
        display: inline-flex; align-items: center;
        background: rgba(16,185,129,0.12);
        color: #34d399; padding: 2px 9px; border-radius: 6px;
        font-family: var(--sl-font-mono); font-size: 0.76rem; font-weight: 700;
        border: 1px solid rgba(16,185,129,0.22); letter-spacing: 0.5px;
        width: fit-content; margin-top: 4px;
    }
    .insp-price-block {
        display: flex; flex-direction: column; align-items: flex-end; gap: 4px;
    }
    .insp-price-value {
        font-family: var(--sl-font-mono); font-size: 1.25rem; font-weight: 800;
        color: #ffffff;
    }
    .insp-var-pos {
        color: #34d399; font-size: 0.78rem; font-weight: 700;
        font-family: var(--sl-font-mono);
        background: rgba(16,185,129,0.1);
        border: 1px solid rgba(16,185,129,0.2);
        padding: 3px 10px; border-radius: 6px;
    }
    .insp-var-neg {
        color: #f87171; font-size: 0.78rem; font-weight: 700;
        font-family: var(--sl-font-mono);
        background: rgba(239,68,68,0.1);
        border: 1px solid rgba(239,68,68,0.2);
        padding: 3px 10px; border-radius: 6px;
    }

    /* Panel card */
    .insp-panel {
        margin: 14px 18px;
        background: linear-gradient(150deg, var(--sl-bg-elevated), var(--sl-bg-surface) 65%);
        border: 1px solid var(--sl-border-default);
        border-radius: 12px;
        padding: 16px 20px;
        box-shadow: 0 4px 16px rgba(0,0,0,0.3);
    }
    .insp-panel-title {
        font-size: 0.70rem !important; font-weight: 700 !important; text-transform: uppercase !important;
        letter-spacing: 1.2px !important; color: #94a3b8 !important;
        margin-bottom: 14px !important; display: flex !important; align-items: center !important; gap: 8px !important;
        font-family: var(--sl-font-display) !important;
    }
    .insp-panel-title::after {
        content: ''; flex: 1; height: 1px;
        background: linear-gradient(90deg, rgba(255,255,255,0.05), transparent);
    }

    /* Score grid */
    .score-grid {
        display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
    }
    .score-card {
        background: rgba(11,14,22,0.6);
        border-radius: 10px; padding: 12px 14px;
        border: 1px solid rgba(255,255,255,0.04);
        text-align: center;
        transition: border-color 0.2s ease;
    }
    .score-card:hover { border-color: rgba(56, 189, 248, 0.18); }
    .score-card-label {
        font-size: 0.60rem; font-weight: 700; text-transform: uppercase;
        letter-spacing: 1.1px; color: var(--sl-text-muted); margin-bottom: 6px;
        font-family: var(--sl-font-body);
    }
    .score-card-value {
        font-family: var(--sl-font-mono); font-size: 1.45rem; font-weight: 800;
    }
    .score-card-sub { font-size: 0.66rem; color: var(--sl-text-muted); margin-top: 2px; font-family: var(--sl-font-body); }
    .sc-final   { color: #fbbf24; }
    .sc-calidad { color: #34d399; }
    .sc-opor    { color: #22d3ee; }

    /* Progress bar system (Mi Cartera PnL style) */
    .cbar-wrap { margin-bottom: 12px; }
    .cbar-header {
        display: flex; justify-content: space-between; align-items: baseline;
        margin-bottom: 5px;
    }
    .cbar-label { font-size: 0.80rem; font-weight: 600; color: #94a3b8; font-family: var(--sl-font-body); }
    .cbar-desc  { font-size: 0.68rem; color: var(--sl-text-muted); margin-top: 2px; font-family: var(--sl-font-body); }
    .cbar-reason { font-size: 0.64rem; color: var(--sl-text-muted); margin-top: 3px; font-family: var(--sl-font-body); font-style: italic; letter-spacing: 0.01em; opacity: 0.85; }
    .cbar-val   { font-family: var(--sl-font-mono); font-size: 0.80rem; font-weight: 700; }
    .cbar-bg {
        background: rgba(255,255,255,0.03); border-radius: 4px;
        height: 5px; width: 100%; overflow: hidden;
    }
    .cbar-fill {
        height: 100%; border-radius: 4px;
        transition: width 0.4s ease;
    }
    .cf-gold   { background: linear-gradient(90deg, #b45309, #fbbf24); }
    .cf-green  { background: linear-gradient(90deg, #047857, #34d399); }
    .cf-cyan   { background: linear-gradient(90deg, #0e7490, #22d3ee); }
    .cf-violet { background: linear-gradient(90deg, #6d28d9, #a78bfa); }

    /* Sub-metric grid */
    .sub-grid {
        display: grid; grid-template-columns: 1fr 1fr; gap: 12px 24px;
    }

    /* Ratio card grid */
    .ratio-grid {
        display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px;
    }
    .ratio-card {
        background: rgba(11,14,22,0.6);
        border: 1px solid rgba(255,255,255,0.04);
        border-radius: 10px; padding: 12px 10px;
        text-align: center;
        transition: border-color 0.2s ease;
    }
    .ratio-card:hover { border-color: rgba(56, 189, 248, 0.18); }
    .ratio-key {
        font-size: 0.58rem; font-weight: 700; text-transform: uppercase;
        letter-spacing: 1.2px; color: var(--sl-text-muted); margin-bottom: 6px;
        font-family: var(--sl-font-body);
    }
    .ratio-val {
        font-family: var(--sl-font-mono); font-size: 1.1rem;
        font-weight: 700; color: #e2e8f0;
    }
    .ratio-val.na { color: var(--sl-text-muted); font-style: italic; font-size: 0.85rem; }
    .ratio-note { font-size: 0.62rem; color: var(--sl-text-muted); margin-top: 3px; font-family: var(--sl-font-body); }

    /* Resumen box */
    .insp-resumen {
        background: rgba(9,12,18,0.7);
        border-left: 3px solid rgba(56, 189, 248, 0.35);
        border-radius: 0 8px 8px 0;
        padding: 12px 16px;
        font-size: 0.82rem; font-style: italic; color: #94a3b8;
        line-height: 1.5; margin-top: 14px;
        font-family: var(--sl-font-body);
    }

    @media (max-width: 820px) {
        .kpi-row, .insp-empty-grid, .ratio-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .movers-row { grid-template-columns: minmax(0, 1fr); }
    }
    @media (max-width: 540px) {
        .sub-grid, .score-grid { grid-template-columns: minmax(0, 1fr); }
        .insp-panel { margin: 12px; padding: 14px; }
        .kpi-row, .ratio-grid { gap: 10px; }
        .kpi-card { padding: 14px 12px; }
        .kpi-header { align-items: baseline; gap: 4px; }
    }

    /* Action buttons in inspector (matching Mi Cartera) */
    div[data-testid="column"]:not(.st-key-topbar_nav *) button,
    .stButton:not(.st-key-topbar_nav *) button {
        background: linear-gradient(145deg, #182133, #131a29) !important;
        border: 1px solid rgba(100,116,139,0.18) !important;
        color: #8fa0b5 !important;
        border-radius: 8px !important;
        padding: 8px 14px !important;
        font-weight: 600 !important;
        font-size: 0.80rem !important;
        transition: all 0.2s ease !important;
    }
    div[data-testid="column"]:not(.st-key-topbar_nav *) button:hover,
    .stButton:not(.st-key-topbar_nav *) button:hover {
        background: linear-gradient(145deg, #1c273d, #182133) !important;
        border-color: rgba(56, 189, 248, 0.35) !important;
        color: #ffffff !important;
        box-shadow: none !important;
    }

    /* --- Secondary Filter Row (Dividendo chip + CSV export button) --- */
    div[data-testid="stHorizontalBlock"]:has(.st-key-ranking_filter_dividendo) {
        gap: 14px !important;
        align-items: flex-start !important;
        margin-top: 6px !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.st-key-ranking_filter_dividendo) label[data-testid="stWidgetLabel"],
    div[data-testid="stHorizontalBlock"]:has(.st-key-ranking_filter_dividendo) label[data-testid="stWidgetLabel"] p {
        min-height: 1.5rem !important;
        margin-bottom: 0.25rem !important;
        display: flex !important;
        align-items: center !important;
        font-family: 'Inter', sans-serif !important;
        font-size: 0.76rem !important;
        font-weight: 600 !important;
        color: #94a3b8 !important;
        letter-spacing: 0.05em !important;
        text-transform: uppercase !important;
        line-height: 1.4 !important;
    }
    .st-key-ranking_filter_dividendo,
    .st-key-ranking_filter_dividendo > div,
    .st-key-ranking_filter_dividendo div[data-testid="element-container"],
    .st-key-ranking_filter_dividendo div[data-testid="stElementContainer"],
    .st-key-ranking_filter_dividendo div[data-testid="stVerticalBlock"],
    .st-key-ranking_export_csv,
    .st-key-ranking_export_csv > div,
    .st-key-ranking_export_csv div[data-testid="element-container"],
    .st-key-ranking_export_csv div[data-testid="stElementContainer"],
    .st-key-ranking_export_csv div[data-testid="stVerticalBlock"] {
        width: 100% !important;
        max-width: 100% !important;
        gap: 0 !important;
    }
    .st-key-ranking_filter_dividendo div[data-testid="stCheckbox"] {
        margin: 0 !important;
        background-color: #0d121c !important;
        border: 1px solid rgba(148, 163, 184, 0.12) !important;
        border-radius: 10px !important;
        padding: 0 14px !important;
        height: 42px !important;
        min-height: 42px !important;
        max-height: 42px !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        box-sizing: border-box !important;
        transition: all 0.2s ease !important;
        width: 100% !important;
        cursor: pointer !important;
    }
    .st-key-ranking_filter_dividendo div[data-testid="stCheckbox"]:hover {
        border-color: rgba(52, 211, 153, 0.45) !important;
        background: rgba(52, 211, 153, 0.05) !important;
    }
    .st-key-ranking_filter_dividendo div[data-testid="stCheckbox"]:has(input:checked) {
        border-color: rgba(52, 211, 153, 0.6) !important;
        background: rgba(52, 211, 153, 0.12) !important;
    }
    .st-key-ranking_filter_dividendo div[data-testid="stCheckbox"] label {
        color: #94a3b8 !important;
        font-weight: 600 !important;
        font-size: 0.84rem !important;
        font-family: 'Inter', sans-serif !important;
        margin: 0 !important;
        cursor: pointer !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        gap: 8px !important;
        width: 100% !important;
        height: 100% !important;
    }
    .st-key-ranking_filter_dividendo div[data-testid="stCheckbox"] label p {
        margin: 0 !important;
        padding: 0 !important;
        font-size: 0.84rem !important;
        line-height: 1 !important;
    }
    .st-key-ranking_filter_dividendo div[data-testid="stCheckbox"]:has(input:checked) label span,
    .st-key-ranking_filter_dividendo div[data-testid="stCheckbox"]:has(input:checked) label p {
        color: #34d399 !important;
        font-weight: 700 !important;
    }
    .st-key-ranking_filter_dividendo div[data-testid="stCheckbox"] input[type="checkbox"] {
        accent-color: #10b981 !important;
        width: 16px !important;
        height: 16px !important;
        margin: 0 !important;
    }
    .st-key-ranking_export_csv button {
        height: 42px !important;
        min-height: 42px !important;
        max-height: 42px !important;
        border-radius: 10px !important;
        background: rgba(56, 189, 248, 0.08) !important;
        border: 1px solid rgba(56, 189, 248, 0.28) !important;
        color: #38bdf8 !important;
        font-weight: 600 !important;
        font-size: 0.82rem !important;
    }
    .st-key-ranking_export_csv button:hover {
        background: rgba(56, 189, 248, 0.16) !important;
        border-color: rgba(56, 189, 248, 0.55) !important;
        color: #ffffff !important;
    }

    /* --- Data Coverage Badge (Table + Inspector) --- */
    .cov-badge {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 2px 6px;
        border-radius: 6px;
        font-family: var(--sl-font-mono);
        font-size: 0.70rem;
        font-weight: 700;
        letter-spacing: 0.2px;
    }
    .cov-high { background: rgba(16,185,129,0.10); color: #34d399; border: 1px solid rgba(16,185,129,0.24); }
    .cov-mid  { background: rgba(56,189,248,0.10); color: #38bdf8; border: 1px solid rgba(56,189,248,0.24); }
    .cov-low  { background: rgba(245,158,11,0.12); color: #fbbf24; border: 1px solid rgba(245,158,11,0.28); }

    .insp-meta-row {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
        margin-top: 6px;
    }
    .insp-meta-pill {
        display: inline-flex;
        align-items: center;
        padding: 2px 9px;
        border-radius: 6px;
        font-family: var(--sl-font-mono);
        font-size: 0.70rem;
        font-weight: 600;
        letter-spacing: 0.3px;
    }
    .insp-meta-pill.model-fin {
        background: rgba(251,191,36,0.10);
        color: #fbbf24;
        border: 1px solid rgba(251,191,36,0.25);
    }
    .insp-meta-pill.model-std {
        background: rgba(56,189,248,0.08);
        color: #38bdf8;
        border: 1px solid rgba(56,189,248,0.22);
    }
    .insp-ajustes {
        margin-top: 10px;
        padding: 9px 13px;
        background: rgba(245, 158, 11, 0.06);
        border: 1px solid rgba(245, 158, 11, 0.20);
        border-radius: 8px;
        font-size: 0.74rem;
        color: #cbd5e1;
        line-height: 1.45;
    }
    .insp-ajustes b { color: #fbbf24; }

    /* --- 1 vs 1 Comparator Table --- */
    .cmp-shell {
        background: var(--sl-surface-glass);
        border: 1px solid var(--sl-border-default);
        border-radius: 12px;
        overflow: hidden;
        margin-top: 10px;
    }
    table.cmp-table {
        width: 100%;
        border-collapse: collapse;
        font-family: var(--sl-font-body);
    }
    table.cmp-table thead th {
        background: #111b2c;
        color: #94a3b8;
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        padding: 11px 14px;
        border-bottom: 1px solid rgba(56,189,248,0.22);
        text-align: center;
    }
    table.cmp-table thead th:first-child { text-align: left; }
    table.cmp-table tbody td {
        padding: 8px 14px;
        font-size: 0.80rem;
        color: #cbd5e1;
        border-bottom: 1px solid rgba(255,255,255,0.04);
        text-align: center;
        font-family: var(--sl-font-mono);
    }
    table.cmp-table tbody td:first-child {
        text-align: left;
        font-family: var(--sl-font-body);
        color: #94a3b8;
        font-weight: 600;
    }
    table.cmp-table tbody tr.cmp-sec td {
        background: rgba(56,189,248,0.05);
        color: #38bdf8;
        font-family: var(--sl-font-body);
        font-size: 0.68rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.1em;
        padding: 6px 14px;
    }
    .cmp-win { color: #34d399 !important; font-weight: 800 !important; }
    .cmp-verdict {
        padding: 11px 16px;
        background: rgba(16,185,129,0.06);
        border-top: 1px solid rgba(16,185,129,0.18);
        font-size: 0.80rem;
        color: #e2e8f0;
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 8px;
    }
"""

@st.cache_data(ttl=300, show_spinner=False)
def obtener_empresas_ranking_procesadas():
    """Procesa y calcula en memoria los datos del ranking con caché de 5 minutos."""
    try:
        db_empresas = query_all("SELECT ticker, nombre, sector, mercado, es_ibex35 FROM empresas WHERE activa = 1 ORDER BY nombre")
    except Exception:
        db_empresas = []
    
    if not db_empresas:
        return []
    
    todos_los_tickers = [emp['ticker'].strip().upper() for emp in db_empresas]
    datos_mercado_batch = descargar_toda_la_banca_lote(todos_los_tickers)
    diccionario_fundamentales = cargar_todos_los_fundamentales_en_paralelo(todos_los_tickers)

    empresas_data = []
    for emp in db_empresas:
        ticker = emp['ticker'].strip().upper()
        nombre = emp.get('nombre') or ticker
        sector = emp.get('sector') or 'Sin clasificar'
        mercado = emp.get('mercado') or 'BME'
        es_ibex35 = int(emp.get('es_ibex35') or 0)
        
        df_ticker = None
        try:
            if isinstance(datos_mercado_batch.columns, pd.MultiIndex):
                if ticker in datos_mercado_batch.columns.get_level_values(0):
                    df_ticker = datos_mercado_batch[ticker]
                    if isinstance(df_ticker.columns, pd.MultiIndex):
                        df_ticker.columns = df_ticker.columns.get_level_values(0)
            else:
                if 'Close' in datos_mercado_batch.columns:
                    df_ticker = datos_mercado_batch
        except Exception:
            pass

        info_fundamentales = diccionario_fundamentales.get(ticker, {})

        precio_actual, variacion = get_closing_variation(
            df_historico=df_ticker,
            ticker=ticker,
            fundamentales=info_fundamentales
        )
        
        score_v6 = calcular_score_stocklab_v7(
            df_historico=df_ticker,
            info=info_fundamentales,
            variacion_diaria=variacion,
            sector=sector
        )
        cobertura = _calcular_cobertura_fundamental(info_fundamentales, sector)
        div_yield_pct = _normalizar_div_pct(_clean_float(info_fundamentales.get('dividendYield')))

        resumen = generar_resumen_fundamental_v7(info_fundamentales)
        var_num = float(variacion) if (variacion is not None and not np.isnan(variacion)) else 0.0
        if precio_actual > 0:
            precio_fmt = f"{precio_actual:.4f} €" if precio_actual < 0.1 else f"{precio_actual:.2f} €"
        else:
            precio_fmt = "N/A"

        empresas_data.append({
            'ticker': ticker,
            'nombre': nombre,
            'sector': sector,
            'mercado': mercado,
            'es_ibex35': es_ibex35,
            'precio': precio_fmt,
            'precio_num': float(precio_actual) if precio_actual > 0 else 0.0,
            'var': var_num,
            'calidad': score_v6['calidad'],
            'oportunidad': score_v6['oportunidad'],
            'score': score_v6['score_final'],
            'resumen': resumen,
            'watchlist': False,
            'df_historico': df_ticker,
            'info_fundamentales': info_fundamentales,
            'data_source':  info_fundamentales.get('_data_source', 'fallback'),
            'data_quality': info_fundamentales.get('_data_quality', 'incomplete'),
            'cobertura_n': cobertura['disponibles'],
            'cobertura_total': cobertura['total'],
            'cobertura_pct': cobertura['pct'],
            'cobertura_nivel': cobertura['nivel'],
            'cobertura_faltantes': cobertura['faltantes'],
            'modelo_financiero': cobertura['modelo_financiero'],
            'div_yield_pct': div_yield_pct if (div_yield_pct is not None and div_yield_pct > 0) else 0.0,
        })
    
    return empresas_data


def show():
    # ── Inyección inmediata de CSS (evita FOUC / layout shifts) ───────────
    st.html(f"<style>{CSS_RANKING}</style>")

    # ── Cabecera Institucional (Estilo Mi Cartera) ────────────────────────
    cabecera_html = """
    <div class="sl-page-header">
        <h1 class="sl-page-title">Ranking</h1>
        <p class="sl-page-subtitle">Clasificación cuantitativa y auditoría de fundamentales</p>
    </div>
    """
    st.html(cabecera_html)

    # ═══════════════════════════════════════════════════════════════════
    # 🚀 OBTENCIÓN INSTANTÁNEA DE DATOS DESDE CACHÉ EN MEMORIA (0 ms)
    # ═══════════════════════════════════════════════════════════════════
    empresas_data_cached = obtener_empresas_ranking_procesadas()
    if not empresas_data_cached:
        st.warning("No hay empresas activas en la base de datos.")
        return

    # Sincronización en tiempo real de Watchlist (sin esperar el TTL de 5 min del ranking)
    try:
        df_wl_live = obtener_watchlist()
        watchlist_live = (
            {str(t).strip().upper() for t in df_wl_live["ticker"].dropna().tolist()}
            if df_wl_live is not None and not df_wl_live.empty
            else set()
        )
    except Exception:
        watchlist_live = set()

    empresas_data = [
        dict(e, watchlist=(e["ticker"] in watchlist_live))
        for e in empresas_data_cached
    ]

    # ── FILTROS DE BÚSQUEDA ───────────────────────────────────────────
    st.markdown('<div class="sl-section-title">Filtros de Búsqueda y Ordenación</div>', unsafe_allow_html=True)

    try:
        mercados_db = query_all("SELECT DISTINCT mercado FROM empresas WHERE mercado IS NOT NULL AND mercado != '' AND activa = 1 ORDER BY mercado")
        mercados_raw = [m["mercado"] for m in mercados_db]
    except Exception:
        mercados_raw = ["BME"]

    lista_mercados = ["Todos"]
    if "BME" in mercados_raw:
        lista_mercados.extend(["IBEX 35", "Mercado Continuo"])
    for m_item in mercados_raw:
        if m_item not in lista_mercados:
            lista_mercados.append(m_item)

    try:
        sectores_db = query_all("SELECT DISTINCT sector FROM empresas WHERE sector IS NOT NULL AND sector != '' AND activa = 1 ORDER BY sector")
        lista_sectores = ["Todos"] + [s["sector"] for s in sectores_db]
    except Exception:
        lista_sectores = ["Todos", "Salud", "Tecnología", "Financiero", "Industria", "Energía"]
    
    col1, col2, col3, col4 = st.columns(4, vertical_alignment="top")
    with col1:
        filtro_mercado = st.selectbox("MERCADO", lista_mercados, index=0)
    with col2:
        filtro_sector = st.selectbox("SECTOR", lista_sectores, index=0)
    with col3:
        ordenar_por = st.selectbox(
            "ORDENAR POR", 
            ["SCORE STOCKLAB ⭐", "CALIDAD 🏆", "OPORTUNIDAD 🎯", "Variación Diaria"], 
            format_func=lambda x: {
                "SCORE STOCKLAB ⭐": "Score StockLab",
                "CALIDAD 🏆": "Calidad",
                "OPORTUNIDAD 🎯": "Oportunidad",
                "Variación Diaria": "Variación Diaria",
            }.get(x, x),
            index=0
        )
    with col4:
        with st.container(key="ranking_filter_watchlist"):
            st.html('<label data-testid="stWidgetLabel" style="min-height:1.5rem;margin-bottom:0.25rem;display:flex;align-items:center;"><div data-testid="stMarkdownContainer"><p style="font-family:\'Inter\',sans-serif;font-size:0.76rem;font-weight:600;color:#94a3b8;letter-spacing:0.05em;text-transform:uppercase;margin:0;line-height:1.4;">WATCHLIST</p></div></label>')
            solo_watchlist = st.checkbox("Solo Watchlist", value=False)

    # ── FILTROS RÁPIDOS DE CALIDAD Y EXPORTACIÓN ──────────────────────
    f2_col1, f2_col2, f2_col3, f2_col4 = st.columns(4, vertical_alignment="top")
    with f2_col1:
        filtro_score_min = st.selectbox(
            "SCORE MÍNIMO",
            [0, 50, 60, 70, 75],
            format_func=lambda v: "Todos (≥ 0)" if v == 0 else f"Score ≥ {v}",
            index=0,
            key="ranking_filter_score_min",
        )
    with f2_col2:
        filtro_cobertura = st.selectbox(
            "COBERTURA DATOS",
            ["Todas", "Alta (≥85%)", "Media o Alta (≥60%)"],
            index=0,
            key="ranking_filter_cobertura",
        )
    with f2_col3:
        with st.container(key="ranking_filter_dividendo"):
            st.html('<label data-testid="stWidgetLabel" style="min-height:1.5rem;margin-bottom:0.25rem;display:flex;align-items:center;"><div data-testid="stMarkdownContainer"><p style="font-family:\'Inter\',sans-serif;font-size:0.76rem;font-weight:600;color:#94a3b8;letter-spacing:0.05em;text-transform:uppercase;margin:0;line-height:1.4;">DIVIDENDO</p></div></label>')
            solo_dividendo = st.checkbox("Solo con dividendo", value=False, key="ranking_chk_dividendo")
    
    # ── FILTRADO Y ORDENACIÓN ─────────────────────────────────────────
    empresas_filtradas = empresas_data
    if filtro_mercado == "IBEX 35":
        empresas_filtradas = [e for e in empresas_filtradas if int(e.get("es_ibex35") or 0) == 1]
    elif filtro_mercado == "Mercado Continuo":
        empresas_filtradas = [e for e in empresas_filtradas if int(e.get("es_ibex35") or 0) == 0]
    elif filtro_mercado != "Todos":
        empresas_filtradas = [e for e in empresas_filtradas if e.get("mercado") == filtro_mercado]
    if filtro_sector != "Todos":
        empresas_filtradas = [e for e in empresas_filtradas if e["sector"] == filtro_sector]
    if solo_watchlist:
        empresas_filtradas = [e for e in empresas_filtradas if e["watchlist"]]
    if solo_dividendo:
        empresas_filtradas = [e for e in empresas_filtradas if float(e.get("div_yield_pct") or 0.0) > 0.0]
    if filtro_score_min > 0:
        empresas_filtradas = [e for e in empresas_filtradas if float(e.get("score") or 0.0) >= filtro_score_min]
    if filtro_cobertura == "Alta (≥85%)":
        empresas_filtradas = [e for e in empresas_filtradas if float(e.get("cobertura_pct") or 0.0) >= 85.0]
    elif filtro_cobertura == "Media o Alta (≥60%)":
        empresas_filtradas = [e for e in empresas_filtradas if float(e.get("cobertura_pct") or 0.0) >= 60.0]
        
    if ordenar_por == "SCORE STOCKLAB ⭐":
        empresas_filtradas = sorted(empresas_filtradas, key=lambda x: (-float(x["score"]), str(x["ticker"]).strip().upper()))
    elif ordenar_por == "CALIDAD 🏆":
        empresas_filtradas = sorted(empresas_filtradas, key=lambda x: (-float(x["calidad"]), str(x["ticker"]).strip().upper()))
    elif ordenar_por == "OPORTUNIDAD 🎯":
        empresas_filtradas = sorted(empresas_filtradas, key=lambda x: (-float(x["oportunidad"]), str(x["ticker"]).strip().upper()))
    elif ordenar_por == "Variación Diaria":
        empresas_filtradas = sorted(empresas_filtradas, key=lambda x: (-float(x["var"]), str(x["ticker"]).strip().upper()))

    with f2_col4:
        with st.container(key="ranking_export_csv"):
            st.html('<label data-testid="stWidgetLabel" style="min-height:1.5rem;margin-bottom:0.25rem;display:flex;align-items:center;"><div data-testid="stMarkdownContainer"><p style="font-family:\'Inter\',sans-serif;font-size:0.76rem;font-weight:600;color:#94a3b8;letter-spacing:0.05em;text-transform:uppercase;margin:0;line-height:1.4;">EXPORTACIÓN</p></div></label>')
            df_export = pd.DataFrame([
                {
                    "Posicion": i + 1,
                    "Ticker": e["ticker"],
                    "Empresa": e["nombre"],
                    "Sector": e["sector"],
                    "Mercado": "IBEX 35" if int(e.get("es_ibex35") or 0) == 1 else e.get("mercado", "BME"),
                    "Score_Final": e["score"],
                    "Calidad": e["calidad"],
                    "Oportunidad": e["oportunidad"],
                    "Precio_EUR": round(float(e.get("precio_num") or 0.0), 4),
                    "Var_Diaria_Pct": round(float(e.get("var") or 0.0), 2),
                    "Div_Yield_Pct": round(float(e.get("div_yield_pct") or 0.0), 2),
                    "Cobertura": f"{e.get('cobertura_n', 0)}/{e.get('cobertura_total', 9)} ({e.get('cobertura_nivel', 'N/A')})",
                    "Modelo": "Financiero" if e.get("modelo_financiero") else "Estándar",
                    "Watchlist": "Sí" if e.get("watchlist") else "No",
                }
                for i, e in enumerate(empresas_filtradas)
            ])
            csv_bytes = df_export.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                label=f"Exportar CSV ({len(empresas_filtradas)})",
                data=csv_bytes,
                file_name="stocklab_ranking.csv",
                mime="text/csv",
                use_container_width=True,
                disabled=(len(empresas_filtradas) == 0),
                key="ranking_btn_download_csv",
            )

    # Determinar líderes de sesión sobre el conjunto filtrado (coherente con los KPIs)
    candidatos_validos = [e for e in empresas_filtradas if e.get("var") is not None and not np.isnan(e.get("var", np.nan))]
    fallback_emp = {'nombre': 'Sin datos', 'ticker': '-', 'var': 0.0}
    top_ganador = max(candidatos_validos, key=lambda x: x["var"]) if candidatos_validos else fallback_emp
    top_perdedor = min(candidatos_validos, key=lambda x: x["var"]) if candidatos_validos else fallback_emp

    # ═══════════════════════════════════════════════════════════════════
    # 🧠 CÁLCULO INTELIGENTE DE CAMBIO DE POSICIONES (COHERENTE POR CRITERIO)
    # ═══════════════════════════════════════════════════════════════════
    
    def calcular_cambio_posiciones_contextual(empresas, pilar_ordenacion='score'):
        """
        Calcula el cambio de posiciones de forma coherente con el criterio activo:
        - Para 'calidad': compara contra 'ranking_estado' en SQLite (que persiste
          posiciones ordenadas por score_calidad).
        - Para otros criterios ('score', 'oportunidad', 'var'): usa su propia línea
          base en st.session_state para evitar mezclar puestos de Calidad con Score/Oportunidad.
        """
        if pilar_ordenacion == 'calidad':
            ordenado_hoy = sorted(empresas, key=lambda x: (-float(x['calidad']), str(x['ticker']).strip().upper()))
        elif pilar_ordenacion == 'oportunidad':
            ordenado_hoy = sorted(empresas, key=lambda x: (-float(x['oportunidad']), str(x['ticker']).strip().upper()))
        elif pilar_ordenacion == 'var':
            ordenado_hoy = sorted(empresas, key=lambda x: (-float(x['var']), str(x['ticker']).strip().upper()))
        else:
            ordenado_hoy = sorted(empresas, key=lambda x: (-float(x['score']), str(x['ticker']).strip().upper()))

        posiciones_hoy = {emp['ticker'].upper(): idx + 1 for idx, emp in enumerate(ordenado_hoy)}

        posiciones_anterior = {}
        if pilar_ordenacion == 'calidad':
            try:
                from services.alert_engine import obtener_ranking_baseline_diario
                posiciones_anterior = obtener_ranking_baseline_diario()
            except Exception:
                posiciones_anterior = {}

        clave = f'_ranking_pos_{pilar_ordenacion}'
        if not posiciones_anterior:
            if clave not in st.session_state:
                st.session_state[clave] = posiciones_hoy.copy()
                return {ticker: 0 for ticker in posiciones_hoy}
            posiciones_anterior = st.session_state[clave]
        else:
            st.session_state[clave] = posiciones_hoy.copy()

        cambios = {}
        for ticker, pos_hoy in posiciones_hoy.items():
            pos_antes = posiciones_anterior.get(ticker, pos_hoy)
            cambios[ticker] = pos_antes - pos_hoy  # positivo = sube puestos

        return cambios
    
    # Determinar el pilar de ordenación activo para el cálculo contextual
    pilar_activo = 'score'
    if ordenar_por == 'CALIDAD 🏆':
        pilar_activo = 'calidad'
    elif ordenar_por == 'OPORTUNIDAD 🎯':
        pilar_activo = 'oportunidad'
    elif ordenar_por == 'Variación Diaria':
        pilar_activo = 'var'
    elif ordenar_por == 'SCORE STOCKLAB ⭐':
        pilar_activo = 'score'
    
    # Calcular cambios de posiciones para todas las empresas
    mapa_cambios = calcular_cambio_posiciones_contextual(empresas_data, pilar_activo)
    
    # Asignar cambios a cada empresa
    for emp in empresas_data:
        emp['cambio'] = mapa_cambios.get(emp['ticker'], 0)

    st.html('<div class="ranking-divider"></div>')
    
    # ── KPIs RESUMEN DINÁMICOS (Mi Cartera Theme) ─────────────────────
    avg_score = sum(e['score'] for e in empresas_filtradas) / len(empresas_filtradas) if empresas_filtradas else 0
    avg_calidad = sum(e['calidad'] for e in empresas_filtradas) / len(empresas_filtradas) if empresas_filtradas else 0
    avg_oportunidad = sum(e['oportunidad'] for e in empresas_filtradas) / len(empresas_filtradas) if empresas_filtradas else 0
    n_analizadas = len(empresas_filtradas)
    
    pct_score = min(100.0, max(0.0, avg_score))
    pct_calidad = min(100.0, max(0.0, avg_calidad))
    pct_oportunidad = min(100.0, max(0.0, avg_oportunidad))

    kpis_html = f"""
    <div class="kpi-row">
        <div class="kpi-card empresas">
            <div class="kpi-header">
                <span class="kpi-title">Activos Analizados</span>
            </div>
            <div class="kpi-value-row">
                <span class="kpi-num">{n_analizadas}</span>
                <span class="kpi-total">empresas</span>
            </div>
            <div class="kpi-track"><div class="kpi-bar" style="width: 100%;"></div></div>
            <div class="kpi-caption">Universo cotizado activo</div>
        </div>
        <div class="kpi-card score">
            <div class="kpi-header">
                <span class="kpi-title">Score StockLab Promedio</span>
            </div>
            <div class="kpi-value-row">
                <span class="kpi-num">{avg_score:.1f}</span>
                <span class="kpi-total">/ 100</span>
            </div>
            <div class="kpi-track"><div class="kpi-bar" style="width: {pct_score:.1f}%;"></div></div>
            <div class="kpi-caption">Ponderado 60% Calidad · 40% Oportunidad</div>
        </div>
        <div class="kpi-card calidad">
            <div class="kpi-header">
                <span class="kpi-title">Calidad Promedio</span>
            </div>
            <div class="kpi-value-row">
                <span class="kpi-num">{avg_calidad:.1f}</span>
                <span class="kpi-total">/ 100</span>
            </div>
            <div class="kpi-track"><div class="kpi-bar" style="width: {pct_calidad:.1f}%;"></div></div>
            <div class="kpi-caption">Solvencia, márgenes y consistencia</div>
        </div>
        <div class="kpi-card oportunidad">
            <div class="kpi-header">
                <span class="kpi-title">Oportunidad Promedio</span>
            </div>
            <div class="kpi-value-row">
                <span class="kpi-num">{avg_oportunidad:.1f}</span>
                <span class="kpi-total">/ 100</span>
            </div>
            <div class="kpi-track"><div class="kpi-bar" style="width: {pct_oportunidad:.1f}%;"></div></div>
            <div class="kpi-caption">Múltiplos de valoración y momentum</div>
        </div>
    </div>
    """
    st.html(kpis_html)

    # ── LÍDERES DE SESIÓN (Twin Terminal Movers) ───────────────────────
    movers_html = f"""
    <div class="movers-row">
        <div class="mover-card bull">
            <div class="mover-left">
                <span class="mover-badge-icon">▲</span>
                <div class="mover-info">
                    <span class="mover-tag">Líder Alcista de la Sesión</span>
                    <span class="mover-name">{top_ganador['nombre']} <span class="mover-ticker">{top_ganador['ticker']}</span></span>
                </div>
            </div>
            <span class="mover-pct">{top_ganador['var']:+.2f}%</span>
        </div>
        <div class="mover-card bear">
            <div class="mover-left">
                <span class="mover-badge-icon">▼</span>
                <div class="mover-info">
                    <span class="mover-tag">Líder Bajista de la Sesión</span>
                    <span class="mover-name">{top_perdedor['nombre']} <span class="mover-ticker">{top_perdedor['ticker']}</span></span>
                </div>
            </div>
            <span class="mover-pct">{top_perdedor['var']:+.2f}%</span>
        </div>
    </div>
    """
    st.html(movers_html)
        
    st.html('<div class="ranking-divider"></div>')
    n_visibles = len(empresas_filtradas)
    st.markdown(f'<div class="sl-section-title">Tabla de Clasificación <span class="table-counter">{n_visibles} activos</span></div>', unsafe_allow_html=True)

    # ═══════════════════════════════════════════════════════════════════
    # 📊 TABLA HTML PREMIUM — FINANCE TERMINAL ARCHITECTURE
    # ═══════════════════════════════════════════════════════════════════
    
    html_filas = ""
    for idx, emp in enumerate(empresas_filtradas):
        puesto = idx + 1

        # Rank styling
        if puesto == 1:
            rank_class = "rank-gold"
            num_class  = "gold"
        elif puesto == 2:
            rank_class = "rank-silver"
            num_class  = "silver"
        elif puesto == 3:
            rank_class = "rank-bronze"
            num_class  = "bronze"
        else:
            rank_class = ""
            num_class  = "normal"

        # Cambio de posición
        cambio_val = emp.get('cambio', 0)
        if cambio_val > 0:
            txt_cambio = f"<span class='chg-up'>&#9650; +{cambio_val}</span>"
        elif cambio_val < 0:
            txt_cambio = f"<span class='chg-down'>&#9660; {cambio_val}</span>"
        else:
            txt_cambio = "<span class='chg-flat'>&mdash;</span>"

        # Variación diaria
        if emp["var"] >= 0:
            txt_var = f"<span class='var-pos'>+{emp['var']:.2f}%</span>"
        else:
            txt_var = f"<span class='var-neg'>{emp['var']:.2f}%</span>"

        badge_wl = "<span class='star-badge' style='color:#fbbf24;font-size:0.85rem;'>&#9733;</span>" if emp["watchlist"] else ""
        # [V7] Badge de datos incompletos: ~ junto al ticker si no hay fuente real
        badge_fallback = ("<span title='Datos no disponibles en ninguna fuente' "
                         "style='font-size:0.68rem;color:#f59e0b;opacity:0.75;"
                         "vertical-align:super;margin-left:2px;'>~</span>"
                         if emp.get('data_source') == 'fallback' else "")

        calidad_s     = f"{emp['calidad']:.1f}"
        opor_s        = f"{emp['oportunidad']:.1f}"
        score_final_s = f"{emp['score']:.1f}"

        # Sector category badge
        sec_str = emp.get("sector") or "General"
        sec_lower = sec_str.lower()
        if any(k in sec_lower for k in ["tecno", "telecom", "software", "ia", "electr", "seguridad"]):
            sec_cls = "sec-tech"
        elif any(k in sec_lower for k in ["financ", "banca", "seguro", "holding", "cartera", "invers"]):
            sec_cls = "sec-fin"
        elif any(k in sec_lower for k in ["salud", "farma", "biotec", "médic"]):
            sec_cls = "sec-health"
        elif any(k in sec_lower for k in ["energ", "renovable", "petr", "gas", "utilit"]):
            sec_cls = "sec-energy"
        elif any(k in sec_lower for k in ["consum", "retail", "moda", "alimen", "restaur", "bebida", "bodega"]):
            sec_cls = "sec-consume"
        else:
            sec_cls = "sec-default"

        # Cobertura fundamental badge
        cov_n = int(emp.get("cobertura_n") or 0)
        cov_tot = int(emp.get("cobertura_total") or 9)
        cov_niv = emp.get("cobertura_nivel") or "Baja"
        cov_falt = emp.get("cobertura_faltantes") or []
        cov_cls = "cov-high" if cov_niv == "Alta" else ("cov-mid" if cov_niv == "Media" else "cov-low")
        cov_tip = (
            f"Cobertura {cov_niv} ({cov_n}/{cov_tot} métricas)"
            + (f" · Faltan: {', '.join(cov_falt)}" if cov_falt else " · Completa")
        )
        badge_cov = f'<span class="cov-badge {cov_cls}" title="{cov_tip}">{cov_n}/{cov_tot}</span>'

        # Build row as a single flat string (no internal newlines)
        row = (f'<tr class="{rank_class}">'
               f'<td style="text-align:center;"><span class="rnk-num {num_class}">{puesto}</span></td>'
               f'<td><span class="rnk-ticker">{emp["ticker"]}</span>{badge_fallback}</td>'
               f'<td style="text-align:center;">{txt_cambio}</td>'
               f'<td><span class="rnk-name">{emp["nombre"]}</span></td>'
               f'<td style="text-align:center;"><span class="score-badge score-calidad">{calidad_s}</span></td>'
               f'<td style="text-align:center;"><span class="score-badge score-oportunidad">{opor_s}</span></td>'
               f'<td style="text-align:center;"><span class="score-badge score-final">{score_final_s}</span></td>'
               f'<td style="text-align:center;">{badge_cov}</td>'
               f'<td><span class="rnk-sec-pill {sec_cls}" title="Sector: {sec_str}">{sec_str}</span></td>'
               f'<td style="text-align:center;"><span class="rnk-price">{emp["precio"]}</span></td>'
               f'<td style="text-align:center;">{txt_var}</td>'
               f'<td style="text-align:center;">{badge_wl}</td>'
               f'</tr>')
        html_filas += row

    # Empty state visual si ningún activo coincide con los filtros
    if not html_filas:
        html_filas = (
            '<tr><td colspan="12" style="text-align:center;padding:36px 16px;color:#818cf8;'
            'font-size:0.95rem;background:rgba(255,255,255,0.02);">'
            'No se encontraron activos que coincidan con los filtros seleccionados.</td></tr>'
        )

    # Build the full table as a flat single-line string
    html_tabla = (
        '<div class="ranking-wrapper">'
        '<table class="rnk-table">'
        '<thead><tr>'
        '<th style="text-align:center;width:48px;" title="Posición en el ranking según el criterio de ordenación activo">#</th>'
        '<th title="Símbolo del activo en el Mercado Continuo / BME">Ticker</th>'
        '<th style="text-align:center;width:72px;" title="Variación de puestos en el ranking frente a la sesión anterior">Cambio</th>'
        '<th title="Nombre de la compañía cotizada">Empresa</th>'
        '<th style="text-align:center;" title="Calidad Fundamental (60% peso): Solvencia, márgenes operativos calibrados y foso">Calidad</th>'
        '<th style="text-align:center;" title="Oportunidad Cuantitativa (40% peso): Valoración, tendencia multi-sesión y momentum fundamental">Oportunidad</th>'
        '<th style="text-align:center;" title="Score StockLab V8.5 (0 - 100): Ponderación combinada 60% Calidad + 40% Oportunidad">Score Final</th>'
        '<th style="text-align:center;width:62px;" title="Cobertura de métricas fundamentales disponibles para el cálculo del score">Datos</th>'
        '<th title="Sector económico de la compañía">Sector</th>'
        '<th style="text-align:center;" title="Último precio de cierre cotizado">Precio</th>'
        '<th style="text-align:center;" title="Variación porcentual diaria en la última sesión de mercado">Var. Diaria</th>'
        '<th style="text-align:center;width:52px;" title="Seguimiento activo en tu Watchlist personal">Watchlist</th>'
        '</tr></thead>'
        f'<tbody>{html_filas}</tbody>'
        '</table></div>'
    )
    st.html(html_tabla)
    
    st.html('<div class="ranking-divider"></div>')
    
    # ═══════════════════════════════════════════════════════════════════
    # 🔍 INSPECTOR DE ACTIVOS PREMIUM — 6 PANELES DIFERENCIADOS
    # ═══════════════════════════════════════════════════════════════════
    st.markdown('<div class="sl-section-title">Inspector de Activos StockLab</div>', unsafe_allow_html=True)
    
    if empresas_data:
        # Priorizar las empresas del filtro activo y permitir auditar cualquier activo del universo
        tickers_filtrados = {e["ticker"] for e in empresas_filtradas}
        empresas_inspector = list(empresas_filtradas) + [
            e for e in sorted(empresas_data, key=lambda x: x["nombre"])
            if e["ticker"] not in tickers_filtrados
        ]
        opciones_buscador = [f"{e['ticker']} - {e['nombre']}" for e in empresas_inspector]
        
        seleccion_raw = st.selectbox(
            "Selecciona una empresa para auditar sus fundamentales:", 
            options=opciones_buscador, 
            index=None, 
            placeholder="--- Seleccione una empresa ---"
        )
        
        if seleccion_raw:
            ticker_sel = seleccion_raw.split(" - ")[0]
            emp_sel = next((e for e in empresas_data if e["ticker"] == ticker_sel), None)
            if not emp_sel:
                st.warning(f"No se encontraron datos disponibles para {ticker_sel}.")
                return
            
            # Pre-computed data
            p_calidad    = emp_sel['calidad']
            p_oportunidad = emp_sel['oportunidad']
            p_score_final = emp_sel['score']
            
            # Desgloses
            info_fund  = emp_sel.get('info_fundamentales', {})
            df_hist    = emp_sel.get('df_historico')
            variacion  = emp_sel.get('var', 0.0)
            
            sector_sel           = emp_sel.get('sector', '')
            desglose_calidad     = _desglosar_calidad_detalle(info_fund, sector_sel)
            desglose_oportunidad = _desglosar_oportunidad_detalle(df_hist, info_fund, variacion, sector=sector_sel)
            cobertura_sel        = _calcular_cobertura_fundamental(info_fund, sector_sel)
            
            # Ratios fundamentales — [V8.5] _normalizar_ratio_pct corrige ratios >= 100%
            trailing_pe      = _clean_float(info_fund.get('trailingPE'))
            forward_pe       = _clean_float(info_fund.get('forwardPE'))
            per              = (trailing_pe if (trailing_pe and trailing_pe > 0) else (forward_pe if (forward_pe and forward_pe > 0) else 0.0))
            per_es_forward   = (trailing_pe is None or trailing_pe <= 0) and (forward_pe is not None and forward_pe > 0)
            
            dividend_yield_r = _clean_float(info_fund.get('dividendYield'))
            p_b_raw          = _clean_float(info_fund.get('priceToBook'))
            roe_pct_opt      = _normalizar_ratio_pct(info_fund.get('returnOnEquity'), info_fund)
            
            dividend_yield_pct = _normalizar_div_pct(dividend_yield_r) or 0.0
            
            per_fmt  = round(per, 2)
            p_b_fmt  = round(p_b_raw, 2) if p_b_raw is not None else None
            div_fmt  = round(dividend_yield_pct, 2)
            roe_fmt  = round(roe_pct_opt, 2) if roe_pct_opt is not None else None
            
            # Price & var display
            precio_display = emp_sel['precio']
            var_class      = "insp-var-pos" if variacion >= 0 else "insp-var-neg"
            var_arrow      = "&#9650;" if variacion >= 0 else "&#9660;"
            var_sign       = "+" if variacion >= 0 else ""
            var_display    = f"{var_arrow} {var_sign}{variacion:.2f}%"

            # ─── helper: bar HTML [V8.4] soporte 'unavailable' + 'reason' ──────
            def bar_html(label, val, max_val, desc, color_cls="cf-green", val_color="#34d399", unavailable=False, reason=""):
                reason_line = f'<div class="cbar-reason">{reason}</div>' if reason else ''
                if unavailable:
                    return (f'<div class="cbar-wrap">'
                            f'<div class="cbar-header">'
                            f'<div><div class="cbar-label">{label}</div>'
                            f'<div class="cbar-desc">{desc}</div></div>'
                            f'<div class="cbar-val" style="color:var(--sl-text-muted);font-style:italic;">Sin datos</div>'
                            f'</div><div class="cbar-bg">'
                            f'<div class="cbar-fill" style="width:0%;background:rgba(255,255,255,0.04);"></div>'
                            f'</div>{reason_line}</div>')
                pct = min(100, round((val / max_val) * 100, 1)) if max_val > 0 else 0
                return f"""
                <div class="cbar-wrap">
                  <div class="cbar-header">
                    <div>
                      <div class="cbar-label">{label}</div>
                      <div class="cbar-desc">{desc}</div>
                    </div>
                    <div class="cbar-val" style="color:{val_color};">{val:.1f}<span style="color:var(--sl-text-muted);font-size:0.72rem;font-weight:400;">/{max_val}</span></div>
                  </div>
                  <div class="cbar-bg">
                    <div class="cbar-fill {color_cls}" style="width:{pct}%;"></div>
                  </div>
                  {reason_line}
                </div>"""

            # ─── Variables de contexto inspector [V8.5] ─────────────────
            _modelo_fin  = desglose_calidad.get('modelo_financiero', False)
            _es_banca_sel = desglose_calidad.get('es_banca', False)
            _lbl_fort    = "Fortaleza Financiera (Banca)" if _es_banca_sel else ("Fortaleza Financiera (Seguros)" if _modelo_fin else "Fortaleza Financiera")
            _desc_fort   = "ROA Bancario · Margen Neto calibrado" if _es_banca_sel else ("D/E Seguros · Margen Neto" if _modelo_fin else "Debt/Equity · Current Ratio")
            _desc_val    = "PER bancario (20) · P/B financiero (20)" if _modelo_fin else "PER · EV/EBITDA · P/B (o P/S)"
            _sin_fort    = desglose_calidad.get('sin_datos_fortaleza', False)
            _sin_div     = desglose_calidad.get('sin_datos_dividendos', False)
            _ajustes_sel = desglose_calidad.get('ajustes') or []

            _cov_cls_sel = "cov-high" if cobertura_sel['nivel'] == "Alta" else ("cov-mid" if cobertura_sel['nivel'] == "Media" else "cov-low")
            _cov_tip_sel = (
                f"Faltan: {', '.join(cobertura_sel['faltantes'])}"
                if cobertura_sel['faltantes']
                else "Todas las métricas clave disponibles"
            )
            _modelo_pill_cls = "model-fin" if _modelo_fin else "model-std"
            _modelo_pill_txt = "Modelo Financiero" if _modelo_fin else "Modelo Estándar"
            _ajustes_html = (
                f'<div class="insp-ajustes"><b>Ajustes aplicados:</b> {" · ".join(_ajustes_sel)}</div>'
                if _ajustes_sel else ""
            )

            # ─── PANEL 1: RESUMEN DEL ACTIVO ────────────────────────────
            p1_html = f"""
<div class="inspector-shell">

  <div class="insp-header">
    <div class="insp-title-block">
      <div class="insp-company-name">{emp_sel['nombre']}</div>
      <div class="insp-meta-row">
        <span class="insp-ticker-pill" style="margin-top:0;">{emp_sel['ticker']}</span>
        <span class="insp-meta-pill {_modelo_pill_cls}">{_modelo_pill_txt}</span>
        <span class="cov-badge {_cov_cls_sel}" title="{_cov_tip_sel}">Datos: {cobertura_sel['disponibles']}/{cobertura_sel['total']} · {cobertura_sel['nivel']}</span>
      </div>
    </div>
    <div class="insp-price-block">
      <div class="insp-price-value">{precio_display}</div>
      <span class="{var_class}">{var_display}</span>
    </div>
  </div>

  <!-- PANEL 1: Scores resumen -->
  <div class="insp-panel">
    <div class="insp-panel-title">Puntuaciones Globales</div>
    <div class="score-grid">
      <div class="score-card">
        <div class="score-card-label">Score Final</div>
        <div class="score-card-value sc-final">{p_score_final:.1f}</div>
        <div class="score-card-sub">/ 100 pts</div>
      </div>
      <div class="score-card">
        <div class="score-card-label">Calidad Empresarial</div>
        <div class="score-card-value sc-calidad">{p_calidad:.1f}</div>
        <div class="score-card-sub">60% peso · / 100</div>
      </div>
      <div class="score-card">
        <div class="score-card-label">Oportunidad de Inv.</div>
        <div class="score-card-value sc-opor">{p_oportunidad:.1f}</div>
        <div class="score-card-sub">40% peso · / 100</div>
      </div>
    </div>
  </div>

  <!-- PANEL 2: Score global bar -->
  <div class="insp-panel">
    <div class="insp-panel-title">Desglose de Ponderación</div>
    {bar_html("Score StockLab Final", p_score_final, 100, "Ponderación 60% Calidad + 40% Oportunidad", "cf-gold", "#f59e0b")}
    {bar_html("Calidad Empresarial", p_calidad, 100, "Solidez intrínseca del negocio (curvas suaves calibradas)", "cf-green", "#34d399")}
    {bar_html("Oportunidad de Inversión", p_oportunidad, 100, "Valoración + Momentum técnico multi-sesión + Momentum fundamental", "cf-cyan", "#22d3ee")}
    <div class="insp-resumen">{emp_sel['resumen']}</div>
    {_ajustes_html}
  </div>

  <!-- PANEL 3: Desglose Calidad Empresarial -->
  <div class="insp-panel">
    <div class="insp-panel-title">Auditoría de Calidad Empresarial</div>
    <div class="sub-grid">
      <div>
        {bar_html("Calidad del Negocio", desglose_calidad['calidad_negocio'], 40, "ROE · Margen Operativo calibrado · Crecimiento", "cf-green", "#34d399", reason=desglose_calidad.get('_reason_calidad_negocio', ''))}
        {bar_html(_lbl_fort, desglose_calidad['fortaleza_financiera'], 30, _desc_fort, "cf-cyan", "#22d3ee", unavailable=_sin_fort, reason=desglose_calidad.get('_reason_fortaleza_financiera', ''))}
      </div>
      <div>
        {bar_html("Dividendos / Asignación", desglose_calidad['dividendos'], 10, "Yield & Payout sostenible o Reinversión", "cf-gold", "#fbbf24", unavailable=_sin_div, reason=desglose_calidad.get('_reason_dividendos', ''))}
        {bar_html("Ventaja Competitiva", desglose_calidad['ventaja_competitiva'], 15, "Market Cap · Foso Operativo · Moat ROA/ROE", "cf-violet", "#a78bfa", reason=desglose_calidad.get('_reason_ventaja_competitiva', ''))}
      </div>
    </div>
  </div>

  <!-- PANEL 4: Desglose Oportunidad -->
  <div class="insp-panel">
    <div class="insp-panel-title">Auditoría de Oportunidad de Inversión</div>
    <div class="sub-grid">
      <div>
        {bar_html("Valoración Fundamental", desglose_oportunidad['valoracion'], 40, _desc_val, "cf-gold", "#fbbf24", reason=desglose_oportunidad.get('_reason_valoracion', ''))}
        {bar_html("Momentum Técnico", desglose_oportunidad['momentum_tecnico'], 30, "SMA50/20 · Mom 20d · RSI 14 · Máx 52s", "cf-cyan", "#22d3ee", reason=desglose_oportunidad.get('_reason_momentum_tecnico', ''))}
      </div>
      <div>
        {bar_html("Momentum Fundamental", desglose_oportunidad['momentum_fundamental'], 30, "Ingresos · Beneficios · Margen neto", "cf-violet", "#a78bfa", reason=desglose_oportunidad.get('_reason_momentum_fundamental', ''))}
      </div>
    </div>
  </div>

  <!-- PANEL 5: Ratios Fundamentales -->
  <div class="insp-panel">
    <div class="insp-panel-title">Ratios Fundamentales</div>
    <div class="ratio-grid">
      <div class="ratio-card">
        <div class="ratio-key">PER</div>
        <div class="ratio-val {'na' if per_fmt <= 0 else ''}">{f'{per_fmt}' if per_fmt > 0 else 'N/A'}</div>
        <div class="ratio-note">{'Forward P/E' if per_es_forward else 'Price / Earnings'}</div>
      </div>
      <div class="ratio-card">
        <div class="ratio-key">Div Yield</div>
        <div class="ratio-val {'na' if div_fmt <= 0 else ''}">{f'{div_fmt}%' if div_fmt > 0 else 'N/A'}</div>
        <div class="ratio-note">Dividend Yield</div>
      </div>
      <div class="ratio-card">
        <div class="ratio-key">P / B</div>
        <div class="ratio-val {'na' if p_b_fmt is None else ''}">{f'{p_b_fmt}' if p_b_fmt is not None else 'N/A'}</div>
        <div class="ratio-note">{'Patrimonio negativo' if (p_b_fmt is not None and p_b_fmt <= 0) else 'Price / Book'}</div>
      </div>
      <div class="ratio-card">
        <div class="ratio-key">ROE</div>
        <div class="ratio-val {'na' if roe_fmt is None else ''}">{f'{roe_fmt}%' if roe_fmt is not None else 'N/A'}</div>
        <div class="ratio-note">Return on Equity</div>
      </div>
    </div>
  </div>

</div>
"""
            # [V8.5] Banner transparente de cobertura de datos
            if emp_sel.get('data_source') == 'fallback':
                st.markdown('<div style="margin:4px 0 10px;padding:10px 16px;background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.25);border-radius:10px;font-size:0.79rem;color:#fbbf24;"><b>&#9888; Datos insuficientes:</b> Este activo no dispone de fundamentales verificados. El score de Calidad no es representativo.</div>', unsafe_allow_html=True)
            elif cobertura_sel['faltantes']:
                _falt_txt = ", ".join(cobertura_sel['faltantes'])
                st.markdown(f'<div style="margin:4px 0 10px;padding:10px 16px;background:rgba(56,189,248,0.07);border:1px solid rgba(56,189,248,0.22);border-radius:10px;font-size:0.79rem;color:#93c5fd;"><b>&#8505; Cobertura {cobertura_sel["nivel"]} ({cobertura_sel["disponibles"]}/{cobertura_sel["total"]}):</b> Sin dato en fuente para <i>{_falt_txt}</i>. Los pilares afectados usan valor neutro conservador.</div>', unsafe_allow_html=True)
            flat_html = "".join(line.strip() for line in p1_html.splitlines())
            st.html(flat_html)
            
            # ─── PANEL 6: ACCIONES RÁPIDAS (native Streamlit buttons) ──
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown(
                "<div style='font-size:0.72rem;font-weight:700;text-transform:uppercase;"
                "letter-spacing:1.4px;color:#94a3b8;margin-bottom:10px;'>Acciones Rápidas</div>",
                unsafe_allow_html=True
            )
            
            with st.container(key="inspector_actions"):
                btn_col1, btn_col2, btn_col3 = st.columns(3)
                
                with btn_col1:
                    if emp_sel["watchlist"]:
                        if st.button("Quitar de Watchlist", key=f"btn_obs_{emp_sel['ticker']}", use_container_width=True):
                            eliminar_de_watchlist(emp_sel['ticker'])
                            st.toast(f"Eliminado de la Watchlist: **{emp_sel['ticker']}**")
                            st.rerun()
                    else:
                        if st.button("Añadir a Watchlist", key=f"btn_obs_{emp_sel['ticker']}", use_container_width=True):
                            precio_float = float(emp_sel.get("precio_num") or 0.0)
                            if precio_float <= 0:
                                try:
                                    precio_float = float((market_data.get_stock_data(emp_sel['ticker']) or {}).get("precio_actual") or 0.0)
                                except Exception:
                                    precio_float = 0.0
                            if precio_float > 0:
                                agregar_a_watchlist(emp_sel['ticker'], precio_float, "Añadido desde módulo Ranking V2", None, "COMPRA_EN_CAIDA")
                                st.toast(f"Añadido a la Watchlist con éxito: **{emp_sel['ticker']}**")
                                st.rerun()
                            else:
                                st.warning(f"No se pudo obtener un precio válido para {emp_sel['ticker']}.")
                            
                with btn_col2:
                    if st.button("Analizar Gráfico", key=f"btn_ana_{emp_sel['ticker']}", use_container_width=True):
                        try:
                            db_res = query_all("SELECT nombre FROM empresas WHERE ticker = ?", (emp_sel['ticker'],))
                            nombre_db = db_res[0]['nombre'] if db_res else emp_sel['nombre']
                        except:
                            nombre_db = emp_sel['nombre']
                        
                        opcion_seleccionada = f"{emp_sel['ticker']} - {nombre_db}"
                        st.session_state["selector_global_empresas"] = opcion_seleccionada
                        st.session_state["menu_activo"] = "🏢 Empresas"
                        st.rerun()
                        
                with btn_col3:
                    if st.button("Comprar Acciones", key=f"btn_car_{emp_sel['ticker']}", use_container_width=True):
                        st.session_state["compra_preseleccionada"] = emp_sel['ticker']
                        st.session_state["accion_activa"] = "comprar"
                        _p_buy = float(emp_sel.get("precio_num") or 0.0)
                        if _p_buy > 0:
                            st.session_state["precio_operacion_input"] = _p_buy
                        st.session_state["menu_activo"] = "Mi Cartera"
                        st.rerun()
        else:
            empty_html = """
            <div class="insp-empty-shell">
                <div class="insp-empty-title">Terminal de Auditoría Fundamental en Espera</div>
                <div class="insp-empty-desc">
                    Selecciona cualquier activo cotizado en el desplegable superior para auditar su radiografía completa:
                    ratios de valoración (PER, P/B, Div Yield), solvencia financiera, calidad de balance y catalizadores cuantitativos.
                </div>
                <div class="insp-empty-grid">
                    <div class="insp-empty-pill">
                        <span class="insp-empty-pill-tag">SOLVENCIA</span>
                        <span class="insp-empty-pill-name">Calidad de Balance</span>
                    </div>
                    <div class="insp-empty-pill">
                        <span class="insp-empty-pill-tag">MÚLTIPLOS</span>
                        <span class="insp-empty-pill-name">PER · P/B · Div Yield</span>
                    </div>
                    <div class="insp-empty-pill">
                        <span class="insp-empty-pill-tag">RENTABILIDAD</span>
                        <span class="insp-empty-pill-name">ROE & Márgenes</span>
                    </div>
                    <div class="insp-empty-pill">
                        <span class="insp-empty-pill-tag">SCORING V8.5</span>
                        <span class="insp-empty-pill-name">Puntuación StockLab</span>
                    </div>
                </div>
                <div class="insp-empty-hint">Puedes buscar escribiendo el Ticker (ej: BBVA, REP, ITX) o el Nombre de la Empresa</div>
            </div>
            """
            st.html(empty_html)

        # ═══════════════════════════════════════════════════════════════════
        # ⚖️ COMPARADOR RÁPIDO 1 VS 1 (DESPLEGABLE SIN RECARGAR LA VISTA)
        # ═══════════════════════════════════════════════════════════════════
        if len(empresas_data) >= 2:
            st.html('<div class="ranking-divider"></div>')
            st.markdown('<div class="sl-section-title">Comparador Rápido 1 vs 1</div>', unsafe_allow_html=True)
            with st.expander("Comparar dos empresas cara a cara", expanded=False):
                opciones_cmp = [f"{e['ticker']} - {e['nombre']}" for e in empresas_inspector]
                cmp_c1, cmp_c2 = st.columns(2)
                with cmp_c1:
                    sel_a_raw = st.selectbox(
                        "ACTIVO A",
                        options=opciones_cmp,
                        index=0,
                        key="ranking_cmp_a",
                    )
                with cmp_c2:
                    sel_b_raw = st.selectbox(
                        "ACTIVO B",
                        options=opciones_cmp,
                        index=min(1, len(opciones_cmp) - 1),
                        key="ranking_cmp_b",
                    )

                tk_a = sel_a_raw.split(" - ")[0]
                tk_b = sel_b_raw.split(" - ")[0]
                emp_a = next((e for e in empresas_data if e["ticker"] == tk_a), None)
                emp_b = next((e for e in empresas_data if e["ticker"] == tk_b), None)

                if emp_a and emp_b:
                    if tk_a == tk_b:
                        st.info("Selecciona dos activos distintos para ver la comparativa cara a cara.")
                    else:
                        inf_a, inf_b = emp_a.get("info_fundamentales", {}), emp_b.get("info_fundamentales", {})
                        cal_a = _desglosar_calidad_detalle(inf_a, emp_a.get("sector", ""))
                        cal_b = _desglosar_calidad_detalle(inf_b, emp_b.get("sector", ""))
                        opo_a = _desglosar_oportunidad_detalle(emp_a.get("df_historico"), inf_a, emp_a.get("var", 0.0), sector=emp_a.get("sector", ""))
                        opo_b = _desglosar_oportunidad_detalle(emp_b.get("df_historico"), inf_b, emp_b.get("var", 0.0), sector=emp_b.get("sector", ""))

                        def _ext_per(inf):
                            t = _clean_float(inf.get("trailingPE"))
                            f = _clean_float(inf.get("forwardPE"))
                            return t if (t and t > 0) else (f if (f and f > 0) else None)

                        def _cmp_row(label, va, vb, fmt_fn, higher_better=True):
                            sa = fmt_fn(va) if va is not None else "N/A"
                            sb = fmt_fn(vb) if vb is not None else "N/A"
                            ca, cb = "", ""
                            if va is not None and vb is not None and abs(va - vb) > 1e-6:
                                if (va > vb) if higher_better else (va < vb):
                                    ca = "cmp-win"
                                else:
                                    cb = "cmp-win"
                            return f'<tr><td>{label}</td><td class="{ca}">{sa}</td><td class="{cb}">{sb}</td></tr>'

                        per_a, per_b = _ext_per(inf_a), _ext_per(inf_b)
                        pb_a = _clean_float(inf_a.get("priceToBook"))
                        pb_b = _clean_float(inf_b.get("priceToBook"))
                        pb_a_pos = pb_a if (pb_a is not None and pb_a > 0) else None
                        pb_b_pos = pb_b if (pb_b is not None and pb_b > 0) else None
                        roe_a = _normalizar_ratio_pct(inf_a.get("returnOnEquity"), inf_a)
                        roe_b = _normalizar_ratio_pct(inf_b.get("returnOnEquity"), inf_b)
                        opm_a = _normalizar_ratio_pct(inf_a.get("operatingMargins"), inf_a)
                        opm_b = _normalizar_ratio_pct(inf_b.get("operatingMargins"), inf_b)
                        rg_a = _normalizar_ratio_pct(inf_a.get("revenueGrowth"), inf_a)
                        rg_b = _normalizar_ratio_pct(inf_b.get("revenueGrowth"), inf_b)
                        div_a = emp_a.get("div_yield_pct") if float(emp_a.get("div_yield_pct") or 0) > 0 else None
                        div_b = emp_b.get("div_yield_pct") if float(emp_b.get("div_yield_pct") or 0) > 0 else None

                        filas_cmp = (
                            '<tr class="cmp-sec"><td colspan="3">Puntuación StockLab V8.5</td></tr>'
                            + _cmp_row("Score Final (/100)", emp_a["score"], emp_b["score"], lambda v: f"{v:.1f}")
                            + _cmp_row("Calidad Empresarial (/100)", emp_a["calidad"], emp_b["calidad"], lambda v: f"{v:.1f}")
                            + _cmp_row("Oportunidad de Inversión (/100)", emp_a["oportunidad"], emp_b["oportunidad"], lambda v: f"{v:.1f}")
                            + '<tr class="cmp-sec"><td colspan="3">Pilares Cuantitativos</td></tr>'
                            + _cmp_row("Calidad del Negocio (/40)", cal_a["calidad_negocio"], cal_b["calidad_negocio"], lambda v: f"{v:.1f}")
                            + _cmp_row("Fortaleza Financiera (/30)", cal_a["fortaleza_financiera"], cal_b["fortaleza_financiera"], lambda v: f"{v:.1f}")
                            + _cmp_row("Dividendos / Asignación (/10)", cal_a["dividendos"], cal_b["dividendos"], lambda v: f"{v:.1f}")
                            + _cmp_row("Ventaja Competitiva (/15)", cal_a["ventaja_competitiva"], cal_b["ventaja_competitiva"], lambda v: f"{v:.1f}")
                            + _cmp_row("Valoración Fundamental (/40)", opo_a["valoracion"], opo_b["valoracion"], lambda v: f"{v:.1f}")
                            + _cmp_row("Momentum Técnico (/30)", opo_a["momentum_tecnico"], opo_b["momentum_tecnico"], lambda v: f"{v:.1f}")
                            + _cmp_row("Momentum Fundamental (/30)", opo_a["momentum_fundamental"], opo_b["momentum_fundamental"], lambda v: f"{v:.1f}")
                            + '<tr class="cmp-sec"><td colspan="3">Fundamentales Clave</td></tr>'
                            + _cmp_row("PER (menor es más barato)", per_a, per_b, lambda v: f"{v:.2f}x", higher_better=False)
                            + _cmp_row("P/B (menor es más barato)", pb_a_pos, pb_b_pos, lambda v: f"{v:.2f}x", higher_better=False)
                            + _cmp_row("ROE", roe_a, roe_b, lambda v: f"{v:.1f}%")
                            + _cmp_row("Margen Operativo", opm_a, opm_b, lambda v: f"{v:.1f}%")
                            + _cmp_row("Crecimiento Ingresos", rg_a, rg_b, lambda v: f"{v:+.1f}%")
                            + _cmp_row("Dividend Yield", div_a, div_b, lambda v: f"{v:.2f}%")
                            + f'<tr><td>Cobertura de Datos</td><td>{emp_a.get("cobertura_n", 0)}/{emp_a.get("cobertura_total", 9)} ({emp_a.get("cobertura_nivel", "-")})</td><td>{emp_b.get("cobertura_n", 0)}/{emp_b.get("cobertura_total", 9)} ({emp_b.get("cobertura_nivel", "-")})</td></tr>'
                        )

                        diff_score = emp_a["score"] - emp_b["score"]
                        if abs(diff_score) < 0.05:
                            veredicto = f"Empate técnico en Score Final ({emp_a['score']:.1f} pts)"
                        elif diff_score > 0:
                            veredicto = f"Ventaja global: <b>{emp_a['nombre']} ({tk_a})</b> por <b>+{diff_score:.1f} pts</b>"
                        else:
                            veredicto = f"Ventaja global: <b>{emp_b['nombre']} ({tk_b})</b> por <b>+{abs(diff_score):.1f} pts</b>"

                        cmp_html = (
                            '<div class="cmp-shell">'
                            '<table class="cmp-table">'
                            f'<thead><tr><th>Métrica</th><th>{tk_a} · {emp_a["nombre"]}</th><th>{tk_b} · {emp_b["nombre"]}</th></tr></thead>'
                            f'<tbody>{filas_cmp}</tbody>'
                            '</table>'
                            f'<div class="cmp-verdict"><span>{veredicto}</span>'
                            f'<span style="color:#94a3b8;font-size:0.73rem;">En verde: mejor valor en cada métrica</span></div>'
                            '</div>'
                        )
                        st.html(cmp_html)
    else:
        empty_filtro_html = """
        <div class="insp-empty-shell">
            <div class="insp-empty-title">Sin Activos Disponibles</div>
            <div class="insp-empty-desc">
                No se encontraron activos que coincidan con los filtros seleccionados actualmente en el ranking.
            </div>
        </div>
        """
        st.html(empty_filtro_html)
