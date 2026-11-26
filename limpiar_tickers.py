"""
Script de limpieza para eliminar tickers erróneos y duplicados de la base de datos.

Este script elimina los siguientes tickers obsoletos que causan errores en Yahoo Finance:
- FLUI.MC (reemplazado por FDR.MC - Fluidra)
- REDE.MC (reemplazado por RED.MC - Redeia)

Además, verifica el conteo total de empresas del IBEX 35 para asegurar que sean exactamente 35.
"""

import sqlite3
import os

# Ruta a la base de datos
DB_PATH = os.path.join(os.path.dirname(__file__), "stocklab.db")


def limpiar_tickers_erroneos():
    """Elimina los tickers erróneos de la base de datos."""
    
    if not os.path.exists(DB_PATH):
        print(f"❌ Base de datos no encontrada en: {DB_PATH}")
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # Tickers a eliminar (obsoletos/erróneos)
        tickers_a_eliminar = ["FLUI.MC", "REDE.MC"]
        
        print("🔍 Verificando tickers erróneos en la base de datos...")
        
        for ticker in tickers_a_eliminar:
            # Verificar si existe
            cursor.execute("SELECT id, nombre FROM empresas WHERE ticker = ?", (ticker,))
            resultado = cursor.fetchone()
            
            if resultado:
                empresa_id, nombre = resultado
                print(f"  ⚠️  Encontrado ticker erróneo: {ticker} ({nombre}) - ID: {empresa_id}")
                
                # Verificar si hay operaciones en cartera vinculadas
                cursor.execute("SELECT COUNT(*) FROM cartera WHERE empresa_id = ?", (empresa_id,))
                ops_count = cursor.fetchone()[0]
                
                if ops_count > 0:
                    print(f"     ⚠️  ADVERTENCIA: Hay {ops_count} operación(es) en cartera vinculada(s)")
                    print(f"     Se recomienda revisar manualmente antes de eliminar.")
                    respuesta = input("     ¿Desea continuar con la eliminación? (s/n): ").strip().lower()
                    if respuesta != 's':
                        print(f"     ❌ Eliminación cancelada para {ticker}")
                        continue
                
                # Eliminar el ticker erróneo
                cursor.execute("DELETE FROM empresas WHERE ticker = ?", (ticker,))
                print(f"     ✅ Ticker {ticker} eliminado correctamente")
            else:
                print(f"  ✓ Ticker {ticker} no encontrado (ya fue eliminado o nunca existió)")
        
        # Verificar conteo del IBEX 35
        cursor.execute("SELECT COUNT(*) FROM empresas WHERE es_ibex35 = 1")
        ibex_count = cursor.fetchone()[0]
        print(f"\n📊 Conteo actual del IBEX 35: {ibex_count} empresas")
        
        if ibex_count == 35:
            print("   ✅ El IBEX 35 tiene exactamente 35 componentes")
        elif ibex_count < 35:
            print(f"   ⚠️  El IBEX 35 tiene {ibex_count} componentes (faltan {35 - ibex_count})")
        else:
            print(f"   ⚠️  El IBEX 35 tiene {ibex_count} componentes (sobran {ibex_count - 35})")
        
        # Verificar conteo total
        cursor.execute("SELECT COUNT(*) FROM empresas")
        total_count = cursor.fetchone()[0]
        print(f"\n📈 Total de empresas en catálogo: {total_count}")
        
        # Confirmar cambios
        conn.commit()
        print("\n✅ Limpieza completada exitosamente")
        
    except Exception as e:
        conn.rollback()
        print(f"\n❌ Error durante la limpieza: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    print("=" * 60)
    print("🧹 STOCKLAB - Script de Limpieza de Tickers Erróneos")
    print("=" * 60)
    print()
    limpiar_tickers_erroneos()
