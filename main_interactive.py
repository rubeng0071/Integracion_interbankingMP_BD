#!/usr/bin/env python3
"""
Script principal interactivo para explorar datos de Interbanking
"""

from shared.interbanking_client import InterbankingClient
from datetime import datetime, timedelta
import pandas as pd
import sys

def mostrar_menu():
    """Muestra el menú principal"""
    print("\n🏦 INTERBANKING DATA EXPLORER")
    print("=" * 40)
    print("1. 📊 Ver todas las cuentas")
    print("2. 💰 Consultar saldos")
    print("3. 📈 Ver movimientos")
    print("4. 🔄 Ver transferencias")
    print("5. 📋 Ver extractos")
    print("6. 📊 Exportar todo a Excel")
    print("7. 🔍 Probar conexión")
    print("8. 🧪 Probar disponibilidad de datos")
    print("0. ❌ Salir")
    print("-" * 40)

def solicitar_fechas():
    """Solicita rango de fechas al usuario"""
    print("\n📅 CONFIGURAR RANGO DE FECHAS:")
    print("1. Últimos 7 días")
    print("2. Últimos 30 días") 
    print("3. Mes actual")
    print("4. Septiembre 2025 (recomendado - tiene más datos)")
    print("5. Personalizado")
    print("-" * 40)
    
    while True:
        try:
            opcion = input("Selecciona una opción (1-5): ").strip()
            
            if opcion == "1":
                fecha_hasta = datetime.now()
                fecha_desde = fecha_hasta - timedelta(days=7)
                break
            elif opcion == "2":
                fecha_hasta = datetime.now()
                fecha_desde = fecha_hasta - timedelta(days=30)
                break
            elif opcion == "3":
                fecha_hasta = datetime.now()
                fecha_desde = fecha_hasta.replace(day=1)
                break
            elif opcion == "4":
                fecha_desde = datetime(2025, 9, 1)
                fecha_hasta = datetime(2025, 9, 30)
                break
            elif opcion == "5":
                print("\n📝 Ingresa las fechas (formato: YYYY-MM-DD)")
                fecha_desde_str = input("Fecha desde: ").strip()
                fecha_hasta_str = input("Fecha hasta: ").strip()
                fecha_desde = datetime.strptime(fecha_desde_str, '%Y-%m-%d')
                fecha_hasta = datetime.strptime(fecha_hasta_str, '%Y-%m-%d')
                
                if fecha_desde > fecha_hasta:
                    print("❌ La fecha desde no puede ser mayor que la fecha hasta")
                    continue
                break
            else:
                print("❌ Opción inválida. Selecciona 1, 2, 3, 4 o 5")
                continue
                
        except ValueError as e:
            print(f"❌ Error en formato de fecha: {e}")
            print("💡 Usa el formato YYYY-MM-DD (ej: 2025-09-01)")
            continue
        except (EOFError, KeyboardInterrupt):
            print("\n⚠️ Usando últimos 7 días por defecto")
            fecha_hasta = datetime.now()
            fecha_desde = fecha_hasta - timedelta(days=7)
            break
        except Exception as e:
            print(f"❌ Error: {e}")
            continue
    
    fecha_desde_str = fecha_desde.strftime('%Y-%m-%d')
    fecha_hasta_str = fecha_hasta.strftime('%Y-%m-%d')
    
    print(f"✅ Rango seleccionado: {fecha_desde_str} a {fecha_hasta_str}")
    return fecha_desde_str, fecha_hasta_str

