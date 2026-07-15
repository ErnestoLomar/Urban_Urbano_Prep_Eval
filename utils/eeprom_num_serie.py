##########################################
# Autor: Ernesto Lomar
# Fecha de creación: 26/04/2022
# Ultima modificación: 15/08/2022
#
# Script para obtener número de serie y número de versión de la raspberry
##########################################

#Importamos librerías externas
import logging
import re
import subprocess


I2C_BUS = "1"
EEPROM_ADDRESS = "0x50"
MAX_DATA_ADDRESS = 0xFF
NUM_SERIE_START = 0
NUM_VERSION_START = 100
MAX_CAMPO_BYTES = 64
I2CGET_HEX_RE = re.compile(r"^0x[0-9a-fA-F]{2}$")


def _respuesta(state_num_serie, state_num_version):
    return {
        "state_num_serie": state_num_serie,
        "state_num_version": state_num_version,
    }


def _hay_bus_i2c():
    ok = subprocess.run(
        ["i2cdetect", "-y", I2C_BUS],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if ok.returncode != 0:
        logging.warning("No se pudo detectar bus I2C: %s", ok.stderr.strip())
        return False

    return True


def _leer_byte_eeprom(data_address):
    if data_address < 0 or data_address > MAX_DATA_ADDRESS:
        logging.warning("Direccion EEPROM fuera de rango: %s", hex(data_address))
        return None

    valor = subprocess.run(
        ["i2cget", "-y", I2C_BUS, EEPROM_ADDRESS, hex(data_address)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if valor.returncode != 0:
        logging.warning(
            "No se pudo leer EEPROM %s en %s: %s",
            EEPROM_ADDRESS,
            hex(data_address),
            valor.stderr.strip(),
        )
        return None

    salida = valor.stdout.strip()
    if not I2CGET_HEX_RE.match(salida):
        logging.warning(
            "Respuesta invalida de i2cget en %s: %r",
            hex(data_address),
            salida,
        )
        return None

    return int(salida, 16)


def _leer_texto_eeprom(inicio, max_bytes=MAX_CAMPO_BYTES):
    datos = bytearray()
    fin = min(MAX_DATA_ADDRESS + 1, inicio + max_bytes)

    for data_address in range(inicio, fin):
        byte = _leer_byte_eeprom(data_address)

        if byte is None:
            return None

        if byte == 0:
            break

        datos.append(byte)

    try:
        return datos.decode("utf-8")
    except UnicodeDecodeError:
        logging.warning("La EEPROM contiene bytes que no son UTF-8 desde %s", hex(inicio))
        return datos.decode("utf-8", errors="replace")

#Función para obtener el número de serie y número de versión de la memoria EEPROM y mostrarlo en la GUI
def cargar_num_serie():
    try:
        if not _hay_bus_i2c():
            return _respuesta("NSxxxxx", "NVxxxxx")

        state_num_serie = _leer_texto_eeprom(NUM_SERIE_START)
        state_num_version = _leer_texto_eeprom(NUM_VERSION_START)

        if state_num_serie is None or state_num_version is None:
            return _respuesta("ERR", "ERR")

        return _respuesta(state_num_serie, state_num_version)
    except Exception as e:
        logging.error(e)
        return _respuesta("ERR", "ERR")
