"""
components/stock_chart.py — Motor Gráfico Oficial de StockLab
=============================================================

Componente único y reutilizable basado en Chart.js 4.4.0.
Todas las vistas de StockLab deben utilizar este componente para
renderizar gráficos de precios históricos.

Motor: Chart.js 4.4.0 + chartjs-plugin-zoom 2.0.1 + HammerJS 2.0.8
Renderizado: st.components.v1.html (iframe aislado)

Plugins incluidos:
  - crosshairPlugin:  línea vertical fina + punto dinámico magnético
  - refLinePlugin:    línea horizontal de referencia (último cierre / apertura)
  - measurePlugin:    herramienta drag-to-measure con overlay sombreado

MODIFICACIONES FUTURAS:
  Cualquier cambio visual (colores, grosor, hover, animaciones, zoom)
  debe hacerse ÚNICAMENTE en este archivo.
  Los cambios se propagarán automáticamente a todas las vistas que lo usen.
"""

import json
import streamlit as st
from typing import Optional


def render_stock_chart(
    # ── Datos obligatorios ────────────────────────────────────────────────
    labels: list,               # Etiquetas del eje X (fechas formateadas para hover)
    prices: list,               # Precios de cierre (floats)
    last_close: float,          # Precio de referencia para la línea horizontal
    line_color: str,            # "#10B981" (verde) | "#EF4444" (rojo)

    # ── Ticks del eje X ───────────────────────────────────────────────────
    tick_labels_map: Optional[dict] = None,
    # Dict {hover_label: tick_text_visible} para el eje X.
    # Si es None, se generan automáticamente 5 ticks equidistantes.

    # ── Metadatos ─────────────────────────────────────────────────────────
    currency: str = "EUR",
    ticker: str = "",
    title: str = "",
    main_label: str = "Precio",

    # ── Controles de funcionalidades ─────────────────────────────────────
    show_reference_line: bool = True,   # Línea horizontal de referencia
    show_measure_tool: bool = True,     # Herramienta drag-to-measure
    show_zoom: bool = True,             # Zoom con rueda + Ctrl+pan

    # ── Tamaño ────────────────────────────────────────────────────────────
    height: int = 445,
    y_padding_ratio: float = 0.003, # Ratio de padding Y (default 0.003; 0.10 para patrimonio)

    # ── Series adicionales (opcional) ─────────────────────────────────────
    extra_series: Optional[list] = None,
    # Lista de dicts para dibujar líneas auxiliares sobre la principal:
    #   {
    #     "label":      str                — nombre en leyenda/tooltip
    #     "data":       list[float|None]   — misma longitud que labels
    #                                        (None = hueco sin dibujar)
    #     "color":      "#f59e0b"          — color de la línea
    #     "dash":       [6, 4]             — patrón de trazo (opcional)
    #     "width":      1.2                — grosor (opcional)
    #     "fill_to":    "-1"               — rellenar hasta la serie previa
    #                                        (bandas tipo cono; opcional)
    #     "fill_color": "rgba(0,229,255,0.07)" — color del relleno (opcional)
    #   }
    # Con series extra se muestra la leyenda y el tooltip incluye el nombre.
) -> None:
    """
    Renderiza el gráfico oficial de StockLab usando Chart.js.

    Este es el único componente gráfico de precios en toda la aplicación.
    Proporciona crosshair magnético, punto dinámico, tooltip premium,
    línea de referencia, herramienta de medición y zoom interactivo,
    exactamente igual al gráfico del IBEX 35 del Dashboard.

    Parameters
    ----------
    labels : list[str]
        Lista de etiquetas de fecha formateadas para el tooltip y el eje X.
        Ejemplo: ["01 Jun, 09:00", "01 Jun, 09:05", ...]
    prices : list[float]
        Lista de precios de cierre, misma longitud que labels.
    last_close : float
        Precio de referencia para la línea horizontal discontinua.
        Normalmente el primer precio del periodo (apertura o primer cierre).
    line_color : str
        Color hexadecimal de la línea y el punto. "#10B981" para verde,
        "#EF4444" para rojo.
    tick_labels_map : dict, optional
        Mapeo {hover_label: texto_tick_visible} para el eje X.
        Si None, se generan automáticamente 5 ticks equidistantes.
    currency : str
        Moneda para el tooltip (default "EUR").
    ticker : str
        Ticker del activo (informativo, puede usarse en futuras extensiones).
    title : str
        Título del gráfico (informativo).
    show_reference_line : bool
        Si True, dibuja la línea horizontal de referencia.
    show_measure_tool : bool
        Si True, activa la herramienta drag-to-measure.
    show_zoom : bool
        Si True, activa zoom con rueda del ratón y Ctrl+pan.
    height : int
        Altura del componente en píxeles (default 445).
    y_padding_ratio : float
        Ratio de margen vertical para el eje Y (default 0.003).
    """

    # ── Validación de datos ───────────────────────────────────────────────
    if not labels or not prices or len(labels) != len(prices):
        st.info("No hay datos suficientes para mostrar el gráfico.")
        return

    # ── Tick labels automáticos ───────────────────────────────────────────
    if tick_labels_map is None:
        n_ticks = 5
        total = len(labels)
        if total > 1:
            indices = [int(i * (total - 1) / (n_ticks - 1)) for i in range(n_ticks)]
        else:
            indices = [0]
        indices = sorted(set(indices))
        tick_labels_map = {labels[i]: labels[i] for i in indices}

    # ── Valores derivados ─────────────────────────────────────────────────
    # El rango Y contempla la serie principal y todas las auxiliares
    _vals = [float(p) for p in prices if p is not None]
    for _s in (extra_series or []):
        _vals += [float(v) for v in _s.get("data", []) if v is not None]
    ymin = min(_vals)
    ymax = max(_vals)
    y_pad = (ymax - ymin) * y_padding_ratio if ymax != ymin else max(abs(ymin) * y_padding_ratio, 0.01)
    y_min_chart = ymin - y_pad
    y_max_chart = ymax + y_pad

    _lc_up      = line_color.upper()
    is_green    = _lc_up == "#10B981"
    is_cyan     = _lc_up in ("#38BDF8", "#0EA5E9", "#06B6D4")
    fill_color  = (
        "rgba(16, 185, 129, 0.05)" if is_green
        else ("rgba(56, 189, 248, 0.05)" if is_cyan else "rgba(239, 68, 68, 0.05)")
    )
    main_label_js = json.dumps(main_label or "Precio")

    # ── Serialización JS ──────────────────────────────────────────────────
    labels_js       = json.dumps(labels)
    values_js       = json.dumps([float(v) if v is not None else None for v in prices])
    extra_series_js = json.dumps([
        {
            "label":     s.get("label", f"Serie {i + 1}"),
            "data":      [float(v) if v is not None else None for v in s.get("data", [])],
            "color":     s.get("color", "#94a3b8"),
            "dash":      s.get("dash", []),
            "width":     float(s.get("width", 1.2)),
            "fillTo":    s.get("fill_to"),
            "fillColor": s.get("fill_color", "rgba(0, 0, 0, 0)"),
        }
        for i, s in enumerate(extra_series or [])
    ])
    tick_labels_js  = json.dumps(tick_labels_map)
    currency_js     = json.dumps(currency)

    # Flags JS para funcionalidades opcionales
    js_show_ref     = "true"  if show_reference_line else "false"
    js_show_measure = "true"  if show_measure_tool   else "false"

    # Scripts de zoom (solo si están activos)
    zoom_scripts = """
<script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
""" if show_zoom else ""

    zoom_plugin_config = """
      zoom: {
        zoom: {
          wheel: { enabled: true, speed: 0.08 },
          pinch: { enabled: true },
          mode: 'x',
        },
        pan: {
          enabled: true,
          mode: 'x',
          modifierKey: 'ctrl',
        },
        limits: {
          x: { minRange: 5 },
        },
      },
""" if show_zoom else ""

    # ── HTML del componente ───────────────────────────────────────────────
    chart_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: transparent; overflow: hidden; }}
  #chart-container {{
    position: relative;
    width: 100%;
    height: {height}px;
    padding: 0 4px;
  }}
  canvas {{
    display: block;
    user-select: none;
    -webkit-user-select: none;
  }}