def seleccionar_cuenta(cuentas_df):
    """Permite seleccionar una cuenta"""
    if cuentas_df.empty:
        print("❌ No hay cuentas disponibles")
        return None
    
    print("\n📋 Cuentas disponibles:")
    print("-" * 80)
    for idx, cuenta in cuentas_df.iterrows():
        print(f"{idx + 1:2d}. {cuenta['account_number']} - {cuenta['bank_name']} ({cuenta['currency']})")
    print(f" 0. Todas las cuentas")
    print("-" * 80)
    
    while True:
        try:
            entrada = input("Selecciona una cuenta (0 para todas): ").strip()
            if not entrada:
                print("❌ Por favor ingresa un número")
                continue
                
            seleccion = int(entrada)
            if seleccion == 0:
                return "todas"
            elif 1 <= seleccion <= len(cuentas_df):
                cuenta_seleccionada = cuentas_df.iloc[seleccion - 1]
                print(f"✅ Seleccionaste: {cuenta_seleccionada['account_number']} - {cuenta_seleccionada['bank_name']}")
                return cuenta_seleccionada
            else:
                print(f"❌ Selección inválida. Ingresa un número entre 0 y {len(cuentas_df)}")
                continue
        except ValueError:
            print("❌ Entrada inválida. Ingresa solo números")
            continue
        except KeyboardInterrupt:
            print("\n❌ Operación cancelada")
            return None

def ver_cuentas(client):
    """Muestra todas las cuentas"""
    print("\n📊 CONSULTANDO CUENTAS...")
    cuentas_df, general_data = client.get_cuentas()
    
    if not cuentas_df.empty:
        print(f"\n✅ {len(cuentas_df)} cuentas encontradas:")
        print("-" * 100)
        for idx, cuenta in cuentas_df.iterrows():
            print(f"{idx + 1:2d}. {cuenta['account_number']:20} | {cuenta['bank_name']:15} | {cuenta['currency']:3} | {cuenta['account_type']:2}")
        print("-" * 100)
        return cuentas_df
    else:
        print("❌ No se encontraron cuentas")
        return pd.DataFrame()

def ver_saldos(client, cuentas_df):
    """Consulta saldos"""
    print("\n💰 CONSULTANDO SALDOS...")
    
    try:
        cuenta_seleccionada = seleccionar_cuenta(cuentas_df)
        if cuenta_seleccionada is None:
            return
        
        print("\n📅 Selecciona rango de fechas:")
        print("1. Solo saldos actuales")
        print("2. Últimos 7 días (con histórico)")
        print("3. Últimos 30 días (con histórico)")
        
        opcion_fecha = input("Selecciona una opción (1-3): ").strip()
        
        if opcion_fecha == "1":
            # Solo saldos actuales, sin fechas
            fecha_desde = None
            fecha_hasta = None
        else:
            if opcion_fecha == "3":
                dias = 30
            else:
                dias = 7
            fecha_hasta = datetime.now()
            fecha_desde = fecha_hasta - timedelta(days=dias)
            fecha_desde = fecha_desde.strftime('%Y-%m-%d')
            fecha_hasta = fecha_hasta.strftime('%Y-%m-%d')
        
        print(f"\n🔍 Consultando saldos...")
        
        if isinstance(cuenta_seleccionada, str) and cuenta_seleccionada == "todas":
            saldos_df, general_data = client.get_saldos(date_since=fecha_desde, date_until=fecha_hasta)
        else:
            saldos_df, general_data = client.get_saldos(
                account_numbers=[cuenta_seleccionada['account_number']],
                date_since=fecha_desde, 
                date_until=fecha_hasta
            )
        
        if not saldos_df.empty:
            print(f"\n✅ {len(saldos_df)} registros de saldos encontrados")
            
            # Mostrar saldos actuales (no históricos)
            # Usar .fillna() para manejar valores NaN y luego filtrar
            saldos_actuales = saldos_df[saldos_df['is_historical'].fillna(False) == False]
            
            if not saldos_actuales.empty:
                print("\n💰 SALDOS ACTUALES:")
                print("=" * 80)
                for _, saldo in saldos_actuales.iterrows():
                    print(f"🏦 Cuenta: {saldo['account_number']} - {saldo.get('account_label', 'N/A')}")
                    print(f"   Banco: {saldo.get('bank_number', 'N/A')}")
                    print(f"   💵 Saldo contable: ${saldo.get('countable_balance', 0):,.2f}")
                    print(f"   🏦 Saldo operativo inicial: ${saldo.get('initial_operating_balance', 0):,.2f}")
                    print(f"   💰 Saldo operativo actual: ${saldo.get('current_operating_balance', 0):,.2f}")
                    print(f"   📈 Proyección 24hs: ${saldo.get('projected_balance_24hs', 0):,.2f}")
                    print(f"   📊 Proyección 48hs: ${saldo.get('projected_balance_48hs', 0):,.2f}")
                    print("-" * 80)
            
            # Mostrar históricos si los hay
            saldos_historicos = saldos_df[saldos_df['is_historical'].fillna(False) == True]
            if not saldos_historicos.empty and opcion_fecha != "1":
                print(f"\n📊 SALDOS HISTÓRICOS ({len(saldos_historicos)} registros):")
                print("=" * 80)
                for _, saldo in saldos_historicos.head(10).iterrows():  # Solo primeros 10
                    print(f"📅 {saldo.get('operation_date', 'N/A')} - Cuenta: {saldo['account_number']}")
                    print(f"   💰 Saldo del día: ${saldo.get('day_balance', 0):,.2f}")
                    print(f"   📉 Total débitos: ${saldo.get('total_debits', 0):,.2f}")
                    print(f"   📈 Total créditos: ${saldo.get('total_credits', 0):,.2f}")
                    print("-" * 40)
                
                if len(saldos_historicos) > 10:
                    print(f"... y {len(saldos_historicos) - 10} registros más")
        else:
            print("❌ No se encontraron saldos para los criterios seleccionados")
            
    except Exception as e:
        print(f"❌ Error consultando saldos: {e}")
        import traceback
        traceback.print_exc()

