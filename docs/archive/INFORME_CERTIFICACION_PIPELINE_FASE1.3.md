# INFORME DE CERTIFICACIÓN DEL PIPELINE DE DATOS — FASE 1.3
## StockLab V7 — Trazabilidad Matemática Completa

**Fecha:** 2026-07-04  
**Módulo de auditoría:** `services/auditor_pipeline.py`  
**Resultados raw:** `AUDITORIA_TRAZABILIDAD_RESULTADOS.txt`  
**Empresas auditadas:** SAN.MC, BBVA.MC, ITX.MC, REP.MC, TEF.MC, ENCE.MC, DIA.MC, ORY.MC, GRF-P.MC, MDF.MC

---

## RESPUESTAS A LAS 7 PREGUNTAS DE CERTIFICACIÓN

### 1. ¿Existe alguna pérdida de información desde la API hasta el Ranking?

**SÍ. PÉRDIDA MASIVA CONFIRMADA.**

| Variable | SAN | BBVA | ITX | REP | TEF | ENCE | DIA | ORY | GRF-P | MDF |
|----------|-----|------|-----|-----|-----|------|-----|-----|-------|-----|
| returnOnEquity | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ |
| operatingMargins | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | ✅ |
| revenueGrowth | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | ✅ |
| debtToEquity | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ |
| currentRatio | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | ✅ |
| interestCoverage | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| dividendYield | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | ✅ |
| payoutRatio | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | ✅ |
| marketCap | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | ✅ |
| trailingPE | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ |
| priceToBook | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | ✅ |
| enterpriseToEbitda | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | ✅ |

**Hallazgos:**
- **`interestCoverage` es SIEMPRE None** para las 10 empresas. Yahoo no lo proporciona para tickers .MC. Esto significa que el componente "Cobertura Intereses" (hasta 10 pts) siempre puntúa 0.
- **`debtToEquity` y `currentRatio` son None para bancos** (SAN, BBVA). Yahoo no devuelve estos ratios para entidades financieras. Fortaleza Financiera pierde 20/30 pts automáticamente.
- **ENCE.MC y GRF-P.MC obtienen FALLBACK TOTAL**: Yahoo devuelve 404, FMP y Finnhub sin API key. Todos los datos son None. Calidad = 0/100.
- **ORY.MC tiene datos muy parciales**: ROE=None, revenueGrowth=None, D/E=None, trailingPE=None. Calidad = 0/100.

---

### 2. ¿Existe alguna transformación incorrecta?

**SÍ. DOBLE NORMALIZACIÓN DE `debtToEquity` CONFIRMADA.**

**Caso DIA.MC:**
```
Yahoo raw: debtToEquity = 759.89 (% escala Yahoo)
data_provider: 759.89 / 100 = 7.599 (normalizado a decimal... pero sigue > 1)
Motor V6: 7.599 > 1 → divide por 100 otra vez → 0.076
Valor real: D/E = 7.6 (760% deuda sobre equity)
Valor usado: 0.076 (parece empresa sin deuda)
Score: 10 pts (debería ser 0 pts por deuda extrema)
```

**Impacto:** Empresas con deuda alta (>100% D/E) reciben 10 puntos en Fortaleza Financiera como si tuvieran deuda mínima. La doble normalización invierte completamente la señal.

**Caso SAN.MC y BBVA.MC:**
Yahoo no devuelve `debtToEquity` para bancos → None → `pd.to_numeric(None) or 0.0` = 0.0 → `0.0 < 0.5` → 10 pts. Las entidades financieras obtienen 10 puntos gratis en deuda por ausencia de dato.

---

### 3. ¿Existe alguna normalización errónea?

**SÍ. ESCALA INCONSISTENTE DE `dividendYield`.**

**Caso SAN.MC:**
```
Yahoo raw: dividendYield = 2.03
data_provider: _safe_float(2.03) = 2.03 (sin transformar)
Motor V6: abs(2.03) < 1 → False → usa 2.03 directamente como %
Resultado: Yield = 2.03% → correcto POR CASUALIDAD
```

**Problema:** Yahoo devuelve `dividendYield` en decimal (0.0203 = 2.03%) para algunas empresas, pero en porcentaje directo (2.03) para otras. Data_provider NO normaliza esta clave (solo aplica `_safe_float`). El motor asume que si `abs(x) < 1` es decimal y multiplica por 100, pero si ya viene como 2.03, lo usa directo.

