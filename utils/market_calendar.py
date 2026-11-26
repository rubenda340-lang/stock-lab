"""
Calendario de Negociación Bursátil — BME / Mercado Continuo Español
===================================================================
Gestión de días hábiles bursátiles y cálculo de festivos del sistema TARGET2 / BME.

Festivos de negociación oficiales de las bolsas españolas (BME):
  - 1 de enero (Año Nuevo)
  - Viernes Santo (Good Friday - variable según Pascua)
  - Lunes de Pascua (Easter Monday - variable según Pascua)
  - 1 de mayo (Día del Trabajador)
  - 25 de diciembre (Navidad)
  - 26 de diciembre (San Esteban / Boxing Day)
"""

from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Union
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


def _calcular_pascua(year: int) -> date:
    """Calcula el Domingo de Resurrección para un año dado (algoritmo anónimo de Butcher/Meeus)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, mes, dia)


def festivos_bme_rango(desde_year: int = 2024, hasta_year: int = 2030) -> List[str]:
    """Devuelve la lista de fechas (ISO 'YYYY-MM-DD') de festivos bursátiles oficiales de BME."""
    festivos = []
    for y in range(desde_year, hasta_year + 1):
        festivos.extend([
            f"{y}-01-01",
            f"{y}-05-01",
            f"{y}-12-25",
            f"{y}-12-26",
        ])
        pascua = _calcular_pascua(y)
        viernes_santo = pascua - timedelta(days=2)
        lunes_pascua = pascua + timedelta(days=1)
        festivos.append(viernes_santo.isoformat())
        festivos.append(lunes_pascua.isoformat())
    return sorted(festivos)


def fecha_habil_bme(dias: int, desde: Optional[date] = None) -> str:
    """
    Calcula la fecha objetivo sumando `dias` sesiones hábiles bursátiles a partir de `desde`,
    omitiendo fines de semana y festivos oficiales de BME. Retorna cadena ISO 'YYYY-MM-DD'.

    Regla:
      - Si `desde` es día hábil: se suman `dias` sesiones hacia adelante.
      - Si `desde` es fin de semana o festivo: la primera sesión hábil futura (ej. lunes)
        es ya la sesión 1, por lo que el objetivo a 1 día es ese mismo lunes.
    """
    d_obj = desde or date.today()
    base = np.datetime64(d_obj.isoformat())
    festivos = festivos_bme_rango(d_obj.year - 1, d_obj.year + 2)
    holidays_arr = np.array(festivos, dtype="datetime64[D]")

    es_habil = bool(np.is_busday(base, holidays=holidays_arr))
    if es_habil:
        return str(np.busday_offset(base, dias, roll="forward", holidays=holidays_arr))
    else:
        # Próxima sesión hábil (ej. lunes si hoy es sábado/domingo)
        prox_habil = np.busday_offset(base, 0, roll="forward", holidays=holidays_arr)
        # Para 1 sesión es esa misma prox_habil (offset 0), para 2 es prox_habil + 1, etc.
        offset = max(0, dias - 1)
        return str(np.busday_offset(prox_habil, offset, roll="forward", holidays=holidays_arr))


def bdate_range_bme(start, periods: int) -> pd.DatetimeIndex:
    """
    Genera un rango de fechas de sesiones bursátiles BME excluyendo fines de semana y festivos.
    """
    start_ts = pd.Timestamp(start)
    festivos = set(festivos_bme_rango(start_ts.year - 1, start_ts.year + 2))
    fechas: List[pd.Timestamp] = []
    curr = start_ts
    while len(fechas) < periods:
        if curr.weekday() < 5 and curr.strftime("%Y-%m-%d") not in festivos:
            fechas.append(curr)
        curr += pd.Timedelta(days=1)
    return pd.DatetimeIndex(fechas)


HORA_CORTE_BME = 18
MINUTO_CORTE_BME = 30
HORA_LIMITE_EMISION = 8
MINUTO_LIMITE_EMISION = 30


def get_ahora_madrid() -> datetime:
    """Devuelve la fecha/hora actual en la zona horaria de Madrid (CET/CEST)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Madrid"))
    except Exception:
        ahora_utc = datetime.now(timezone.utc)
        return ahora_utc + timedelta(hours=2)


def sesion_cerrada_hoy() -> bool:
    """
    Retorna True si la sesión bursátil del mercado continuo español (BME) de hoy
    ya ha concluido (o si hoy no hay mercado por ser fin de semana / festivo).
    El cierre oficial con subasta concluye a las 17:35 CET/CEST.
    """
    ahora_madrid = get_ahora_madrid()
    d_hoy = ahora_madrid.date()
    base = np.datetime64(d_hoy.isoformat())
    festivos = festivos_bme_rango(d_hoy.year - 1, d_hoy.year + 2)
    holidays_arr = np.array(festivos, dtype="datetime64[D]")

    # Si hoy es fin de semana o festivo, no hay sesión activa
    if not np.is_busday(base, holidays=holidays_arr):
        return True

    return (ahora_madrid.hour > 17) or (ahora_madrid.hour == 17 and ahora_madrid.minute >= 35)