</style>
</head>
<body>
<div id="chart-container">
  <canvas id="ibexChart"></canvas>
</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
{zoom_scripts}
<script>
const labels       = {labels_js};
const values       = {values_js};
const lineColor    = "{line_color}";
const fillColorBase= "{fill_color}";
const lastClose    = {last_close};
const yMin         = {y_min_chart};
const yMax         = {y_max_chart};
const tickLabelsMap= {tick_labels_js};
const currency     = {currency_js};
const SHOW_REF_LINE    = {js_show_ref};
const SHOW_MEASURE_TOOL= {js_show_measure};

// Decimales adaptativos según la magnitud del precio (penny stocks: 0.0021 → 4 dec)
const maxPrice = values.length ? Math.max(...values.filter(v => v != null)) : 1;
const numDecimals = maxPrice < 1.0 ? 4 : 2;

// ── Variables de estado para drag-to-measure ──────────────────────────────
let dragStartIndex = null;
let dragEndIndex   = null;
let isDragging     = false;
let isMouseDown    = false;

// ── Plugin 1: Crosshair — línea vertical fina + punto dinámico magnético ─
const crosshairPlugin = {{
  id: 'crosshair',
  afterDraw(chart) {{
    if (dragStartIndex !== null && dragEndIndex !== null) return;
    if (chart._crosshairX === undefined || chart._crosshairX === null) return;
    const ctx = chart.ctx;
    const chartArea = chart.chartArea;
    const x = chart._crosshairX;
    const y = chart._crosshairY;

    ctx.save();

    // Línea vertical sutil (1px, dashed, semitransparente)
    ctx.beginPath();
    ctx.strokeStyle = 'rgba(148, 163, 184, 0.28)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.moveTo(x, chartArea.top);
    ctx.lineTo(x, chartArea.bottom);
    ctx.stroke();

    // Punto (círculo) con halo neón sutil en el dato más cercano
    if (y !== null) {{
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.arc(x, y, 8.5, 0, Math.PI * 2);
      ctx.fillStyle = lineColor + '2e';
      ctx.fill();
      ctx.beginPath();
      ctx.arc(x, y, 4.5, 0, Math.PI * 2);
      ctx.fillStyle = lineColor;
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = '#f8fafc';
      ctx.stroke();
    }}

    ctx.restore();
  }}
}};

