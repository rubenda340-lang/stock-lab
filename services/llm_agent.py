"""
Agente IA Real — StockLab (Gemini + Function Calling + Google Search)
=====================================================================
Sustituye al intérprete de reglas de assistant_engine por un LLM real
(Google Gemini Flash, tier gratuito) que decide qué herramienta invocar
para responder con los datos vivos de la plataforma.

Arquitectura:
  pregunta usuario → Gemini → function_call → herramienta local (datos
  reales) → Gemini redacta respuesta → {titulo, texto, df} para la vista.

Capacidades:
  · 18 herramientas locales (radar, fundamentales, cartera, watchlist,
    noticias con sentimiento, rankings por variación...).
  · Búsqueda externa con Google Search grounding para información de
    actualidad fuera de StockLab (con citas de fuentes en la respuesta).
  · Memoria conversacional: últimas 6 interacciones (12 mensajes) en
    st.session_state['ia_gemini_hist'] para preguntas de seguimiento.

Las herramientas reutilizan los handlers de assistant_engine (que queda
como fallback automático si no hay API key, no hay red o se agota la cuota).

SDK: google-genai (el antiguo google-generativeai está deprecado).

Configuración:
  1. Obtener key gratuita en https://aistudio.google.com/apikey
  2. Añadir a .env:  STOCKLAB_GEMINI_API_KEY=tu_key
  3. Sin key, el asistente funciona en "modo rápido" (reglas) automáticamente.
"""

from typing import Callable, Dict, List, Optional

import pandas as pd
import streamlit as st

from config import GEMINI_API_KEY, GEMINI_SEARCH_ENABLED
from services import assistant_engine

# Mapeo de modelos seleccionables a sus identificadores de API reales con cascada de fallback
# (Verificados contra client.models.list() de la API Key activa)
MODELOS_CONFIG = {
    "Gemini 3.8 Flash": ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-flash-latest"],
    "Gemini 3.7 Flash": ["gemini-3.7-flash", "gemini-3.8-flash", "gemini-flash-latest"],
    "Gemini 3.5 Flash Lite": ["gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.6-flash"],
    "Gemini 3.1 Pro": ["gemini-3.1-pro-preview", "gemini-2.5-pro", "gemini-3.8-flash"],
    "Gemini 2.5 Pro": ["gemini-2.5-pro", "gemini-3.1-pro-preview", "gemini-3.8-flash"],
}

MODELOS_DISPONIBLES = list(MODELOS_CONFIG.keys())
DEFAULT_MODELO = "Gemini 3.5 Flash Lite"
MODELOS = MODELOS_CONFIG[DEFAULT_MODELO]

# Límites diarios de peticiones (RPD) observados en Google AI Studio
LIMITES_DIARIOS = {
    "Gemini 3.8 Flash": {"requests": 20, "tokens": 250_000, "rpm": 5},
    "Gemini 3.7 Flash": {"requests": 20, "tokens": 250_000, "rpm": 5},
    "Gemini 3.5 Flash Lite": {"requests": 500, "tokens": 250_000, "rpm": 15},
    "Gemini 3.1 Pro": {"requests": 20, "tokens": 250_000, "rpm": 2},
    "Gemini 2.5 Pro": {"requests": 20, "tokens": 250_000, "rpm": 2},
}


def _fecha_cuota_google() -> str:
    """
    Retorna la fecha según el huso de cuota de Google AI Studio (US Pacific Time).
    La cuota diaria oficial de Google se reinicia a medianoche PT.
    PT tiene horario de verano: UTC-7 (PDT) en verano y UTC-8 (PST) en invierno.
    """
    from datetime import datetime, timezone, timedelta
    try:
        from zoneinfo import ZoneInfo  # requiere tzdata en Windows
        return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        # Fallback sin dependencias: regla DST de EE.UU.
        # (2º domingo de marzo 10:00 UTC → 1º domingo de noviembre 9:00 UTC)
        ahora = datetime.now(timezone.utc)
        y = ahora.year

        def _nth_domingo(mes: int, n: int) -> datetime:
            d = datetime(y, mes, 1)
            return d + timedelta(days=((6 - d.weekday()) % 7) + 7 * (n - 1))

        inicio = _nth_domingo(3, 2).replace(hour=10, tzinfo=timezone.utc)
        fin = _nth_domingo(11, 1).replace(hour=9, tzinfo=timezone.utc)
        offset = -7 if inicio <= ahora < fin else -8
        return (ahora + timedelta(hours=offset)).date().isoformat()


def registrar_consumo(
    modelo: str,
    prompt_tokens: int = 0,
    resp_tokens: int = 0,
    total_tokens: int = 0,
) -> None:
    """Registra una petición y los tokens consumidos en la base de datos para el modelo indicado."""
    try:
        hoy = _fecha_cuota_google()
        if total_tokens <= 0:
            total_tokens = prompt_tokens + resp_tokens

        from services.auth_service import get_current_user, is_cloud_user_authenticated, supabase_rest
        if is_cloud_user_authenticated():
            user = get_current_user()
            uid = user["id"]
            ok_q, rows = supabase_rest(
                "GET",
                "ia_consumo_diario",
                params={"user_id": f"eq.{uid}", "fecha": f"eq.{hoy}", "modelo": f"eq.{modelo}", "limit": 1},
            )
            prev_pet = int(rows[0].get("peticiones") or 0) if (ok_q and isinstance(rows, list) and rows) else 0
            prev_tok = int(rows[0].get("tokens") or 0) if (ok_q and isinstance(rows, list) and rows) else 0
            supabase_rest(
                "POST",
                "ia_consumo_diario",
                params={"on_conflict": "user_id,fecha,modelo"},
                payload={
                    "user_id": uid,
                    "fecha": hoy,
                    "modelo": modelo,
                    "peticiones": prev_pet + 1,
                    "tokens": prev_tok + int(total_tokens),
                },
                prefer="resolution=merge-duplicates,return=minimal",
            )
            return

        from database.connection import get_connection
        conn = get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO ia_consumo_diario (fecha, modelo, tokens_prompt, tokens_respuesta, total_tokens)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (hoy, modelo, int(prompt_tokens), int(resp_tokens), int(total_tokens)),
                )
        finally:
            conn.close()
    except Exception as e:
        print(f"[llm_agent] Advertencia al registrar consumo diario: {e}")