def barra_hoy_completa() -> bool:
    """
    Retorna True si la barra diaria de hoy ya puede considerarse completa y consolidada
    (mercado cerrado + subasta finalizada + margen de Yahoo Finance >= 18:30 CET).
    """
    if not sesion_cerrada_hoy():
        return False
    ahora = get_ahora_madrid()
    # Si es fin de semana o festivo, no hay sesión en curso
    d_hoy = ahora.date()
    base = np.datetime64(d_hoy.isoformat())
    festivos = festivos_bme_rango(d_hoy.year - 1, d_hoy.year + 2)
    holidays_arr = np.array(festivos, dtype="datetime64[D]")
    if not np.is_busday(base, holidays=holidays_arr):
        return True

    return (ahora.hour > HORA_CORTE_BME) or (ahora.hour == HORA_CORTE_BME and ahora.minute >= MINUTO_CORTE_BME)


def es_fecha_resoluble(fecha_objetivo_iso: str) -> bool:
    """
    Determina si una predicción con fecha_objetivo dada puede liquidarse por vencimiento.
    - fecha_objetivo < hoy: SÍ (sesión ya cerrada en días previos).
    - fecha_objetivo == hoy: SÍ sólo si la sesión de hoy ya ha cerrado (>= 17:35 CET o festivo).
    - fecha_objetivo > hoy: NO (aún no ha llegado su vencimiento).
    """
    hoy_iso = date.today().isoformat()
    if fecha_objetivo_iso < hoy_iso:
        return True
    if fecha_objetivo_iso == hoy_iso:
        return sesion_cerrada_hoy()
    return False


def limite_emision(fecha_sesion: Union[str, date, datetime]) -> datetime:
    """
    Retorna el instante límite oficial (08:30 Europe/Madrid) de la siguiente sesión
    hábil bursátil de BME tras `fecha_sesion`.
    """
    if isinstance(fecha_sesion, datetime):
        d_sesion = fecha_sesion.date()
    elif isinstance(fecha_sesion, str):
        d_sesion = date.fromisoformat(fecha_sesion[:10])
    elif isinstance(fecha_sesion, date):
        d_sesion = fecha_sesion
    else:
        raise TypeError(f"fecha_sesion debe ser str, date o datetime, recibido {type(fecha_sesion)}")

    sig_habil_iso = fecha_habil_bme(1, desde=d_sesion)
    sig_date = date.fromisoformat(sig_habil_iso)
    return datetime(
        sig_date.year,
        sig_date.month,
        sig_date.day,
        HORA_LIMITE_EMISION,
        MINUTO_LIMITE_EMISION,
        0,
        tzinfo=ZoneInfo("Europe/Madrid"),
    )


def en_ventana_emision(
    fecha_sesion: Union[str, date, datetime],
    ahora: Optional[datetime] = None,
) -> bool:
    """
    Retorna True si `ahora` se encuentra dentro de la ventana de emisión válida
    para la sesión `fecha_sesion`:
    [fecha_sesion 18:30 Europe/Madrid, siguiente sesión hábil 08:30 Europe/Madrid).

    Si `ahora` es None, se utiliza get_ahora_madrid().
    Si `ahora` se pasa como naive (sin tzinfo), lanza ValueError.
    """
    madrid_tz = ZoneInfo("Europe/Madrid")

    if ahora is None:
        ahora_madrid = get_ahora_madrid()
    else:
        if getattr(ahora, "tzinfo", None) is None:
            raise ValueError("ahora debe ser un datetime con zona horaria (tz-aware)")
        ahora_madrid = ahora.astimezone(madrid_tz)

    if isinstance(fecha_sesion, datetime):
        d_sesion = fecha_sesion.date()
    elif isinstance(fecha_sesion, str):
        d_sesion = date.fromisoformat(fecha_sesion[:10])
    elif isinstance(fecha_sesion, date):
        d_sesion = fecha_sesion
    else:
        raise TypeError(f"fecha_sesion debe ser str, date o datetime, recibido {type(fecha_sesion)}")

    inicio_ventana = datetime(
        d_sesion.year,
        d_sesion.month,
        d_sesion.day,
        HORA_CORTE_BME,
        MINUTO_CORTE_BME,
        0,
        tzinfo=madrid_tz,
    )
    fin_ventana = limite_emision(d_sesion)

    return inicio_ventana <= ahora_madrid < fin_ventana
