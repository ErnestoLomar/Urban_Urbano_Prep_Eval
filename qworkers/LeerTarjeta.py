##########################################
# Lector de tarjetas + QR separados
# NFC aislado en PROCESO (nfc_reader_proc.py)
##########################################

# Librerías externas
from PyQt5.QtCore import QObject, pyqtSignal, QSettings, QThread, pyqtSlot, Qt
import time
import serial
import logging
from time import strftime
from datetime import datetime
import queue as pyqueue
import subprocess
import multiprocessing as mp
import os

# Librerías propias
from ventas_queries import (
    insertar_item_venta,
    obtener_ultimo_folio_de_item_venta,
    guardar_venta_digital,
    obtener_ultimo_folio_de_venta_digital,
    actualizar_estado_venta_digital_revisado,
)
from queries import obtener_datos_aforo, insertar_estadisticas_boletera
from tickets_usados import insertar_ticket_usado, verificar_ticket_completo, verificar_ticket
import variables_globales as vg
from hw import HUB

from nfc_reader_proc import nfc_reader_main

setattr(vg, "nfc_closed_for_hce", False)

_NFC_MAX_FALLOS_CONSECUTIVOS = 3
_NFC_RESET_COOLDOWN_S = 10.0
_QR_COOLDOWN_S = 2.0

MAIN_LOG_PATH = "/home/pi/Urban_Urbano/logs/leertarjeta.log"


def _setup_main_logging():
    try:
        os.makedirs(os.path.dirname(MAIN_LOG_PATH), exist_ok=True)
        logging.basicConfig(
            filename=MAIN_LOG_PATH,
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )
    except Exception:
        pass


_setup_main_logging()