def marcar_cuota_agotada(modelo: str, motivo: str = "Google 429 Quota Exceeded") -> None:
    """Registra en BD que un modelo ha alcanzado su cuota diaria en Google AI Studio."""
    try:
        from database.connection import get_connection
        conn = get_connection()
        try:
            hoy = _fecha_cuota_google()
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO ia_cuota_agotada (fecha, modelo, motivo)
                    VALUES (?, ?, ?)
                    """,
                    (hoy, modelo, str(motivo)[:255]),
                )
        finally:
            conn.close()
    except Exception as e:
        print(f"[llm_agent] Advertencia al marcar cuota agotada: {e}")


def _es_429(err_str: str) -> bool:
    """True si el error es un 429/rate-limit de Google (cualquier tipo)."""
    s = (err_str or "").lower()
    return ("429" in s or "resource_exhausted" in s
            or "quota exceeded" in s or "quotavalue" in s)


def _es_cuota_diaria(err_str: str) -> bool:
    """
    True solo si el error 429 corresponde a cuota DIARIA (RPD) de Google.
    Los 429 por minuto (RPM) son transitorios (se liberan en ~60 s) y NO
    deben bloquear el modelo durante todo el ciclo diario.
    Google especifica la métrica en el mensaje: 'generate_requests_per_model_per_day'
    (diaria) vs '..._per_minute' (transitoria).
    """
    s = (err_str or "").lower()
    if "per_day" in s or "perday" in s or "per-day" in s or "daily" in s:
        return True
    if "per_minute" in s or "perminute" in s or "per-minute" in s:
        return False
    # 429 sin métrica identificable: asumir transitorio. El límite diario
    # real de Google siempre incluye 'per_day' en los detalles del error.
    return False


def es_cuota_agotada(modelo: str) -> bool:
    """True si el modelo ha agotado su cuota diaria de Google para el ciclo de hoy."""
    try:
        from database.connection import get_connection
        conn = get_connection()
        try:
            hoy = _fecha_cuota_google()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 1 FROM ia_cuota_agotada
                WHERE fecha = ? AND modelo = ?
                """,
                (hoy, modelo),
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        return False


def obtener_uso_hoy(modelo: str) -> Dict:
    """
    Retorna las métricas y porcentaje de cuota diaria consumida hoy para un modelo concreto:
    - modelo: Nombre del modelo consultado
    - peticiones: Número de llamadas realizadas hoy
    - limite_peticiones: Límite de llamadas según cuota de AI Studio (20 RPD o 500 RPD)
    - tokens: Total de tokens consumidos hoy
    - limite_tokens: Límite TPM de tokens (250K)
    - porcentaje: Float entre 0.0 y 100.0
    - estado: 'normal' (<70%), 'aviso' (70%-89%), 'limite' (>=90%)
    - agotada: True si Google rechazó por cuota diaria alcanzada
    """
    limites = LIMITES_DIARIOS.get(modelo, {"requests": 20, "tokens": 250_000, "rpm": 5})
    lim_req = limites["requests"]
    lim_tok = limites["tokens"]
    agotada = es_cuota_agotada(modelo)

    peticiones = 0
    total_tokens = 0
    try:
        hoy = _fecha_cuota_google()
        from services.auth_service import get_current_user, is_cloud_user_authenticated, supabase_rest
        if is_cloud_user_authenticated():
            # En modo nube multiusuario, limitar peticiones por usuario (máx. 20/día por modelo)
            lim_req = min(lim_req, 20)
            user = get_current_user()
            ok_q, rows = supabase_rest(
                "GET",
                "ia_consumo_diario",
                params={"user_id": f"eq.{user['id']}", "fecha": f"eq.{hoy}", "modelo": f"eq.{modelo}", "limit": 1},
            )
            if ok_q and isinstance(rows, list) and rows:
                peticiones = int(rows[0].get("peticiones") or 0)
                total_tokens = int(rows[0].get("tokens") or 0)
            if peticiones >= lim_req:
                agotada = True
        else:
            from database.connection import get_connection
            conn = get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(total_tokens), 0)
                    FROM ia_consumo_diario
                    WHERE fecha = ? AND modelo = ?
                    """,
                    (hoy, modelo),
                )
                row = cursor.fetchone()
                if row:
                    peticiones = int(row[0] or 0)
                    total_tokens = int(row[1] or 0)
            finally:
                conn.close()
    except Exception as e:
        print(f"[llm_agent] Advertencia al consultar uso diario: {e}")

    if agotada:
        peticiones = lim_req
        porcentaje = 100.0
        estado = "limite"
    else:
        # La cuota diaria efectiva en AI Studio se mide por peticiones/día (RPD);
        # lim_tok (250K) es límite por minuto (TPM), no acumulado diario.
        porcentaje = (peticiones / lim_req) * 100.0 if lim_req > 0 else 0.0
        porcentaje = min(100.0, max(0.0, porcentaje))

        if porcentaje >= 90.0:
            estado = "limite"
        elif porcentaje >= 70.0:
            estado = "aviso"
        else:
            estado = "normal"

    return {
        "modelo": modelo,
        "peticiones": peticiones,
        "limite_peticiones": lim_req,
        "tokens": total_tokens,
        "limite_tokens": lim_tok,
        "porcentaje": round(porcentaje, 1),
        "estado": estado,
        "agotada": agotada,
    }

_SYSTEM_PROMPT = """Eres el agente de IA de StockLab, una terminal institucional de inversión
del mercado español (IBEX 35 y Mercado Continuo BME). Respondes SIEMPRE en español,
con tono profesional, analítico, claro y directo.