def ver_movimientos(client, cuentas_df):
    """Consulta movimientos"""
    print("\n📈 CONSULTANDO MOVIMIENTOS...")
    
    cuenta_seleccionada = seleccionar_cuenta(cuentas_df)
    if cuenta_seleccionada is None or (isinstance(cuenta_seleccionada, str) and cuenta_seleccionada == "todas"):
        print("❌ Debes seleccionar una cuenta específica para movimientos")
        return
    
    print("\n📋 Tipo de movimientos:")
    print("1. Del día")
    print("2. Anteriores")
    print("3. Diferidos")
    print("4. ZUGHUS (solo v2)")
    
    try:
        tipo_opcion = input("Selecciona tipo (1-4): ").strip()
        tipos = {"1": "dia", "2": "anteriores", "3": "diferidos", "4": "zughus"}
        movement_type = tipos.get(tipo_opcion, "anteriores")
        
        version = "v2" if movement_type == "zughus" else "v2"  # Usar v2 por defecto
        
        fecha_desde, fecha_hasta = solicitar_fechas()
        
        movimientos_df, _ = client.get_movimientos(
            account_number=cuenta_seleccionada['account_number'],
            movement_type=movement_type,
            bank_number=cuenta_seleccionada['bank_number'],
            date_since=fecha_desde,
            date_until=fecha_hasta,
            version=version
        )
        
        if not movimientos_df.empty:
            print(f"\n✅ {len(movimientos_df)} movimientos encontrados:")
            print("\n📈 ÚLTIMOS 5 MOVIMIENTOS:")
            for _, mov in movimientos_df.head().iterrows():
                print(f"Fecha: {mov.get('process_date', 'N/A')}")
                print(f"Descripción: {mov.get('code_description_bank', 'N/A')}")
                print(f"Importe: ${mov.get('amount', 0):,.2f} ({mov.get('debit_credit_type', 'N/A')})")
                print("-" * 50)
        else:
            print("❌ No se encontraron movimientos")
            
    except Exception as e:
        print(f"❌ Error: {e}")

def ver_transferencias(client):
    """Consulta transferencias"""
    print("\n🔄 CONSULTANDO TRANSFERENCIAS...")
    
    fecha_desde, fecha_hasta = solicitar_fechas()
    
    transferencias_df, _ = client.get_transferencias_detalle(
        date_since=fecha_desde,
        date_until=fecha_hasta
    )
    
    if not transferencias_df.empty:
        print(f"\n✅ {len(transferencias_df)} transferencias encontradas:")
        print("\n🔄 ÚLTIMAS 5 TRANSFERENCIAS:")
        for _, trans in transferencias_df.head().iterrows():
            print(f"ID: {trans.get('transfer_id', 'N/A')}")
            print(f"Fecha: {trans.get('request_date', 'N/A')}")
            print(f"Importe: ${trans.get('amount', 0):,.2f} {trans.get('currency', 'N/A')}")
            print(f"Estado: {trans.get('status', 'N/A')}")
            print("-" * 50)
    else:
        print("❌ No se encontraron transferencias")

