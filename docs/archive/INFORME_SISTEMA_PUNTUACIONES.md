# 📊 Informe Técnico: Sistema de Puntuaciones StockLab V6

> **Fecha:** 2026-07-02  
> **Archivo fuente:** `views/ranking.py`  
> **Versión del motor:** StockLab V6

---

## 1. Arquitectura General

El sistema reside íntegramente en `views/ranking.py` y se estructura en **3 niveles**:

```
calcular_score_stocklab_v6()          ← Motor principal (orquestador)
├── _calcular_calidad_empresarial()   ← SCORE 1: Calidad (0-100)
└── _calcular_oportunidad_inversion() ← SCORE 2: Oportunidad (0-100)

SCORE FINAL = (Calidad × 0.60) + (Oportunidad × 0.40)
```

Para el **Inspector de Activos**, existen dos funciones espejo de desglose:
- `_desglosar_calidad_detalle()` → devuelve los 4 sub-componentes de Calidad
- `_desglosar_oportunidad_detalle()` → devuelve los 3 sub-componentes de Oportunidad

---

## 2. Fuentes de Datos

| Fuente | Función | Caché | Datos que aporta |
|---|---|---|---|
| **Yahoo Finance API** (vía `yfinance`) | `cargar_todos_los_fundamentales_en_paralelo()` | 12h, disco | ROE, Margen Operativo, Revenue Growth, Debt/Equity, Current Ratio, Interest Coverage, Dividend Yield, Payout Ratio, Market Cap, PER, P/B, EV/EBITDA |
| **Yahoo Finance API** (vía `yfinance`) | `descargar_toda_la_banca_lote()` | 5 min, memoria | Precios de cierre últimos 5 días (para SMA20, RSI14, variación diaria) |
| **Base de datos local** (`empresas`) | `query_all()` | Sin caché | Ticker, Nombre, Sector de empresas activas |
| **Base de datos local** (`watchlist`) | `existe_en_watchlist()` | Sin caché | Estado de watchlist por ticker |

> ⚠️ Todos los datos financieros provienen de **Yahoo Finance**. No hay fuentes alternativas ni APIs de pago. La sesión HTTP usa headers de navegador para evitar bloqueos 401/429.

### Campos descargados por ticker (fundamentales)

```python
{
    'trailingPE':         info.get('trailingPE'),
    'returnOnEquity':     info.get('returnOnEquity'),
    'priceToBook':        info.get('priceToBook'),
    'dividendYield':      info.get('dividendYield'),
    'payoutRatio':        info.get('payoutRatio'),
    'debtToEquity':       info.get('debtToEquity'),
    'currentRatio':       info.get('currentRatio'),
    'interestCoverage':   info.get('interestCoverage'),
    'marketCap':          info.get('marketCap', 0),
    'operatingMargins':   info.get('operatingMargins'),
    'revenueGrowth':      info.get('revenueGrowth'),
    'enterpriseToEbitda': info.get('enterpriseToEbitda')
}
```

---

## 3. 🏆 Ranking CALIDAD EMPRESARIAL (0-100 pts)

Evalúa la **solidez intrínseca** del negocio. Es puramente fundamental, no usa datos de precio.

### Desglose de componentes

| Componente | Pts Máx | Métrica Yahoo | Umbrales de puntuación |
|---|---|---|---|
| **CALIDAD DEL NEGOCIO** | **40** | | |
| ├─ ROE | 10 | `returnOnEquity` | >20%→10 · >15%→8 · >10%→6 · >5%→3 · ≤5%→0 |
| ├─ Margen Operativo | 10 | `operatingMargins` | >20%→10 · >15%→8 · >10%→6 · >5%→3 · ≤5%→0 |
| └─ Crecimiento Ingresos | 20 | `revenueGrowth` | >20%→20 · >10%→15 · >5%→10 · >0%→5 · ≤0%→0 |
| **FORTALEZA FINANCIERA** | **30** | | |
| ├─ Debt/Equity | 10 | `debtToEquity` | <0.5→10 · ≤1.0→8 · ≤2.0→5 · >2.0→0 |
| ├─ Current Ratio | 10 | `currentRatio` | >2.0→10 · ≥1.5→8 · ≥1.0→5 · <1.0→0 |
| └─ Cobertura Intereses | 10 | `interestCoverage` | >10→10 · >5→8 · >2→5 · >0→3 · ≤0→0 |
| **DIVIDENDOS SOSTENIBLES** | **10** | `dividendYield` + `payoutRatio` | Yield>0 & Payout<70%→10 · Yield>0 & Payout<90%→5 · else→0 |
| **VENTAJA COMPETITIVA** | **20** | `marketCap` | Cap>10B→10 · Cap>2B→5 · Cap>5B→+5 historial |

### Notas sobre normalización