REGLAS:
1. OBLIGATORIO: para CUALQUIER pregunta sobre datos de mercado, empresas,
   subidas/bajadas, señales, riesgo/volatilidad, sectores, dividendos, eventos,
   alertas, cartera o actualidad económica/macro, DEBES llamar a las herramientas
   disponibles (y puedes encadenar varias en una misma respuesta si aporta valor,
   p. ej. ficha_empresa + riesgo_empresa + noticias_empresa). NUNCA inventes cifras.
   Mapa rápido de herramientas:
   - "empresas que más han subido/bajado esta semana/mes" → top_ganadoras_semana / top_perdedoras_semana / top_ganadoras_mes / top_perdedoras_mes
   - "mejor momentum" → top_momentum
   - "señales alcistas/bajistas/laterales" → senales
   - "sobreventa/sobrecompra" → sobreventa / sobrecompra
   - "resume/analiza X" → ficha_empresa (y opcionalmente riesgo_empresa)
   - "riesgo/volatilidad/drawdown/beta/liquidez de X" → riesgo_empresa
   - "compara X e Y" → comparar_empresas
   - "sector bancario / energía / sectores" → resumen_sector
   - "dividendos / eventos" → top_dividendos / proximos_eventos
   - "ranking/mejores" → top_ranking
   - "PER bajo/value" → value_per_dividendo
   - "noticias de X" / "noticias del IBEX" → noticias_empresa
   - "actualidad macro / BCE / Fed / tipos / inflación / bolsa internacional / OPAs / cualquier tema externo reciente" → buscar_actualidad_web
   - "qué alertas tengo / avisos" → mis_alertas
   - "mi cartera / cartera real / cartera simulada" → mi_cartera
   - "mi watchlist / mis seguidas" → mi_watchlist
   - "precisión del motor / métricas" → metricas_predicciones
2. Interpreta los datos devueltos por las herramientas en lugar de limitarte a
   repetirlos: destaca lo clave (riesgos, catalizadores, contexto).
3. Si la pregunta es puramente conceptual (qué es el RSI, cómo interpretar un
   PER, diferencia entre beta y volatilidad...), responde directamente con rigor
   y claridad didáctica sin llamar a herramientas.
4. Cuando menciones actualidad o noticias, apóyate estrictamente en los titulares
   devueltos por noticias_empresa o buscar_actualidad_web. Si no encuentras el dato,
   dilo con transparencia.