class QrReaderWorker(QObject):
    mostrar_mensaje = pyqtSignal(str, str, float)

    def __init__(self, hub, id_unidad: str):
        super().__init__()
        self.settings = None
        self.hub = hub
        self.idUnidad = id_unidad
        self.ser = None
        self._running = True
        self.ultimo_qr = ""
        self.ultimo_qr_ts = 0.0

    @pyqtSlot()
    def start(self):
        try:
            self.settings = QSettings('/home/pi/Urban_Urbano/ventanas/settings.ini', QSettings.IniFormat)
        except Exception as e:
            logging.info(f"QR settings error: {e}")
            self.settings = None

        try:
            self.ser = serial.Serial(port='/dev/ttyACM0', baudrate=115200, timeout=1)
            print("QR: puerto abierto", flush=True)
        except Exception as e:
            print("QR: no se pudo abrir puerto al inicio:", e, flush=True)
            try:
                self.ser = serial.Serial()
            except Exception:
                self.ser = None

        self.run()

    @pyqtSlot()
    def stop(self):
        self._running = False
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass

    def _restablecer_puerto(self):
        try:
            time.sleep(1)
            if self.ser and self.ser.is_open:
                print("QR: cerrando puerto", flush=True)
                try:
                    self.ser.close()
                except Exception:
                    pass
            while self._running and (not self.ser or not self.ser.is_open):
                try:
                    print("QR: intentando abrir puerto /dev/ttyACM0", flush=True)
                    time.sleep(2)
                    self.ser = serial.Serial(port='/dev/ttyACM0', baudrate=115200, timeout=1)
                    print("QR: puerto restablecido", flush=True)
                except Exception:
                    pass
        except Exception as e:
            print("QR: error restableciendo puerto:", e, flush=True)
            logging.info(f"QR restablecer error: {e}")

    def _emit_mensaje(self, titulo: str, cuerpo: str, segundos: float):
        try:
            self.mostrar_mensaje.emit(titulo or "", cuerpo or "", float(segundos))
        except Exception as e:
            logging.info(f"QR emit error: {e}")

    def run(self):
        try:
            while self._running:
                if not (self.ser and self.ser.is_open):
                    self._restablecer_puerto()
                    continue

                try:
                    qr_bytes = self.ser.readline()
                except Exception as e:
                    print("QR: error al leer:", e, flush=True)
                    logging.info(f"QR read error: {e}")
                    self._restablecer_puerto()
                    continue

                qr_str_raw = (qr_bytes or b"").decode(errors="ignore").strip()
                if not qr_str_raw:
                    continue

                qr_str = qr_str_raw
                if "PD," in qr_str_raw:
                    partes = qr_str_raw.split("PD,")
                    candidatos = []
                    for p in partes:
                        p = p.strip()
                        if not p:
                            continue
                        cand = "PD," + p
                        candidatos.append(cand)

                    if candidatos:
                        elegido = None
                        # buscamos desde el último hacia atrás uno que parezca completo
                        for cand in reversed(candidatos):
                            # 13 campos => al menos 12 comas
                            if cand.count(",") >= 12:
                                elegido = cand
                                break
                        if elegido is None:
                            # si ninguno cumple, nos quedamos con el último por compatibilidad
                            elegido = candidatos[-1]
                        qr_str = elegido
                    else:
                        qr_str = qr_str_raw

                now = time.monotonic()

                # Filtro de relecturas del MISMO QR muy seguidas
                if qr_str == getattr(self, "ultimo_qr", ""):
                    # Si es el mismo QR y pasó poco tiempo, lo ignoramos por completo
                    if (now - getattr(self, "ultimo_qr_ts", 0.0)) < _QR_COOLDOWN_S:
                        # Solo lo ignoramos, sin ventana ni beep
                        continue
                    else:
                        # Si ya pasó el cooldown, lo tratamos como "UTILIZADO"
                        print("El ultimo QR se vuelve a pasar")
                        self._emit_mensaje("UTILIZADO", ".....", 4.5)
                        self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                        time.sleep(4.5)
                        self.ultimo_qr = qr_str
                        self.ultimo_qr_ts = time.monotonic()
                        continue

                # ---- LÓGICA ORIGINAL DE QR (idéntica) ----
                try:
                    print("El QR es:", qr_str)

                    if str(self.settings.value('folio_de_viaje')) == "":
                        print("No hay ningún viaje activo")
                        self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                        time.sleep(1)
                        continue

                    qr_list = [p.strip() for p in qr_str.split(",")]
                    print("El tamaño del QR es:", len(qr_list))

                    # --- NUEVO FORMATO PD ---
                    if len(qr_list) >= 1 and qr_list[0] == "PD":
                        print("El QR es Nuevo")
                        if len(qr_list) != 13:
                            print("El QR digital no es válido")
                            self._emit_mensaje("INVALIDO", "", 4.5)
                            self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                            time.sleep(4.5)
                            continue

                        print("El QRList es:", qr_list)

                        _, unidad_qr, fecha_qr, hora_qr, id_tarifa, origen, destino, tipo_de_pasajero, servicio_qr, id_monedero, saldo_posterior, precio, tipo_transaccion = qr_list

                        fecha_hoy = strftime('%d-%m-%Y').replace('/', '-')
                        if fecha_hoy != fecha_qr:
                            self._emit_mensaje("CADUCO", "Fecha diferente", 4.5)
                            print("La fecha del QR es diferente a la fecha actual")
                            self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                            time.sleep(4.5)
                            continue

                        print("Fecha valida")

                        en_geocerca = False
                        try:
                            geo_actual = str(str(vg.geocerca.split(",")[1]).split("_")[0])
                            origen_norm = str(origen).split("_")[0]
                            if origen_norm == geo_actual:
                                en_geocerca = True
                        except Exception as e:
                            print("Error en geocerca: ", e)
                            logging.info(e)

                        if not en_geocerca:
                            self._emit_mensaje("EQUIVOCADO", str(origen), 4.5)
                            self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                            time.sleep(4.5)
                            print("La geocerca no es valida")
                            continue

                        print("Geocerca valida")

                        es_ticket_usado = verificar_ticket_completo(qr_str)
                        if es_ticket_usado is not None:
                            self._emit_mensaje("UTILIZADO", ".....", 4.5)
                            self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                            time.sleep(4.5)
                            print("El ticket ya fue usado")
                            # marcamos también como último QR para que el cooldown funcione
                            self.ultimo_qr = qr_str
                            self.ultimo_qr_ts = time.monotonic()
                            continue

                        print("Ticket valido")

                        servicio = servicio_qr
                        if not servicio:
                            try:
                                for servicio_vg in vg.todos_los_servicios_activos:
                                    if str(destino) in str(servicio_vg[2]):
                                        servicio = str(servicio_vg[5]) + "-" + str(str(servicio_vg[1]).split("_")[0]) + "-" + str(str(servicio_vg[2]).split("_")[0])
                                        break
                                if not servicio:
                                    for transbordo in vg.todos_los_transbordos_activos:
                                        if str(destino) in str(transbordo[2]):
                                            servicio = str(transbordo[5]) + "-" + str(str(transbordo[1]).split("_")[0]) + "-" + str(str(transbordo[2]).split("_")[0])
                                            break
                            except Exception as e:
                                print("Error al obtener el servicio: ", e)
                                logging.info(e)

                        print("Servicio valido")

                        usted_se_dirige = str(servicio).split("-")[2] if servicio else ""
                        print("Usted se dirige:", usted_se_dirige)

                        try:
                            ultimo = obtener_ultimo_folio_de_venta_digital() or (None, 0)
                            print("Ultimo folio digital:", ultimo)
                            folio_venta_digital = (ultimo[1] if isinstance(ultimo, (list, tuple)) and len(ultimo) > 1 else 0) + 1
                            print("Folio digital:", folio_venta_digital)
                            logging.info(f"Folio digital: {folio_venta_digital}")
                        except Exception as e:
                            logging.info(e)
                            print("Error al obtener el folio digital: ", e)
                            folio_venta_digital = 1

                        print("Folio valido")

                        try:
                            folio_asignacion = str(self.settings.value('folio_de_viaje'))
                            geocerca_id = int(str(self.settings.value('geocerca')).split(",")[0])

                            venta_guardada = guardar_venta_digital(
                                folio_venta_digital,
                                folio_asignacion,
                                fecha_qr,
                                hora_qr,
                                id_tarifa,
                                geocerca_id,
                                tipo_de_pasajero,
                                "n",
                                tipo_transaccion,
                                id_monedero,
                                saldo_posterior,
                                precio
                            )
                        except Exception as e:
                            logging.info(e)
                            print("Error al guardar la venta digital: ", e)
                            venta_guardada = None

                        if venta_guardada:
                            try:
                                insertar_ticket_usado(qr_str)
                            except Exception as e:
                                logging.info(e)
                            try:
                                self.ultimo_qr = qr_str
                                self.ultimo_qr_ts = time.monotonic()
                                self.settings.setValue('total_de_folios', f"{int(self.settings.value('total_de_folios')) + 1}")
                            except Exception as e:
                                print("Error al actualizar el ultimo QR: ", e)
                                logging.info(e)

                            actualizar_estado_venta_digital_revisado("OK", folio_venta_digital, folio_asignacion)
                            print("Estado de venta actualizado a OK.")

                            self._emit_mensaje("ACEPTADO", usted_se_dirige if usted_se_dirige else "No encontrado", 5.0)
                        else:
                            self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                            print("Error al guardar la venta digital")
                            time.sleep(0.5)
                        continue  # fin PD

                    print("Formato anterior")

                    # --- FORMATO ANTERIOR (9/10) ---
                    if len(qr_list) not in (9, 10):
                        self._emit_mensaje("INVALIDO", "", 4.5)
                        self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                        time.sleep(4.5)
                        continue

                    fecha_qr = qr_list[0]
                    fecha_hoy = strftime('%d-%m-%Y').replace('/', '-')
                    if fecha_hoy != fecha_qr:
                        self._emit_mensaje("CADUCO", "Fecha diferente", 4.5)
                        self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                        time.sleep(4.5)
                        continue

                    hora_caduca = qr_list[1]
                    hora_actual = strftime("%H:%M:%S")
                    if hora_actual > hora_caduca:
                        self._emit_mensaje("CADUCO", str(hora_caduca), 4.5)
                        self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                        time.sleep(4.5)
                        continue

                    tramo = qr_list[5]
                    tipo_de_pasajero = str(qr_list[6]).lower()
                    p_n = "normal"
                    if tipo_de_pasajero == "estudiante":
                        id_tipo_de_pasajero, p_n = 1, "preferente"
                    elif tipo_de_pasajero == "menor":
                        id_tipo_de_pasajero, p_n = 3, "preferente"
                    elif tipo_de_pasajero == "mayor":
                        id_tipo_de_pasajero, p_n = 4, "preferente"
                    else:
                        id_tipo_de_pasajero = 2

                    en_geocerca = False
                    try:
                        doble_tarnsbordo_o_no = str(qr_list[7])
                        geo_actual = str(str(vg.geocerca.split(",")[1]).split("_")[0])
                        if doble_tarnsbordo_o_no == "st":
                            if geo_actual in str(qr_list[8]):
                                en_geocerca = True
                        else:
                            if geo_actual in str(qr_list[8]) or (len(qr_list) > 9 and geo_actual in str(qr_list[9])):
                                en_geocerca = True
                    except Exception as e:
                        logging.info(e)

                    if not en_geocerca:
                        if doble_tarnsbordo_o_no == "st":
                            destino_esperado = str(qr_list[8])
                        else:
                            destino_esperado = f"{qr_list[8]} o {qr_list[9]}" if len(qr_list) > 9 else str(qr_list[8])

                        self._emit_mensaje("EQUIVOCADO", destino_esperado, 4.5)
                        self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                        time.sleep(4.5)
                        continue

                    es_ticket_usado = verificar_ticket_completo(qr_str)
                    if es_ticket_usado is not None:
                        self._emit_mensaje("UTILIZADO", ".....", 4.5)
                        self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                        time.sleep(4.5)
                        # marcamos QR para que el cooldown lo ignore en relecturas
                        self.ultimo_qr = qr_str
                        self.ultimo_qr_ts = time.monotonic()
                        continue

                    try:
                        from impresora import imprimir_boleto_normal_sin_servicio, imprimir_boleto_normal_con_servicio
                    except Exception as e:
                        logging.info(e)

                    servicio = ""
                    usted_se_dirige = ""
                    destino = str(tramo).split("-")[1] if "-" in str(tramo) else str(tramo)

                    if doble_tarnsbordo_o_no == "st":
                        for servicio_vg in vg.todos_los_servicios_activos:
                            if str(destino) in str(servicio_vg[2]):
                                servicio = str(servicio_vg[5]) + "-" + str(str(servicio_vg[1]).split("_")[0]) + "-" + str(str(servicio_vg[2]).split("_")[0])
                    else:
                        for transbordo in vg.todos_los_transbordos_activos:
                            if str(destino) in str(transbordo[2]):
                                servicio = str(transbordo[5]) + "-" + str(str(transbordo[1]).split("_")[0]) + "-" + str(str(transbordo[2]).split("_")[0])

                    ultimo_folio_de_venta = obtener_ultimo_folio_de_item_venta()
                    if ultimo_folio_de_venta is not None:
                        if int(self.settings.value('reiniciar_folios')) == 0:
                            ultimo_folio_de_venta = int(ultimo_folio_de_venta[1]) + 1
                        else:
                            ultimo_folio_de_venta = 1
                            self.settings.setValue('reiniciar_folios', 0)
                    else:
                        ultimo_folio_de_venta = 1

                    hecho = False
                    if servicio != "":
                        usted_se_dirige = str(servicio).split("-")[2]
                        hecho = imprimir_boleto_normal_con_servicio(
                            ultimo_folio_de_venta, fecha_hoy, hora_actual, self.idUnidad, servicio, tramo, qr_list
                        )
                    else:
                        hecho = imprimir_boleto_normal_sin_servicio(
                            ultimo_folio_de_venta, fecha_hoy, hora_actual, self.idUnidad, tramo, qr_list
                        )

                    if hecho:
                        insertar_item_venta(
                            ultimo_folio_de_venta,
                            str(self.settings.value('folio_de_viaje')),
                            fecha_hoy,
                            hora_actual,
                            int(0),
                            int(str(self.settings.value('geocerca')).split(",")[0]),
                            id_tipo_de_pasajero,
                            "t",
                            p_n,
                            tipo_de_pasajero,
                            0
                        )

                        self.ultimo_qr = qr_str
                        self.ultimo_qr_ts = time.monotonic()
                        self.settings.setValue('total_de_folios', f"{int(self.settings.value('total_de_folios')) + 1}")
                        insertar_ticket_usado(qr_str)

                        self._emit_mensaje("ACEPTADO", usted_se_dirige if usted_se_dirige != "" else "No encontrado", 5.0)
                    else:
                        self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                        time.sleep(0.5)

                except Exception as e:
                    print("QR: excepción general:", e)
                    logging.info(e)

        except Exception as e:
            print("QR loop aborted:", e)
            logging.info(e)