La función aplica una normalización defensiva a los valores decimales:
- Si `abs(valor) < 1` → se multiplica ×100 (asumiendo formato decimal, ej: 0.15 = 15%)
- Si `abs(valor) >= 1` → se usa tal cual (ej: 15.0 = 15%)

Esto aplica a: ROE, Margen Operativo, Revenue Growth, Dividend Yield, Payout Ratio.

Para Debt/Equity existe una lógica adicional: si el valor > 1, se divide entre 100 (Yahoo a veces lo devuelve como porcentaje, ej: 85.0 → 0.85).

---

## 4. 🎯 Ranking OPORTUNIDAD DE INVERSIÓN (0-100 pts)

Evalúa si es **buen momento** para invertir. Combina valoración, análisis técnico y sentimiento.

### Desglose de componentes

| Componente | Pts Máx | Métrica | Umbrales de puntuación |
|---|---|---|---|
| **VALORACIÓN FUNDAMENTAL** | **40** | | |
| ├─ PER | 20 | `trailingPE` | 0<PER<12→20 · 12≤PER≤18→15 · PER>18→5 · PER≤0→10 |
| ├─ Price/Book | 10 | `priceToBook` | 0<P/B<1.2→10 · 1.2≤P/B≤2.5→5 · else→0 |
| └─ EV/EBITDA | 10 | `enterpriseToEbitda` | 0<EV<10→10 · 10≤EV≤15→7 · EV>15→3 · else→5 |
| **MOMENTUM TÉCNICO** | **25** | Precios 5d (`yf.download`) | |
| ├─ SMA20 | 15 | Precio vs media móvil 20d | Precio > SMA20 → +15 |
| ├─ RSI 14 | 10 | Cálculo manual RSI | 30≤RSI≤70→+10 · RSI<30→+5 · RSI>70→+0 |
| └─ Variación Diaria | 5 | `(hoy-ayer)/ayer × 100` | Si >0 → min(5, var×2) |
| **MOMENTUM FUNDAMENTAL** | **35** | `revenueGrowth` | Base=30 · Growth>15%→35 · >5%→32 · <0%→25 |

### Fallbacks y valores por defecto

| Situación | Valor asignado |
|---|---|
| Sin datos históricos suficientes (< 20 días) | Momentum técnico = **12.5 pts** (punto medio) |
| Fallo total en cálculo de oportunidad | Score = **50.0 pts** |
| Pérdida en RSI = 0 | Se salta el cálculo de RSI (no divide por cero) |
| Variación diaria negativa | No suma puntos en variación (solo premia subidas) |

### Cálculo del RSI (implementación manual)

```python
delta = precios.diff()
gain = delta.where(delta > 0, 0).rolling(window=14).mean().iloc[-1]
loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean().iloc[-1]
rs = gain / loss
rsi = 100 - (100 / (1 + rs))
```

> ⚠️ Se calcula sobre solo 5 días de datos descargados con ventana de 14. Con tan pocos puntos, el RSI puede ser impreciso.

---

## 5. ⭐ Score StockLab Final

$$\text{Score Final} = (\text{Calidad} \times 0.60) + (\text{Oportunidad} \times 0.40)$$

| Parámetro | Valor |
|---|---|
| Peso Calidad | **60%** |
| Peso Oportunidad | **40%** |
| Rango resultante | 0 – 100 |
| Redondeo | 1 decimal |

### Interpretación de rangos (implícita)

| Rango Score Final | Significado |
|---|---|
| 80 – 100 | Excelente oportunidad en empresa de alta calidad |
| 60 – 79 | Buena empresa con timing aceptable |
| 40 – 59 | Zona neutral, revisar componentes individuales |
| 0 – 39 | Señal débil, precaución |

---

## 6. Flujo de Ejecución Completo

```
show()
 │
 ├── FASE 1: Pre-carga masiva
 │    ├── query_all("SELECT ticker, nombre, sector FROM empresas WHERE activa=1")
 │    ├── descargar_toda_la_banca_lote(tickers)           → precios batch (caché 5min)
 │    └── cargar_todos_los_fundamentales_en_paralelo()     → fundamentales (10 workers, caché 12h)
 │
 ├── FASE 2: Procesamiento en memoria (0 llamadas de red)
 │    └── Para cada empresa:
 │         ├── Extraer precio y variación del batch descargado
 │         ├── calcular_score_stocklab_v6(df_hist, info_fund, var_diaria)
 │         │    ├── _calcular_calidad_empresarial(info)           → 0-100
 │         │    ├── _calcular_oportunidad_inversion(df, info, var) → 0-100
 │         │    └── score_final = calidad×0.6 + oportunidad×0.4
 │         ├── existe_en_watchlist(ticker)
 │         ├── generar_resumen_fundamental_v6(info)
 │         └── Guardar en empresas_data[]
 │
 ├── FASE 3: UI — Tabla de Clasificación
 │    ├── Filtros: Market, Sector, Solo Watchlist, Ordenar por
 │    ├── Cálculo de cambio de posiciones (hoy vs ayer simulado)
 │    ├── KPIs resumen dinámicos
 │    └── Tabla HTML premium (card-row architecture)
 │
 └── FASE 4: UI — Inspector de Activos (6 paneles)
      ├── Panel 1: Puntuaciones Globales (Score, Calidad, Oportunidad)
      ├── Panel 2: Desglose de Ponderación (barras visuales)
      ├── Panel 3: Auditoría Calidad Empresarial (4 sub-barras)
      ├── Panel 4: Auditoría Oportunidad de Inversión (3 sub-barras)
      ├── Panel 5: Ratios Fundamentales (PER, Div Yield, P/B, ROE)
      └── Panel 6: Acciones Rápidas (Watchlist, Gráfico, Compra)
```