// ── Plugin 2: RefLine — línea horizontal de referencia ────────────────────
const refLinePlugin = {{
  id: 'refLine',
  afterDraw(chart) {{
    if (!SHOW_REF_LINE) return;
    const ctx = chart.ctx;
    const chartArea = chart.chartArea;
    const yScale = chart.scales.y;
    const yPixel = yScale.getPixelForValue(lastClose);

    if (yPixel < chartArea.top || yPixel > chartArea.bottom) return;

    ctx.save();
    ctx.beginPath();
    ctx.strokeStyle = 'rgba(148, 163, 184, 0.26)';
    ctx.lineWidth = 1;
    ctx.setLineDash([5, 4]);
    ctx.moveTo(chartArea.left, yPixel);
    ctx.lineTo(chartArea.right, yPixel);
    ctx.stroke();
    ctx.restore();
  }}
}};

// ── Plugin 3: Measure — herramienta drag-to-measure ──────────────────────
const measurePlugin = {{
  id: 'measure',
  afterDraw(chart) {{
    if (!SHOW_MEASURE_TOOL) return;
    if (dragStartIndex === null || dragEndIndex === null) return;
    const ctx = chart.ctx;
    const chartArea = chart.chartArea;
    const meta = chart.getDatasetMeta(0);

    if (dragStartIndex >= meta.data.length || dragEndIndex >= meta.data.length) return;

    const startPt = meta.data[dragStartIndex];
    const endPt   = meta.data[dragEndIndex];
    if (!startPt || !endPt) return;

    const xStart = startPt.x;
    const xEnd   = endPt.x;
    const yStart = startPt.y;
    const yEnd   = endPt.y;

    ctx.save();

    // 1. Sombreado de fondo entre las dos líneas
    ctx.beginPath();
    ctx.rect(chartArea.left, chartArea.top, chartArea.right - chartArea.left, chartArea.bottom - chartArea.top);
    ctx.clip();

    ctx.fillStyle = 'rgba(56, 189, 248, 0.07)';
    ctx.fillRect(Math.min(xStart, xEnd), chartArea.top, Math.abs(xEnd - xStart), chartArea.bottom - chartArea.top);

    // 2. Líneas verticales discontinuas
    ctx.beginPath();
    ctx.strokeStyle = 'rgba(148, 163, 184, 0.42)';
    ctx.lineWidth = 1.1;
    ctx.setLineDash([4, 4]);
    ctx.moveTo(xStart, chartArea.top);
    ctx.lineTo(xStart, chartArea.bottom);
    ctx.moveTo(xEnd, chartArea.top);
    ctx.lineTo(xEnd, chartArea.bottom);
    ctx.stroke();

    // 3. Puntos de intersección
    ctx.setLineDash([]);
    [{{ x: xStart, y: yStart }}, {{ x: xEnd, y: yEnd }}].forEach(pt => {{
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, 5, 0, Math.PI * 2);
      ctx.fillStyle = lineColor;
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = '#ffffff';
      ctx.stroke();
    }});

    ctx.restore(); // Quitar clipping para la cajita

    // 4. Calcular métricas diferenciales
    const idxA  = Math.min(dragStartIndex, dragEndIndex);
    const idxB  = Math.max(dragStartIndex, dragEndIndex);
    const valA  = values[idxA];
    const valB  = values[idxB];
    const dateA = labels[idxA];
    const dateB = labels[idxB];

    const diffVal = valB - valA;
    const diffPct = (diffVal / valA) * 100;
    const sign    = diffVal >= 0 ? '+' : '';
    const colorText = diffVal >= 0 ? '#34d399' : '#f87171';

    const labelText = "Rango: ";
    const valText   = `${{sign}}${{diffVal.toLocaleString('es-ES', {{minimumFractionDigits: numDecimals, maximumFractionDigits: numDecimals}})}} ${{currency}}`;
    const pctText   = ` (${{sign}}${{diffPct.toFixed(2)}}%)`;
    const dateText  = `  ${{dateA}} — ${{dateB}}`;

    ctx.save();
    ctx.font = '500 12px Inter, sans-serif';
    const textWidth = ctx.measureText(labelText + valText + pctText + dateText).width;
    const boxWidth  = textWidth + 20;
    const boxHeight = 26;

    // Centrar la caja horizontalmente entre xStart y xEnd
    let boxX = Math.min(xStart, xEnd) + Math.abs(xEnd - xStart) / 2 - boxWidth / 2;
    if (boxX < chartArea.left  + 4) boxX = chartArea.left  + 4;
    if (boxX + boxWidth > chartArea.right - 4) boxX = chartArea.right - 4 - boxWidth;
    const boxY = chartArea.top + 10;

    // Dibujar cajita
    ctx.beginPath();
    if (typeof ctx.roundRect === 'function') {{
      ctx.roundRect(boxX, boxY, boxWidth, boxHeight, 8);
    }} else {{
      ctx.rect(boxX, boxY, boxWidth, boxHeight);
    }}
    ctx.fillStyle = 'rgba(17, 23, 35, 0.94)';
    ctx.fill();
    ctx.strokeStyle = 'rgba(148, 163, 184, 0.20)';
    ctx.lineWidth = 1;
    ctx.stroke();

    // Textos
    ctx.textBaseline = 'middle';
    let currentX = boxX + 10;
    const centerY = boxY + boxHeight / 2;

    ctx.fillStyle = '#64748b';
    ctx.font = '500 12px Inter, sans-serif';
    ctx.fillText(labelText, currentX, centerY);
    currentX += ctx.measureText(labelText).width;

    ctx.fillStyle = colorText;
    ctx.font = '700 12px JetBrains Mono, monospace';
    ctx.fillText(valText, currentX, centerY);
    currentX += ctx.measureText(valText).width;

    ctx.fillStyle = colorText;
    ctx.font = '700 12px JetBrains Mono, monospace';
    ctx.fillText(pctText, currentX, centerY);
    currentX += ctx.measureText(pctText).width;

    ctx.fillStyle = '#cbd5e1';
    ctx.font = '500 12px Inter, sans-serif';
    ctx.fillText(dateText, currentX, centerY);

    ctx.restore();
  }}
}};

