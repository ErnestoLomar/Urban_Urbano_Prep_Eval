# pn532_blinka_adapter.py
# -*- coding: utf-8 -*-

import time
import board
import busio
import digitalio
from adafruit_pn532.spi import PN532_SPI


class Pn532Blinka:
    """
    Adapter PN532 (Blinka) SIN poseer el pin RST.
    El reset físico debe hacerse por GPIOHub (único dueño de D27).
    """

    def __init__(self, cs_pin=board.CE0, baudrate=250_000, rst_pin=None):
        self._cs_pin = cs_pin
        self._baudrate = baudrate

        self.spi = None
        self.cs = None
        self.pn = None

        self._tg = 0x01
        self.t_poll = 0.12
        self.t_apdu = 0.25

        self._build()

    def _build(self):
        self.spi = busio.SPI(board.SCLK, board.MOSI, board.MISO)

        t0 = time.monotonic()
        while not self.spi.try_lock():
            if (time.monotonic() - t0) > 0.8:
                raise RuntimeError("SPI busy (try_lock timeout)")
            time.sleep(0.001)

        try:
            self.spi.configure(baudrate=self._baudrate, phase=0, polarity=0)
        finally:
            self.spi.unlock()

        self.cs = digitalio.DigitalInOut(self._cs_pin)

        # IMPORTANTÍSIMO: reset=None para que Blinka NO toque D27
        self.pn = PN532_SPI(self.spi, self.cs, reset=None)

    def begin(self):
        return True

    def getFirmwareVersion(self):
        return self.pn.firmware_version

    def hard_reset(self):
        """No-op: el reset físico lo hace GPIOHub."""
        return False

    def _safe_call(self, cmd, response_length=0, params=b"", timeout=None, retries=3, sleep_s=0.02):
        if timeout is None:
            timeout = self.t_apdu

        last = None
        for _ in range(retries):
            try:
                return self.pn.call_function(cmd, response_length=response_length, params=params, timeout=timeout)
            except (RuntimeError, OSError) as e:
                last = e
                time.sleep(sleep_s)
                try:
                    self.pn.SAM_configuration()
                except Exception:
                    pass
        return None

    def _rf_tune(self):
        try:
            self._safe_call(0x32, response_length=0, params=bytes([0x01, 0x00]))
            time.sleep(0.02)
            self._safe_call(0x32, response_length=0, params=bytes([0x01, 0x01]))
            self._safe_call(0x32, response_length=0, params=bytes([0x05, 0xFF, 0x01, 0xFF]))
        except Exception:
            pass

    def SAMConfig(self):
        self.pn.SAM_configuration()
        self._rf_tune()

    def inListPassiveTarget(self, timeout=None):
        if timeout is None:
            timeout = self.t_poll
        resp = self._safe_call(0x4A, response_length=255, params=bytes([0x01, 0x00]), timeout=timeout)
        if resp and len(resp) >= 2 and resp[0] >= 1:
            self._tg = resp[1]
            return True
        return False

    def refresh_target(self, timeout=None):
        return self.inListPassiveTarget(timeout=timeout)

    def inDataExchange(self, data_bytes, response_len=255):
        resp = self._safe_call(
            0x40,
            response_length=response_len,
            params=bytes([self._tg]) + bytes(data_bytes),
            timeout=self.t_apdu
        )
        if not resp:
            return False, b""
        ok = (resp[0] == 0x00)
        return ok, bytes(resp[1:])

    def deinit(self):
        try:
            if self.cs:
                try:
                    self.cs.deinit()
                except Exception:
                    pass
        finally:
            self.cs = None

        try:
            if self.spi:
                try:
                    self.spi.deinit()
                except Exception:
                    pass
        finally:
            self.spi = None

        self.pn = None