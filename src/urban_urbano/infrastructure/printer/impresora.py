from datetime import datetime, timedelta
import logging
from time import strftime
import time
from escpos.printer import Usb
from PyQt5.QtCore import QSettings
import urban_urbano.config.variables_globales as vg
import sys
import subprocess

sys.path.insert(1, '/home/pi/Urban_Urbano/db')

from urban_urbano.data.repositories.operadores import obtener_operador_por_UID
from urban_urbano.data.repositories.ventas_queries import (
    obtener_ultimo_folio_de_item_venta,
    obtener_total_de_ventas_por_folioviaje,
    obtener_total_de_aforos_digitales_por_folioviaje,
    obtener_total_saldo_digital_por_folioviaje,
    obtener_ultimo_folio_de_venta_digital,
)
from urban_urbano.data.repositories.asignaciones_queries import obtener_asignacion_por_folio_de_viaje, obtener_ultima_asignacion


SETTINGS_PATH = '/home/pi/Urban_Urbano/ventanas/settings.ini'
NC_IMPRESORA = '0x04c5'
NS_IMPRESORA = '0x126e'

# IMPORTANTE:
# En tu código original el importe se toma de boleto[11].
# Se deja este índice para no romper tu estructura actual.
# Si en tu tabla item_venta el importe está en otra columna, ajusta este valor.
INDICE_IMPORTE_BOLETO = 11

# Se intenta leer primero boleto[10] como tipo de pasajero.
# Si no coincide con un tipo conocido, el código escanea toda la tupla buscando
# "normal", "menor", "mayor" o "estudiante".
INDICE_TIPO_PASAJERO_BOLETO = 9


