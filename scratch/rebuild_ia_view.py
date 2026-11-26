"""
Script de construcción para fusionar el agente original de IA con el nuevo panel descriptivo
y el laboratorio experimental en views/ia.py.
"""
import re

with open("scratch/ia_old.py", encoding="utf-8") as f:
    old_content = f.read()

# 1. Modificar _render_inspeccion_activo para eliminar los conos con deriva y la métrica de Confianza
old_cone_block = """        closes = sub["Close"]
        sma20 = closes.rolling(20).mean()
        sma50 = closes.rolling(50).mean()
        cono = signal_engine.proyectar_cono(
            ind["close"], s_hz["score"], ind["vol_d"],
            ind["ultima_fecha"], dias=dias,
        )
        fechas_hist = [pd.Timestamp(d).strftime("%d %b %Y") for d in sub.index]
        fechas_fut = ([pd.Timestamp(d).strftime("%d %b %Y") for d in cono["fechas"]]
                      if cono else [])
        labels = fechas_hist + fechas_fut
        n_hist, n_fut = len(fechas_hist), len(fechas_fut)

        def _pad_hist(serie: pd.Series) -> list:
            return [None if pd.isna(v) else round(float(v), 4)
                    for v in serie] + [None] * n_fut

        ultimo = float(closes.iloc[-1])
        extra_series = [
            {"label": "SMA20", "data": _pad_hist(sma20),
             "color": "#f59e0b", "dash": [4, 4], "width": 1.0},
            {"label": "SMA50", "data": _pad_hist(sma50),
             "color": "#8b5cf6", "dash": [4, 4], "width": 1.0},
        ]
        if cono:
            def _pad_cone(vals) -> list:
                return ([None] * (n_hist - 1) + [ultimo]
                        + [round(float(v), 4) for v in vals])

            extra_series += [
                {"label": "Pesimista", "data": _pad_cone(cono["pesimista"]),
                 "color": "#ef4444", "dash": [6, 4], "width": 1.2},
                {"label": "Optimista", "data": _pad_cone(cono["optimista"]),
                 "color": "#10b981", "dash": [6, 4], "width": 1.2,
                 "fill_to": "-1", "fill_color": "rgba(0, 229, 255, 0.07)"},
                {"label": "Escenario base", "data": _pad_cone(cono["base"]),
                 "color": "#00e5ff", "dash": [8, 4], "width": 1.6},
            ]

        color_linea = "#10B981" if ultimo >= float(closes.iloc[0]) else "#EF4444"

        render_stock_chart(
            labels=labels,
            prices=_pad_hist(closes),
            last_close=ultimo,
            line_color=color_linea,
            currency="EUR",
            ticker=ticker,
            title=f"{nombre} — Proyección Algorítmica ({hz_label})",
            show_reference_line=True,
            show_measure_tool=False,
            show_zoom=True,
            height=460,
            y_padding_ratio=0.06,
            extra_series=extra_series,
        )"""

new_cone_block = """        closes = sub["Close"]
        sma20 = closes.rolling(20).mean()
        sma50 = closes.rolling(50).mean()
        fechas_hist = [pd.Timestamp(d).strftime("%d %b %Y") for d in sub.index]
        labels = fechas_hist

        def _pad_hist(serie: pd.Series) -> list:
            return [None if pd.isna(v) else round(float(v), 4) for v in serie]

        ultimo = float(closes.iloc[-1])
        extra_series = [
            {"label": "SMA20", "data": _pad_hist(sma20),
             "color": "#f59e0b", "dash": [4, 4], "width": 1.0},
            {"label": "SMA50", "data": _pad_hist(sma50),
             "color": "#8b5cf6", "dash": [4, 4], "width": 1.0},
        ]

        color_linea = "#10B981" if ultimo >= float(closes.iloc[0]) else "#EF4444"

        render_stock_chart(
            labels=labels,
            prices=_pad_hist(closes),
            last_close=ultimo,
            line_color=color_linea,
            currency="EUR",
            ticker=ticker,
            title=f"{nombre} — Evolución Técnica ({hz_label})",
            show_reference_line=True,
            show_measure_tool=False,
            show_zoom=True,
            height=460,
            y_padding_ratio=0.06,
            extra_series=extra_series,
        )"""

assert old_cone_block in old_content, "old_cone_block not found in old_content"
content = old_content.replace(old_cone_block, new_cone_block)

