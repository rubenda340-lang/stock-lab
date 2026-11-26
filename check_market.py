"""
Script de Auditoría de Mercado - StockLab
Verifica la conectividad de todos los tickers activos con Yahoo Finance.

Uso: python check_market.py
"""

import sqlite3
import yfinance as yf
import pandas as pd
from pathlib import Path


def obtener_tickers_activos():
    """Obtiene la lista de tickers activos desde la base de datos."""
    db_path = Path(__file__).parent / "stocklab.db"
    
    if not db_path.exists():
        print(f"❌ Error: Base de datos no encontrada en {db_path}")
        return []
    
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT ticker, nombre FROM empresas WHERE activa = 1 ORDER BY nombre")
        resultados = cursor.fetchall()
        conn.close()
        return resultados
    except Exception as e:
        print(f"❌ Error al conectar con la base de datos: {e}")
        return []


def verificar_ticker(ticker, nombre):
    """
    Verifica si un ticker tiene datos válidos en Yahoo Finance.
    Retorna True si tiene datos, False si está vacío o falla.
    """
    try:
        # Descarga ligera de prueba: 5 días, intervalo diario
        df = yf.download(ticker, period='5d', interval='1d', progress=False)
        
        # 🔧 Aplanar columnas MultiIndex si Yahoo las devuelve agrupadas
        # Si las columnas son MultiIndex (ej: ('Close', 'ACS.MC')), nos quedamos solo con el nivel de la métrica
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        
        # Verificación estricta de datos válidos
        if df.empty or 'Close' not in df.columns or df['Close'].isnull().all():
            return False
        
        # Verificar que haya al menos un dato de cierre válido
        precios_validos = df['Close'].dropna()
        return len(precios_validos) > 0
        
    except Exception as e:
        print(f"🔴 [FALLO] {ticker} - Error interno: {str(e)}")
        return False


def main():
    """Función principal de auditoría."""
    print("=" * 70)
    print("🔍 AUDITORÍA DE CONECTIVIDAD - STOCKLAB MARKET DATA")
    print("=" * 70)
    print()
    
    # Paso 1: Obtener tickers activos
    print("📡 Conectando con la base de datos...")
    tickers_activos = obtener_tickers_activos()
    
    if not tickers_activos:
        print("❌ No se encontraron tickers activos en la base de datos.")
        return
    
    total_empresas = len(tickers_activos)
    print(f"✅ {total_empresas} empresas activas encontradas.\n")
    print("-" * 70)
    print("Iniciando verificación de conectividad con Yahoo Finance...\n")
    
    # Paso 2: Auditoría en bloque
    exitosos = 0
    fallidos = 0
    fallos_detalle = []
    
    for idx, (ticker, nombre) in enumerate(tickers_activos, 1):
        # Mostrar progreso
        progreso = f"[{idx}/{total_empresas}]"
        
        # Verificar ticker
        tiene_datos = verificar_ticker(ticker, nombre)
        
        if tiene_datos:
            exitosos += 1
            print(f"🟢 {progreso} [OK] {ticker} - {nombre}: Conexión exitosa.")
        else:
            fallidos += 1
            fallos_detalle.append((ticker, nombre))
            print(f"🔴 {progreso} [FALLO] {ticker} - {nombre}: Sin datos en Yahoo.")
    
    # Paso 3: Informe estadístico final
    print("\n" + "=" * 70)
    print("📊 INFORME ESTADÍSTICO FINAL")
    print("=" * 70)
    print()
    
    porcentaje_exito = (exitosos / total_empresas * 100) if total_empresas > 0 else 0
    
    print(f"📈 Total de empresas auditadas: {total_empresas}")
    print(f"✅ Conexiones exitosas: {exitosos}")
    print(f"❌ Fallos de conexión: {fallidos}")
    print(f"📊 Porcentaje de éxito: {porcentaje_exito:.1f}%")
    print()
    
    if fallos_detalle:
        print("⚠️  TICKERS CON PROBLEMAS:")
        print("-" * 70)
        for ticker, nombre in fallos_detalle:
            print(f"   • {ticker} - {nombre}")
        print()
    
    # Mensaje final según resultado
    if fallidos == 0:
        print(f"🎉 ¡Catálogo auditado: {exitosos}/{total_empresas} empresas operativas al {porcentaje_exito:.0f}%!")
    elif porcentaje_exito >= 90:
        print(f"⚠️  Catálogo auditado: {exitosos}/{total_empresas} empresas operativas ({porcentaje_exito:.0f}% de éxito)")
        print(f"   Se recomienda revisar los {fallidos} tickers con problemas.")
    else:
        print(f"🚨 Catálogo auditado: {exitosos}/{total_empresas} empresas operativas ({porcentaje_exito:.0f}% de éxito)")
        print(f"   Atención: {fallidos} tickers requieren revisión urgente.")
    
    print("=" * 70)


if __name__ == "__main__":
    main()