Chart.register(crosshairPlugin, refLinePlugin, measurePlugin);

// Posicionador de tooltip fijo en la parte superior
Chart.Tooltip.positioners.customTop = function(elements, eventPosition) {{
  if (!elements.length) return false;
  const chart = this.chart;
  const chartArea = chart.chartArea;
  return {{
    x: elements[0].element.x,
    y: chartArea.top + 10,
  }};
}};

const canvasEl = document.getElementById('ibexChart');
const ctx      = canvasEl.getContext('2d');

// Gradiente de relleno multicapa suave (estilo editorial Obsidian Slate)
const gradient = ctx.createLinearGradient(0, 0, 0, {height});
const lcUp     = (lineColor || '').toUpperCase();
if (lcUp === '#10B981') {{
  gradient.addColorStop(0,    'rgba(16, 185, 129, 0.22)');
  gradient.addColorStop(0.30, 'rgba(16, 185, 129, 0.12)');
  gradient.addColorStop(0.65, 'rgba(16, 185, 129, 0.04)');
  gradient.addColorStop(1,    'rgba(16, 185, 129, 0.00)');
}} else if (lcUp === '#38BDF8' || lcUp === '#0EA5E9' || lcUp === '#06B6D4') {{
  gradient.addColorStop(0,    'rgba(56, 189, 248, 0.22)');
  gradient.addColorStop(0.30, 'rgba(56, 189, 248, 0.11)');
  gradient.addColorStop(0.65, 'rgba(56, 189, 248, 0.04)');
  gradient.addColorStop(1,    'rgba(56, 189, 248, 0.00)');
}} else {{
  gradient.addColorStop(0,    'rgba(239, 68, 68, 0.20)');
  gradient.addColorStop(0.30, 'rgba(239, 68, 68, 0.11)');
  gradient.addColorStop(0.65, 'rgba(239, 68, 68, 0.03)');
  gradient.addColorStop(1,    'rgba(239, 68, 68, 0.00)');
}}