# ==============================================================#
#                  WORKER NFC (TARJETAS) ÚNICAMENTE             #
# ==============================================================#
class LeerTarjetaWorker(QObject):

    try:
        finished = pyqtSignal()
        progress = pyqtSignal(str)
        mensaje = pyqtSignal(str, str, float)
        leyendo_tarjeta = pyqtSignal(bool)  # True=mostar loading, False=ocultar
    except Exception as e:
        print(e)
        logging.info(e)

    def __init__(self):
        super().__init__()
        self.hub = HUB

        self._nfc_fallos = 0
        self._nfc_ultimo_reset_ts = 0.0

        # Anti “doble lectura / lectura a medias”
        self._last_ok_csn = ""
        self._last_ok_ts = 0.0
        self._ignore_until = 0.0
        self._invalid_streak = 0
        
        self._ui_loading = False

        # (Opcional) estabilidad: mismo pack 2 veces antes de actuar
        # self._last_pack_seen = ""
        # self._last_pack_seen_count = 0

        self.settings = QSettings('/home/pi/Urban_Urbano/ventanas/settings.ini', QSettings.IniFormat)
        try:
            self.idUnidad = str(obtener_datos_aforo()[1])
        except Exception:
            self.idUnidad = ""

        # --- Proceso NFC aislado ---
        self._ctx = mp.get_context("spawn")
        self._cmd_q = self._ctx.Queue()
        self._evt_q = self._ctx.Queue()
        self._nfc_proc = None
        self._last_mode_sent = None
        self._start_nfc_process()

        self.pn532_hard_reset()

        # --- Hilo QR (igual que lo tienes) ---
        self.qr_thread = QThread(self)
        self.qr_worker = QrReaderWorker(self.hub, self.idUnidad)
        self.qr_worker.moveToThread(self.qr_thread)
        self.qr_worker.mostrar_mensaje.connect(self.reenviar_mensaje, Qt.QueuedConnection)
        self.qr_thread.started.connect(self.qr_worker.start)
        self.qr_thread.start()
        
    def _set_loading(self, on: bool):
        if on != self._ui_loading:
            self._ui_loading = on
            try:
                self.leyendo_tarjeta.emit(on)
            except Exception:
                pass

    def _start_nfc_process(self):
        if self._nfc_proc and self._nfc_proc.is_alive():
            return
        try:
            self._nfc_proc = self._ctx.Process(
                target=nfc_reader_main,
                args=(self._cmd_q, self._evt_q),
            )
            self._nfc_proc.daemon = True
            self._nfc_proc.start()
            logging.info(f"NFC proc started pid={self._nfc_proc.pid}")
            print(f"NFC proc started pid={self._nfc_proc.pid}", flush=True)
        except Exception as e:
            logging.info(f"No se pudo iniciar proceso NFC: {e}")
            print(f"No se pudo iniciar proceso NFC: {e}", flush=True)

    def _send_nfc_cmd(self, d: dict):
        try:
            self._cmd_q.put(d)
        except Exception:
            pass

    @pyqtSlot(str, str, float)
    def reenviar_mensaje(self, titulo, cuerpo, segundos):
        try:
            self.mensaje.emit(titulo, cuerpo, segundos)
        except Exception as e:
            logging.info(f"reenviar_mensaje error: {e}")

    def pn532_hard_reset(self):
        try:
            self._send_nfc_cmd({"type": "CLOSE"})
            time.sleep(0.10)
        except Exception:
            pass

        try:
            print("\x1b[1;32mHard reset PN532\033[0;m", flush=True)
            self.hub.pulse("nfc_rst", 400)
        except Exception as e:
            logging.info(f"Error reset PN532: {e}")
            print(f"Error reset PN532: {e}", flush=True)

        time.sleep(0.60)

    def _maybe_reset_nfc(self):
        now = time.monotonic()
        if (
            self._nfc_fallos >= _NFC_MAX_FALLOS_CONSECUTIVOS
            and (now - self._nfc_ultimo_reset_ts) >= _NFC_RESET_COOLDOWN_S
        ):
            self.pn532_hard_reset()
            self._nfc_ultimo_reset_ts = now
            self._nfc_fallos = 0

    def _campo_invalido(self, valor):
        v = (valor or "").strip().upper()
        return v in ("IN", "INVALID", "INVALIDO", "ERROR")

    def _sync_mode_to_process(self):
        mode = "CARD" if vg.modo_nfcCard else "HCE"
        if mode != self._last_mode_sent:
            self._send_nfc_cmd({"type": "SET_MODE", "mode": mode})
            self._last_mode_sent = mode
            print(f"NFC mode -> {mode}", flush=True)
            logging.info(f"NFC mode -> {mode}")

    def _drain_evt_queue(self, max_items=200):
        """
        Vacia la cola para evitar procesar basura acumulada (muy importante si antes había sleep).
        """
        try:
            n = 0
            while n < max_items:
                _ = self._evt_q.get_nowait()
                n += 1
        except pyqueue.Empty:
            return
        except Exception:
            return

    def _get_latest_pack(self):
        """
        Drena eventos y se queda con el último PACK disponible.
        Retorna string pack o "".
        """
        pack = ""
        try:
            while True:
                evt = self._evt_q.get_nowait()
                if not evt:
                    continue

                t = evt.get("type")
                if t == "LOG":
                    msg = evt.get("msg", "")
                    if msg:
                        # si no quieres spam, comenta este print:
                        # print(f"[NFC PROC] {msg}", flush=True)
                        logging.info(f"[NFC PROC] {msg}")

                elif t == "ERROR":
                    err = evt.get("err", "")
                    if err:
                        print(f"[NFC PROC ERROR] {err}", flush=True)
                        logging.error(f"[NFC PROC ERROR] {err}")

                elif t == "PACK":
                    pack = evt.get("pack", "") or ""

        except pyqueue.Empty:
            pass
        except Exception as e:
            logging.info(f"evt drain error: {e}")

        return pack

    def _is_noise_pack(self, csn, tipo, vig, nombre):
        """
        Define “lectura a medias / ruido”:
        - csn vacío
        - o tipo/vig/nombre == IN (con csn presente suele pasar cuando el lector pierde la tarjeta)
        """
        if not csn:
            return True
        # si el UID existe pero lo demás viene IN, típicamente es “tarjeta movida/retirada”
        if self._campo_invalido(tipo) or self._campo_invalido(vig) or self._campo_invalido(nombre):
            return True
        return False

    def run(self):
        poll_interval = 0.03

        while True:
            start = time.monotonic()
            now = start

            # reiniciar proceso si murió
            if not (self._nfc_proc and self._nfc_proc.is_alive()):
                print("NFC proc muerto, reiniciando...", flush=True)
                logging.info("NFC proc muerto, reiniciando...")
                self._start_nfc_process()
                self._last_mode_sent = None
                # limpia cola vieja
                self._drain_evt_queue()

            self._sync_mode_to_process()

            # si NO estamos en modo CARD, no procesar PACKs
            if not vg.modo_nfcCard:
                vg.nfc_closed_for_hce = True
                time.sleep(0.05)
                continue
            else:
                if vg.nfc_closed_for_hce:
                    vg.nfc_closed_for_hce = False

            # cooldown después de un OK: ignora todo (evita doble ventana)
            if now < self._ignore_until:
                # drena para que no se acumule basura
                self._drain_evt_queue(max_items=50)
                elapsed = time.monotonic() - start
                if elapsed < poll_interval:
                    time.sleep(poll_interval - elapsed)
                continue

            pack = self._get_latest_pack()
            if not pack:
                elapsed = time.monotonic() - start
                if elapsed < poll_interval:
                    time.sleep(poll_interval - elapsed)
                continue
            
            # === AQUI PRENDES EL LOADING ===
            # self._set_loading(True)

            # (Opcional) exigir estabilidad: mismo pack 2 veces antes de actuar
            #if pack == self._last_pack_seen:
            #    self._last_pack_seen_count += 1
            #else:
            #    self._last_pack_seen = pack
            #    self._last_pack_seen_count = 1

            #if self._last_pack_seen_count < 2:
            #    # aún no “estable”
            #    elapsed = time.monotonic() - start
            #    if elapsed < poll_interval:
            #        time.sleep(poll_interval - elapsed)
            #    continue
            
            try:
                print(f"NFC PACK recibido: {pack}", flush=True)
                logging.info(f"NFC PACK recibido: {pack}")

                try:
                    csn, tipo, vig, nombre = pack.split("|", 3)
                except ValueError:
                    # no muestres inválida por basura; sólo cuenta fallo
                    self._nfc_fallos += 1
                    self._maybe_reset_nfc()
                    continue

                csn = (csn or "").strip()
                tipo = (tipo or "").strip()
                vig = (vig or "").strip()
                nombre = (nombre or "").strip()

                print(f"csn={csn} tipo={tipo} vig={vig[:12]} nombre={nombre[:12]}", flush=True)

                # Si es “ruido / lectura a medias”, NO dispares TARJETAINVALIDA inmediatamente.
                if self._is_noise_pack(csn, tipo, vig, nombre):
                    # si hubo OK reciente, ignora totalmente
                    if (now - self._last_ok_ts) < 1.2:
                        continue

                    # exige varias inválidas seguidas antes de mostrar ventana
                    self._invalid_streak += 1
                    if self._invalid_streak < 3:
                        continue

                    self._invalid_streak = 0
                    self._nfc_fallos += 1
                    self._maybe_reset_nfc()
                    try:
                        self.mensaje.emit("TARJETAINVALIDA", "", 2.0)
                    except Exception:
                        pass
                    self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                    time.sleep(0.2)
                    continue

                # pack bueno => reset streak
                self._invalid_streak = 0
                if self._nfc_fallos:
                    self._nfc_fallos = 0

                # Dedupe: misma tarjeta OK repetida muy rápido
                if csn == self._last_ok_csn and (now - self._last_ok_ts) < 1.2:
                    continue

                # --- Tu lógica actual (KI) ---
                fecha = strftime('%Y/%m/%d').replace('/', '')[2:]
                fecha_actual = str(subprocess.run("date", stdout=subprocess.PIPE, shell=True))
                indice = fecha_actual.find(":")
                hora = str(fecha_actual[(int(indice) - 2):(int(indice) + 6)]).replace(":", "")

                try:
                    tipo2 = (tipo or "")[:2]
                    if tipo2 == "KI":
                        vig = vig or ""
                        nombre_limpio = (nombre or "").replace("*", " ").replace(".", " ").replace("-", " ").replace("_", " ")
                        datos_completos_tarjeta = f"{vig}{nombre}"
                        vigenciaTarjeta = vig[:12]
                        print("Datos completos de la tarjeta:", datos_completos_tarjeta, flush=True)

                        if len(vigenciaTarjeta) == 12 and vigenciaTarjeta[:2].isdigit() and int(vigenciaTarjeta[:2]) >= 22:
                            now_dt = datetime.now()
                            vigenciaActual = f'{str(now_dt.strftime("%Y-%m-%d %H:%M:%S"))[2:].replace(" ", "").replace("-", "").replace(":", "")}'
                            if vigenciaActual <= vigenciaTarjeta:
                                if len(csn) == 14:
                                    # ======= OK =======
                                    vg.vigencia_de_tarjeta = vigenciaTarjeta
                                    num_operador = vig[12:17] if len(vig) >= 17 else ""

                                    if str(self.settings.value('ventana_actual')) not in ("chofer", "corte", "enviar_vuelta", "cerrar_turno"):
                                        if len(vg.numero_de_operador_inicio) > 0 or len(self.settings.value('numero_de_operador_inicio')) > 0:
                                            vg.numero_de_operador_final = num_operador
                                            vg.nombre_de_operador_final = nombre_limpio
                                            self.settings.setValue('numero_de_operador_final', f"{num_operador}")
                                            self.settings.setValue('nombre_de_operador_final', f"{nombre_limpio}")
                                        else:
                                            vg.numero_de_operador_inicio = num_operador
                                            vg.nombre_de_operador_inicio = nombre_limpio
                                            self.settings.setValue('numero_de_operador_inicio', f"{num_operador}")
                                            self.settings.setValue('nombre_de_operador_inicio', f"{nombre_limpio}")

                                    vg.csn_chofer_respaldo = csn
                                    self.progress.emit(csn)
                                    self.hub.buzzer_blinks(2, on_ms=100, off_ms=100)

                                    # *** CLAVE: marcar OK + cooldown + limpiar cola ***
                                    self._last_ok_csn = csn
                                    self._last_ok_ts = time.monotonic()
                                    self._ignore_until = self._last_ok_ts + 1.0  # 1s de blindaje
                                    self._drain_evt_queue(max_items=200)

                                    # NO hagas sleep(2) aquí; eso genera backlog
                                    # time.sleep(0.05)

                                else:
                                    insertar_estadisticas_boletera(str(self.idUnidad), fecha, hora, "TI", f"{csn},{vigenciaTarjeta}")
                                    try:
                                        self.mensaje.emit("TARJETAINVALIDA", "", 2.0)
                                    except Exception:
                                        pass
                                    self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                                    time.sleep(0.2)
                            else:
                                insertar_estadisticas_boletera(str(self.idUnidad), fecha, hora, "SV", f"{csn}")
                                try:
                                    self.mensaje.emit("FUERADEVIGENCIA", "", 2.0)
                                except Exception:
                                    pass
                                self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                                time.sleep(0.2)
                        else:
                            insertar_estadisticas_boletera(str(self.idUnidad), fecha, hora, "TI", f"{csn},{vigenciaTarjeta}")
                            try:
                                self.mensaje.emit("TARJETAINVALIDA", "", 2.0)
                            except Exception:
                                pass
                            self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                            time.sleep(0.2)
                    else:
                        # Si quieres que NO dispare inválida para NO/otros, cámbialo aquí.
                        insertar_estadisticas_boletera(str(self.idUnidad), fecha, hora, "TD", f"{csn},{tipo}")
                        try:
                            self.mensaje.emit("TARJETAINVALIDA", "", 2.0)
                        except Exception:
                            pass
                        self.hub.buzzer_blinks(5, on_ms=55, off_ms=55)
                        time.sleep(0.2)

                except Exception as e:
                    print("\x1b[1;31;47mNo se pudo leer la tarjeta:\033[0;m", str(e), flush=True)
                    logging.info(e)
                    self._nfc_fallos += 1
                    self._maybe_reset_nfc()
            finally:
                # === SIEMPRE APAGAR LOADING ===
                self._set_loading(False)

            elapsed = time.monotonic() - start
            if elapsed < poll_interval:
                time.sleep(poll_interval - elapsed)

    def stop_all(self):
        try:
            self.qr_worker.stop()
        except Exception:
            pass
        try:
            self.qr_thread.quit()
            self.qr_thread.wait(1000)
        except Exception:
            pass

        try:
            self._send_nfc_cmd({"type": "STOP"})
            time.sleep(0.1)
        except Exception:
            pass
        try:
            if self._nfc_proc and self._nfc_proc.is_alive():
                self._nfc_proc.terminate()
        except Exception:
            pass