# Eliminar métrica Confianza del diálogo de inspección
old_conf_kpi = """                <div class="ia-diag-metrics-grid">
                    <div class="ia-diag-mini-kpi">
                        <span class="mini-lbl">Confianza</span>
                        <span class="mini-val cyan">{s_hz['confianza']:.0f}%</span>
                    </div>
                    <div class="ia-diag-mini-kpi">
                        <span class="mini-lbl">Proyección</span>
                        <span class="mini-val {'pos' if s_hz['pct']>=0 else 'neg'}">{s_hz['pct']:+.2f}%</span>
                    </div>
                    <div class="ia-diag-mini-kpi">
                        <span class="mini-lbl">Volatilidad (D)</span>
                        <span class="mini-val neutral">~{vol_pct:.1f}%</span>
                    </div>
                </div>"""

new_conf_kpi = """                <div class="ia-diag-metrics-grid" style="grid-template-columns: 1fr 1fr;">
                    <div class="ia-diag-mini-kpi">
                        <span class="mini-lbl">Proyección</span>
                        <span class="mini-val {'pos' if s_hz['pct']>=0 else 'neg'}">{s_hz['pct']:+.2f}%</span>
                    </div>
                    <div class="ia-diag-mini-kpi">
                        <span class="mini-lbl">Volatilidad (D)</span>
                        <span class="mini-val neutral">~{vol_pct:.1f}%</span>
                    </div>
                </div>"""

assert old_conf_kpi in content, "old_conf_kpi not found in content"
content = content.replace(old_conf_kpi, new_conf_kpi)

# 2. Modificar _seccion_radar para eliminar columna de Confianza y filtro de Confianza
old_conviccion = """"⭐ Alta convicción (Confianza ≥ 75%)",\n        """
content = content.replace(old_conviccion, "")

old_filter_conf = """    if conv_elegida == "⭐ Alta convicción (Confianza ≥ 75%)":
        df = df[df[f"conf_{hz_key}"] >= 75.0]
    elif conv_elegida == "🟢 Solo Alcistas":"""
new_filter_conf = """    if conv_elegida == "🟢 Solo Alcistas":"""
assert old_filter_conf in content, "old_filter_conf not found in content"
content = content.replace(old_filter_conf, new_filter_conf)

# Eliminar columna Confianza de disp en _seccion_radar
old_disp_radar = """        disp = pd.DataFrame({
            "Empresa": df["empresa"],
            "Precio": df["precio"].map(lambda v: f"{v:.2f} €"),
            "RSI": df["rsi"].map(lambda v: f"{v:.0f}"),
            "Confianza (Exp.)": df[f"conf_{hz_key}"].map(lambda v: f"{v:.0f}%"),
            "Proyección": df[f"pct_{hz_key}"].map(lambda v: f"{v:+.2f}%"),
            "Predicción (Exp.)": [f"{_senal_icon(s)} {s}" for s in df[f"senal_{hz_key}"]],
            "Gráfico": [
                f'<a class="ia-eye-link" href="?page=IA&amp;ia_eye={t}&amp;ia_hz={hz_sel}" target="_self" '
                f'title="Ver proyección y diagnóstico de {_html.escape(str(n), quote=True)} a {hz_label}">👁</a>'
                for t, n in zip(df["ticker"], df["empresa"])
            ],
        })

        col_widths_radar = {
            "Empresa": "24%",
            "Precio": "13%",
            "RSI": "10%",
            "Confianza (Exp.)": "13%",
            "Proyección": "14%",
            "Predicción (Exp.)": "16%",
            "Gráfico": "10%",
        }"""

new_disp_radar = """        disp = pd.DataFrame({
            "Empresa": df["empresa"],
            "Precio": df["precio"].map(lambda v: f"{v:.2f} €"),
            "RSI": df["rsi"].map(lambda v: f"{v:.0f}"),
            "Proyección": df[f"pct_{hz_key}"].map(lambda v: f"{v:+.2f}%"),
            "Predicción (Exp.)": [f"{_senal_icon(s)} {s}" for s in df[f"senal_{hz_key}"]],
            "Gráfico": [
                f'<a class="ia-eye-link" href="?page=IA&amp;ia_eye={t}&amp;ia_hz={hz_sel}" target="_self" '
                f'title="Ver diagnóstico de {_html.escape(str(n), quote=True)} a {hz_label}">👁</a>'
                for t, n in zip(df["ticker"], df["empresa"])
            ],
        })

        col_widths_radar = {
            "Empresa": "26%",
            "Precio": "16%",
            "RSI": "12%",
            "Proyección": "18%",
            "Predicción (Exp.)": "18%",
            "Gráfico": "10%",
        }"""

assert old_disp_radar in content, "old_disp_radar not found"
content = content.replace(old_disp_radar, new_disp_radar)