// Filtrar las etiquetas del eje X: solo mostrar las del mapa de ticks
const tickSet = new Set(Object.keys(tickLabelsMap));

// ── Datasets: serie principal + auxiliares opcionales ─────────────────────
const extraSeries = {extra_series_js};
const mainDataset = {{
  data: values,
  borderColor: lineColor,
  borderWidth: 1.75,
  fill: true,
  backgroundColor: gradient,
  pointRadius: 0,
  pointHoverRadius: 0,
  tension: 0,
}};
if (extraSeries.length > 0) {{ mainDataset.label = {main_label_js}; }}
const extraDatasets = extraSeries.map(s => ({{
  label: s.label,
  data: s.data,
  borderColor: s.color,
  borderWidth: s.width,
  borderDash: s.dash,
  fill: s.fillTo || false,
  backgroundColor: s.fillColor,
  pointRadius: 0,
  pointHoverRadius: 0,
  tension: 0,
}}));

const chart = new Chart(ctx, {{
  type: 'line',
  data: {{
    labels: labels,
    datasets: [mainDataset, ...extraDatasets]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    animation: {{ duration: 400, easing: 'easeInOutQuart' }},
    interaction: {{
      mode: 'index',
      intersect: false,
    }},
    plugins: {{
      legend: {{
        display: extraSeries.length > 0,
        position: 'top',
        align: 'end',
        labels: {{
          color: '#94a3b8',
          font: {{ family: 'Inter, sans-serif', size: 10, weight: '500' }},
          boxWidth: 14,
          boxHeight: 2,
          padding: 10,
        }},
      }},
      tooltip: {{
        enabled: (ctx) => !(dragStartIndex !== null && dragEndIndex !== null),
        position: 'customTop',
        yAlign: 'top',
        backgroundColor: 'rgba(13, 20, 34, 0.96)',
        borderColor: 'rgba(56, 189, 248, 0.30)',
        borderWidth: 1,
        cornerRadius: 8,
        callbacks: {{
          title: (items) => items[0].label,
          label: (item) => {{
            const val = item.parsed.y;
            if (val === null || val === undefined) return null;
            const txt = val.toLocaleString('es-ES', {{minimumFractionDigits: numDecimals, maximumFractionDigits: numDecimals}}) + ' ' + currency;
            return item.dataset.label ? item.dataset.label + ': ' + txt : txt;
          }},
        }},
        titleColor: '#94a3b8',
        titleFont: {{ family: 'Inter, sans-serif', size: 11, weight: '500' }},
        bodyColor: '#f8fafc',
        bodyFont: {{ family: 'JetBrains Mono, monospace', size: 13.5, weight: '700' }},
        padding: 10,
        caretSize: 0,
        displayColors: extraSeries.length > 0,
        boxPadding: 4,
      }},
      {zoom_plugin_config}
    }},
    scales: {{
      x: {{
        grid: {{ display: false, drawBorder: false }},
        border: {{ display: false }},
        ticks: {{
          color: '#78879c',
          font: {{ family: 'Inter, sans-serif', size: 11, weight: '500' }},
          maxRotation: 0,
          autoSkip: false,
          callback: function(value, index) {{
            const lbl = this.getLabelForValue(value);
            return tickSet.has(lbl) ? tickLabelsMap[lbl] : null;
          }}
        }},
      }},
      y: {{
        min: yMin,
        max: yMax,
        position: 'left',
        grid: {{
          color: 'rgba(148, 163, 184, 0.06)',
          drawBorder: false,
        }},
        border: {{ display: false, dash: [2, 2] }},
        ticks: {{
          color: '#78879c',
          font: {{ family: 'Inter, sans-serif', size: 11, weight: '500' }},
          count: 5,
          callback: (v) => v.toLocaleString('es-ES', {{minimumFractionDigits: numDecimals, maximumFractionDigits: numDecimals}}),
        }},
      }},
    }},
  }},
}});

