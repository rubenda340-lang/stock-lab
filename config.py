"""
Configuración Centralizada — StockLab
======================================

Single Source of Truth para todas las variables de entorno y credenciales.
Carga automáticamente desde .env usando python-dotenv.

Uso:
    from config import FMP_API_KEY, FINNHUB_API_KEY

Añadir nuevas APIs:
    1. Añade la variable en .env (ej: ALPHA_VANTAGE_KEY=xxx)
    2. Declárala aquí con get_env("ALPHA_VANTAGE_KEY")
    3. Importa desde tu módulo: from config import ALPHA_VANTAGE_KEY
"""

import os
from dotenv import load_dotenv

# Cargar variables desde .env (si existe)
# override=False respeta variables de entorno del sistema si ya están definidas
load_dotenv(override=False)


def get_env(key: str, default: str = "") -> str:
    """
    Obtiene una variable de entorno de forma segura.

    Parameters
    ----------
    key : str
        Nombre de la variable de entorno.
    default : str
        Valor por defecto si la variable no existe.

    Returns
    -------
    str
        Valor de la variable o default. Nunca retorna None.
    """
    value = os.environ.get(key, default)
    return value.strip() if value else default


# ═══════════════════════════════════════════════════════════════════
# 🔑 API KEYS — Servicios Externos
# ═══════════════════════════════════════════════════════════════════

# Financial Modeling Prep (FMP)
# Endpoint: https://financialmodelingprep.com/api/v3/ratios/{ticker}
# Plan free: 250 req/día | Docs: https://site.financialmodelingprep.com/developer/docs
FMP_API_KEY: str = get_env("STOCKLAB_FMP_API_KEY")

# Finnhub
# Endpoint: https://finnhub.io/api/v1/stock/metric
# Plan free: 60 req/min | Docs: https://finnhub.io/docs/api
FINNHUB_API_KEY: str = get_env("STOCKLAB_FINNHUB_API_KEY")

# Google Gemini (agente IA del asistente StockLab)
# Endpoint: SDK google-genai (function calling)
# Plan free: 20-500 req/día según modelo | Key: https://aistudio.google.com/apikey
GEMINI_API_KEY: str = get_env("STOCKLAB_GEMINI_API_KEY")

# Google Search grounding (búsqueda web nativa de Gemini)
# Requiere facturación habilitada en el proyecto de AI Studio; en tier
# gratuito devuelve 429. Poner STOCKLAB_GEMINI_SEARCH=1 para activarlo.
GEMINI_SEARCH_ENABLED: bool = get_env("STOCKLAB_GEMINI_SEARCH", "0").strip().lower() in ("1", "true", "yes")


# ═══════════════════════════════════════════════════════════════════
# 📋 ESTADO DE CONFIGURACIÓN — Diagnóstico rápido
# ═══════════════════════════════════════════════════════════════════

def verificar_configuracion() -> dict:
    """
    Retorna el estado de todas las API Keys configuradas.
    Útil para diagnóstico en la vista de Configuración.

    Returns
    -------
    dict
        {nombre_key: bool} indicando si cada clave está configurada.
    """
    return {
        "FMP_API_KEY": bool(FMP_API_KEY),
        "FINNHUB_API_KEY": bool(FINNHUB_API_KEY),
        "GEMINI_API_KEY": bool(GEMINI_API_KEY),
    }


def obtener_keys_faltantes() -> list[str]:
    """
    Retorna lista de nombres de keys que NO están configuradas.

    Returns
    -------
    list[str]
        Nombres de variables de entorno faltantes.
    """
    estado = verificar_configuracion()
    return [nombre for nombre, configurada in estado.items() if not configurada]
