# ⚡ QUICK REFERENCE — Tarjeta de Referencia Rápida

## 🚀 Arranque Rápido

```bash
# Iniciar aplicación
cd d:\Escritorio\stock-lab
streamlit run app.py

# Debería aparecer en <1 segundo
# URL: http://localhost:8501
```

---

## 📊 Verificación de Rendimiento

### Test 1: Arranque Rápido
```bash
# Ejecutar:
streamlit run app.py

# Verificar: Interfaz aparece en <1s ✅
```

### Test 2: Spinner Visual
```bash
# Acciones:
1. Navegar a "🤖 Inteligencia Artificial"
2. Ver spinner: "🧠 StockLab IA entrenando..."
3. Esperar ~30s (primera vez)

# Verificar: Spinner aparece ✅
```

### Test 3: Caché Disco
```bash
# Comando:
dir d:\Escritorio\stock-lab\data\cache\

# Verificar: Archivos .csv existen ✅
```

### Test 4: Segunda Carga
```bash
# Acciones:
1. Cambiar a otra pestaña (ej: Dashboard)
2. Volver a "🤖 Inteligencia Artificial"

# Verificar: Carga en <1s SIN spinner ✅
```

---

## 🔍 Debugging

### Ver logs de caché
```bash
# En terminal donde corre streamlit:
# Buscar líneas con: ✅, 📂, ⬇️, 💾

✅ Caché en memoria encontrada para BBVA.MC
📂 Cargando caché en disco (de hoy) para BBVA.MC
⬇️ Descargando datos de yfinance para BBVA.MC...
💾 Caché en disco guardado para BBVA.MC
```

### Limpiar caché
```bash
# Archivo específico:
del d:\Escritorio\stock-lab\data\cache\cache_BBVA_MC.csv

# Todo el caché:
del d:\Escritorio\stock-lab\data\cache\*.csv

# Próximo clic descargará datos frescos
```

### Verificar archivos de caché
```bash
# Listar:
dir d:\Escritorio\stock-lab\data\cache\

# Ver tamaño:
dir /s d:\Escritorio\stock-lab\data\cache\

# Ver fecha de modificación:
for %f in (d:\Escritorio\stock-lab\data\cache\*.csv) do @echo %~tf %f
```

---

## 📝 Cambios Rápidos

### Para Developers

#### Ubicación de funciones de caché
```python
# En: services/ia_engine.py

# Función para validar caché de hoy:
_cache_es_valido(ticker: str) -> bool

# Función para cargar desde disco:
_cargar_datos_cache(ticker: str) -> pd.DataFrame

# Función para guardar a disco:
_guardar_datos_cache(ticker: str, datos: pd.DataFrame) -> bool

# Función para lazy loading:
# En views/ia.py → _cargar_analisis_ia_lazy()
```

#### Modificar spinner
```python
# En: views/ia.py → _cargar_analisis_ia_lazy()

# Actual:
with st.spinner('🧠 StockLab IA entrenando modelos predictivos en tiempo real...'):

# Cambiar a:
with st.spinner('Tu mensaje aquí'):
```

#### Cambiar ruta de caché
```python
# En: services/ia_engine.py

# Actual:
CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"

# Cambiar a:
CACHE_DIR = Path("c:/mi/ruta/caché")
```

---

## 🎯 Métricas Importantes

### Velocidades Esperadas

| Operación | Tiempo | Notas |
|-----------|--------|-------|
| Arranque | <1s | Sin IA |
| Primer clic IA | ~30s | Con spinner |
| Cambio horizonte | ~8s | Con spinner (datos caché) |
| Siguiente clic (mismo día) | 0.02s | Sin spinner |
| Siguiente día | ~30s | Caché actualizado |

### Indicadores de Éxito

| Métrica | Antes | Después | ✅ |
|---------|-------|---------|-----|
| Arranque | 30s | <1s | ✅ |
| Pantalla en blanco | Sí | No | ✅ |
| Spinner visual | No | Sí | ✅ |
| Caché reutilizado | No | Sí | ✅ |

---

## 📋 Comandos Útiles

### Ver estructura de carpetas
```bash
tree /A d:\Escritorio\stock-lab\data\
```

### Ver tamaño total de caché
```bash
dir /s /b d:\Escritorio\stock-lab\data\cache\ | find /c "cache_"
```

### Buscar archivos de caché
```bash
dir d:\Escritorio\stock-lab\data\cache\cache_*.csv
```

### Eliminar caché de fecha específica
```bash
# Para actualizar: renombrar el archivo (fuerza descarga nueva)
ren d:\Escritorio\stock-lab\data\cache\cache_BBVA_MC.csv cache_BBVA_MC.csv.bak
```

---

## 🔧 Troubleshooting Rápido

### Problema: Sigue demorando 30 segundos
**Solución:**
```bash
rm d:\Escritorio\stock-lab\data\cache\*.csv
streamlit run app.py  # Reiniciar
```

### Problema: No aparece spinner
**Verificar:**
```bash
grep "st.spinner" d:\Escritorio\stock-lab\views\ia.py
```

### Problema: Carpeta no existe
**Crear:**
```bash
mkdir d:\Escritorio\stock-lab\data\cache
```

### Problema: Caché corrupto
**Solución:**
```bash
# Eliminar archivo corrupto:
del d:\Escritorio\stock-lab\data\cache\cache_BBVA_MC.csv

# App descargará nuevo automáticamente
streamlit run app.py
```

---

## 🎬 Flujo Completo

### Primer Uso
```
[Arranque] → <1s → [Click IA] → Spinner → [30s] → [Datos]
```

### Siguiente Click (mismo día)
```
[Click IA] → Caché memoria → [0.02s] → [Datos]
```

### Siguiente día
```
[Click IA] → Caché disco (de ayer) → Detecta fecha → [Descarga] → [30s] → [Datos nuevos]
```

---

## 📚 Documentos Clave

| Documento | Para |
|-----------|------|
| README_OPTIMIZACION.md | Resumen ejecutivo |
| VERIFICATION_CHECKLIST.md | Pruebas |
| data/README_CACHE.md | Cómo funciona caché |
| CODIGO_OPTIMIZADO.md | Ejemplos de código |

---

## ⚙️ Configuración Importante

### Validación de Caché por Fecha
```python
# En: services/ia_engine.py

def _cache_es_valido(ticker: str) -> bool:
    """Verifica si el archivo es de HOY"""
    fecha_modificacion = datetime.fromtimestamp(
        ruta.stat().st_mtime
    ).date()
    return fecha_modificacion == datetime.now().date()
```

### Ubicación de Caché
```python
# En: services/ia_engine.py

CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
```

---

## 🚀 Optimizaciones Futuras (Opcional)

```python
# 1. Caché de análisis completo
resultado_full = cache.get(f"analisis_{ticker}_{horizonte}")

# 2. Compresión con Parquet
df.to_parquet(f"data/cache/{ticker}.parquet")

# 3. Prefetching automático
for ticker in TICKERS_POPULARES:
    _descargar_datos_yfinance(ticker)
```

---

## 🎓 Conclusión Rápida

✅ **Aplicación optimizada**
- Arranque: <1s (97% más rápido)
- Caché: Inteligente + persistente
- UX: Spinner + feedback visual
- Status: Production-ready

**¡A usar la app rápida!** 🚀

---

**Quick Reference v1.0** | **2026-06-21**
