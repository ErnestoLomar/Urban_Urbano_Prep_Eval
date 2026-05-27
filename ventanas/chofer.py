##########################################
# Autor: Ernesto Lomar
# Fecha de creación: 11/04/2022
# Ultima modificación: 24/11/2025
#
# Script de la ventana chofer.
#
##########################################

# Librerías externas
from PyQt5 import uic
from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtCore import Qt, QSettings
import sys
from servicios import Rutas
from time import strftime
import logging
import time

# Hub de GPIO (BCM)
from gpio_hub import GPIOHub, PINMAP

# Librerías propias
from servicio_pensiones import obtener_servicios_de_pension, obtener_pensiones
import variables_globales as vg
from variables_globales import VentanaActual
from asignaciones_queries import (
    guardar_auto_asignacion,
    obtener_ultima_asignacion,
    aniadir_folio_de_viaje_a_auto_asignacion,
    eliminar_auto_asignacion_por_folio,
    obtener_ultimo_folio_auto_asignacion,
    modificar_folio_auto_asignacion
)
from queries import obtener_datos_aforo
from matrices_tarifarias import (
    obtener_servicio_por_numero_de_servicio_y_origen,
    obtener_transbordos_por_origen_y_numero_de_servicio
)
from servicio_pensiones import obtener_origen_por_numero_de_servicio
from emergentes import VentanaEmergente

# Instancia del hub
try:
    HUB = GPIOHub(PINMAP)
except Exception as e:
    print("No se pudo iniciar GPIOHub: " + str(e))
    logging.info(e)