try:

    def sumar_dos_horas(hora1, hora2):
        try:
            formato = "%H:%M:%S"
            lista = hora2.split(":")
            hora = int(lista[0])
            minuto = int(lista[1])
            segundo = int(lista[2])
            h1 = datetime.strptime(hora1, formato)
            dh = timedelta(hours=hora)
            dm = timedelta(minutes=minuto)
            ds = timedelta(seconds=segundo)
            resultado1 = h1 + ds
            resultado2 = resultado1 + dm
            resultado = resultado2 + dh
            return str(resultado.strftime(formato))
        except Exception as e:
            print("pasaje.py, linea 151: " + str(e))
            logging.error(f"Error en sumar_dos_horas: {e}")
            return hora1


    def inicializar_impresora():
        return Usb(int(NC_IMPRESORA, 16), int(NS_IMPRESORA, 16), 0)


    def obtener_settings():
        return QSettings(SETTINGS_PATH, QSettings.IniFormat)


    def valor_settings(settings, clave, default='0,0.0'):
        valor = settings.value(clave, default)
        if valor is None or str(valor).strip() == '':
            return default
        return str(valor)


    def normalizar_tipo_pasajero(tipo):
        """
        Regresa una de estas llaves:
        - estudiante
        - normal
        - menor
        - mayor

        Si no reconoce el tipo, regresa None.
        """
        texto = str(tipo).strip().lower()

        equivalencias = {
            'estudiante': 'estudiante',
            'estud': 'estudiante',
            'est': 'estudiante',
            'student': 'estudiante',

            'normal': 'normal',
            'adulto': 'normal',
            'general': 'normal',

            'menor': 'menor',
            'chico': 'menor',
            'niño': 'menor',
            'nino': 'menor',

            'mayor': 'mayor',
            'adulto mayor': 'mayor',
            'ad.may': 'mayor',
            'ad_may': 'mayor',
            'adm': 'mayor',
        }

        if texto in equivalencias:
            return equivalencias[texto]

        # Coincidencias parciales seguras
        if 'estud' in texto:
            return 'estudiante'
        if 'normal' in texto:
            return 'normal'
        if 'menor' in texto or 'chico' in texto or 'niño' in texto or 'nino' in texto:
            return 'menor'
        if 'mayor' in texto or 'ad.may' in texto or 'ad_may' in texto:
            return 'mayor'

        return None


    def crear_resumen_vacio():
        return {
            'estudiante': {'cantidad': 0, 'monto': 0.0},
            'normal': {'cantidad': 0, 'monto': 0.0},
            'menor': {'cantidad': 0, 'monto': 0.0},
            'mayor': {'cantidad': 0, 'monto': 0.0},
        }


    def obtener_importe_boleto(boleto):
        """
        En tu código original se usa boleto[11] como importe.
        Esta función conserva esa lógica, pero evita que el corte reviente si
        aparece un dato inválido.
        """
        try:
            return float(boleto[INDICE_IMPORTE_BOLETO])
        except Exception as e:
            logging.error(f"No se pudo leer importe del boleto {boleto}: {e}")
            return 0.0


    def obtener_tipo_pasajero_boleto(boleto):
        """
        Intenta obtener el tipo de pasajero desde boleto[10].
        Si no lo reconoce, busca en toda la tupla/lista.
        """
        try:
            tipo = normalizar_tipo_pasajero(boleto[INDICE_TIPO_PASAJERO_BOLETO])
            if tipo:
                return tipo
        except Exception:
            pass

        try:
            for campo in boleto:
                tipo = normalizar_tipo_pasajero(campo)
                if tipo:
                    return tipo
        except Exception:
            pass

        logging.warning(f"No se pudo identificar tipo de pasajero para boleto: {boleto}. Se clasifica como normal.")
        return 'normal'


    def calcular_resumen_efectivo_desde_bd(boletos):
        """
        Fuente de verdad del corte de efectivo: BASE DE DATOS.

        Esto corrige el descuadre donde:
        - Total general salía de BD.
        - Desglose salía de QSettings.

        Ahora total, folios y desglose salen del mismo origen.
        """
        resumen = crear_resumen_vacio()

        if not boletos:
            return resumen

        for boleto in boletos:
            tipo = obtener_tipo_pasajero_boleto(boleto)
            importe = obtener_importe_boleto(boleto)

            resumen[tipo]['cantidad'] += 1
            resumen[tipo]['monto'] += importe

        return resumen


    def total_folios_resumen(resumen):
        return sum(datos['cantidad'] for datos in resumen.values())


    def total_monto_resumen(resumen):
        return sum(datos['monto'] for datos in resumen.values())


    def calcular_resumen_efectivo_desde_settings(settings):
        """
        Solo se usa para logging/comparación, no para imprimir el corte.
        """
        resumen = crear_resumen_vacio()
        mapa = {
            'info_estudiantes': 'estudiante',
            'info_normales': 'normal',
            'info_chicos': 'menor',
            'info_ad_mayores': 'mayor',
        }

        for clave_settings, clave_resumen in mapa.items():
            try:
                cantidad, monto = valor_settings(settings, clave_settings).split(',')
                resumen[clave_resumen]['cantidad'] = int(float(cantidad))
                resumen[clave_resumen]['monto'] = float(monto)
            except Exception as e:
                logging.error(f"No se pudo leer {clave_settings} desde settings: {e}")

        return resumen


    def registrar_diferencia_settings_vs_bd(settings, resumen_bd):
        """
        Deja evidencia en logs si QSettings no coincide contra BD.
        No modifica el ticket, porque el ticket debe imprimirse con BD.
        """
        resumen_settings = calcular_resumen_efectivo_desde_settings(settings)

        folios_settings = total_folios_resumen(resumen_settings)
        monto_settings = total_monto_resumen(resumen_settings)
        folios_bd = total_folios_resumen(resumen_bd)
        monto_bd = total_monto_resumen(resumen_bd)

        if folios_settings != folios_bd or round(monto_settings, 2) != round(monto_bd, 2):
            logging.error(
                "DESCUADRE SETTINGS VS BD - "
                f"settings=({folios_settings} folios, ${monto_settings:.2f}) "
                f"bd=({folios_bd} folios, ${monto_bd:.2f}) "
                f"detalle_settings={resumen_settings} detalle_bd={resumen_bd}"
            )


    def actualizar_resumen_settings_por_tipo(settings, tipo_de_pasajero, importe=0.0):
        """
        Mantengo esta función por compatibilidad con tu flujo actual.
        Aun así, el corte de efectivo ya NO depende de estos valores.
        """
        tipo = normalizar_tipo_pasajero(tipo_de_pasajero) or 'normal'

        clave_por_tipo = {
            'estudiante': 'info_estudiantes',
            'normal': 'info_normales',
            'menor': 'info_chicos',
            'mayor': 'info_ad_mayores',
        }

        clave = clave_por_tipo[tipo]

        try:
            cantidad_actual, monto_actual = valor_settings(settings, clave).split(',')
            nueva_cantidad = int(float(cantidad_actual)) + 1
            nuevo_monto = float(monto_actual) + float(importe)
            settings.setValue(clave, f"{nueva_cantidad},{nuevo_monto:.1f}")
            settings.sync()
        except Exception as e:
            logging.error(f"Error actualizando {clave} en settings: {e}")


    def imprimir_boleto_normal_con_servicio(ultimo_folio_de_venta, fecha, hora, idUnidad, servicio, tramo, qr):
        try:
            instancia_impresora = inicializar_impresora()
            fecha = str(strftime('%d-%m-%Y')).replace('/', '-')
            settings = obtener_settings()

            instancia_impresora.set(align='center')
            logging.info("Impresora encontrada")
            instancia_impresora.text(f"Folio: {(ultimo_folio_de_venta)}            {fecha} {hora}\n")
            instancia_impresora.text(f"Unidad: {idUnidad}       IMPORTE {qr[6]}:  $ {0}\n")
            instancia_impresora.text(f"Servicio: {servicio}\n")
            tramo_servicio_actual = str(str(tramo).split("-")[0]) + "-" + str(str(servicio).split("-")[2])
            instancia_impresora.text(f"Tramo: {tramo_servicio_actual}\n")

            # En este flujo el importe impreso es 0, por eso se actualiza monto 0.
            # El corte final se calcula desde BD, no desde settings.
            actualizar_resumen_settings_por_tipo(settings, str(qr[6]).lower(), 0.0)

            instancia_impresora.cut()
            time.sleep(1)
            return True
        except Exception as e:
            print("Sucedio algo al imprimir ticket normal con servicio: " + str(e))
            logging.info(e)
            return False


    def imprimir_boleto_normal_sin_servicio(ultimo_folio_de_venta, fecha, hora, idUnidad, tramo, qr):
        try:
            instancia_impresora = inicializar_impresora()
            fecha = str(strftime('%d-%m-%Y')).replace('/', '-')
            settings = obtener_settings()

            instancia_impresora.set(align='center')
            instancia_impresora.text(f"Folio: {(ultimo_folio_de_venta)}            {fecha} {hora}\n")
            instancia_impresora.text(f"Unidad: {idUnidad}       IMPORTE {qr[6]}:  $ {0}\n")
            instancia_impresora.text("Aparentemente no estas en el servicio correcto\n")
            destino_del_qr = str(str(tramo).split("-")[1])
            instancia_impresora.text(f"No se encontro el destino {destino_del_qr}\n")

            # En este flujo el importe impreso es 0, por eso se actualiza monto 0.
            # El corte final se calcula desde BD, no desde settings.
            actualizar_resumen_settings_por_tipo(settings, str(qr[6]).lower(), 0.0)

            instancia_impresora.cut()
            time.sleep(1)
            return True
        except Exception as e:
            print("Sucedio algo al imprimir ticket normal sin servicio: " + str(e))
            logging.info(e)
            return False


    def imprimir_boleto_normal_pasaje(folio, fecha, hora, unidad, tipo_pasajero, importe, servicio, tramo):
        try:
            instancia_impresora = inicializar_impresora()
            fecha = str(strftime('%d-%m-%Y')).replace('/', '-')
            instancia_impresora.set(align='center')
            logging.info("Impresora encontrada")
            instancia_impresora.text(f"Folio: {folio}            {fecha} {hora}\n")
            instancia_impresora.text(f"Unidad: {unidad}       IMPORTE {tipo_pasajero}:  $ {importe}\n")
            instancia_impresora.text(f"Servicio: {servicio}\n")
            instancia_impresora.text(f"Tramo: {tramo}\n")
            instancia_impresora.cut()
            time.sleep(1)
            return True
        except Exception as e:
            print(e)
            logging.info(e)
            return False


    def imprimir_boleto_con_qr_pasaje(folio, fecha, hora, unidad, tipo_pasajero, importe, servicio, tramo, servicio_o_transbordo):
        try:
            instancia_impresora = inicializar_impresora()
            fecha = str(strftime('%d-%m-%Y')).replace('/', '-')
            instancia_impresora.set(align='center')
            logging.info("Impresora encontrada")
            instancia_impresora.text(f"Folio: {folio}            {fecha} {hora}\n")
            instancia_impresora.text(f"Unidad: {unidad}       IMPORTE {tipo_pasajero}:  $ {importe}\n")
            instancia_impresora.text(f"Servicio: {servicio}\n")
            instancia_impresora.text(f"Tramo: {tramo}\n")

            if 'NE' in servicio_o_transbordo[8]:
                unidad_a_transbordar = str(str(servicio_o_transbordo[7]).split("_")[0]).replace("'", "")
                instancia_impresora.text(f"Transbordar unidad en: {unidad_a_transbordar}\n")
                estimado = "02:00:00"
                hora_antes_de = sumar_dos_horas(hora, estimado)
                instancia_impresora.text(f"Antes de {fecha} {hora_antes_de}\n")
                instancia_impresora.qr(
                    f"{fecha},{hora_antes_de},{unidad},{importe},{servicio},{tramo},{tipo_pasajero},{'st'},{unidad_a_transbordar}",
                    0,
                    5,
                )
                instancia_impresora.cut()
                time.sleep(1)
                return True
            else:
                unidad_a_transbordar1 = str(str(servicio_o_transbordo[7]).split("_")[0]).replace("'", "")
                unidad_a_transbordar2 = str(str(servicio_o_transbordo[8]).split("_")[0]).replace("'", "")
                instancia_impresora.text(f"Transbordar unidad en: {unidad_a_transbordar1}\n")
                instancia_impresora.text(f"Luego transbordar unidad en: {unidad_a_transbordar2}\n")
                estimado = "02:00:00"
                hora_antes_de = sumar_dos_horas(hora, estimado)
                instancia_impresora.text(f"Antes de {fecha} {hora_antes_de}\n")
                instancia_impresora.qr(
                    f"{fecha},{hora_antes_de},{unidad},{importe},{servicio},{tramo},{tipo_pasajero},{'ct'},{unidad_a_transbordar1},{unidad_a_transbordar2}",
                    0,
                    5,
                )
                instancia_impresora.cut()
                time.sleep(1)
                return True
        except Exception as e:
            print(e)
            logging.info(e)
            return False


    def imprimir_ticket_de_corte(idUnidad, imprimir):
        try:
            settings = obtener_settings()
            fecha = str(vg.fecha_actual).replace('/', '-') if vg.fecha_actual else subprocess.check_output(['date', '+%d-%m-%Y']).decode().strip()
            hora_actual = vg.hora_actual if vg.hora_actual else subprocess.check_output(['date', '+%H:%M:%S']).decode().strip()

            total_de_boletos_db = []
            total_boletos_digitales = 0
            total_digital_liquidar = 0.0

            ultima_venta_bd = obtener_ultimo_folio_de_item_venta()
            ultima_venta_bd_digital = obtener_ultimo_folio_de_venta_digital()
            logging.info(f"Última venta en la base de datos: {ultima_venta_bd}")

            folio_de_viaje = settings.value('folio_de_viaje', '')
            folio_consulta = folio_de_viaje if folio_de_viaje else vg.folio_asignacion

            if folio_consulta:
                total_boletos_digitales = obtener_total_de_aforos_digitales_por_folioviaje(folio_consulta)
                total_digital_liquidar = obtener_total_saldo_digital_por_folioviaje(folio_consulta)
                total_de_boletos_db = obtener_total_de_ventas_por_folioviaje(folio_consulta)
            else:
                logging.warning("No hay folio_de_viaje ni vg.folio_asignacion para consultar ventas.")

            if not total_de_boletos_db:
                logging.info("No hay ventas registradas.")
                ultima_venta_bd = [0, 0]
                total_de_boletos_db = []

            # CORRECCIÓN PRINCIPAL:
            # Efectivo se calcula 100% desde BD: desglose, folios y total.
            resumen_efectivo = calcular_resumen_efectivo_desde_bd(total_de_boletos_db)
            total_folios_efectivo = total_folios_resumen(resumen_efectivo)
            total_a_liquidar_bd = total_monto_resumen(resumen_efectivo)

            # Solo para auditoría en logs. El ticket ya no usa QSettings para efectivo.
            registrar_diferencia_settings_vs_bd(settings, resumen_efectivo)

            total_liquidar_suma = total_a_liquidar_bd + float(total_digital_liquidar or 0.0)

            try:
                trama_dos_del_viaje = obtener_ultima_asignacion()
                logging.info(f"Última asignación: {trama_dos_del_viaje}")
            except Exception as e:
                logging.error(f"Error al obtener última asignación: {e}")
                trama_dos_del_viaje = [""] * 7

            instancia_impresora = inicializar_impresora()
            imprimir_tickets(
                instancia_impresora,
                settings,
                idUnidad,
                trama_dos_del_viaje,
                fecha,
                hora_actual,
                ultima_venta_bd,
                total_folios_efectivo,
                total_a_liquidar_bd,
                total_boletos_digitales,
                total_digital_liquidar,
                ultima_venta_bd_digital,
                total_liquidar_suma,
                resumen_efectivo,
            )
            return True
        except Exception as e:
            print("Error en imprimir_ticket_de_corte: ", e)
            logging.error(f"Error en imprimir_ticket_de_corte: {e}")
            return not imprimir


    def obtener_valor_lista(lista, indice, default=''):
        try:
            if lista is None:
                return default
            return lista[indice]
        except Exception:
            return default


    def imprimir_tickets(
        impresora,
        settings,
        idUnidad,
        asignacion,
        fecha,
        hora,
        ultima_venta,
        total_folios,
        total_liquidar,
        total_boletos_digitales,
        total_digital_liquidar,
        ultima_venta_bd_digital,
        total_liquidar_suma,
        resumen_efectivo,
    ):
        impresora.set(align='center')

        for _ in range(2):
            # General
            impresora.text("RESUMEN GENERAL\n")
            impresora.text(f"Fv: {obtener_valor_lista(asignacion, 6)}  Sw: {vg.version_del_software}\n")
            impresora.text(f"Unidad: {idUnidad}    Serv: {settings.value('servicio')}\n")
            impresora.text(f"Ultimo folio de pago con efectivo: {obtener_valor_lista(ultima_venta, 1, 0)}\n\n")

            # Efectivo
            impresora.text("RESUMEN DE VENTAS CON EFECTIVO\n")
            impresora.text(f"Total a liquidar efectivo: $ {float(total_liquidar):.1f}\n")
            impresora.text(f"Total de folios efectivo: {int(total_folios)}\n")
            imprimir_clasificacion_boletos_desde_resumen(impresora, resumen_efectivo)
            impresora.text("\n")

            # Digital
            impresora.text("RESUMEN DE VENTAS DIGITALES\n")
            impresora.text(f"Total digital: ${float(total_digital_liquidar or 0.0):.1f}\n")
            impresora.text(f"Total de folios digitales: {int(total_boletos_digitales or 0)}\n")
            imprimir_clasificacion_boletos_digitales(impresora, settings)
            impresora.text("\n")

            # Inicio
            impresora.text("INICIO DE VIAJE\n")
            impresora.text(f"Fecha y hora: {obtener_valor_lista(asignacion, 4)} {obtener_valor_lista(asignacion, 5)}\n")
            impresora.text(
                f"Quien abrio: {obtener_nombre_operador(settings, vg.nombre_de_operador_inicio, vg.numero_de_operador_inicio, vg.csn_chofer, 'inicio')}\n\n"
            )

            # Fin
            impresora.text("FIN DE VIAJE\n")
            impresora.text(f"Fecha y hora (impresion): {fecha} {hora}\n")
            impresora.text(
                f"Quien cerro: {obtener_nombre_operador(settings, vg.nombre_de_operador_final, vg.numero_de_operador_final, vg.csn_chofer, 'final')}\n"
            )
            impresora.cut()


    def imprimir_clasificacion_boletos_desde_resumen(impresora, resumen):
        etiquetas = {
            'estudiante': 'Estud',
            'normal': 'Normal',
            'menor': 'Menor',
            'mayor': 'Ad.May',
        }

        for clave in ['estudiante', 'normal', 'menor', 'mayor']:
            cantidad = int(resumen[clave]['cantidad'])
            monto = float(resumen[clave]['monto'])
            impresora.text(f"{etiquetas[clave]}:       {cantidad}  $       {monto:.1f}\n")


    def imprimir_clasificacion_boletos(impresora, settings):
        """
        Se conserva por compatibilidad, pero ya no debe usarse para el resumen
        de ventas con efectivo. El efectivo debe imprimirse con
        imprimir_clasificacion_boletos_desde_resumen().
        """
        for clave in ['info_estudiantes', 'info_normales', 'info_chicos', 'info_ad_mayores']:
            nombre = {
                'info_estudiantes': "Estud",
                'info_normales': "Normal",
                'info_chicos': "Menor",
                'info_ad_mayores': "Ad.May",
            }[clave]
            cantidad, monto = valor_settings(settings, clave).split(',')
            impresora.text(f"{nombre}:       {cantidad}  $       {float(monto):.1f}\n")


    def imprimir_clasificacion_boletos_digitales(impresora, settings):
        for clave in ['info_estudiantes_digital', 'info_normales_digital', 'info_chicos_digital', 'info_ad_mayores_digital']:
            nombre = {
                'info_estudiantes_digital': "Estud",
                'info_normales_digital': "Normal",
                'info_chicos_digital': "Menor",
                'info_ad_mayores_digital': "Ad.May",
            }[clave]
            cantidad, monto = valor_settings(settings, clave).split(',')
            impresora.text(f"{nombre}:       {cantidad}  $       {float(monto):.1f}\n")


    def obtener_nombre_operador(settings, nombre, numero, csn, tipo):
        operador = None

        nombre_setting = settings.value(f'nombre_de_operador_{tipo}')
        numero_setting = settings.value(f'numero_de_operador_{tipo}')
        csn_setting = settings.value('csn_chofer')

        if nombre:
            if numero:
                return f"{numero} {nombre}"
            elif numero_setting:
                return f"{numero_setting} {nombre}"
            elif csn_setting:
                operador = obtener_operador_por_UID(csn_setting)
                if operador:
                    return f"{operador[1]} {operador[2]}"
                elif csn:
                    operador = obtener_operador_por_UID(csn)
                    if operador:
                        return f"{operador[1]} {operador[2]}"
                return nombre
            elif csn:
                operador = obtener_operador_por_UID(csn)
                if operador:
                    return f"{operador[1]} {operador[2]}"
                return nombre
            return nombre

        elif nombre_setting:
            if numero:
                return f"{numero} {nombre_setting}"
            elif numero_setting:
                return f"{numero_setting} {nombre_setting}"
            elif csn_setting:
                operador = obtener_operador_por_UID(csn_setting)
                if operador:
                    return f"{operador[1]} {operador[2]}"
                elif csn:
                    operador = obtener_operador_por_UID(csn)
                    if operador:
                        return f"{operador[1]} {operador[2]}"
                return nombre_setting
            elif csn:
                operador = obtener_operador_por_UID(csn)
                if operador:
                    return f"{operador[1]} {operador[2]}"
                return nombre_setting
            return nombre_setting

        elif numero:
            if csn_setting:
                operador = obtener_operador_por_UID(csn_setting)
                if operador:
                    return f"{operador[1]} {operador[2]}"
                elif csn:
                    operador = obtener_operador_por_UID(csn)
                    if operador:
                        return f"{operador[1]} {operador[2]}"
                return numero
            elif csn:
                operador = obtener_operador_por_UID(csn)
                if operador:
                    return f"{operador[1]} {operador[2]}"
                return numero
            return numero

        elif numero_setting:
            if csn_setting:
                operador = obtener_operador_por_UID(csn_setting)
                if operador:
                    return f"{operador[1]} {operador[2]}"
                elif csn:
                    operador = obtener_operador_por_UID(csn)
                    if operador:
                        return f"{operador[1]} {operador[2]}"
                return numero_setting
            elif csn:
                operador = obtener_operador_por_UID(csn)
                if operador:
                    return f"{operador[1]} {operador[2]}"
                return numero_setting
            return numero_setting

        elif csn_setting:
            operador = obtener_operador_por_UID(csn_setting)
            if operador:
                return f"{operador[1]} {operador[2]}"

        elif csn:
            operador = obtener_operador_por_UID(csn)
            if operador:
                return f"{operador[1]} {operador[2]}"

        return "----------"

except Exception as e:
    print("No hubo comunicacion con impresora")
    logging.error(f"Error cargando módulo de impresión: {e}")
