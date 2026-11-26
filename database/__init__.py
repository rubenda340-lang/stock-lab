# Módulo de base de datos de StockLab
from database.connection import get_connection, init_db, query_all, query_val
from database.watchlist import (
    agregar_a_watchlist,
    actualizar_watchlist,
    eliminar_de_watchlist,
    obtener_watchlist,
    existe_en_watchlist
)
from database.preferencias import (
    obtener_preferencias_alertas,
    guardar_preferencias_alertas,
    DEFAULT_PREFERENCIAS_ALERTAS,
)
from database.cartera import (
    agregar_operacion,
    obtener_operaciones,
    actualizar_operacion,
    eliminar_operacion,
    calcular_posiciones_abiertas,
    obtener_resumen_cartera,
    obtener_empresas_disponibles,
    obtener_empresa_por_ticker,
    # Funciones V2.0 optimizadas con filtro por tipo de cartera
    calcular_liquidez,
    calcular_liquidez_filtrada,
    calcular_capital_invertido,
    calcular_patrimonio_total,
    calcular_patrimonio_total_filtrado,
    calcular_desglose_resultados_filtrado,
    calcular_posiciones_abiertas_filtradas,
    obtener_historial_patrimonio,
    obtener_distribucion_sectores_filtrada,
    obtener_distribucion_empresas_filtrada,
    obtener_precios_actuales_batch
)