# Actualizar el banner de _seccion_radar con el banner experimental destacado
old_banner_radar = """    # Banner de advertencia institucional: Señales experimentales sin significación demostrada
    st.markdown(
        \"\"\"
        <div style="background: rgba(245, 158, 11, 0.07); border: 1px solid rgba(245, 158, 11, 0.22);
                    border-radius: 8px; padding: 10px 14px; margin: 8px auto 14px auto; font-family: 'Inter', sans-serif;">
            <div style="color: #fbbf24; font-weight: 600; font-size: 0.80rem; display: flex; align-items: center; gap: 6px;">
                <span>⚠️</span> <span>SEÑALES EXPERIMENTALES — SIN PODER PREDICTIVO DEMOSTRADO</span>
            </div>
            <div style="color: #94a3b8; font-size: 0.74rem; line-height: 1.45; margin-top: 3px;">
                Las etiquetas direccionales (ALCISTA / BAJISTA) y sus porcentajes de confianza se muestran exclusivamente con fines de investigación y forward-testing congelado. Los backtests cuantitativos institucionales han verificado que carecen de significación estadística (|t| &lt; 1,0) en acciones líquidas tras costes. No constituyen recomendación ni señal operativa.
            </div>
        </div>
        \"\"\",
        unsafe_allow_html=True,
    )"""

new_banner_radar = """    # Banner de advertencia institucional de Laboratorio Experimental
    st.markdown(
        \"\"\"
        <div style="background: rgba(245, 158, 11, 0.08); border: 1px solid rgba(245, 158, 11, 0.35);
                    border-left: 5px solid #f59e0b; border-radius: 8px; padding: 12px 18px; margin: 10px 0 18px 0; font-family: 'Inter', sans-serif;">
            <div style="color: #fbbf24; font-weight: 700; font-size: 0.84rem; display: flex; align-items: center; gap: 8px;">
                <span>🧪</span> <span>LABORATORIO EXPERIMENTAL — SEÑALES Y RADAR HISTÓRICO</span>
            </div>
            <div style="color: #cbd5e1; font-size: 0.77rem; line-height: 1.5; margin-top: 4px;">
                Esta vista se mantiene exclusivamente con fines de investigación cuantitativa, trazabilidad y forward-testing congelado.
                Como concluye la auditoría institucional, las señales direccionales <b>no son operables</b> debido a la fricción de comisiones, horquillas e impuestos reales (&ge; 20 pb por operación).
                <b>No constituyen recomendación ni asesoramiento financiero.</b>
            </div>
        </div>
        \"\"\",
        unsafe_allow_html=True,
    )"""

assert old_banner_radar in content, "old_banner_radar not found"
content = content.replace(old_banner_radar, new_banner_radar)

# 3. Añadir importaciones para el panel descriptivo
imports_to_add = """from services.descriptive_metrics import (
    cargar_datos_cache_historico,
    calcular_metricas_volatilidad,
    calcular_frecuencias_historicas,
    calcular_extremos_y_drawdown,
    calcular_metricas_liquidez,
    calcular_sensibilidad_benchmark,
    cargar_snapshot_fundamental,
)
"""
content = imports_to_add + content

