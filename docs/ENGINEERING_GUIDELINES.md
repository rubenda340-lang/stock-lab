# StockLab — Engineering Guidelines

> Documento vivo de reglas y decisiones arquitectónicas del proyecto.
> Última actualización: 1 de Agosto de 2026.

---

## 1. Reglas Generales de Código

- **Nunca duplicar motores de scoring.** Existen tres implementaciones aisladas con propósitos distintos (`utils/scoring.py` para la Ficha Fundamental, `views/ranking.py` para la Tabla V7 y `services/ranking_engine.py` para el backend). No unificarlas hasta que StockLab esté funcionalmente terminado.
- **Nunca crear CSS `<style>` inline si existe una clase equivalente inyectada en otra vista.** Antes de añadir CSS, verificar si la clase ya existe en otro archivo de `views/`.
- **Todo helper reutilizable debe vivir en `utils/`.** No crear formateadores locales (`_formatear_moneda`, `_safe_float`, etc.) dentro de una vista si ya existe uno equivalente.
- **Todo acceso HTTP debe reutilizar la cabecera `User-Agent` estándar.** No hardcodear el User-Agent string; consumirlo desde `config.py` cuando se centralice.
- **No crear nuevas claves de `st.session_state` sin documentarlas** en este archivo (sección 6).
- **No añadir nuevas tablas SQLite sin registrar su mapa CRUD** en este archivo (sección 5).
- **Toda nueva vista debe indicar su nivel de impacto** (★☆☆☆☆ a ★★★★★) en la sección correspondiente.

---

## 2. Base de Datos

- **`seed_companies()` es estrictamente no destructiva.** Utiliza `INSERT OR IGNORE` basado en `ticker UNIQUE`. Nunca añadir `DELETE FROM empresas` sin condición.
- **`PRAGMA foreign_keys = ON`** está activo globalmente en `get_connection()`. Cualquier DELETE sobre `empresas` desencadena borrado en cascada en `cartera`.
- **Tablas sin uso actual (esquemas muertos):** `diario`, `notas`, `mercados`. No eliminarlas; están reservadas para futuras funcionalidades.

---

## 3. Caché Streamlit

- **`@st.cache_resource`:** Usar exclusivamente para objetos persistentes de sesión (conexiones DB, modelos ML). Sin TTL.
- **`@st.cache_data`:** Usar para datos que caducan. TTL recomendados:
  - Precios en tiempo real: 300s (5 min)
  - Datos fundamentales: 43200s (12 h)
  - Consultas de cartera: 30-60s
- **Invalidación:** Centralizada en `database/cartera.py` mediante `_invalidar_cache_cartera()` tras operaciones de escritura, invalidando selectivamente las funciones de cartera sin invocar `st.cache_data.clear()` en las vistas.

---

## 4. Deuda Técnica Aceptada (Decisiones Conscientes)

| Componente | Decisión | Fecha | Razón |
| :--- | :--- | :--- | :--- |
| `views/dashboard.py` (1.786 líneas) | NO dividir | 2026-08-01 | Funciona correctamente. Beneficio bajo vs riesgo alto de regresión. |
| `views/ranking.py` (1.924 líneas) | NO dividir | 2026-08-01 | Concentra lógica V7 activa en desarrollo. |
| Scoring triplicado | NO unificar | 2026-08-01 | Cada motor alimenta una UI distinta con ponderaciones diferentes. |
| CSS inline (180 KB) | NO centralizar | 2026-08-01 | Funciona correctamente. Centralizar cuando StockLab esté terminado. |

---

## 5. Registro de Tablas SQLite (Mapa CRUD)

| Tabla | Escritura | Lectura | Borrado | Estado |
| :--- | :--- | :--- | :--- | :--- |
| `empresas` | `connection.py` (INSERT OR IGNORE) | 6 vistas | `connection.py` (solo tickers inválidos) | Activa |
| `cartera` | `cartera.py` | `cartera.py`, `mi_cartera_v2.py`, `dashboard.py` | `cartera.py` | Activa |
| `watchlist` | `watchlist.py` | `watchlist.py` | `watchlist.py` | Activa |
| `ranking_estado` | `alert_engine.py` | `ranking.py` | — | Activa |
| `eventos` | `dashboard.py` | `dashboard.py` | `dashboard.py` | Activa |
| `diario` | — | — | — | Esquema reservado |
| `notas` | — | — | — | Esquema reservado |
| `mercados` | — | — | — | Esquema reservado |

---

## 6. Registro de Claves de `st.session_state`

| Clave | Escritores | Lectores | Propósito |
| :--- | :--- | :--- | :--- |
| `menu_activo` | `ranking`, `empresas`, `watchlist`, `mi_cartera_v2` | `app.py` | Enrutamiento topbar |
| `db_initialized` | `app.py` | `app.py` | Guard de inicialización |
| `tipo_cartera_activa` | `mi_cartera_v2` | `mi_cartera_v2` | Real vs Simulada |
| `compra_preseleccionada` | `empresas`, `ranking`, `watchlist` | `mi_cartera_v2` | Pre-poblar ticker compra |
| `selector_global_empresas` | `empresas`, `ranking`, `mi_cartera_v2` | `empresas` | Sincronizar ficha |
| `ia_config` | `ia` | `ia` | Hiperparámetros ML |
| `modal_venta` | `mi_cartera_v2` | `mi_cartera_v2` | Modal de desinversión |
| `editar_operacion` | `mi_cartera_v2` | `mi_cartera_v2` | Edición de transacciones |
| `periodo_patrimonio` | `mi_cartera_v2` | `mi_cartera_v2` | Filtro temporal gráfico |

---

## 7. Matriz de Impacto por Archivo

| Archivo | Impacto | Afecta a |
| :--- | :---: | :--- |
| `database/connection.py` | ★★★★★ | Todo el sistema |
| `app.py` | ★★★★★ | Arranque y enrutamiento global |
| `services/data_provider.py` | ★★★★☆ | Dashboard, Ranking, Ficha Empresa |
| `market_data.py` | ★★★★☆ | Dashboard, Empresas, Ranking |
| `database/cartera.py` | ★★★★☆ | Mi Cartera, Dashboard |
| `config.py` | ★★★☆☆ | APIs (FMP, Finnhub) |
| `components/stock_chart.py` | ★★★☆☆ | Dashboard, Empresas, Cartera |
| `views/diario.py` | ★☆☆☆☆ | Solo Diario |

---

## 8. Cambios Críticos — Protocolo Obligatorio

Toda modificación sobre los siguientes archivos:

- `app.py`
- `database/connection.py`
- `database/cartera.py`
- `services/data_provider.py`

**requiere obligatoriamente:**

1. Backup previo o commit previo del estado funcional.
2. Cambio aislado (un solo cambio por commit).
3. Prueba manual posterior confirmando que la aplicación arranca y opera sin errores.

---

## 9. Filosofía del Proyecto

StockLab prioriza:

1. **Estabilidad.** El sistema funciona correctamente antes y después de cada cambio.
2. **Consistencia visual.** La experiencia de usuario se mantiene coherente entre vistas.
3. **Mantenibilidad.** El código es comprensible y modificable sin riesgo.

Antes que:

- Microoptimizaciones.
- Refactorizaciones estéticas.
- Cambios arquitectónicos innecesarios.

> No se reabrirá una fase de auditoría o refactorización hasta que StockLab esté **funcionalmente terminado**. El esfuerzo se dedica al producto, no a la ingeniería interna.
