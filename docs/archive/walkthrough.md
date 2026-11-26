# Walkthrough: Implementación del Módulo "Mi Cartera"

## Introducción

Este documento describe la implementación técnica del módulo de cartera de inversión simulada para StockLab v2.0. El módulo permite registrar compras y ventas, calcular posiciones automáticamente y analizar la rentabilidad de la cartera.

## Arquitectura General

```
┌─────────────────────────────────────────────────────────────┐
│                    views/mi_cartera.py                       │
│  (Presentación: UI, formularios, tablas, modales)           │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                  database/cartera.py                         │
│  (Lógica de negocio: CRUD, cálculos, agregaciones)          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              database/connection.py                          │
│  (Acceso a datos: SQLite, query_all, query_val)             │
└─────────────────────────────────────────────────────────────┘
```

## 1. Capa de Datos: `database/cartera.py`

### Funciones Principales

#### `agregar_operacion()`
Registra una nueva operación (COMPRA o VENTA) en la tabla `cartera`.

```python
def agregar_operacion(
    empresa_id: int,
    tipo: str,           # "COMPRA" o "VENTA"
    cantidad: float,
    precio_unitario: float,
    fecha: str = None,   # YYYY-MM-DD, default hoy
    comision: float = 0.0,
    notas: str = None
) -> bool
```

**Flujo:**
1. Si no se proporciona fecha, usa `datetime.now()`
2. Inserta registro en tabla `cartera`
3. Retorna True/False según éxito

#### `calcular_posiciones_abiertas()`
Función crítica que reconstruye las posiciones actuales desde el histórico.

```python
@st.cache_data(ttl=60)
def calcular_posiciones_abiertas() -> list[dict]
```

**Algoritmo:**
```
Para cada operación (ordenada por fecha ASC):
    SI tipo == "COMPRA":
        acciones += cantidad
        coste_acumulado += cantidad × precio
    
    SI tipo == "VENTA":
        coste_por_accion = coste_acumulado / acciones
        coste_venta = cantidad × coste_por_accion
        acciones -= cantidad
        coste_acumulado -= coste_venta

Filtrar solo posiciones con acciones > 0
Calcular precio_medio = coste_acumulado / acciones
```

**Ejemplo:**
```
Compra: 10 acc @ 20€ → acciones=10, coste=200€
Compra: 15 acc @ 25€ → acciones=25, coste=575€
Venta:   5 acc @ 30€ → acciones=20, coste=460€ (575 - 5×23)

Precio medio final: 460€ / 20 = 23€/acción
```

#### `obtener_resumen_cartera()`
Calcula métricas agregadas de toda la cartera.

```python
@st.cache_data(ttl=60)
def obtener_resumen_cartera() -> dict
```

**Retorna:**
- `valor_total`: Σ(acciones × precio_actual)
- `coste_total`: Σ(coste_acumulado)
- `rentabilidad_absoluta`: valor_total - coste_total
- `rentabilidad_pct`: ((valor_total / coste_total) - 1) × 100
- `num_posiciones`: count(posiciones con acciones > 0)
- `liquidez`: 0.0 (placeholder)

### Otras Funciones

- `obtener_operaciones()`: Consulta con filtros (empresa, tipo, fechas)
- `actualizar_operacion()`: UPDATE parcial de campos
- `eliminar_operacion()`: DELETE con retorno de éxito
- `obtener_empresas_disponibles()`: Lista de empresas para selectores
- `obtener_empresa_por_ticker()`: Búsqueda rápida por ticker

## 2. Capa de Presentación: `views/mi_cartera.py`

### Estructura de Renderizado

```python
def show():
    # 1. Inyectar CSS
    # 2. Reset scroll
    # 3. Verificar empresa preseleccionada
    # 4. Renderizar modales (si activos)
    # 5. Renderizar secciones principales:
    #    - Resumen superior (KPIs)
    #    - Tabla de posiciones
    #    - Formulario de compra
    #    - Histórico de operaciones
    # 6. Sección de futuras mejoras
```

### Componentes Visuales

#### `_renderizar_resumen_superior()`
Muestra 4 tarjetas KPI usando `st.metric()`:
- Valor Total (con delta %)
- Rentabilidad Total (con color condicional)
- Posiciones Abiertas
- Liquidez Simulada

#### `_renderizar_tabla_posiciones()`
Tabla interactiva con:
- DataFrame estilizado con colores condicionales
- Acciones rápidas por fila: Vender, Ver Detalle
- Botones organizados en grid de 4 columnas

**Estilos CSS:**
```css
.posicion-positiva { color: #10b981; }  /* Verde */
.posicion-negativa { color: #ef4444; }  /* Rojo */
```

#### `_renderizar_formulario_compra()`
Formulario con `st.form()`:
- Selector de empresa (con preselección opcional)
- Date input para fecha
- Number inputs para cantidad, precio, comisión
- Text area para notas
- Validación antes de guardar

#### `_renderizar_modal_venta()`
Modal condicional (solo si `st.session_state['modal_venta']` existe):
- Muestra acciones disponibles
- Valida cantidad ≤ disponible
- Botones Confirmar/Cancelar

#### `_renderizar_historico_operaciones()`
Tabla completa con filtros:
- Selectbox empresa
- Selectbox tipo (COMPRA/VENTA/Todas)
- Selectbox rango temporal
- Tabla estilizada con colores por tipo
- Selector de operación para editar/eliminar

#### `_renderizar_modal_edicion()`
Formulario de edición precargado con datos existentes.

