# 💾 Cache Persistente — StockLab IA Optimization

## Descripción

Esta carpeta contiene el caché en disco de datos históricos de yfinance.

### Estrategia de Caché

1. **Caché en Memoria (Volátil)** — Dentro de la sesión Streamlit
   - Reutiliza datos durante la misma sesión
   - Se pierde al refrescar la página

2. **Caché en Disco (Persistente)** — Archivos CSV locales
   - Un archivo por ticker (ej: `cache_BBVA_MC.csv`)
   - Se valida la fecha de modificación (si es de hoy, se reutiliza)
   - Reduce descargas de yfinance significativamente

### Archivos Generados Automáticamente

```
/data/cache/
├── cache_BBVA_MC.csv      # Datos históricos de BBVA (si existe, es de hoy)
├── cache_AENA_MC.csv      # Datos históricos de AENA
├── cache_SAN_MC.csv       # Datos históricos de Santander
└── cache_ACS_MC.csv       # Datos históricos de ACS
```

### Flujo de Carga

```mermaid
obtener_analisis_ia()
  ├─ ¿Caché en memoria? → SÍ: Retorna (0ms)
  └─ NO:
    ├─ ¿Caché en disco de hoy? → SÍ: Carga + caché memoria (< 1s)
    └─ NO: Descarga yfinance + guarda en disco (30s) + caché memoria
```

### Impacto de Rendimiento

**Antes (Sin Caché en Disco):**
- Primer arranque: ~30 segundos
- Cambio de ticker: ~30 segundos cada vez
- Cambio de horizonte/modelo: ~30 segundos cada vez

**Después (Con Caché en Disco + Lazy Loading):**
- Primer arranque: <1 segundo (sin cargar IA)
- Primer clic IA: ~30 segundos (primera descarga yfinance)
- Siguientes clics IA (mismo día): <1 segundo (caché disco)
- Cambio de ticker (primer clic): ~30 segundos
- Cambio de ticker (siguientes clics, mismo día): <1 segundo

### Limpieza

Los archivos de caché se actualizan automáticamente cada día. Si necesitas forzar una actualización:

```bash
# Eliminar archivo específico
rm data/cache/cache_BBVA_MC.csv

# Eliminar todo el caché
rm data/cache/*.csv
```

### Notas Técnicas

- El caché se valida mediante `stat().st_mtime` (fecha última modificación)
- Cada archivo contiene datos desde 2020 hasta la fecha actual
- Solo se guarda en disco si `len(datos) >= 50` filas
- Manejo de errores: Si el caché está corrupto, se descarga automáticamente desde yfinance