def ver_extractos(client, cuentas_df):
    """Consulta extractos"""
    print("\n📋 CONSULTANDO EXTRACTOS...")
    
    cuenta_seleccionada = seleccionar_cuenta(cuentas_df)
    if cuenta_seleccionada is None or (isinstance(cuenta_seleccionada, str) and cuenta_seleccionada == "todas"):
        print("❌ Debes seleccionar una cuenta específica para extractos")
        return
    
    fecha_desde, fecha_hasta = solicitar_fechas()
    
    print(f"\n🔍 Consultando extractos para cuenta {cuenta_seleccionada['account_number']} - {cuenta_seleccionada['bank_name']}...")
    
    extractos_df, _ = client.get_extractos(
        account_number=cuenta_seleccionada['account_number'],
        bank_number=cuenta_seleccionada['bank_number'],
        date_since=fecha_desde,
        date_until=fecha_hasta
    )
    
    if not extractos_df.empty:
        print(f"\n✅ {len(extractos_df)} registros de extractos encontrados")
        print("\n📋 RESUMEN DE EXTRACTOS:")
        print("=" * 80)
        
        # Agrupar por extracto único
        extractos_unicos = extractos_df.drop_duplicates(subset=['statement_number'])
        
        for _, ext in extractos_unicos.head(10).iterrows():  # Mostrar hasta 10
            print(f"📄 Extracto: {ext.get('statement_number', 'N/A')}")
            print(f"   📅 Fecha: {ext.get('operation_date', 'N/A')}")
            print(f"   📊 Movimientos: {ext.get('total_movements', 'N/A')}")
            print(f"   💰 Saldo inicial: ${ext.get('opening_balance', 0):,.2f}")
            print(f"   💵 Saldo final: ${ext.get('ending_balance', 0):,.2f}")
            print("-" * 40)
        
        if len(extractos_unicos) > 10:
            print(f"... y {len(extractos_unicos) - 10} extractos más")
        
        # Mostrar algunos movimientos del primer extracto
        primer_extracto_num = extractos_unicos.iloc[0]['statement_number']
        movimientos_extracto = extractos_df[extractos_df['statement_number'] == primer_extracto_num]
        
        if len(movimientos_extracto) > 1:  # Si hay movimientos detallados
            print(f"\n📈 MOVIMIENTOS DEL EXTRACTO {primer_extracto_num} (primeros 5):")
            print("=" * 80)
            for _, mov in movimientos_extracto.head(5).iterrows():
                if pd.notna(mov.get('amount')):  # Solo si tiene datos de movimiento
                    print(f"📅 {mov.get('real_date_activity', 'N/A')}")
                    print(f"   💰 ${mov.get('amount', 0):,.2f} ({mov.get('debit_credit_type', 'N/A')})")
                    print(f"   📝 {mov.get('code_description_bank', 'N/A')}")
                    print("-" * 40)
    else:
        print("❌ No se encontraron extractos para esta cuenta y período")
        print("\n💡 Sugerencias:")
        print("   • Prueba con un rango de fechas diferente")
        print("   • Algunas cuentas pueden no tener extractos disponibles")
        print("   • La cuenta de Patagonia (25510003885400020) tiene extractos disponibles")