# 4. Añadir las funciones de renderizado del Panel Descriptivo y Auditoría antes de show()
descriptive_code = '''
# ═══════════════════════════════════════════════════════════════════
# 📊 RENDERIZADO DEL PANEL DESCRIPTIVO DE MERCADO Y RIESGO
# ═══════════════════════════════════════════════════════════════════

def _render_panel_descriptivo(
    closes: pd.DataFrame,
    highs: pd.DataFrame,
    lows: pd.DataFrame,
    vols: pd.DataFrame,
    empresas_dict: Dict[str, str],
    ibex_set: set
) -> None:
    st.markdown("""
    <div style="background: rgba(15, 23, 42, 0.75); border: 1px solid rgba(234, 179, 8, 0.35); border-left: 5px solid #eab308; border-radius: 10px; padding: 14px 20px; margin-bottom: 24px;">
        <div style="font-size: 0.88rem; font-weight: 700; color: #facc15; display: flex; align-items: center; gap: 8px; margin-bottom: 4px;">
            ⚠️ AVISO REGULATORIO: PANEL ESTRICTAMENTE DESCRIPTIVO
        </div>
        <div style="font-size: 0.80rem; color: #cbd5e1; line-height: 1.45;">
            Este panel contiene exclusivamente <b>estadísticas cuantitativas descriptivas, medidas históricas de volatilidad y perfiles de liquidez</b> basados en cotizaciones pasadas cerradas a fin de sesión (<b>corte oficial BME 18:30 CET</b>).<br>
            <b>No contiene recomendaciones de compra o venta, proyecciones direccionales ni pronósticos de precios futuros.</b> Rentabilidades pasadas no garantizan rendimientos futuros.
        </div>
    </div>
    """, unsafe_allow_html=True)

    col_sel1, col_sel2 = st.columns([2.5, 1])
    with col_sel1:
        tickers_disponibles = [t for t in empresas_dict.keys() if t in closes.columns and t != "^IBEX"]
        tickers_disponibles = sorted(tickers_disponibles, key=lambda t: (0 if t in ibex_set else 1, empresas_dict.get(t, t)))
        formato_opcion = lambda t: f"{t} — {empresas_dict.get(t, t)} {'[IBEX 35]' if t in ibex_set else '[Continuo]'}"
        ticker_sel = st.selectbox(
            "Seleccionar activo para análisis:",
            tickers_disponibles,
            format_func=formato_opcion,
            index=0,
            key="ia_desc_ticker_select"
        )
    with col_sel2:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        es_ibex_act = ticker_sel in ibex_set
        badge_cls = "tag-cyan" if es_ibex_act else "tag-amber"
        badge_txt = "CONSTITUYENTE IBEX 35" if es_ibex_act else "MERCADO CONTINUO"
        st.markdown(f"<span class='tag-badge {badge_cls}' style='padding:6px 12px; font-weight:700;'>{badge_txt}</span>", unsafe_allow_html=True)

    if not ticker_sel or ticker_sel not in closes.columns:
        st.warning("No hay datos históricos para el activo seleccionado.")
        return

    p = closes[ticker_sel].dropna()
    v = vols[ticker_sel].reindex(p.index) if ticker_sel in vols.columns else None
    h = highs[ticker_sel].reindex(p.index) if ticker_sel in highs.columns else None
    l = lows[ticker_sel].reindex(p.index) if ticker_sel in lows.columns else None

    if len(p) < 60:
        st.warning("Serie histórica insuficiente para análisis cuantitativo.")
        return

    vol_eur_mat = (closes * vols).rolling(60).mean().iloc[-1] if not vols.empty else None

    m_vol = calcular_metricas_volatilidad(p, volumen=v, highs=h, lows=l)
    m_ext = calcular_extremos_y_drawdown(p, highs=h, lows=l, ventana_dias=252)
    m_liq = calcular_metricas_liquidez(p, volumen=v, volumenes_mercado_medios=vol_eur_mat, ventana_dias=252)
    m_frecs = calcular_frecuencias_historicas(p, ventana_dias=252)

    p_ibex = closes["^IBEX"].dropna() if "^IBEX" in closes.columns else pd.Series(dtype=float)
    m_sens = calcular_sensibilidad_benchmark(p, p_ibex, ventana_corr=60)
    data_fund, fecha_fund = cargar_snapshot_fundamental(ticker_sel)

    # 1. Rango 52s y Drawdown
    st.markdown("<h4 style='font-size: 1.0rem; font-weight: 700; color: #f1f5f9; margin-top: 10px; margin-bottom: 12px;'>1. Rango de 52 Semanas y Drawdown Histórico</h4>", unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Último Cierre Oficial (t)", f"{m_ext['precio_actual']:.3f} €", help=f"Corte oficial de sesión 18:30 CET: {str(p.index[-1])[:10]}")
    with c2:
        dist_max = m_ext['distancia_max_52w_pct']
        st.metric("Distancia al Máximo 52s", f"{dist_max:+.2f}%", delta=f"Pico: {m_ext['max_52w']:.3f} €", delta_color="inverse" if dist_max < -15 else "normal")
    with c3:
        dist_min = m_ext['distancia_min_52w_pct']
        st.metric("Distancia al Mínimo 52s", f"{dist_min:+.2f}%", delta=f"Suelo: {m_ext['min_52w']:.3f} €")
    with c4:
        st.metric("Max Drawdown (252d)", f"{m_ext['max_drawdown_252d_pct']:.2f}%", delta=f"Actual: {m_ext['drawdown_actual_pct']:.2f}%", delta_color="inverse")

    # Barra visual de rango 52s
    pos_rango = m_ext['posicion_rango_52w_pct']
    st.markdown(f"""
    <div style="background: rgba(13, 19, 32, 0.6); padding: 10px 16px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.06); margin-bottom: 20px;">
        <div style="display: flex; justify-content: space-between; font-size: 0.74rem; color: #94a3b8; font-family: monospace;">
            <span>Min 52s: {m_ext['min_52w']:.3f} €</span>
            <span style="color: #00e5ff; font-weight: 700;">Posición en Rango: {pos_rango:.1f}%</span>
            <span>Max 52s: {m_ext['max_52w']:.3f} €</span>
        </div>
        <div style="width: 100%; height: 7px; background: rgba(255,255,255,0.08); border-radius: 999px; overflow: hidden; margin-top: 8px;">
            <div style="width: {pos_rango}%; height: 100%; background: linear-gradient(90deg, #3b82f6 0%, #00e5ff 100%); border-radius: 999px;"></div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # 2. Volatilidad Realizada & Percentil
    st.markdown("<h4 style='font-size: 1.0rem; font-weight: 700; color: #f1f5f9; margin-top: 10px; margin-bottom: 12px;'>2. Volatilidad Realizada Anualizada y Percentiles Propios</h4>", unsafe_allow_html=True)
    v1, v2, v3, v4 = st.columns(4)
    with v1:
        st.metric("Volatilidad 20d", f"{m_vol['vol_20d_anual']:.1f}%", help="Desviación estándar móvil anualizada (20 sesiones) sobre retornos limpios")
    with v2:
        st.metric("Volatilidad 60d", f"{m_vol['vol_60d_anual']:.1f}%", help="Volatilidad trimestral histórica")
    with v3:
        st.metric("Volatilidad 252d", f"{m_vol['vol_252d_anual']:.1f}%", help="Volatilidad anual histórica")
    with v4:
        pct_1y = m_vol['percentil_1y']
        pct_3y = m_vol['percentil_3y']
        pct_txt = f"{pct_1y:.1f}%" if pd.notna(pct_1y) else "N/D"
        st.metric("Percentil vs 1 Año", pct_txt, delta=f"3 Años: {pct_3y:.1f}%" if pd.notna(pct_3y) else None, help="Percentil de volatilidad 20d calculado con datos históricos <= t")

    st.markdown(f"<div style='font-size:0.76rem; color:#94a3b8; margin-top:-6px; margin-bottom:20px;'>Régimen descriptivo: <b style='color:#00e5ff;'>{m_vol['regimen_descriptivo']}</b></div>", unsafe_allow_html=True)

    # 3. Frecuencias Históricas
    st.markdown("<h4 style='font-size: 1.0rem; font-weight: 700; color: #f1f5f9; margin-top: 10px; margin-bottom: 6px;'>3. Frecuencia Histórica de Movimientos Observados (Últimas 252 Sesiones)</h4>", unsafe_allow_html=True)
    st.markdown("""
    <div style="display:inline-block; padding: 5px 12px; background: rgba(99, 102, 241, 0.12); border: 1px dashed rgba(99, 102, 241, 0.4); border-radius: 6px; font-size: 0.74rem; font-weight: 600; color: #a5b4fc; margin-bottom: 12px;">
        Frecuencia histórica observada empíricamente en las últimas 252 sesiones de mercado completas. No constituye pronóstico probabilístico ni proyección futura.
    </div>
    """, unsafe_allow_html=True)

    df_frec = pd.DataFrame(m_frecs)
    html_frec = """
    <div style="width: 100%; overflow-x: auto; border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 8px; margin-bottom: 22px;">
        <table style="width: 100%; border-collapse: collapse; font-size: 0.80rem; text-align: left;">
            <thead>
                <tr style="background: rgba(255, 255, 255, 0.04); color: #94a3b8; font-size: 0.70rem; text-transform: uppercase;">
                    <th style="padding: 9px 12px;">Horizonte (h)</th>
                    <th style="padding: 9px 12px;">Umbral (&plusmn;X%)</th>
                    <th style="padding: 9px 12px;">N Sesiones</th>
                    <th style="padding: 9px 12px;">Frec. Sube (&gt; +X%)</th>
                    <th style="padding: 9px 12px;">Frec. Baja (&lt; -X%)</th>
                    <th style="padding: 9px 12px;">Total (|r| &gt; X%)</th>
                    <th style="padding: 9px 12px;">Etiqueta</th>
                </tr>
            </thead>
            <tbody>
    """
    for _, row in df_frec.iterrows():
        html_frec += f"""
                <tr style="border-bottom: 1px solid rgba(255, 255, 255, 0.04);">
                    <td style="padding: 8px 12px; font-weight:700; color:#00e5ff; font-family: monospace;">{row['horizonte']}</td>
                    <td style="padding: 8px 12px; font-family: monospace;">&plusmn;{row['umbral_pct']:.1f}%</td>
                    <td style="padding: 8px 12px; font-family: monospace;">{row['n_observaciones']} barras</td>
                    <td style="padding: 8px 12px; color: #10b981; font-family: monospace;">{row['frec_sube_pct']:.1f}%</td>
                    <td style="padding: 8px 12px; color: #f43f5e; font-family: monospace;">{row['frec_baja_pct']:.1f}%</td>
                    <td style="padding: 8px 12px; font-weight:700; font-family: monospace;">{row['frec_extremo_total_pct']:.1f}%</td>
                    <td style="padding: 8px 12px;"><span style="background: rgba(0, 229, 255, 0.1); color: #00e5ff; padding: 2px 7px; border-radius: 4px; font-size: 0.70rem;">{row['etiqueta']}</span></td>
                </tr>
        """
    html_frec += """
            </tbody>
        </table>
    </div>
    """
    st.markdown(html_frec, unsafe_allow_html=True)

    # 4. Liquidez y Sensibilidad vs IBEX
    st.markdown("<h4 style='font-size: 1.0rem; font-weight: 700; color: #f1f5f9; margin-top: 10px; margin-bottom: 12px;'>4. Perfil de Liquidez y Sensibilidad frente al IBEX 35</h4>", unsafe_allow_html=True)
    l1, l2, l3, l4 = st.columns(4)
    with l1:
        veur_20 = m_liq['volumen_eur_20d']
        veur_txt = f"{veur_20 / 1e6:.2f} M€" if veur_20 >= 1e6 else f"{veur_20 / 1e3:.1f} k€"
        st.metric("Volumen Medio 20d", veur_txt, delta=f"60d: {m_liq['volumen_eur_60d']/1e6:.2f} M€")
    with l2:
        pct_zeros = m_liq['pct_dias_sin_variacion']
        st.metric("Días sin Variación (252d)", f"{pct_zeros:.2f}%", delta=m_liq['tercil_liquidez'], delta_color="normal" if pct_zeros < 5 else "inverse")
    with l3:
        corr_60 = m_sens.get("correlacion_ibex_60d", np.nan)
        st.metric("Correlación 60d con IBEX", f"{corr_60:.2f}" if pd.notna(corr_60) else "N/D", help="Correlación de Pearson móvil a 60 días con el índice oficial")
    with l4:
        beta_60 = m_sens.get("beta_ibex_60d", np.nan)
        st.metric("Beta Móvil 60d", f"{beta_60:.2f}" if pd.notna(beta_60) else "N/D", help="Sensibilidad respecto al IBEX 35")

    if m_liq['alerta_microestructura_open']:
        st.warning("⚠️ Alerta de microestructura: En el tercil bajo de liquidez, el 19.29% de las sesiones no cruzan apertura real. Los análisis basados en Open se clasifican como NO VERIFICADOS.")

    # 5. Snapshot Fundamental
    st.markdown("<h4 style='font-size: 1.0rem; font-weight: 700; color: #f1f5f9; margin-top: 14px; margin-bottom: 8px;'>5. Ratios y Múltiplos Fundamentales Institucionales</h4>", unsafe_allow_html=True)
    st.markdown(f"<div style='font-size: 0.74rem; color: #94a3b8; margin-bottom: 12px;'>Fecha de snapshot en disco: <b style='color:#00e5ff;'>{fecha_fund}</b> · Fuente: {data_fund.get('_data_source', 'Caché Local')}</div>", unsafe_allow_html=True)
    if data_fund:
        f1, f2, f3, f4, f5, f6 = st.columns(6)
        def _f_val(val, es_pct=False):
            if val is None or pd.isna(val): return "N/D"
            return f"{val*100:.2f}%" if es_pct else f"{val:.2f}"
        with f1: st.metric("PER Trailing", _f_val(data_fund.get('trailingPE')), help=f"Forward: {_f_val(data_fund.get('forwardPE'))}")
        with f2: st.metric("Precio / Libro", _f_val(data_fund.get('priceToBook')))
        with f3: st.metric("EV / EBITDA", _f_val(data_fund.get('enterpriseToEbitda')))
        with f4: st.metric("Div. Yield", _f_val(data_fund.get('dividendYield'), es_pct=True))
        with f5: st.metric("Margen Operativo", _f_val(data_fund.get('operatingMargins'), es_pct=True))
        with f6: st.metric("Deuda / Fondos", _f_val(data_fund.get('debtToEquity')))


# ═══════════════════════════════════════════════════════════════════
# 📑 RENDERIZADO DE AUDITORÍA Y METODOLOGÍA (ADDENDUM)
# ═══════════════════════════════════════════════════════════════════

def _render_auditoria_metodologia() -> None:
    st.markdown("""
    <div style="background: rgba(13, 19, 32, 0.7); border: 1px solid rgba(255,255,255,0.08); border-radius: 10px; padding: 20px; margin-bottom: 20px;">
        <h4 style="color:#00e5ff; font-size:1.0rem; margin-top:0;">Resumen Oficial Auditado (Addendum v3)</h4>
        <ol style="font-size:0.82rem; line-height:1.65; color:#cbd5e1;">
            <li><b>Sin señal direccional operable:</b> El argumento concluyente de descarte radica en que el spread bruto (+5 a +10 pb) es absorbido por los costes de transacción (&ge; 20 pb ida y vuelta más comisiones e ITF). El contraste formal OOS vs Diseño en intradía resulta z = 1.40 (p = 0.1612), estadísticamente inconcluyente por menor potencia muestral, no un colapso estructural.</li>
            <li><b>Retractación de Tabla 3:</b> Se retractan formalmente las tablas previas de componentes ADX por falta de especificación pre-registrada estable. Se verifica que el estadístico t = 3.62 idéntico en diseño para IBEX 35 y Tercil Alto es una coincidencia numérica por redondeo (3.621 vs 3.618) derivada de una correlación diaria entre ambos universos del 95.31%.</li>
            <li><b>Retractación de '1M Invertido':</b> El spread de -2.25% en 1M era un artefacto del agrupamiento pooled que sobreponderaba fechas de crash en valores con rebote posterior (Solaria en feb 2022 e Indra en feb 2025 concentran el top-10). Evaluado de forma transversal (cross-sectional), las medianas muestran que el decil 10 supera al decil 1 (+1.21% vs +0.26%).</li>
            <li><b>Reconciliación de Benchmark:</b> Se retira formalmente la hipótesis de 'cotizaciones indicativas 09:00'. El diferencial entre ^IBEX y constituyentes (+3.8% anual) responde al arrastre de dividendos (Price Index vs Total Return auto_adjust).</li>
            <li><b>Validación de columna Open:</b> En el tercil bajo de liquidez, el 19.29% de las barras tienen Open == Close previo. Todo análisis de apertura en ilíquidos se clasifica como NO VERIFICADO.</li>
            <li><b>Ley 5/2020 (ITF):</b> Texto consolidado del BOE verificado (Art. 5, apartado 3). La base imponible en operaciones del mismo día es el saldo neto de compras (operaciones intradía cerradas en el día exentas). En posiciones mantenidas a vencimiento, la tasa efectiva es de ~15.7 pb.</li>
            <li><b>Líneas base de signo condicionado:</b> El motor pierde sistemáticamente contra P(sube | r &ne; 0) en todos los horizontes (-0.96 pp a -4.56 pp), y en 3 clases no supera a la regla trivial de 'Siempre Lateral'.</li>
            <li><b>Criterio G no cumplido:</b> Ningún modelo de volatilidad cumple las condiciones prefijadas para el Radar de Riesgo (BSS negativo en OOS y pendiente logística alejada de 1.0). ATR presenta el mejor Spearman (+0.19) pero el peor QLIKE (-7.1192) debido a un sesgo de escala masivo del +38.7% (penalización matemática b - 1 - ln b).</li>
        </ol>
    </div>
    """, unsafe_allow_html=True)
'''