// ── Event listeners del canvas ────────────────────────────────────────────

// Double-click: resetear zoom (solo si el plugin está activo) y limpiar selección de rango
canvasEl.addEventListener('dblclick', function() {{
  if (typeof chart.resetZoom === 'function') {{
    chart.resetZoom();
  }}
  dragStartIndex = null;
  dragEndIndex   = null;
  chart.draw();
}});

canvasEl.addEventListener('mousedown', function(e) {{
  if (!SHOW_MEASURE_TOOL) return;
  if (e.button !== 0) return;

  const rect  = canvasEl.getBoundingClientRect();
  const mouseX = e.clientX - rect.left;
  const chartArea = chart.chartArea;

  if (mouseX < chartArea.left || mouseX > chartArea.right) return;

  e.preventDefault();

  isMouseDown = true;

  const meta = chart.getDatasetMeta(0);
  let closestIdx = -1;
  let minDist    = Infinity;
  for (let i = 0; i < meta.data.length; i++) {{
    const pt = meta.data[i];
    const d  = Math.abs(pt.x - mouseX);
    if (d < minDist) {{
      minDist    = d;
      closestIdx = i;
    }}
  }}

  if (closestIdx !== -1) {{
    dragStartIndex = closestIdx;
    dragEndIndex   = null;
    isDragging     = true;
    chart.draw();
  }}
}});

canvasEl.addEventListener('mousemove', function(e) {{
  const rect   = canvasEl.getBoundingClientRect();
  const mouseX = e.clientX - rect.left;
  const chartArea = chart.chartArea;

  if (mouseX < chartArea.left || mouseX > chartArea.right) {{
    chart._crosshairX = null;
    chart._crosshairY = null;
    chart.draw();
    return;
  }}

  // Búsqueda magnética: encuentra el punto más cercano en X (snapping)
  const meta = chart.getDatasetMeta(0);
  let closestPoint = null;
  let closestIdx   = -1;
  let minDist      = Infinity;
  for (let i = 0; i < meta.data.length; i++) {{
    const pt = meta.data[i];
    const d  = Math.abs(pt.x - mouseX);
    if (d < minDist) {{
      minDist      = d;
      closestPoint = pt;
      closestIdx   = i;
    }}
  }}

  if (closestPoint) {{
    chart._crosshairX = closestPoint.x;
    chart._crosshairY = closestPoint.y;
  }}

  if (SHOW_MEASURE_TOOL && isMouseDown && isDragging && closestIdx !== -1) {{
    dragEndIndex = closestIdx;
  }}

  chart.draw();
}});

window.addEventListener('mouseup', function(e) {{
  if (isMouseDown) {{
    isMouseDown    = false;
    isDragging     = false;
    dragStartIndex = null;
    dragEndIndex   = null;
    chart.draw();
  }}
}});

canvasEl.addEventListener('mouseleave', function() {{
  chart._crosshairX = null;
  chart._crosshairY = null;
  chart.draw();
}});
</script>
</body>
</html>"""

    st.iframe(chart_html, height=height + 5)