#### `_renderizar_confirmacion_eliminacion()`
Diálogo de confirmación antes de eliminar.

## 3. Integración entre Módulos

### Patrón de Navegación con Preselección

**Origen (ej. Empresas):**
```python
if st.button("💼 Añadir a Cartera"):
    st.session_state['compra_preseleccionada'] = ticker
    st.session_state['menu_activo'] = "Mi Cartera"
    st.rerun()
```

**Destino (Mi Cartera):**
```python
def show():
    empresa_preseleccionada = None
    if 'compra_preseleccionada' in st.session_state:
        empresa_preseleccionada = st.session_state['compra_preseleccionada']
        del st.session_state['compra_preseleccionada']
    
    _renderizar_formulario_compra(empresa_preseleccionada)
```

### Normalización de Claves de Menú

En `app.py`, un mapeador defensivo convierte claves sin emojis:

```python
normalizar_menu = {
    "Mi Cartera": "💼 Mi Cartera",
    "Empresas": "🏢 Empresas",
    # ...
}
if st.session_state["menu_activo"] in normalizar_menu:
    st.session_state["menu_activo"] = normalizar_menu[st.session_state["menu_activo"]]
```

Esto permite usar `"Mi Cartera"` o `"💼 Mi Cartera"` indistintamente.

## 4. Manejo de Caché

### Estrategia de Caché

| Función | TTL | Razón |
|---------|-----|-------|
| `calcular_posiciones_abiertas()` | 60s | Cálculo costoso, pero debe reflejar cambios recientes |
| `obtener_resumen_cartera()` | 60s | Depende de posiciones y precios |
| `market_data.get_stock_data()` | 300s | Llamada externa a API, más lenta |

### Invalidación Manual

Después de modificar operaciones:
```python
calcular_posiciones_abiertas.clear()
obtener_resumen_cartera.clear()
st.rerun()
```

## 5. Manejo de Errores

### Patrón Consistente

```python
try:
    datos = market_data.get_stock_data(ticker)
    precio_actual = datos.get('precio_actual', 0)
except Exception:
    precio_actual = 0.0  # Fallback seguro
    # Interfaz continúa funcionando
```

### Mensajes al Usuario

- `st.success()`: Operaciones exitosas
- `st.error()`: Errores técnicos
- `st.warning()`: Advertencias (datos incompletos)
- `st.info()`: Estados vacíos, guías

## 6. Consideraciones de Rendimiento

### Optimizaciones Aplicadas

1. **Caché TTL**: Evita recálculos frecuentes
2. **Consultas eficientes**: JOINs en SQL, no en Python
3. **Lazy loading**: Precios actuales solo para posiciones abiertas
4. **Invalidación selectiva**: Solo limpiar caché cuando sea necesario

### Posibles Mejoras Futuras

- Batch fetching de precios (una llamada para múltiples tickers)
- WebSockets para actualización en tiempo real
- Persistencia de posiciones calculadas (tabla materializada)

## 7. Testing Manual Recomendado

### Escenarios de Prueba

1. **Compra simple**
   - Registrar compra de 10 acciones
   - Verificar posición aparece en tabla
   - Verificar KPIs actualizados

2. **Compras múltiples**
   - Comprar 10 acc @ 20€
   - Comprar 15 acc @ 25€
   - Verificar precio medio = 23€

3. **Venta parcial**
   - Tener 25 acciones
   - Vender 5 acciones
   - Verificar quedan 20 acciones
   - Verificar precio medio se mantiene

4. **Venta total**
   - Vender todas las acciones
   - Verificar posición desaparece
   - Verificar KPIs actualizados

5. **Edición de operación**
   - Editar precio de compra
   - Verificar posición recalculada

6. **Eliminación de operación**
   - Eliminar una compra
   - Verificar posición ajustada

7. **Navegación desde otros módulos**
   - Click "Añadir a Cartera" en Empresas
   - Verificar formulario preseleccionado
   - Click "Comprar" en Watchlist
   - Click "Comprar Acciones" en Ranking

8. **Errores de market_data**
   - Desconectar internet
   - Verificar interfaz no se rompe
   - Verificar mensajes amistosos

## 8. Extensibilidad

### Puntos de Extensión Preparados

1. **Gestión de liquidez**
   - Campo `liquidez` ya existe en resumen
   - Añadir tabla `movimientos_caja`
   - Implementar lógica de cash flow

2. **Dividendos**
   - Añadir tabla `dividendos`
   - Vincular a posiciones
   - Calcular yield

3. **Gráficos**
   - Estructura de datos ya preparada
   - Añadir Plotly/Altair
   - Time series de valor de cartera

4. **Exportación**
   - DataFrame ya construido
   - Añadir `df.to_csv()` / `df.to_excel()`

5. **Análisis IA**
   - Datos estructurados disponibles
   - Integrar con modelos ML
   - Recomendaciones automáticas

## Conclusión

El módulo "Mi Cartera" está implementado siguiendo los principios de:
- **Modularidad**: Separación clara de responsabilidades
- **Robustez**: Manejo de errores consistente
- **Escalabilidad**: Preparado para futuras ampliaciones
- **Consistencia**: Mismo estilo visual y patrones que resto de StockLab
- **Rendimiento**: Caché estratégico y consultas optimizadas

La arquitectura basada en reconstruir posiciones desde el histórico (en lugar de almacenarlas) garantiza integridad de datos y flexibilidad para soportar cualquier combinación de compras y ventas.