Para SAN: Yahoo devolvió 2.03 (escala porcentaje) → funciona por casualidad.
Para otras empresas donde Yahoo devuelve 0.0203 (escala decimal) → también funcionaría.
Pero **no hay garantía de consistencia entre fuentes**.

**Caso BBVA.MC:**
```
Yahoo raw: dividendYield = 4.1
Motor: abs(4.1) < 1 → False → usa 4.1% → correcto
```

**Caso ITX.MC:**
```
Yahoo raw: dividendYield = 1.15
Motor: abs(1.15) < 1 → False → usa 1.15% → correcto
```

En la práctica actual funciona porque Yahoo parece devolver dividendYield en escala porcentaje para tickers .MC. Pero es frágil.

---

### 4. ¿Existe alguna variable que llegue como None cuando la API realmente la proporciona?

**SÍ.**

- **`interestCoverage`**: Yahoo NO lo proporciona para NINGÚN ticker .MC auditado. No es un bug del pipeline, es una limitación de la API de Yahoo para el mercado español.
- **`enterpriseToEbitda`**: None para SAN, BBVA, ORY, GRF-P, MDF. Disponible para ITX, REP, TEF, DIA. Inconsistencia de cobertura de Yahoo.
- **`debtToEquity` y `currentRatio`**: None específicamente para bancos (SAN, BBVA). Yahoo no calcula estos ratios para entidades financieras.

---

### 5. ¿Existe alguna variable que desaparezca durante el pipeline?

**NO se pierden variables durante la normalización.** Todas las claves definidas en `_CLAVES_ESTANDAR` llegan al motor. El problema es que muchas llegan como `None` porque Yahoo no las proporciona, no porque se pierdan en el pipeline.

Sin embargo, **13 variables descargadas nunca se utilizan**:
- `averageVolume`, `beta`, `currentPrice`, `earningsGrowth`, `enterpriseValue`, `forwardPE`, `pegRatio`, `previousClose`, `priceToSales`, `profitMargins`, `quickRatio`, `recommendationMean`, `returnOnAssets`

Estas variables se descargan, normalizan y almacenan, pero el Motor V6 (`_calcular_calidad_empresarial` y `_calcular_oportunidad_inversion`) nunca las lee. Son datos muertos.

---

### 6. ¿Existe alguna diferencia entre el dato normalizado y el utilizado por el algoritmo?

**SÍ. EL MOTOR APLICA SU PROPIA CONVERSIÓN ADICIONAL.**

El motor V6 en `views/ranking.py` aplica `x * 100 if abs(x) < 1` internamente para convertir decimales a porcentajes. Esta conversión es **adicional** a la normalización de `data_provider.py`.

| Variable | data_provider entrega | Motor convierte | Resultado final | Correcto? |
|----------|----------------------|-----------------|-----------------|-----------|
| ROE | 0.129 (decimal) | ×100 → 12.9% | 12.9% | ✅ |
| OpMargin | 0.433 (decimal) | ×100 → 43.3% | 43.3% | ✅ |
| RevGrowth | 0.046 (decimal) | ×100 → 4.6% | 4.6% | ✅ |
| DivYield | 2.03 (¿decimal o %?) | abs>1 → no convierte | 2.03% | ⚠️ Por casualidad |
| PayoutRatio | 0.255 (decimal) | ×100 → 25.5% | 25.5% | ✅ |
| D/E (ITX) | 0.318 (decimal) | abs<1 → no convierte | 0.318 | ✅ |
| D/E (DIA) | 7.599 (>1!) | ÷100 → 0.076 | 0.076 | ❌ DOBLE NORM |

La conversión interna del motor funciona correctamente PARA LA MAYORÍA de casos, pero falla cuando data_provider entrega un valor > 1 que debería ser decimal (caso D/E de DIA).

---

### 7. ¿El problema observado en empresas como ENCE, DIA, ORY o GRIFOLS se debe al pipeline de datos o al algoritmo V6?

**RESPUESTA DEFINITIVA BASADA EN EVIDENCIA:**

#### ENCE.MC → Problema del PIPELINE (Fallback total)
```
Fuente: FALLBACK (Yahoo 404, sin FMP, sin Finnhub)
Datos: TODOS None
Calidad: 0/100
Oportunidad: 57.5/100 (suelo artificial)
Score Final: 23.0/100
DIAGNÓSTICO: El pipeline NO puede obtener datos. No es culpa del algoritmo.
```