def probar_disponibilidad_datos(client, cuentas_df):
    """Prueba qué datos están disponibles para cada cuenta"""
    print("\n🧪 PROBANDO DISPONIBILIDAD DE DATOS...")
    
    if cuentas_df.empty:
        print("❌ No hay cuentas para probar")
        return

    # CAL-07: ventana relativa a hoy. Antes estaba hardcodeado 2025-09-01..30,
    # lo que dejaba el test de disponibilidad inservible al pasar el tiempo.
    fecha_hasta_dt = datetime.now()
    fecha_desde_dt = fecha_hasta_dt - timedelta(days=90)
    fecha_desde = fecha_desde_dt.strftime('%Y-%m-%d')
    fecha_hasta = fecha_hasta_dt.strftime('%Y-%m-%d')

    print(f"📅 Probando período (últimos 90 días): {fecha_desde} a {fecha_hasta}")
    print("=" * 80)
    
    for _, cuenta in cuentas_df.iterrows():
        print(f"\n🏦 {cuenta['account_number']} - {cuenta['bank_name']} ({cuenta['currency']})")
        print("-" * 60)
        
        # CAL-06: usamos `except Exception as exc` en lugar de `except:` bare.
        # El bare except captura también KeyboardInterrupt y SystemExit, lo que
        # impide cancelar el script con Ctrl+C.

        try:
            saldos_df, _ = client.get_saldos(account_numbers=[cuenta['account_number']])
            if not saldos_df.empty:
                print("✅ Saldos: Disponibles")
            else:
                print("❌ Saldos: No disponibles")
        except Exception as exc:
            print(f"❌ Saldos: Error ({type(exc).__name__}: {exc})")

        try:
            mov_df, _ = client.get_movimientos(
                account_number=cuenta['account_number'],
                movement_type='anteriores',
                bank_number=cuenta['bank_number'],
                date_since=fecha_desde,
                date_until=fecha_hasta,
                version='v2'
            )
            if not mov_df.empty:
                print(f"✅ Movimientos: {len(mov_df)} registros")
            else:
                print("❌ Movimientos: No disponibles")
        except Exception as exc:
            print(f"❌ Movimientos: Error ({type(exc).__name__}: {exc})")

        try:
            ext_df, _ = client.get_extractos(
                account_number=cuenta['account_number'],
                bank_number=cuenta['bank_number'],
                date_since=fecha_desde,
                date_until=fecha_hasta
            )
            if not ext_df.empty:
                print(f"✅ Extractos: {len(ext_df)} registros")
            else:
                print("❌ Extractos: No disponibles")
        except Exception as exc:
            print(f"❌ Extractos: Error ({type(exc).__name__}: {exc})")
    
    print("\n" + "=" * 80)
    print("💡 Usa las cuentas marcadas con ✅ para mejores resultados")

def exportar_excel(client):
    """Exporta datos a Excel"""
    print("\n📊 EXPORTANDO DATOS A EXCEL...")
    print("=" * 50)
    
    print("\n📅 SELECCIONA RANGO DE DATOS PARA EXPORTAR:")
    print("Este rango se aplicará a movimientos, transferencias y extractos")
    print("-" * 50)
    
    try:
        fecha_desde, fecha_hasta = solicitar_fechas()
        
        print(f"\n📊 Exportando datos del {fecha_desde} al {fecha_hasta}...")
        print("⏳ Esto puede tomar unos minutos...")
        
        # Preguntar por el límite de registros
        print(f"\n📊 CONFIGURAR LÍMITE DE REGISTROS:")
        print("1. 100 registros por endpoint (rápido)")
        print("2. 500 registros por endpoint (recomendado)")
        print("3. 1000 registros por endpoint (completo)")
        print("4. Personalizado")
        
        while True:
            try:
                limite_opcion = input("Selecciona una opción (1-4): ").strip()
                if limite_opcion == "1":
                    limit = 100
                    break
                elif limite_opcion == "2":
                    limit = 500
                    break
                elif limite_opcion == "3":
                    limit = 1000
                    break
                elif limite_opcion == "4":
                    limit = int(input("Ingresa el límite personalizado: ").strip())
                    if limit <= 0:
                        print("❌ El límite debe ser mayor a 0")
                        continue
                    break
                else:
                    print("❌ Opción inválida")
                    continue
            except ValueError:
                print("❌ Ingresa un número válido")
                continue
            except (EOFError, KeyboardInterrupt):
                limit = 500
                break
        
        print(f"✅ Límite configurado: {limit} registros por endpoint")
        
        # Preguntar sobre paginación
        print(f"\n🔄 CONFIGURAR PAGINACIÓN:")
        print("¿Quieres obtener TODOS los datos disponibles usando paginación?")
        print("⚠️  Esto puede tomar mucho tiempo si hay miles de registros")
        print("1. Sí, obtener todos los datos (recomendado)")
        print("2. No, solo la primera página")
        
        while True:
            try:
                pag_opcion = input("Selecciona una opción (1-2): ").strip()
                if pag_opcion == "1":
                    use_pagination = True
                    print("✅ Paginación activada - obtendrás todos los datos")
                    break
                elif pag_opcion == "2":
                    use_pagination = False
                    print("✅ Solo primera página - exportación rápida")
                    break
                else:
                    print("❌ Opción inválida")
                    continue
            except (EOFError, KeyboardInterrupt):
                use_pagination = True
                break
        
        filename = client.export_to_excel(
            date_since=fecha_desde,
            date_until=fecha_hasta,
            limit=limit,
            use_pagination=use_pagination
        )
        print(f"\n✅ Datos exportados exitosamente a: {filename}")
        
        # Mostrar resumen del archivo
        print(f"\n📋 RESUMEN DEL ARCHIVO:")
        print(f"   📁 Archivo: {filename}")
        print(f"   📅 Período: {fecha_desde} a {fecha_hasta}")
        print(f"   📊 Hojas incluidas: Cuentas, Saldos, Movimientos, Transferencias, Extractos")
        
        while True:
            try:
                respuesta = input("\n¿Quieres abrir el archivo ahora? (y/n): ").strip().lower()
                if respuesta in ['y', 'yes', 'sí', 'si', 's']:
                    import os
                    try:
                        os.startfile(filename)  # Windows
                        print("✅ Archivo abierto")
                    except Exception as e:
                        print(f"❌ No se pudo abrir automáticamente: {e}")
                        print(f"📁 Ubicación del archivo: {filename}")
                    break
                elif respuesta in ['n', 'no']:
                    print(f"📁 Archivo guardado en: {filename}")
                    break
                else:
                    print("❌ Responde 'y' para sí o 'n' para no")
            except (EOFError, KeyboardInterrupt):
                print(f"\n📁 Archivo guardado en: {filename}")
                break
            
    except Exception as e:
        print(f"❌ Error exportando: {e}")
        import traceback
        traceback.print_exc()