# Insertar descriptive_code antes de def precargar_ia
pos_precargar = content.find("def precargar_ia() -> None:")
assert pos_precargar != -1, "def precargar_ia not found"
content = content[:pos_precargar] + descriptive_code + "\n\n" + content[pos_precargar:]

# 5. Modificar show() para:
# - Cargar base data
# - Renderizar _seccion_agente(ctx)
# - Crear las pestañas: "📊 Panel de Análisis", "🔬 Laboratorio (experimental)", "📑 Auditoría y Metodología"
old_show_code = """def show() -> None:
    \"\"\"Punto de entrada principal para la vista de Inteligencia Artificial.\"\"\"
    _css()

    # ── Datos base ───────────────────────────────────────────────────
    empresas = query_all(
        \"SELECT ticker, nombre FROM empresas WHERE activa = 1 ORDER BY nombre\"
    )
    tickers = tuple(e[\"ticker\"] for e in empresas)
    nombres = tuple(e[\"nombre\"] for e in empresas)
    tilts = _cargar_tilts(tickers)

    # ── Radar + tracking (registro y resolución automática) ─────────
    radar = signal_engine.calcular_radar(tickers, nombres, tilts)
    if not st.session_state.get(\"_ia_tracking_sincronizado\"):
        try:
            db_pred.init_predicciones_table()
            if not radar.empty:
                precios = dict(zip(radar[\"ticker\"], radar[\"precio\"]))
                db_pred.resolver_predicciones(precios)
                db_pred.registrar_predicciones(radar)
            st.session_state[\"_ia_tracking_sincronizado\"] = True
        except Exception:
            pass  # el tracking nunca debe romper la vista

    # ── Contexto compartido para el asistente ────────────────────────
    ctx = {
        \"radar\": radar,
        \"tickers\": tickers,
        \"nombre2ticker\": dict(zip(nombres, tickers)),
        \"ticker2nombre\": dict(zip(tickers, nombres)),
        \"fundamentales\": assistant_engine.cargar_fundamentales_disco(tickers),
    }

    # ── Flujo de secciones (sin header de página ni status strip) ────
    _seccion_agente(ctx)
    st.markdown('<div class="ia-divider"></div>', unsafe_allow_html=True)
    _seccion_radar(radar, tickers, nombres, tilts)
    st.markdown('<div class="ia-divider"></div>', unsafe_allow_html=True)
    _seccion_historial(radar=radar)

    # ── Disclaimer compacto ──────────────────────────────────────────
    st.markdown(
        '<div class="ia-disclaimer"><b>⚠️ Aviso:</b> Las señales y proyecciones '
        'son estimaciones probabilísticas. No constituyen recomendación de inversión. '
        'Rentabilidades pasadas no garantizan rendimientos futuros.</div>',
        unsafe_allow_html=True,
    )"""