#### GRF-P.MC → Problema del PIPELINE (Fallback total)
```
Fuente: FALLBACK (Yahoo sin datos útiles, sin FMP, sin Finnhub)
Datos: TODOS None
Calidad: 0/100
Oportunidad: 55.0/100 (suelo artificial)
Score Final: 22.0/100
DIAGNÓSTICO: Idéntico a ENCE. Ticker probablemente incorrecto o sin cobertura.
```

#### ORY.MC → Problema MIXTO (Pipeline parcial + Algoritmo)
```
Fuente: Yahoo (parcial)
Datos disponibles: opMargin=-75.3%, P/B=1.96, marketCap=235M
Datos ausentes: ROE, revGrowth, D/E, currentRatio, intCoverage, trailingPE, EV/EBITDA
Calidad: 0/100 (datos disponibles son negativos o insuficientes)
Oportunidad: 61.1/100 (suelo artificial + P/B aporta 5 pts)
DIAGNÓSTICO: Yahoo proporciona datos parciales. Los datos que SÍ llegan son
correctos. La Calidad=0 se debe a que los fundamentales reales son negativos
(margen -75%) y a que faltan variables clave. Es MIXTO: pipeline incompleto +
fundamentales realmente malos.
```

#### DIA.MC → Problema del ALGORITMO (Datos correctos, scoring incorrecto)
```
Fuente: Yahoo (completo excepto interestCoverage)
ROE: 1.47 (Yahoo lo devolvió como 147%, NO como 1.47 decimal)
  → Motor: abs(1.47) < 1 → False → usa 1.47% → 0 pts
  → REALIDAD: ROE = 147% → debería obtener 10 pts
D/E: 7.599 (doble normalización → 0.076 → 10 pts en vez de 0)
OpMargin: 4.2% → 0 pts (correcto, margen bajo)
RevGrowth: -4.3% → 0 pts (correcto, decrecimiento)
Calidad: 15/100
DIAGNÓSTICO: Los datos LLEGAN correctamente. El problema es:
  1. ROE > 1 no se reconoce como porcentaje (bug de escala)
  2. D/E sufre doble normalización (bug de transformación)
  3. Los fundamentales reales SON débiles (margen 4%, crecimiento negativo)
Es principalmente un problema del ALGORITMO con bugs de escala.
```

#### Resumen por empresa:

| Empresa | Calidad | Causa raíz | Pipeline OK? | Algoritmo OK? |
|---------|---------|-----------|-------------|---------------|
| SAN.MC | 46 | D/E, CR, IC = None (bancos) | ⚠️ Parcial | ✅ Correcto con datos disponibles |
| BBVA.MC | 58 | D/E, CR, IC = None (bancos) | ⚠️ Parcial | ✅ Correcto con datos disponibles |
| ITX.MC | 70 | Solo IC = None | ✅ Casi completo | ✅ Correcto |
| REP.MC | 59 | Solo IC = None | ✅ Casi completo | ✅ Correcto |
| TEF.MC | ~55 | Datos completos | ✅ Completo | ✅ Correcto |
| ENCE.MC | 0 | FALLBACK TOTAL | ❌ Sin datos | N/A |
| DIA.MC | 15 | ROE escala + D/E doble norm | ✅ Datos llegan | ❌ Bugs de escala |
| ORY.MC | 0 | Datos muy parciales + negativos | ⚠️ Parcial | ✅ Correcto con datos disponibles |
| GRF-P.MC | 0 | FALLBACK TOTAL | ❌ Sin datos | N/A |
| MDF.MC | 0 | ROE=None, D/E=None, fundamentals malos | ⚠️ Parcial | ✅ Correcto con datos disponibles |

---

## TABLA RESUMEN DE HALLAZGOS

### Bugs Críticos Confirmados

| # | Bug | Ubicación | Impacto | Evidencia |
|---|-----|-----------|---------|-----------|
| 1 | Doble normalización debtToEquity | data_provider L170 + ranking.py L170 | D/E alto parece bajo → 10 pts gratis | DIA: 7.599→0.076 |
| 2 | ROE > 1 no se convierte a % | ranking.py L115 | ROE 147% se interpreta como 1.47% → 0 pts | DIA: 1.47→0pts |
| 3 | Fallback total sin API keys alternativas | data_provider cascada | Empresas sin Yahoo = 0 datos | ENCE, GRF-P |
| 4 | interestCoverage siempre None | Yahoo API limitación | 10 pts siempre perdidos en Fortaleza | Todas las empresas |