def main():
    print("🏦 Iniciando Interbanking Data Explorer")
    print("=" * 50)
    
    try:
        # Crear cliente
        client = InterbankingClient.from_env()
        
        # Probar conexión
        if not client.test_connection():
            print("❌ No se pudo conectar al API. Verifica tus credenciales.")
            return
        
        # Obtener cuentas una vez
        cuentas_df = pd.DataFrame()
        
        while True:
            mostrar_menu()
            
            try:
                opcion = input("Selecciona una opción (0-8): ").strip()
                
                if opcion == "0":
                    print("👋 ¡Hasta luego!")
                    break
                elif opcion == "1":
                    cuentas_df = ver_cuentas(client)
                elif opcion == "2":
                    if cuentas_df.empty:
                        cuentas_df = ver_cuentas(client)
                    ver_saldos(client, cuentas_df)
                elif opcion == "3":
                    if cuentas_df.empty:
                        cuentas_df = ver_cuentas(client)
                    ver_movimientos(client, cuentas_df)
                elif opcion == "4":
                    ver_transferencias(client)
                elif opcion == "5":
                    if cuentas_df.empty:
                        cuentas_df = ver_cuentas(client)
                    ver_extractos(client, cuentas_df)
                elif opcion == "6":
                    exportar_excel(client)
                elif opcion == "7":
                    client.test_connection()
                elif opcion == "8":
                    if cuentas_df.empty:
                        cuentas_df = ver_cuentas(client)
                    probar_disponibilidad_datos(client, cuentas_df)
                else:
                    print("❌ Opción inválida")
                
                input("\nPresiona Enter para continuar...")
                
            except KeyboardInterrupt:
                print("\n👋 ¡Hasta luego!")
                break
            except Exception as e:
                print(f"❌ Error: {e}")
                input("\nPresiona Enter para continuar...")
        
    except ValueError as e:
        print(f"❌ Error de configuración: {e}")
        print("💡 Ejecuta 'python setup.py' para configurar las credenciales")
    except Exception as e:
        print(f"❌ Error inesperado: {e}")

if __name__ == "__main__":
    main()