class VentanaChofer(QWidget):

    def __init__(self, close_signal, close_signal_pasaje):
        super().__init__()
        try:
            uic.loadUi("/home/pi/Urban_Urbano/ui/chofer.ui", self)

            # Configuración de la ventana chofer.
            self.settings = QSettings('/home/pi/Urban_Urbano/ventanas/settings.ini', QSettings.IniFormat)
            self.settings.setValue('ventana_actual', "chofer")
            vg.ventana_actual = VentanaActual.CHOFER

            if len(vg.csn_chofer) != 0:
                self.settings.setValue('csn_chofer', vg.csn_chofer)
            self.setWindowFlags(Qt.FramelessWindowHint)
            self.setGeometry(0, 0, 800, 480)
            self.inicializar_comboBox()
            self.inicializar_labels()
            self.spinBox_vuelta.valueChanged.connect(self.handle_spin)
            self.spinBox_vuelta.setMinimum(1)

            # Variables
            self.close_signal = close_signal
            self.close_signal_pasaje = close_signal_pasaje
            self.turno = ""
            self.pension = ""
            self.servicio = ""
            self.vuelta = 1
            self.pension_selec = ""
            self.idUnidad = str(obtener_datos_aforo()[1])
            self.intentos = 1

            # Nombre del operador
            if len(vg.nombre_de_operador_inicio) > 0:
                self.lbl_name_operador.setText(vg.nombre_de_operador_inicio)
            else:
                if len(self.settings.value('nombre_de_operador_inicio')) > 0:
                    self.lbl_name_operador.setText(self.settings.value('nombre_de_operador_inicio'))
                else:
                    self.lbl_name_operador.setText("")
                    print("No hay nombre de operador")

        except Exception as e:
            print(e)
            logging.info(e)

    # Inicializamos las señales de los labels al darles click
    def inicializar_labels(self):
        try:
            self.label_ok.mousePressEvent = self.handle_ok
            self.label_cancel.mousePressEvent = self.handle_cancel
        except Exception as e:
            print(e)
            logging.info(e)

    # Cargar pensiones de DB
    def cargar_pensiones_db(self):
        try:
            self.lista_pensiones = []
            self.lista_pensiones_db = obtener_pensiones()
            for p in self.lista_pensiones_db:
                pension = p[1]
                if pension != "AlttusTI":
                    self.lista_pensiones.append(pension)
        except Exception as e:
            print(e)
            logging.info(e)

    # Inicializamos combos
    def inicializar_comboBox(self):
        try:
            self.cargar_pensiones_db()

            # Turnos
            self.comboBox_turno.setStyleSheet('selection-background-color: gray; font: 18pt "Rockwell";')
            self.lista_turnos = {"Matutino": "Matutino", "Vespertino": "Vespertino"}
            self.comboBox_turno.addItems(self.lista_turnos)
            self.comboBox_turno.activated[str].connect(self.turno_seleccionado)

            # Pensiones
            self.comboBox_pension.setStyleSheet('QComboBox { combobox-popup: 1; selection-background-color: gray; font: 18pt "Rockwell";}')
            self.comboBox_pension.setMaxVisibleItems(15)
            self.comboBox_pension.addItems(self.lista_pensiones)
            self.comboBox_pension.activated[str].connect(self.pension_seleccionada)
            self.comboBox_pension.setCurrentIndex(20)

            # Servicios
            self.comboBox_servicio.setStyleSheet('QComboBox { combobox-popup: 1; selection-background-color: gray; font: 18pt "Rockwell";}')
            self.comboBox_servicio.setMaxVisibleItems(15)
            self.comboBox_servicio.activated[str].connect(self.servicio_seleccionado)
        except Exception as e:
            print(e)
            logging.info(e)

    # Spin de vuelta
    def handle_spin(self):
        try:
            self.vuelta = self.spinBox_vuelta.value()
            vg.vuelta = self.vuelta
            self.settings.setValue('vuelta', self.vuelta)
        except Exception as e:
            print(e)
            logging.info(e)
            
    def limpiar_estado_inicio(self):
        try:
            try:
                HUB.write("fan_en", False)
            except Exception as e:
                logging.info(f"No se pudo apagar el ventilador: {e}")

            self.servicio = ""

            vg.csn_chofer = ""
            vg.numero_de_operador_inicio = ""
            vg.numero_de_operador_final = ""
            vg.nombre_de_operador_inicio = ""
            vg.nombre_de_operador_final = ""

            self.settings.setValue('ventana_actual', "")
            self.settings.setValue('csn_chofer', "")
            self.settings.setValue('csn_chofer_dos', "")
            self.settings.setValue('numero_de_operador_inicio', "")
            self.settings.setValue('numero_de_operador_final', "")
            self.settings.setValue('nombre_de_operador_inicio', "")
            self.settings.setValue('nombre_de_operador_final', "")

        except Exception as e:
            print(e)
            logging.info(e)


    def obtener_csn_inicio_valido(self):
        try:
            csn_settings = str(self.settings.value('csn_chofer') or "").strip()
            csn_global = str(vg.csn_chofer or "").strip()

            if csn_settings and csn_global and csn_settings != csn_global:
                logging.error(
                    f"[ERROR_CSN_DESFASADO] "
                    f"settings.csn_chofer={csn_settings}, "
                    f"vg.csn_chofer={csn_global}, "
                    f"nombre_inicio_settings={self.settings.value('nombre_de_operador_inicio')}, "
                    f"nombre_inicio_vg={vg.nombre_de_operador_inicio}"
                )

                self.settings.setValue('csn_chofer_dos', "")
                return ""

            csn_inicio = csn_settings or csn_global

            if len(csn_inicio) != 14:
                logging.error(
                    f"[ERROR_CSN_INVALIDO_INICIO] "
                    f"csn_inicio={csn_inicio}, "
                    f"settings.csn_chofer={csn_settings}, "
                    f"vg.csn_chofer={csn_global}, "
                    f"nombre_inicio_settings={self.settings.value('nombre_de_operador_inicio')}, "
                    f"nombre_inicio_vg={vg.nombre_de_operador_inicio}"
                )
                return ""

            vg.csn_chofer = csn_inicio
            self.settings.setValue('csn_chofer', csn_inicio)

            return csn_inicio

        except Exception as e:
            print(e)
            logging.info(e)
            return ""

    # OK (crear viaje)
    def handle_ok(self, event):
        try:
            self.close()

            folio_asignacion_viaje = vg.folio_asignacion

            if len(str(folio_asignacion_viaje)) > 1:
                self.ve = VentanaEmergente("VOID", "Ya existe un viaje", 4.5)
                self.ve.show()
                HUB.buzzer_blinks(5, on_ms=55, off_ms=55)

                print("Ya existe una asignacion de viaje")
                self.limpiar_estado_inicio()
                vg.folio_asignacion = 0
                return

            fecha_completa = strftime('%Y-%m-%d %H:%M:%S')
            hora = strftime('%H:%M:%S')
            fecha_vg = str(vg.fecha_actual).replace('/', '-')

            print("La fecha actual de la raspberry es: ", fecha_vg)

            pension_elegida = str(self.pension_selec or self.comboBox_pension.currentText() or "").strip()
            servicio_elegido = str(self.servicio or self.comboBox_servicio.currentText() or "").strip()
            turno_elegido = str(self.comboBox_turno.currentText() or "").strip()

            if pension_elegida == "":
                print("No hay pension seleccionada")
                logging.info("[ERROR_INICIO_VIAJE] No hay pension seleccionada")
                self.limpiar_estado_inicio()
                HUB.buzzer_blinks(5, on_ms=55, off_ms=55)
                return

            if servicio_elegido == "":
                print("No hay servicio seleccionado")
                logging.info("[ERROR_INICIO_VIAJE] No hay servicio seleccionado")
                self.limpiar_estado_inicio()
                HUB.buzzer_blinks(5, on_ms=55, off_ms=55)
                return

            try:
                numero_servicio = int(servicio_elegido.split(" - ")[0])
            except Exception as e:
                print("Servicio inválido: ", servicio_elegido)
                logging.info(f"[ERROR_INICIO_VIAJE] Servicio inválido={servicio_elegido}, error={e}")
                self.limpiar_estado_inicio()
                HUB.buzzer_blinks(5, on_ms=55, off_ms=55)
                return

            origen = obtener_origen_por_numero_de_servicio(numero_servicio)

            total_de_servicios = obtener_servicio_por_numero_de_servicio_y_origen(
                numero_servicio,
                str(origen[3]).replace("(", "").replace(")", "").replace(",", "").replace("'", "")
            )

            if len(total_de_servicios) == 0:
                print("No hay servicios disponibles")
                print("Total de servicios: ", len(total_de_servicios))
                logging.info(
                    f"[ERROR_INICIO_VIAJE] No hay servicios disponibles. "
                    f"servicio={servicio_elegido}, pension={pension_elegida}"
                )

                self.limpiar_estado_inicio()
                HUB.buzzer_blinks(5, on_ms=55, off_ms=55)
                return

            # Última asignación antes de insertar la nueva.
            ultima_asignacion = obtener_ultima_asignacion()
            print("La ultima asignacion es: ", ultima_asignacion)

            vg.servicio = servicio_elegido
            vg.turno = turno_elegido

            self.servicio = servicio_elegido
            self.pension_selec = pension_elegida
            self.turno = turno_elegido

            self.settings.setValue('servicio', servicio_elegido)
            self.settings.setValue('pension', pension_elegida)
            self.settings.setValue('turno', turno_elegido)

            csn_inicio = self.obtener_csn_inicio_valido()

            if csn_inicio == "":
                print("No hay CSN válido para iniciar viaje")
                logging.error(
                    f"[ERROR_INICIO_VIAJE] No hay CSN válido. "
                    f"settings.csn_chofer={self.settings.value('csn_chofer')}, "
                    f"vg.csn_chofer={vg.csn_chofer}, "
                    f"operador_settings={self.settings.value('nombre_de_operador_inicio')}, "
                    f"operador_vg={vg.nombre_de_operador_inicio}"
                )
                HUB.buzzer_blinks(5, on_ms=55, off_ms=55)
                return

            guardar_auto_asignacion(
                csn_inicio,
                f"{self.settings.value('servicio')},{self.settings.value('pension')}",
                fecha_vg,
                hora
            )

            folio = self.crear_folio()

            if folio is None or str(folio) == "":
                print("No se pudo crear folio")
                logging.error("[ERROR_INICIO_VIAJE] No se pudo crear folio")
                self.limpiar_estado_inicio()
                HUB.buzzer_blinks(5, on_ms=55, off_ms=55)
                return

            # Evitar repetir folio.
            if ultima_asignacion is not None and str(ultima_asignacion[1]) == str(folio):
                print("Se procederá a aumentar el folio ya que es el mismo que el anterior")
                logging.info("Se procederá a aumentar el folio ya que es el mismo que el anterior")

                folio = obtener_ultimo_folio_auto_asignacion()['folio'] + 1
                modificar_folio_auto_asignacion(folio, ultima_asignacion[0])

            print("Folio creado: ", folio)

            while True:
                folio_de_viaje = f"{''.join(fecha_completa[:10].split('-'))[3:]}{self.idUnidad}{folio}"

                if len(folio_de_viaje) == 12:
                    vg.folio_asignacion = folio_de_viaje

                    self.settings.setValue('folio_de_viaje', folio_de_viaje)

                    print("Folio de viaje: ", folio_de_viaje)
                    logging.info(
                        f"[INICIO_VIAJE_OK] "
                        f"folio_de_viaje={folio_de_viaje}, "
                        f"csn_inicio={csn_inicio}, "
                        f"operador_inicio={self.settings.value('nombre_de_operador_inicio')}, "
                        f"servicio={servicio_elegido}, "
                        f"pension={pension_elegida}"
                    )

                    aniadir_folio_de_viaje_a_auto_asignacion(
                        folio,
                        folio_de_viaje,
                        fecha_vg
                    )

                    self.rutas = Rutas(
                        turno_elegido,
                        servicio_elegido,
                        self.close_signal,
                        self.close_signal_pasaje
                    )
                    self.rutas.setGeometry(0, 0, 800, 440)
                    self.rutas.setWindowFlags(Qt.FramelessWindowHint)
                    self.rutas.show()
                    break

                if self.intentos == 3:
                    self.intentos = 0

                    try:
                        ultimo_folio_de_autoasignacion = str(obtener_ultima_asignacion()[1])
                        eliminar_auto_asignacion_por_folio(ultimo_folio_de_autoasignacion)
                    except Exception as e:
                        logging.info(f"No se pudo eliminar autoasignacion fallida: {e}")

                    print("No se creo correctamente el folio")
                    logging.error(
                        f"[ERROR_INICIO_VIAJE] Folio de viaje inválido. "
                        f"folio={folio}, idUnidad={self.idUnidad}, "
                        f"folio_de_viaje={folio_de_viaje}"
                    )

                    self.limpiar_estado_inicio()
                    HUB.buzzer_blinks(5, on_ms=55, off_ms=55)
                    break

                self.intentos += 1

        except Exception as e:
            print(e)
            logging.info(e)

            self.limpiar_estado_inicio()
            HUB.buzzer_blinks(5, on_ms=55, off_ms=55)

    def crear_folio(self):
        try:
            folio = str(obtener_ultima_asignacion()[1])
            print("Folio de la base de datos: ", folio)
            if len(str(folio)) == 1:
                folio = "0" + str(folio)
            else:
                folio = str(folio)
            return folio
        except Exception as e:
            print(e)
            logging.info(e)

    # Cancelar
    def handle_cancel(self, event):
        try:
            try:
                HUB.write("fan_en", False)
            except Exception as e:
                logging.info(f"No se pudo apagar el ventilador: {e}")
            vg.csn_chofer = ""
            self.settings.setValue('ventana_actual', "")
            self.settings.setValue('csn_chofer', "")

            vg.numero_de_operador_inicio = ""
            vg.numero_de_operador_final = ""
            vg.nombre_de_operador_inicio = ""
            vg.nombre_de_operador_final = ""
            self.settings.setValue('numero_de_operador_inicio', "")
            self.settings.setValue('numero_de_operador_final', "")
            self.settings.setValue('nombre_de_operador_inicio', "")
            self.settings.setValue('nombre_de_operador_final', "")

            self.close()
        except Exception as e:
            print(e)
            logging.info(e)

    # Turno seleccionado
    def turno_seleccionado(self, seleccion):
        try:
            self.turno = seleccion
            vg.turno = self.turno
            self.settings.setValue('turno', self.turno)
        except Exception as e:
            print(e)
            logging.info(e)

    # Pensión seleccionada
    def pension_seleccionada(self, seleccion):
        try:
            self.pension_selec = seleccion
            self.comboBox_servicio.clear()
            self.lista_servicios = []
            self.lista_servicios_db = obtener_servicios_de_pension(self.pension_selec)
            for s in self.lista_servicios_db:
                servicio = str(s[0]) + " - " + str(s[1]) + " - " + str(s[2])
                self.lista_servicios.append(servicio)
            self.comboBox_servicio.addItems(self.lista_servicios)
        except Exception as e:
            print(e)
            logging.info(e)

    # Servicio seleccionado
    def servicio_seleccionado(self, seleccion):
        try:
            self.servicio = seleccion
        except Exception as e:
            print(e)
            logging.info(e)