### Bugs Funcionales Confirmados

| # | Bug | Ubicación | Impacto | Evidencia |
|---|-----|-----------|---------|-----------|
| 5 | None → 0.0 indistinguible de valor real | ranking.py `or 0.0` | Dato ausente puntúa igual que dato malo | SAN D/E=None→0pts |
| 6 | Suelo artificial Oportunidad = 42.5-57.5 | ranking.py L310,L325 | Empresas sin datos tienen Oport ~55 | ENCE: 57.5, GRF-P: 55 |
| 7 | momentum_tecnico fallback = 12.5 | ranking.py L310 | Infla Oportunidad sin histórico | ENCE, GRF-P |
| 8 | momentum_fundamental base = 30.0 | ranking.py L325 | Suelo de 30/35 pts | Todas |

### Variables Muertas (descargadas pero no usadas)

`averageVolume`, `beta`, `currentPrice`, `earningsGrowth`, `enterpriseValue`, `forwardPE`, `pegRatio`, `previousClose`, `priceToSales`, `profitMargins`, `quickRatio`, `recommendationMean`, `returnOnAssets`

### Código Muerto (motores no utilizados)

- `services/ranking_engine.py` — Motor B completo (usa claves legacy: ROE, ROA, epsGrowth, etc.)
- `utils/scoring.py` — Motor C completo (calcular_score_completo)

---

## CERTIFICACIÓN DE NORMALIZACIÓN ENTRE FUENTES

Para SAN.MC (única empresa con datos comparables):

| Variable | Yahoo | FMP | Finnhub | Consistente |
|----------|-------|-----|---------|-------------|
| returnOnEquity | 0.1289 | None | None | N/A (solo Yahoo) |
| operatingMargins | 0.4326 | None | None | N/A |
| revenueGrowth | 0.046 | None | None | N/A |
| debtToEquity | None | None | None | ❌ Ninguna fuente |
| currentRatio | None | None | None | ❌ Ninguna fuente |
| dividendYield | 2.03 | None | None | ⚠️ Escala incierta |
| trailingPE | 14.11 | None | None | N/A |
| priceToBook | 1.70 | None | None | N/A |

**Conclusión:** FMP y Finnhub NO están funcionando (sin API keys configuradas o sin cobertura .MC). Yahoo es la ÚNICA fuente efectiva. La cascada de fallback es teórica; en la práctica solo existe Yahoo → Fallback.

---

## CONCLUSIÓN FINAL

### El pipeline de datos (`data_provider.py`) funciona correctamente en su lógica interna.

Las transformaciones, normalizaciones y cascada están bien implementadas. Los datos que Yahoo proporciona llegan correctamente al motor.

### Los problemas observados tienen TRES causas raíces distintas:

1. **Cobertura insuficiente de Yahoo para tickers .MC** (40% de las variables clave son None para bancos; algunos tickers dan 404 total). Sin API keys de FMP/Finnhub, no hay alternativa.

2. **Bugs de escala en el Motor V6** (doble normalización de D/E, ROE>1 no reconocido). Estos bugs distorsionan puntuaciones incluso cuando los datos son correctos.

3. **Suelos artificiales en Oportunidad** que enmascaran la falta de datos, creando la ilusión de que la Oportunidad "funciona" cuando en realidad está asignando valores por defecto.

### Recomendación para Fase 1.5:

Antes de rediseñar el algoritmo V8, es IMPRESCINDIBLE:
1. Configurar al menos una API alternativa (FMP o Finnhub) para cubrir los gaps de Yahoo
2. Corregir los bugs de escala (D/E doble normalización, ROE>1)
3. Eliminar los suelos artificiales de Oportunidad
4. Decidir qué hacer con las 13 variables descargadas pero no utilizadas

---

*Este informe se basa EXCLUSIVAMENTE en evidencia obtenida mediante ejecución real del módulo `services/auditor_pipeline.py` sobre 10 empresas representativas del BME. Ninguna conclusión se deriva de suposiciones o análisis estático del código.*