5. Cuando des visiones direccionales, incluye una cautela breve ("no constituye
   recomendación de inversión"), sin repetirla de forma mecánica en cada turno.
6. Formato markdown limpio con **negritas** en las cifras clave y listas breves.
   NO incluyas tablas Markdown en el texto (la interfaz renderiza las tablas aparte).
   Evita emojis decorativos. Extensión recomendada: ~160–240 palabras salvo que pidan
   un informe detallado."""


def _build_system_prompt() -> str:
    """Inyecta la fecha actual para contextualizar noticias y consultas temporales."""
    from datetime import date
    return f"{_SYSTEM_PROMPT}\n\nFecha actual de referencia: {date.today().isoformat()}."


# ═══════════════════════════════════════════════════════════════════
# 🔌 DISPONIBILIDAD
# ═══════════════════════════════════════════════════════════════════

def disponible() -> bool:
    """True si hay API key configurada y el SDK instalado."""
    if not GEMINI_API_KEY:
        return False
    try:
        from google import genai  # noqa: F401
        return True
    except ImportError:
        return False


def activo() -> bool:
    """True si el agente real está operativo (no degradado a reglas)."""
    return disponible() and not st.session_state.get("ia_agente_fallback", False)


# ═══════════════════════════════════════════════════════════════════
# 🧰 HERRAMIENTAS (function calling)
# ═══════════════════════════════════════════════════════════════════

def _resolver_ticker(nombre: str, ctx: Dict) -> Optional[str]:
    """Nombre o símbolo libre → ticker, usando el detector del asistente."""
    encontrados = assistant_engine._detectar_empresas(
        assistant_engine._norm(nombre), ctx)
    return encontrados[0] if encontrados else None


def _build_herramientas(ctx: Dict, holder: Dict) -> List[Callable]:
    """
    Construye las herramientas como closures sobre el contexto vivo.
    Cada una devuelve texto compacto para el LLM y guarda en `holder`
    el título y el DataFrame para que la vista lo renderice.
    """
    radar = ctx["radar"]

    def _registrar(resp: Dict, nombre_tool: str = "") -> str:
        if nombre_tool:
            holder.setdefault("herramientas", []).append(nombre_tool)
        fuentes_r = resp.get("fuentes") or []
        if fuentes_r:
            holder.setdefault("fuentes_web", []).extend(fuentes_r)
        df = resp.get("df")
        if df is not None and not df.empty:
            holder["df"] = df
            holder["titulo"] = resp["titulo"]
            return f"{resp.get('texto', '')}\n\n{df.to_string(index=False)}".strip()
        if holder.get("df") is None:
            holder["titulo"] = resp["titulo"]
        return resp["texto"]

    # ── Herramientas que reutilizan handlers del asistente ────────────

    def top_momentum() -> str:
        """Empresas con mayor momentum (impulso de precio) a 20 días."""
        return _registrar(assistant_engine._top_momentum(ctx), "top_momentum")

    def senales(tipo: str) -> str:
        """Señales del radar a 1 semana. tipo: ALCISTA, BAJISTA o LATERAL."""
        tipo = str(tipo).upper().strip()
        if tipo not in ("ALCISTA", "BAJISTA", "LATERAL"):
            tipo = "ALCISTA"
        return _registrar(assistant_engine._senales(ctx, tipo), "senales")

    def sobreventa() -> str:
        """Empresas en sobreventa técnica (RSI < 35), candidatas a rebote."""
        return _registrar(assistant_engine._sobreventa(ctx), "sobreventa")

    def sobrecompra() -> str:
        """Empresas en sobrecompra técnica (RSI > 65), riesgo de recorte."""
        return _registrar(assistant_engine._sobrecompra(ctx), "sobrecompra")

    def value_per_dividendo() -> str:
        """Empresas value: PER bajo (<18) con rentabilidad por dividendo >= 2%."""
        return _registrar(assistant_engine._value(ctx), "value_per_dividendo")

    def top_dividendos() -> str:
        """Mayores rentabilidades por dividendo del mercado."""
        return _registrar(assistant_engine._top_dividendos(ctx), "top_dividendos")

    def proximos_eventos() -> str:
        """Próximos dividendos, resultados y eventos del calendario."""
        return _registrar(assistant_engine._proximos_eventos(), "proximos_eventos")

    def top_ranking() -> str:
        """Top 10 del ranking de calidad fundamental de StockLab."""
        return _registrar(assistant_engine._top_ranking(), "top_ranking")

    def mas_volatiles() -> str:
        """Empresas más volátiles (mayor riesgo e incertidumbre)."""
        return _registrar(assistant_engine._volatiles(ctx), "mas_volatiles")

    def ficha_empresa(empresa: str) -> str:
        """Análisis completo de una empresa: precio, RSI, momentum, fundamentales
        (PER, P/B, EV/EBITDA, dividendo, ROE, margen, deuda), rango 52s y señales.
        empresa: nombre o ticker (ej. 'BBVA', 'Santander', 'IBE.MC')."""
        ticker = _resolver_ticker(empresa, ctx)
        if not ticker:
            return f"No encuentro ninguna empresa llamada '{empresa}' en StockLab."
        holder["ultimo_ticker"] = ticker
        return _registrar(assistant_engine._ficha(ticker, ctx), "ficha_empresa")

    def riesgo_empresa(empresa: str) -> str:
        """Perfil cuantitativo de riesgo y liquidez de una empresa (Panel de Análisis):
        volatilidad anualizada 20d/60d/252d, percentiles 1A/3A, rango 52 semanas,
        Max Drawdown, liquidez en euros y Beta/Correlación 60d frente al IBEX 35.
        empresa: nombre o ticker (ej. 'BBVA', 'ITX.MC')."""
        ticker = _resolver_ticker(empresa, ctx)
        if not ticker:
            return f"No encuentro ninguna empresa llamada '{empresa}' en StockLab."
        holder["ultimo_ticker"] = ticker
        return _registrar(assistant_engine._riesgo_empresa(ticker, ctx), "riesgo_empresa")

    def resumen_sector(sector: str = "") -> str:
        """Analiza las empresas de un sector del mercado español (ej. 'banca',
        'energía', 'salud', 'construcción', 'inmobiliaria', 'turismo', 'tecnología')
        con variación, momentum, PER, dividendo y calidad. Si sector está vacío,
        lista los principales sectores disponibles."""
        return _registrar(assistant_engine._resumen_sector(sector, ctx), "resumen_sector")

    def mis_alertas() -> str:
        """Devuelve las alertas activas del Dashboard (críticas, watchlist cerca de
        objetivo, cartera, cambios de ranking y volatilidad de mercado)."""
        return _registrar(assistant_engine._mis_alertas(ctx), "mis_alertas")

    def comparar_empresas(empresa1: str, empresa2: str) -> str:
        """Comparativa directa entre dos empresas (precio, RSI, momentum,
        señales semanal y mensual). Acepta nombres o tickers."""
        t1 = _resolver_ticker(empresa1, ctx)
        t2 = _resolver_ticker(empresa2, ctx)
        if not t1 or not t2:
            return (f"No puedo comparar: no encuentro "
                    f"'{empresa1 if not t1 else empresa2}' en StockLab.")
        holder["ultimo_ticker"] = t1
        return _registrar(assistant_engine._comparar(t1, t2, ctx), "comparar_empresas")

    def noticias_empresa(empresa: str = "") -> str:
        """Titulares recientes y balance de sentimiento de una empresa del
        mercado español. Si se omite empresa, devuelve noticias generales
        del mercado (IBEX 35). empresa: nombre o ticker (ej. 'BBVA')."""
        if empresa and str(empresa).strip():
            ticker = _resolver_ticker(str(empresa), ctx)
            if not ticker:
                return _registrar(assistant_engine._buscar_actualidad_web(str(empresa)), "buscar_actualidad_web")
            holder["ultimo_ticker"] = ticker
            return _registrar(assistant_engine._noticias(ticker, ctx), "noticias_empresa")
        return _registrar(assistant_engine._noticias(None, ctx), "noticias_empresa")

    def buscar_actualidad_web(consulta: str) -> str:
        """Busca noticias y actualidad económica/financiera abierta en la web
        (BCE, Fed, tipos de interés, inflación, petróleo, Wall Street, empresas
        internacionales, OPAs o cualquier tema externo a BME).
        consulta: términos de búsqueda claros en español o inglés."""
        return _registrar(assistant_engine._buscar_actualidad_web(consulta), "buscar_actualidad_web")

    def mi_cartera(tipo: str = "") -> str:
        """Posiciones abiertas de la cartera del usuario: valoración actual,
        P&L por posición, patrimonio total y rentabilidad.
        tipo: opcional, 'SIMULADA', 'REAL' o vacío para usar la cartera activa."""
        return _registrar(assistant_engine._mi_cartera(ctx, tipo=tipo or None), "mi_cartera")

    def mi_watchlist() -> str:
        """Empresas en la lista de seguimiento (watchlist) del usuario con
        su precio actual, variación desde el alta y señal semanal."""
        return _registrar(assistant_engine._mi_watchlist(ctx), "mi_watchlist")

    # ── Herramientas nuevas (rankings por variación de precio) ────────

    def _top_variacion(n: int, columna: str, ascendente: bool,
                       titulo: str, nombre_tool: str) -> str:
        holder.setdefault("herramientas", []).append(nombre_tool)
        if radar is None or radar.empty or columna not in radar.columns:
            return "Sin datos de mercado en el radar ahora mismo."
        n = max(1, min(int(n), 15))
        sel = radar.nsmallest(n, columna) if ascendente else radar.nlargest(n, columna)
        df = pd.DataFrame({
            "Empresa": sel["empresa"].values,
            "Var. semana (%)": sel["mom5"].values,
            "Var. día (%)": sel["var_dia"].values,
            "Mom. 20d (%)": sel["mom20"].values,
            "Señal 1S": sel["senal_1S"].values,
        })
        holder["titulo"] = titulo.format(n=len(sel))
        holder["df"] = df
        return df.to_string(index=False)

    def top_ganadoras_semana(n: int = 5) -> str:
        """Las N empresas que MÁS han subido en la última semana
        (5 sesiones). n por defecto 5, máximo 15."""
        return _top_variacion(n, "mom5", False, "Top {n} ganadoras de la semana", "top_ganadoras_semana")

    def top_perdedoras_semana(n: int = 5) -> str:
        """Las N empresas que MÁS han caído en la última semana
        (5 sesiones). n por defecto 5, máximo 15."""
        return _top_variacion(n, "mom5", True, "Top {n} perdedoras de la semana", "top_perdedoras_semana")

    def top_ganadoras_mes(n: int = 5) -> str:
        """Las N empresas que MÁS han subido en el último mes
        (20 sesiones). n por defecto 5, máximo 15."""
        return _top_variacion(n, "mom20", False, "Top {n} ganadoras del mes", "top_ganadoras_mes")

    def top_perdedoras_mes(n: int = 5) -> str:
        """Las N empresas que MÁS han caído en el último mes
        (20 sesiones). n por defecto 5, máximo 15."""
        return _top_variacion(n, "mom20", True, "Top {n} perdedoras del mes", "top_perdedoras_mes")

    def metricas_predicciones(horizonte: str = "Todos") -> str:
        """Auditoría cuantitativa y precisión del motor predictivo de IA:
        tasa de acierto (%), score de calidad (0-100), señales resueltas y vivas.
        horizonte: 'Todos', '1D', '3D', '1S' o '1M'."""
        hz = str(horizonte).strip()
        if hz not in ("Todos", "1D", "3D", "1S", "1M"):
            hz = "Todos"
        return _registrar(assistant_engine._metricas_predicciones(hz), "metricas_predicciones")

    def proyeccion_empresa(empresa: str, dias: int = 20) -> str:
        """Cono cuantitativo de proyección probabilística a 20 sesiones para una empresa:
        precio esperado base, techo estadístico (+1σ) y suelo estadístico (-1σ).
        empresa: nombre o ticker (ej. 'BBVA', 'Santander', 'ITX.MC'). dias: 20 por defecto."""
        ticker = _resolver_ticker(empresa, ctx)
        if not ticker:
            return f"No encuentro la empresa '{empresa}' para proyectar."
        holder["ultimo_ticker"] = ticker
        return _registrar(assistant_engine._proyeccion(ticker, ctx, dias=dias), "proyeccion_empresa")

    return [
        top_momentum, senales, sobreventa, sobrecompra,
        value_per_dividendo, top_dividendos, proximos_eventos,
        top_ranking, mas_volatiles, ficha_empresa, riesgo_empresa,
        resumen_sector, mis_alertas, comparar_empresas,
        noticias_empresa, buscar_actualidad_web, mi_cartera, mi_watchlist,
        top_ganadoras_semana, top_perdedoras_semana,
        top_ganadoras_mes, top_perdedoras_mes,
        metricas_predicciones, proyeccion_empresa,
    ]


# ═══════════════════════════════════════════════════════════════════
# 🤖 NÚCLEO GEMINI
# ═══════════════════════════════════════════════════════════════════

def _extraer_fuentes(resp) -> List[str]:
    """Extrae las URLs de grounding (Google Search) de una respuesta, máx. 5."""
    fuentes: List[str] = []
    try:
        candidatos = getattr(resp, "candidates", None) or []
        if not candidatos:
            return fuentes
        gm = getattr(candidatos[0], "grounding_metadata", None)
        for chunk in (getattr(gm, "grounding_chunks", None) or [])[:8]:
            web = getattr(chunk, "web", None)
            uri = getattr(web, "uri", None) if web else None
            if uri:
                titulo = (getattr(web, "title", "") or uri).strip()
                fuentes.append(f"- [{titulo}]({uri})")
    except Exception:
        pass
    return list(dict.fromkeys(fuentes))[:5]


def _requiere_herramienta(pregunta: str, ctx: Dict) -> bool:
    """Heurística ligera para distinguir consultas de datos vivos de preguntas conceptuales."""
    norm = assistant_engine._norm(pregunta or "")
    norm_clean = norm.lstrip("¿¡?.,;:! \t\n")
    if not norm_clean:
        return False
    if assistant_engine._detectar_empresas(norm, ctx):
        return True

    # Indicadores inequívocos de consulta sobre datos vivos/personales (anulan prefijo conceptual)
    palabras_accion_viva = (
        "mi cartera", "mis posiciones", "mi watchlist", "mis seguidas",
        "esta semana", "este mes", "hoy", "mas han subido", "mas han bajado",
        "top ", "mejores", "peores", "ranking", "noticias de", "proximos",
        "cuales", "que empresas", "lista de", "pasado con", "compara ",
        "analiza ", "resume ", "proyeccion de", "precision del motor",
        "alerta", "avisos", "sector ", "sectores", "actualidad", "bce",
        "fed", "tipos de interes", "inflacion", "petroleo", "wall street", "opa",
    )
    prefijos_conceptuales = (
        "que es ", "que son ", "que significa ", "explica ", "explicame ",
        "definicion de ", "como funciona ", "como se calcula ",
        "como leer ", "como interpretar ", "diferencia entre ",
        "como esta definid",
    )
    if norm_clean.startswith(prefijos_conceptuales) and not any(k in norm_clean for k in palabras_accion_viva):
        return False

    palabras_datos = (
        "subido", "bajado", "ganador", "perdedor", "momentum", "senal",
        "alcista", "bajista", "sobreventa", "sobrecompra", "cartera",
        "posicion", "watchlist", "seguida", "dividendo", "evento",
        "resultado", "ranking", "volatil", "noticia", "pasado con",
        "precio", "cotiz", "compara", "analiza", "resume", "proyecc",
        "metrica", "precision", "acierto", "value", "barata", "ibex",
        "radar", "esta semana", "este mes", "hoy", "alerta", "sector",
        "drawdown", "beta", "liquidez", "actualidad", "bce", "fed",
        "inflacion", "petroleo", "tipos de interes", "wall street", "opa",
    )
    return any(k in norm_clean for k in palabras_datos)


def _extraer_tokens_usage(resp, fallback_texto: str = "") -> tuple:
    """Extrae (prompt_tokens, resp_tokens, total_tokens) de una respuesta de Gemini."""
    p_tokens = 0
    r_tokens = 0
    t_tokens = 0
    usage = getattr(resp, "usage_metadata", None)
    if usage:
        p_tokens = getattr(usage, "prompt_token_count", 0) or 0
        r_tokens = getattr(usage, "candidates_token_count", 0) or 0
        t_tokens = getattr(usage, "total_token_count", 0) or 0
    if t_tokens == 0 and fallback_texto:
        t_tokens = max(150, len(fallback_texto) // 3)
    return int(p_tokens), int(r_tokens), int(t_tokens)


def _sugerencias_seguimiento(herramientas: List[str], ultimo_ticker: Optional[str], ctx: Dict) -> List[str]:
    """Genera 3 preguntas de seguimiento contextuales y clicables tras la respuesta."""
    nom = ctx.get("ticker2nombre", {}).get(ultimo_ticker, ultimo_ticker) if ultimo_ticker else None
    hs = set(herramientas or [])

    if nom and "proyeccion_empresa" in hs:
        return [
            f"Perfil de riesgo y volatilidad de {nom}",
            f"Últimas noticias de {nom}",
            f"Resume fundamentales de {nom}",
        ]
    if nom and ("ficha_empresa" in hs or "comparar_empresas" in hs):
        return [
            f"Perfil de riesgo y volatilidad de {nom}",
            f"Últimas noticias de {nom}",
            f"Proyección cuantitativa de {nom}",
        ]
    if nom and "riesgo_empresa" in hs:
        return [
            f"Resume fundamentales y señales de {nom}",
            f"Últimas noticias de {nom}",
            "¿Qué empresas tienen menor volatilidad o mejor ranking?",
        ]
    if "mi_cartera" in hs or "mi_watchlist" in hs:
        return [
            "¿Qué alertas tengo activas hoy?",
            "¿Qué próximos dividendos hay?",
            "¿Qué señales alcistas hay hoy?",
        ]
    if "resumen_sector" in hs:
        return [
            "¿Cuáles son las mejores del ranking de calidad?",
            "¿Cuáles tienen PER bajo y buen dividendo?",
            "¿Qué noticias hay del mercado hoy?",
        ]
    if "buscar_actualidad_web" in hs or "noticias_empresa" in hs:
        return [
            "¿Cómo está el sector bancario hoy?",
            "¿Qué empresas han subido más esta semana?",
            "¿Qué alertas tengo activas hoy?",
        ]
    return [
        "¿Qué alertas tengo activas hoy?",
        "¿Cómo está el sector bancario?",
        "¿Qué empresas han subido más esta semana?",
    ]


def _responder_gemini(pregunta: str, ctx: Dict, modelo_elegido: str = DEFAULT_MODELO) -> Dict:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    holder: Dict = {
        "titulo": "Agente StockLab",
        "df": None,
        "herramientas": [],
        "fuentes_web": [],
        "ultimo_ticker": None,
    }
    herramientas = _build_herramientas(ctx, holder)
    tools = list(herramientas)  # callables → AFC (el SDK las ejecuta solo)
    if GEMINI_SEARCH_ENABLED:
        # Búsqueda web nativa de Gemini (requiere facturación en AI Studio;
        # en tier gratuito devuelve 429 → por eso es opt-in vía .env)
        tools.insert(0, types.Tool(google_search=types.GoogleSearch()))
    config = types.GenerateContentConfig(
        system_instruction=_build_system_prompt(),
        tools=tools,
    )

    modelos_a_probar = MODELOS_CONFIG.get(modelo_elegido, MODELOS_CONFIG[DEFAULT_MODELO])
    ultimo_error: Optional[Exception] = None
    motivo_cuota_diaria: Optional[str] = None
    necesita_tool = _requiere_herramienta(pregunta, ctx)

    # Memoria conversacional: últimas 6 interacciones (12 mensajes) en texto plano
    hist_prev = st.session_state.get("ia_gemini_hist", [])[-12:]
    history = [
        types.Content(
            role=("user" if m.get("role") == "user" else "model"),
            parts=[types.Part.from_text(text=m.get("text", ""))],
        )
        for m in hist_prev if m.get("text")
    ]

    for modelo in modelos_a_probar:
        resp2 = None  # reiniciar en cada modelo de la cascada (bug locals)
        holder["titulo"] = "Agente StockLab"
        holder["df"] = None
        holder["herramientas"] = []
        holder["fuentes_web"] = []
        holder["ultimo_ticker"] = None
        try:
            chat = client.chats.create(model=modelo, config=config, history=history)
            resp = chat.send_message(pregunta)
            texto = (resp.text or "").strip()
            if not texto:
                raise ValueError("respuesta vacía del modelo")

            # Solo reintentar con recordatorio si la pregunta requería datos reales
            # (evita desperdiciar una 2.ª llamada de API en preguntas conceptuales).
            if necesita_tool and not holder["herramientas"] and holder["df"] is None:
                recordatorio = (
                    f"Recuerda: para responder a '{pregunta}' debes llamar a "
                    f"una de las herramientas disponibles en lugar de responder "
                    f"de memoria. Hazlo ahora."
                )
                try:
                    resp2 = chat.send_message(recordatorio)
                    texto2 = (resp2.text or "").strip()
                    if texto2 and (holder["herramientas"] or holder["df"] is not None):
                        texto = texto2
                except Exception:
                    pass  # si el reintento falla, usamos la primera respuesta

            # Una respuesta válida (aunque sin herramienta) NO degrada el agente
            st.session_state["ia_agente_fallback"] = False

            # Fuentes combinadas: grounding de Google + enlaces reales de RSS/prensa
            fuentes = _extraer_fuentes(resp)
            if resp2 is not None:
                fuentes = fuentes or _extraer_fuentes(resp2)
            for fw in holder.get("fuentes_web", []):
                if fw not in fuentes:
                    fuentes.append(fw)
            fuentes = fuentes[:5]
            if fuentes and "**Fuentes:**" not in texto:
                texto += "\n\n**Fuentes:**\n" + "\n".join(fuentes)

            # Memoria conversacional: guardar interacción (máx. 12 mensajes)
            hist = st.session_state.get("ia_gemini_hist", [])
            hist.append({"role": "user", "text": pregunta})
            hist.append({"role": "model", "text": texto})
            st.session_state["ia_gemini_hist"] = hist[-12:]

            # Registrar 1 fila por llamada real enviada a la API de Gemini
            p1, r1, t1 = _extraer_tokens_usage(resp, pregunta + texto)
            registrar_consumo(
                modelo=modelo_elegido,
                prompt_tokens=p1,
                resp_tokens=r1,
                total_tokens=t1,
            )
            if resp2 is not None:
                p2, r2, t2 = _extraer_tokens_usage(resp2, texto)
                registrar_consumo(
                    modelo=modelo_elegido,
                    prompt_tokens=p2,
                    resp_tokens=r2,
                    total_tokens=t2,
                )

            tools_usadas = list(dict.fromkeys(holder.get("herramientas", [])))
            ultimo_tk = holder.get("ultimo_ticker") or (
                st.session_state.get("ia_ultimo_ticker") if hasattr(st, "session_state") else None
            )
            return {
                "titulo": holder["titulo"],
                "texto": texto,
                "df": holder["df"],
                "modelo": modelo_elegido,
                "herramientas": tools_usadas,
                "sugerencias": _sugerencias_seguimiento(tools_usadas, ultimo_tk, ctx),
            }
        except Exception as e:  # modelo no disponible, cuota, red...
            ultimo_error = e
            if _es_cuota_diaria(str(e)):
                motivo_cuota_diaria = str(e)
            continue

    # Marcar el modelo como agotado solo si agotó toda la cascada sin respuesta válida
    if motivo_cuota_diaria is not None:
        marcar_cuota_agotada(modelo_elegido, motivo_cuota_diaria)

    raise ultimo_error or RuntimeError(f"sin modelos disponibles para {modelo_elegido}")


def get_default_modelo() -> str:
    """Devuelve el primer modelo disponible que no tenga la cuota agotada."""
    for m in ["Gemini 3.5 Flash Lite", "Gemini 3.8 Flash", "Gemini 3.7 Flash", "Gemini 3.1 Pro", "Gemini 2.5 Pro"]:
        if not es_cuota_agotada(m):
            return m
    return "Gemini 3.5 Flash Lite"


# ═══════════════════════════════════════════════════════════════════
# 🚦 PUNTO DE ENTRADA (misma firma que assistant_engine.responder)
# ═══════════════════════════════════════════════════════════════════

def obtener_modelos_alternativos(modelo_excluido: str) -> List[str]:
    """Devuelve los modelos disponibles cuya cuota diaria aún no está agotada."""
    return [m for m in MODELOS_DISPONIBLES if m != modelo_excluido and not es_cuota_agotada(m)]


def _texto_alternativos_md(modelo_excluido: str) -> str:
    alts = obtener_modelos_alternativos(modelo_excluido)
    if not alts:
        return "Todos los modelos en la nube han alcanzado su cuota diaria hoy; el motor local seguirá respondiendo."
    partes = []
    for m in alts[:3]:
        lim = LIMITES_DIARIOS.get(m, {}).get("requests", 20)
        partes.append(f"**{m}** ({lim} consultas/día)" if lim >= 100 else f"**{m}**")
    return "Para seguir usando IA en la nube hoy, selecciona " + " o ".join(partes) + "."


def _enriquecer_local(resp: Dict, ctx: Dict) -> Dict:
    """Añade fuentes web (si existen) y sugerencias de seguimiento contextuales al fallback local."""
    fuentes = resp.get("fuentes") or []
    if fuentes and "**Fuentes:**" not in resp.get("texto", ""):
        resp["texto"] = resp.get("texto", "") + "\n\n**Fuentes:**\n" + "\n".join(fuentes[:5])
    ultimo_tk = st.session_state.get("ia_ultimo_ticker") if hasattr(st, "session_state") else None
    tit_l = (resp.get("titulo") or "").lower()
    inferred = []
    if "proyección" in tit_l or "proyeccion" in tit_l:
        inferred = ["proyeccion_empresa"]
    elif "riesgo" in tit_l:
        inferred = ["riesgo_empresa"]
    elif "sector" in tit_l:
        inferred = ["resumen_sector"]
    elif "cartera" in tit_l:
        inferred = ["mi_cartera"]
    elif "watchlist" in tit_l:
        inferred = ["mi_watchlist"]
    elif "noticias" in tit_l or "actualidad" in tit_l:
        inferred = ["buscar_actualidad_web"]
    elif ultimo_tk:
        inferred = ["ficha_empresa"]
    resp.setdefault("herramientas", ["motor_local"])
    resp.setdefault("sugerencias", _sugerencias_seguimiento(inferred, ultimo_tk, ctx))
    return resp


def responder(pregunta: str, ctx: Dict, modelo_elegido: str = DEFAULT_MODELO) -> Dict:
    """
    Agente real con el modelo de Gemini seleccionado por el usuario.
    En cualquier fallo crítico degrada transparentemente al asistente de reglas.
    """
    if not disponible():
        resp = _enriquecer_local(assistant_engine.responder(pregunta, ctx), ctx)
        resp["modelo"] = "Motor Local"
        return resp

    if es_cuota_agotada(modelo_elegido):
        resp = _enriquecer_local(assistant_engine.responder(pregunta, ctx), ctx)
        resp["modelo"] = "Motor Local (Cuota agotada)"
        resp["texto"] = (
            resp["texto"]
            + f"\n\n> ⚠️ *La cuota diaria de Google para **{modelo_elegido}** ha sido alcanzada (se reinicia a las 09:00 AM). "
            f"Esta respuesta ha sido generada por el motor local de StockLab. "
            f"{_texto_alternativos_md(modelo_elegido)}*"
        )
        return resp

    try:
        return _responder_gemini(pregunta, ctx, modelo_elegido=modelo_elegido)
    except Exception as e:
        st.session_state["ia_agente_fallback"] = True
        if _es_cuota_diaria(str(e)):
            marcar_cuota_agotada(modelo_elegido, str(e))

        resp = _enriquecer_local(assistant_engine.responder(pregunta, ctx), ctx)
        if es_cuota_agotada(modelo_elegido):
            resp["modelo"] = "Motor Local (Cuota agotada)"
            resp["texto"] = (
                resp["texto"]
                + f"\n\n> ⚠️ *La cuota diaria de Google para **{modelo_elegido}** ha sido alcanzada (se reinicia a las 09:00 AM). "
                f"Esta respuesta ha sido generada por el motor local de StockLab. "
                f"{_texto_alternativos_md(modelo_elegido)}*"
            )
        elif _es_429(str(e)):
            # 429 transitorio (límite por minuto): no bloqueamos el modelo,
            # solo avisamos de que reintente en ~1 minuto
            resp["modelo"] = "Motor Local (IA saturada)"
            resp["texto"] = (
                resp["texto"]
                + f"\n\n> ⏱️ *El servicio de IA está saturado puntualmente "
                f"(límite de peticiones por minuto de **{modelo_elegido}**). "
                f"Esta respuesta es del motor local. **Reintenta en ~1 minuto** "
                f"o cambia de modelo en el selector superior.*"
            )
        else:
            resp["modelo"] = "Motor Local"
        return resp