---

## 7. Funciones del Sistema

| Función | Tipo | Entrada | Salida | Descripción |
|---|---|---|---|---|
| `cargar_todos_los_fundamentales_en_paralelo()` | `@st.cache_data(ttl=43200, persist="disk")` | `lista_tickers: list` | `dict[ticker, dict]` | Descarga fundamentales en paralelo (10 workers) |
| `descargar_toda_la_banca_lote()` | `@st.cache_data(ttl=300)` | `lista_tickers: list` | `pd.DataFrame` | Descarga precios batch en 1 llamada |
| `_calcular_calidad_empresarial()` | Privada | `info: dict` | `float (0-100)` | Calcula Score de Calidad |
| `_calcular_oportunidad_inversion()` | Privada | `df_historico, info, var_diaria` | `float (0-100)` | Calcula Score de Oportunidad |
| `calcular_score_stocklab_v6()` | Pública | `df_historico, info, var_diaria` | `dict{calidad, oportunidad, score_final}` | Motor principal V6 |
| `_desglosar_calidad_detalle()` | Privada | `info: dict` | `dict{4 componentes}` | Desglose para Inspector |
| `_desglosar_oportunidad_detalle()` | Privada | `df_historico, info, var_diaria` | `dict{3 componentes}` | Desglose para Inspector |
| `generar_resumen_fundamental_v6()` | Pública | `info: dict` | `str` | Resumen legible PER/Yield/P/B/ROE |
| `show()` | Pública | — | — | Renderiza toda la vista de Ranking |

---

## 8. Observaciones y Limitaciones Detectadas

| # | Aspecto | Detalle | Impacto |
|---|---|---|---|
| 1 | **Sin MACD** | El diseño original mencionaba MACD en el Panel 4, pero el código solo usa SMA20 y RSI14 | Momentum técnico incompleto |
| 2 | **RSI con datos insuficientes** | Se calcula con ventana de 14 sobre solo 5 días de datos | RSI puede ser impreciso o siempre fallback |
| 3 | **Ventaja Competitiva = Market Cap** | No evalúa moat real (márgenes sostenidos, patentes, switching costs) | Proxy muy simplificado |
| 4 | **Momentum Fundamental estático** | Solo mira `revenueGrowth` puntual, sin tendencias trimestrales ni guidance | No captura aceleración/desaceleración |
| 5 | **Sin Free Cash Flow ni ROIC** | No se usan métricas de eficiencia de capital más allá de ROE | Visión parcial de la rentabilidad real |
| 6 | **Normalización dual riesgosa** | `if abs(val) < 1 then ×100` puede dar resultados incorrectos en edge cases | Posibles scores inflados o deflateados |
| 7 | **Debt/Equity normalización inversa** | Si >1 se divide /100, asumiendo formato porcentaje de Yahoo | Puede fallar si Yahoo cambia formato |
| 8 | **Variación diaria solo premia subidas** | Las bajadas no restan puntos en momentum técnico | Sesgo alcista en el scoring |
| 9 | **Sin ajuste por sector** | Los umbrales son universales, no se adaptan a sectores (banca vs tech) | Comparación injusta entre industrias |
| 10 | **Cambio de posiciones simulado** | El "ayer" se genera reordenando aleatoriamente, no con datos históricos reales | Indicador de cambio no fiable |

---

## 9. Dependencias Externas

| Librería | Uso |
|---|---|
| `streamlit` | Framework UI, caché, widgets |
| `yfinance` | Datos financieros de Yahoo Finance |
| `pandas` | Manipulación de datos, cálculos técnicos |
| `numpy` | Operaciones numéricas |
| `requests` | Sesión HTTP personalizada para Yahoo |
| `concurrent.futures` | Multithreading para descarga paralela |

---

*Documento generado automáticamente como referencia técnica del sistema StockLab V6.*
