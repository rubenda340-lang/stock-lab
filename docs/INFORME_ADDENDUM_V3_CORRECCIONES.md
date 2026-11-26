# Addendum Metodológico y Correcciones Cuantitativas (v3)

**Proyecto:** Stock-Lab — Auditoría Institucional de Señales y Modelos de Volatilidad  
**Fecha:** Octubre 2026  
**Estatus:** Documento de rectificación formal y consolidación de números reales auditados  
**Archivos de datos asociados:**
- [`data/addendum_v3_p1_oos_contrastes.csv`](file:///d:/Escritorio/stock-lab/data/addendum_v3_p1_oos_contrastes.csv)
- [`data/addendum_v3_p3_deciles_1m_pooled.csv`](file:///d:/Escritorio/stock-lab/data/addendum_v3_p3_deciles_1m_pooled.csv)
- [`data/addendum_v3_p3_deciles_1m_crosssectional.csv`](file:///d:/Escritorio/stock-lab/data/addendum_v3_p3_deciles_1m_crosssectional.csv)
- [`data/addendum_v3_p3_top10_decil1_1m.csv`](file:///d:/Escritorio/stock-lab/data/addendum_v3_p3_top10_decil1_1m.csv)
- [`data/addendum_v3_p4_benchmark_reconciliacion.csv`](file:///d:/Escritorio/stock-lab/data/addendum_v3_p4_benchmark_reconciliacion.csv)
- [`data/addendum_v3_p7_baselines.csv`](file:///d:/Escritorio/stock-lab/data/addendum_v3_p7_baselines.csv)
- [`data/addendum_v3_p8_qlike_bootstrap.csv`](file:///d:/Escritorio/stock-lab/data/addendum_v3_p8_qlike_bootstrap.csv)
- [`data/addendum_v3_pG1_volatilidad_within.csv`](file:///d:/Escritorio/stock-lab/data/addendum_v3_pG1_volatilidad_within.csv)
- [`data/addendum_v3_pG2_cobertura.csv`](file:///d:/Escritorio/stock-lab/data/addendum_v3_pG2_cobertura.csv)
- [`data/addendum_v3_pG3_brier_calibracion.csv`](file:///d:/Escritorio/stock-lab/data/addendum_v3_pG3_brier_calibracion.csv)

---

## Estatus Previo Aceptado
1. **"Sin señal direccional operable":** El argumento concluyente para descartar la operativa de las señales direccionales no es un colapso econométrico fuera de muestra (OOS), sino la **incapacidad del spread bruto (+5 a +10 pb) para absorber los costes reales de fricción** ($\ge 20\text{ pb}$ por ida y vuelta más corretajes e ITF).
2. **"Criterio G no cumplido tal como se fijó":** Ningún modelo de volatilidad cumple conjuntamente las condiciones prefijadas de $BSS > 0$ con límite inferior del $IC_{95\%} > 0$ y pendiente de calibración logística en $[0.90, 1.10]$.
3. **Restricción de alcance:** [`services/signal_engine.py`](file:///d:/Escritorio/stock-lab/services/signal_engine.py) queda archivado sin modificaciones. No se ejecutan variantes nuevas ni se exploran modelos fuera de los auditados.

---

## 1. Evaluación OOS del Score del Motor: Inconcluyente vs Colapso

En informes anteriores se empleó el término *«colapsa fuera de muestra»* para describir la caída del estadístico $t$ del score intradía en el IBEX 35 ($t = 3.62 \to t = 0.87$). Dicha lectura se rectifica formalmente.

El descenso del estadístico $t$ responde principalmente a la reducción del tamaño muestral (345 fechas OOS frente a 895 fechas en diseño), lo que incrementa el error estándar ($SE$) en aproximadamente un $40\%$. Al contrastar la hipótesis nula de igualdad de coeficientes ($\overline{IC}_{\text{diseño}} = \overline{IC}_{\text{OOS}}$) mediante el contraste $z = (\overline{IC}_{\text{diseño}} - \overline{IC}_{\text{OOS}}) / \sqrt{SE_{\text{diseño}}^2 + SE_{\text{OOS}}^2}$:

| Universo | Tramo | $IC_{\text{diseño}}$ ($SE$) | $t_{\text{diseño}}$ ($N$) | $IC_{\text{OOS}}$ ($SE$) | $t_{\text{OOS}}$ ($N$) | Diferencia | $SE_{\text{diff}}$ | $z_{\text{contraste}}$ | $p$-valor | Lectura Correcta |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **IBEX 35** | **Intradía** ($O_{t+1} \to C_{t+1}$) | **+0.0316** (0.0087) | 3.62 (895) | **+0.0106** (0.0122) | 0.87 (345) | +0.0210 | 0.0150 | **+1.40** | **0.1612** | **Inconcluyente** (no significativo) |
| **Tercil Alto** | **Intradía** ($O_{t+1} \to C_{t+1}$) | **+0.0301** (0.0083) | 3.62 (895) | **+0.0037** (0.0113) | 0.33 (345) | +0.0264 | 0.0140 | **+1.88** | **0.0601** | **Inconcluyente** ($p > 0.05$) |
| **IBEX 35** | **Overnight** ($C_t \to O_{t+1}$) | **-0.0729** (0.0082) | -8.87 (895) | **-0.0110** (0.0138) | -0.80 (345) | -0.0619 | 0.0160 | **-3.86** | **0.0001** | **Diferencia Significativa** |
| **Tercil Alto** | **Overnight** ($C_t \to O_{t+1}$) | **-0.0682** (0.0078) | -8.75 (895) | **+0.0020** (0.0129) | 0.15 (345) | -0.0702 | 0.0151 | **-4.66** | **0.000003** | **Diferencia Significativa** |

### Conclusiones del Punto 1:
- **Intradía:** La diferencia entre diseño y OOS **no es estadísticamente significativa** ($p = 0.16$ en IBEX y $p = 0.06$ en Tercil Alto). No se puede afirmar colapso; la evidencia OOS es simplemente **inconcluyente por potencia limitada**.
- **Overnight:** La señal overnight sí experimenta un cambio estructural significativo ($p < 0.001$), disipándose el sesgo fuertemente negativo previo.
- **Veredicto económico:** El descarte de la señal no descansa en un colapso OOS, sino en que el spread intradía bruto (+5 a +10 pb) es insuficiente frente a unos costes mínimos de ejecución de 20 pb por ida y vuelta.

---

## 2. Retractación de la Tabla 3 y Verificación de $t = 3.62$

### 2.1 Causa de la Divergencia entre Versiones de Tabla 3
En la versión 1 ([`tools/diagnostico_fase3a_bis.py`](file:///d:/Escritorio/stock-lab/tools/diagnostico_fase3a_bis.py)), los ICs y $t$-stats de los componentes técnicos condicionados por ADX arrojaron valores que cambiaron radicalmente en la versión 2 ([`tools/diagnostico_fase3a_bis_v2.py`](file:///d:/Escritorio/stock-lab/tools/diagnostico_fase3a_bis_v2.py)):
1. **Cambio de definición de regímenes:** La v1 utilizó el campo `regimen_adx` precalculado del panel, mientras que la v2 recalculó `regimen_adx_pactado` con cortes fijos directos (<20, 20–25, >25).
2. **Filtro de observaciones por fecha:** La v1 requería un mínimo de 8 tickers con señal por fecha (`len(v) < 8`), mientras que la v2 relajó el corte a 6 (`min_obs = 6`). Esto alteró el conjunto de fechas válidas de las series de Newey-West.
3. **Retractación formal:** Al no responder a una especificación pre-registrada estable, **la Tabla 3 queda formalmente retractada**. Se concluye que las aparentes significaciones estadísticas respondían a variaciones en la partición muestral y ruido transversal.

### 2.2 Verificación de la Coincidencia Numérica $t = 3.62$
Se auditó por qué el estadístico $t$ intradía en diseño resultó idéntico a dos decimales ($3.62$) para el IBEX 35 y para el Tercil Alto de liquidez:
- **IBEX 35:** $\overline{IC} = 0.031637,\; SE = 0.008736 \implies t = \mathbf{3.621298}$
- **Tercil Alto (38 tickers, 58.368 barras / 1.536 fechas):** $\overline{IC} = 0.030098,\; SE = 0.008318 \implies t = \mathbf{3.618470}$

**Explicación:** No se trata de un error de duplicación de variables. Los componentes del Tercil Alto presentan una correlación transversal diaria del **$95.31\%$** con el IBEX 35. Al evaluarse sobre los 38 valores del tercil alto, el IC medio desciende ligeramente ($-0.0015$), pero el error estándar se contrae en la misma proporción ($-0.00042$), arrojando ratios $3.621$ y $3.618$ que redondean idénticamente a $3.62$.

---

## 3. Auditoría de Outliers ($|r| > 25\%$), Deciles 1M y Retractación de "1M Invertido"

### 3.1 Presencia de Outliers en Análisis Anteriores
Se detectaron **78 observaciones diarias** en la base de datos con $|r_{\text{diario}}| > 25\%$ (repartidas en valores ilíquidos, contrasplits no ajustados y eventos corporativos extremos):
- **Deciles 1M (v2):** **SÍ incluyó** los outliers (84 barras con salto en ventana de 20 días).
- **Coberturas (G.2 v2):** **SÍ incluyó** los outliers.
- **Volatilidad within-ticker (G.1 v2):** **SÍ incluyó** los outliers.
- **Brier Skill Score (G.3 v2):** **SÍ incluyó** los outliers.

### 3.2 Deciles 1M: Agrupamiento Pooled vs Transversal (Cross-Sectional)
En la versión 2, los deciles se calcularon mediante `pd.qcut` sobre todo el panel agrupado (*pooled*), mezclando observaciones de distintas fechas. Al rehacer el análisis mediante el estándar cuantitativo correcto (**deciles transversales calculados fecha a fecha**) y aplicar filtros de robustez:

#### A. Deciles Pooled (Metodología v2)
| Decil | N Original | Media Orig (%) | N Sin Outliers | Media Sin Outl (%) | Media Winsorizada 1% | Mediana (%) |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **D1** | 4,793 | **+2.70%** | 4,782 | +2.75% | +2.70% | +2.57% |
| **D2** | 4,792 | +1.53% | 4,788 | +1.56% | +1.52% | +1.44% |
| **D5** | 4,792 | +1.29% | 4,792 | +1.29% | +1.29% | +1.26% |
| **D9** | 4,792 | +1.33% | 4,780 | +1.33% | +1.31% | +1.20% |
| **D10** | 4,793 | **+0.45%** | 4,748 | +0.59% | +0.41% | +0.67% |
| **Spread D10-D1** | | **-2.25%** | | **-2.17%** | **-2.30%** | **-1.90%** |

#### B. Deciles Cross-Sectional (Estándar Transversal por Fecha)
| Decil | N Original | Media Orig (%) | N Sin Outliers | Media Sin Outl (%) | Media Winsorizada 1% | Mediana (%) |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **D1** | 5,057 | **+1.13%** | 5,047 | +1.17% | +1.12% | **+0.26%** |
| **D2** | 5,040 | +1.27% | 5,039 | +1.27% | +1.25% | +1.00% |
| **D5** | 5,040 | +1.50% | 5,040 | +1.50% | +1.49% | +1.62% |
| **D9** | 5,040 | +1.36% | 5,031 | +1.39% | +1.36% | +1.27% |
| **D10** | 5,040 | **+0.83%** | 4,987 | +0.93% | +0.77% | **+1.21%** |
| **Spread D10-D1** | | **-0.30%** | | **-0.24%** | **-0.35%** | **+0.95%** |

### 3.3 Top 10 Observaciones que Más Contribuyen a la Media del Decil 1 (1M)
Al aislar las 10 observaciones con mayor rentabilidad forward dentro del Decil 1:

| Fecha Señal | Ticker | Score `pct_raw` | Retorno Forward 1M | Contribución a la Media D1 |
| :---: | :---: | :---: | :---: | :---: |
| 2022-02-23 | SLR.MC | -12.67 | **+60.34%** | +0.0126 pp |
| 2025-02-06 | IDR.MC | -7.84 | **+57.24%** | +0.0119 pp |
| 2025-02-07 | IDR.MC | -8.97 | **+54.24%** | +0.0113 pp |
| 2022-02-21 | SLR.MC | -10.84 | **+52.63%** | +0.0110 pp |
| 2022-02-11 | SLR.MC | -9.69 | **+51.75%** | +0.0108 pp |
| 2025-02-10 | IDR.MC | -6.66 | **+51.43%** | +0.0107 pp |
| 2025-02-05 | IDR.MC | -7.50 | **+50.24%** | +0.0105 pp |
| 2022-02-22 | SLR.MC | -12.33 | **+49.70%** | +0.0104 pp |
| 2025-05-07 | SLR.MC | -8.75 | **+49.17%** | +0.0103 pp |
| 2022-02-18 | SLR.MC | -9.86 | **+47.87%** | +0.0100 pp |

### 3.4 Revisión de la Afirmación "1M Invertido"
- **Diagnóstico:** Al evaluar de forma transversal fecha a fecha (cross-sectional), no se detecta señal en 1M (spread medio transversal $-0.30\%$, con la mediana de D10 en $+1.21\%$ superando a D1 en $+0.26\%$).
- Las observaciones individuales con rebotes extremos dentro del Decil 1 (como SLR e IDR) aportan cuantitativamente solo $\sim 0.11\text{ pp}$ de los $+2.70\%$ medios de la muestra agrupada, y la mediana agrupada (*pooled*) permanece en $-1.90\%$. Por consiguiente, la hipótesis previa de que dos episodios concentraron todo el efecto no está empíricamente respaldada.
- **Conclusión medida:** Por fecha no hay señal operable. La causa de la divergencia con la métrica agrupada (*pooled*) queda clasificada formalmente como **no determinada**.

---

## 4. Comparativa de Benchmark: Constituyentes Equiponderados vs ^IBEX (No Reconciliado)

Se evaluaron las discrepancias entre la serie oficial `^IBEX` de Yahoo Finance y la cartera equiponderada de sus 35 constituyentes:

| Serie | Overnight Medio (%) | Intradía Medio (%) | Total Diario Medio (%) | Naturaleza del Índice |
| :--- | :---: | :---: | :---: | :--- |
| **^IBEX (Índice Oficial)** | +0.022% | +0.051% | +0.073% | Price Return (sin dividendos) |
| **IBEX 35 Constituyentes (EW)** | +0.079% | +0.011% | +0.088% | **Total Return** (`auto_adjust=True`) |

1. **Impacto de dividendos:** El arrastre por dividendos entre el índice de precios oficial (`^IBEX`) y los constituyentes ajustados por dividendos (`auto_adjust=True`) explica aproximadamente **~1.5 pb/día en total** (~3.8% anualizado). Sin embargo, este factor explica la deriva a largo plazo pero **no el reparto diferencial observado entre el tramo overnight y el tramo intradía**. Por tanto, **se retira la calificación de "reconciliado"** y se deja clasificado como discrepancia estructural pendiente de atribución intradiaria.
2. **Retirada de conjeturas:** Se retira por completo la hipótesis especulativa sobre "cotizaciones indicativas a las 09:00:00" al no existir evidencia empírica que la respalde.
3. **Decisión metodológica:** A partir de este addendum, todo cálculo de exceso de retorno se realiza contra el **benchmark equiponderado del mismo universo**.

---

## 5. Validación de la Columna Open por Terciles de Liquidez

Se calculó empíricamente el porcentaje de sesiones donde el precio `Open` es exactamente igual al `Close` de la sesión anterior:

| Universo | Total Barras Analizadas | Casos $Open_t == Close_{t-1}$ | Porcentaje | Fiabilidad de Descomposición ON/ID |
| :--- | :---: | :---: | :---: | :--- |
| **IBEX 35** | 53,760 | 331 | **0.62%** | Verificada (Subasta de apertura activa) |
| **Tercil Alto de Liquidez** | 58,368 | 392 | **0.67%** | Verificada |
| **Tercil Medio** | 59,904 | 4,830 | **8.06%** | Parcialmente degradada |
| **Tercil Bajo de Liquidez** | 59,904 | 11,555 | **19.29%** | **NO VERIFICADA (No fiable)** |
| **^IBEX (Índice Oficial)** | 1,536 | 8 | **0.52%** | Verificada |

**Dictamen:** En el tercil bajo de liquidez, en casi una de cada cinco sesiones (**19.29%**) no se cruza operación en la subasta de apertura y el proveedor replica el precio de cierre previo. **Todo análisis que descomponga retornos en overnight e intradía en el tercil bajo o en el universo ilíquido queda formalmente clasificado como [NO VERIFICADO]**.

---

## 6. Consideraciones Legales de la Tasa Tobin (ITF)

- **Marco general:** Conforme a la legislación del Impuesto sobre las Transacciones Financieras (Ley 5/2020), el gravamen (0.20%) aplica a adquisiciones de acciones de sociedades españolas con capitalización bursátil superior a 1.000 millones de euros.
- **Operaciones intradía:** En operaciones de compra y venta cerradas en el mismo día, la liquidación sobre el saldo neto deja la base imponible en cero.
- **Rotación y costes:** La rotación medida del motor es del **23%** (se retira formalmente la cifra previa del 35%). La estimación previa de reconciliación (15.7 pb vs 5.5 pb) **queda formalmente retirada y clasificada como no verificada**, manteniéndose únicamente el principio empírico de que cualquier fricción fiscal acumulada en posiciones mantenidas a vencimiento ensancha la barrera de costes que imposibilita la rentabilidad de las señales.

---

## 7. Líneas Base Estadísticas y Acierto de Signo Condicionado

### 7.1 Corrección del Censo Muestral y Frase de la Banda
- **Censo real del panel:** 115 tickers únicos evaluados a lo largo de 1,260 fechas (entre 2021-10-01 y 2026-09-04), con exactamente 140,938 observaciones por horizonte y un **total de 563,752 observaciones** (no "4 × 50 × 1410").
- **Aclaración conceptual:** En informes previos se confundieron los días con retorno exactamente cero (`r == 0`, que representan entre el **3.3% y el 12.5%** según horizonte) con los días dentro de la banda lateral fijada por el motor (`|r| <= umbral`, que representan entre el **36.5% y el 48.1%**).

### 7.2 Acierto de Signo Frente a la Probabilidad Condicionada $P(\text{sube} \mid r \neq 0)$
Al evaluar exclusivamente las señales direccionales (ALCISTA / BAJISTA) sobre barras donde hubo movimiento efectivo:

| Horizonte | Tramo | N Muestra | % Ceros Exactos | % Dentro Banda | $P(\text{sube} \mid r \neq 0)$ | Hit Signo Motor | Diferencial vs Base Condicionada | Siempre Lateral | Azar Estratificado Ticker $\times$ Vol | Acierto 3C Motor |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **1D** | Diseño | 99,187 | 12.51% | 45.88% | **50.03%** | 49.07% | **-0.96 pp** | 45.88% | 40.20% | 41.23% |
| **1D** | OOS | 39,491 | 12.10% | 48.14% | **50.56%** | 48.91% | **-1.65 pp** | 48.14% | 41.74% | 42.72% |
| **3D** | Diseño | 99,187 | 7.46% | 40.01% | **50.23%** | 49.19% | **-1.05 pp** | 40.01% | 37.94% | 38.75% |
| **3D** | OOS | 39,491 | 7.32% | 42.61% | **51.12%** | 48.41% | **-2.70 pp** | 42.61% | 39.61% | 39.88% |
| **1S** | Diseño | 99,187 | 5.91% | 43.75% | **50.33%** | 48.85% | **-1.47 pp** | 43.75% | 39.58% | 40.30% |
| **1S** | OOS | 39,491 | 5.88% | 47.10% | **51.80%** | 48.25% | **-3.55 pp** | 47.10% | 42.00% | 42.16% |
| **1M** | Diseño | 99,187 | 3.37% | 36.51% | **50.56%** | 47.37% | **-3.18 pp** | 36.51% | 37.50% | 36.64% |
| **1M** | OOS | 39,491 | 3.28% | 39.59% | **54.06%** | 49.50% | **-4.56 pp** | 39.59% | 40.62% | 38.72% |

- **Conclusión de signo:** En todos los horizontes y en ambas mitades, el motor **no supera a la base no condicionada** (entre $-0.96\text{ pp}$ y $-4.56\text{ pp}$).
- **Conclusión 3 clases:** La tasa de acierto del motor en 3 clases (~38%–42%) apenas iguala al azar estratificado analítico condicionado por ticker y régimen de volatilidad (~38%–42%), y pierde ampliamente frente a la regla trivial de "Siempre Lateral" (~40%–48%).

---

## 8. Evaluación QLIKE con Contrastes Estadísticos y Explicación de ATR

### 8.1 Contraste Formal QLIKE vs Baseline Ingenuo (std 252d)
Se evaluó la pérdida QLIKE $L(\hat{\sigma}^2, r^2) = \ln(\hat{\sigma}^2) + \frac{r^2}{\hat{\sigma}^2}$ sobre las 1,283 fechas válidas del IBEX 35 mediante el test de Diebold-Mariano (Newey-West con 5 retardos) y Block Bootstrap temporal (500 réplicas con bloques de 20 días de mercado):

| Estimador | QLIKE Medio | Mejora vs Baseline | $SE_{\text{mejora}}$ | $t_{\text{Diebold-Mariano}}$ | $p$-valor DM | $IC_{95\%}$ Bootstrap Bloques | Ratio Escala $b = \overline{r^2/\hat{\sigma}^2}$ | Penalización Teórica Escala $b - 1 - \ln b$ |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Baseline 252d** | -7.1397 | 0.0000 | 0.0000 | 0.00 | 1.0000 | [0.0000, 0.0000] | 1.038 | +0.0007 |
| **EWMA ($\lambda=0.94$)** | -7.1476 | +0.0080 | 0.0148 | +0.54 | 0.5907 | [-0.0178, +0.0419] | 1.144 | +0.0095 |
| **EWMA ($\lambda=0.97$)** | -7.1641 | **+0.0245** | 0.0116 | **+2.12** | **0.0345** | [-0.0003, +0.0538] | 1.073 | +0.0026 |
| **ATR 14d / 1.6** | -7.1192 | **-0.0205** | 0.0172 | **-1.19** | 0.2350 | [-0.0586, +0.0183] | **1.387** | **+0.0599** |

*(Nota: Valores de QLIKE más negativos representan menor pérdida / mejor ajuste).*

### 8.2 ¿Por qué ATR Obtiene el Mejor Spearman pero el Peor QLIKE?
1. **Invarianza de escala del coeficiente Spearman:** Spearman evalúa exclusivamente el orden de los rangos transversales y es completamente insensible al nivel o escala absoluta del pronóstico. El ATR a 14 días captura con gran agilidad el rango de las velas recientes, permitiendo ordenar con precisión qué valores tienen mayor volatilidad relativa ($IC_{\text{Spearman}} = +0.1946$, el más alto).
2. **Sensibilidad estricta de QLIKE a la calibración del nivel:** La función de pérdida QLIKE penaliza fuertemente cualquier sesgo de escala. Se demuestra analíticamente que un pronóstico con un sesgo proporcional $b = E[r^2/\hat{\sigma}^2] \neq 1$ sufre un recargo constante en su pérdida media igual a $b - 1 - \ln(b) \ge 0$.
   Para el estimador ATR / 1.6, el ratio empírico es **$b = 1.387$** (la varianza realizada es un 38.7% superior a la pronosticada), lo que impone una penalización sistemática irreducible de **$+0.0599$** puntos a su score.
3. **Explosión asimétrica en barras comprimidas:** Cuando una acción entra en compresión de rango y su ATR disminuye bruscamente, cualquier salto posterior de precio genera un cociente $r^2 / \hat{\sigma}^2$ extremo, penalizando gravemente el promedio QLIKE.

---

## 9. Re-evaluación del Criterio G (Radar de Riesgo)

### 9.1 Corrección del BSS de Diseño (+6.16% → +0.74%)
En la versión preliminar se reportó un BSS en diseño del $+6.16\%$. Dicho valor no es reproducible con el pipeline actual de cálculo (tratamiento de valores NaN, convergencia del solver IRLS y definición muestral del baseline); la causa exacta del cambio no está determinada.

Evaluado con la formulación canónica del Brier Skill Score:
$$BSS = 1 - \frac{BS_{\text{modelo}}}{BS_{\text{benchmark}}}$$
el valor empírico exacto en la muestra de diseño para el IBEX 35 con el pipeline actual auditado es de **$+0.74\%$** (con intervalo de confianza bootstrap del $95\%$ que cruza el cero: $[-0.63\%, +2.62\%]$). Los valores reportados con anterioridad (+6.16%) se marcan formalmente como **no reproducibles**.

### 9.2 Resultados Auditados

Con los datos auditados (excluyendo NaNs, aislando el índice ^IBEX del grupo Resto, utilizando regresión logística exacta IRLS y block bootstrap por fechas de mercado):

| Versión Muestral | Universo | Tramo | BSS (%) | $IC_{95\%}$ BSS (Date-Block Boot) | Pendiente Calibración Logística | ¿Cumple Criterio G? |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| **Original (v2 corregido)** | IBEX 35 | Diseño | +0.74% | [-0.63%, +2.62%] | 0.52 | **NO** |
| | IBEX 35 | OOS | -1.80% | [-3.78%, +0.96%] | 0.47 | **NO** |
| | Resto | Diseño | -18.40% | [-20.92%, -17.00%] | -0.04 | **NO** |
| | Resto | OOS | -21.05% | [-25.04%, -17.70%] | -0.02 | **NO** |
| **Sin Outliers ($|r|>25\%$)** | IBEX 35 | Diseño | +0.71% | [-0.68%, +2.52%] | 0.52 | **NO** |
| | IBEX 35 | OOS | -1.78% | [-3.78%, +0.96%] | 0.46 | **NO** |
| | Resto | Diseño | -18.10% | [-20.42%, -16.28%] | -0.07 | **NO** |
| | Resto | OOS | -18.49% | [-21.08%, -15.88%] | -0.04 | **NO** |

**Dictamen Concluyente:**
El Criterio G **no se cumple en ningún escenario**. El límite inferior del BSS cruza el cero en diseño y se torna negativo en OOS. La pendiente de calibración logística (~0.46–0.52 en IBEX y ~0 en Resto) denota un severo desajuste de probabilidades, confirmando que las transformaciones gaussianas directas no constituyen un modelo calibrado operable.

---

*Fin del Addendum v3. Documentación y consolidación de magnitudes medidas.*