new_show_code = """def show() -> None:
    \"\"\"Punto de entrada principal para la vista de Inteligencia Artificial.\"\"\"
    _css()

    # ── Datos base ───────────────────────────────────────────────────
    empresas_raw = query_all(
        \"SELECT ticker, nombre, es_ibex35 FROM empresas WHERE activa = 1 ORDER BY nombre\"
    )
    tickers = tuple(e[\"ticker\"] for e in empresas_raw)
    nombres = tuple(e[\"nombre\"] for e in empresas_raw)
    empresas_dict = {e[\"ticker\"]: e[\"nombre\"] for e in empresas_raw}
    ibex_set = set(e[\"ticker\"] for e in empresas_raw if e.get(\"es_ibex35\") == 1)
    tilts = _cargar_tilts(tickers)

    # ── Radar + tracking (registro y resolución automática para el laboratorio) ──
    radar = signal_engine.calcular_radar(tickers, nombres, tilts)
    if not st.session_state.get(\"_ia_tracking_sincronizado\"):
        try:
            db_pred.init_predicciones_table()
            if not radar.empty:
                precios = dict(zip(radar[\"ticker\"], radar[\"precio\"]))
                db_pred.resolver_predicciones(precios)
                db_pred.registrar_predicciones(radar)
            st.session_state[\"_ia_tracking_sincronizado\"] = True
        except Exception:
            pass

    # ── Contexto compartido para el asistente conversacional web ─────
    ctx = {
        \"radar\": radar,
        \"tickers\": tickers,
        \"nombre2ticker\": dict(zip(nombres, tickers)),
        \"ticker2nombre\": dict(zip(tickers, nombres)),
        \"fundamentales\": assistant_engine.cargar_fundamentales_disco(tickers),
    }

    # 1. Agente Conversacional de IA (búsqueda web, prompt bar y FAQ) intacto
    _seccion_agente(ctx)

    st.markdown('<div class="ia-divider"></div>', unsafe_allow_html=True)

    # 2. Pestañas del Área IA: Panel de Análisis, Laboratorio Experimental y Auditoría
    tab_analisis, tab_lab, tab_auditoria = st.tabs([
        \"📊 Panel de Análisis\",
        \"🔬 Laboratorio (experimental)\",
        \"📑 Auditoría y Metodología\"
    ])

    with tab_analisis:
        closes, highs, lows, vols = cargar_datos_cache_historico()
        _render_panel_descriptivo(closes, highs, lows, vols, empresas_dict, ibex_set)

    with tab_lab:
        _seccion_radar(radar, tickers, nombres, tilts)
        st.markdown('<div class="ia-divider"></div>', unsafe_allow_html=True)
        _seccion_historial(radar=radar)

    with tab_auditoria:
        _render_auditoria_metodologia()

    # ── Disclaimer compacto ──────────────────────────────────────────
    st.markdown(
        '<div class="ia-disclaimer"><b>⚠️ Aviso:</b> La información aquí provista tiene carácter estrictamente analítico y cuantitativo. '
        'No constituye asesoramiento financiero ni recomendación de inversión. '
        'Rentabilidades pasadas no garantizan rendimientos futuros.</div>',
        unsafe_allow_html=True,
    )"""

assert old_show_code in content, "old_show_code not found"
content = content.replace(old_show_code, new_show_code)

with open("views/ia.py", "w", encoding="utf-8") as f:
    f.write(content)

print("views/ia.py successfully rebuilt!")
