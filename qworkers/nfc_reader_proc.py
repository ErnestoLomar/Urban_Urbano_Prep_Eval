# /home/pi/Urban_Urbano/qworkers/nfc_reader_proc.py
import os
import time
import ctypes
import queue
import logging
import traceback

LOG_PATH = "/home/pi/Urban_Urbano/logs/nfc_proc.log"

def _setup_logging():
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        logging.basicConfig(
            filename=LOG_PATH,
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )
    except Exception:
        pass

def nfc_reader_main(cmd_q, evt_q, so_path="/home/pi/Urban_Urbano/qworkers/libernesto.so"):
    """
    Proceso dedicado a hablar con libernesto.so (libnfc/freefare).
    - Recibe comandos por cmd_q: SET_MODE/CLOSE/STOP
    - Emite eventos por evt_q: PACK/ERROR/LOG
    """
    _setup_logging()

    def send_log(msg: str):
        try:
            logging.info(msg)
        except Exception:
            pass
        try:
            evt_q.put({"type": "LOG", "msg": msg, "ts": time.time()})
        except Exception:
            pass

    def send_err(msg: str):
        try:
            logging.error(msg)
        except Exception:
            pass
        try:
            evt_q.put({"type": "ERROR", "err": msg, "ts": time.time()})
        except Exception:
            pass

    # Reduce ruido de libnfc, y habilita debug de tu .so si quieres
    os.environ.setdefault("LIBNFC_LOG_LEVEL", "0")
    os.environ.setdefault("LIBERNESTO_DEBUG", "1")  # cambia a "0" si no quieres debug del .so

    send_log(f"nfc_reader_main start (pid={os.getpid()}) so_path={so_path}")

    try:
        lib = ctypes.CDLL(so_path, mode=getattr(ctypes, "RTLD_GLOBAL", 0))

        lib.ev2PackInfo.argtypes = []
        lib.ev2PackInfo.restype = ctypes.c_char_p

        lib.nfc_close_all.argtypes = []
        lib.nfc_close_all.restype = None

        if hasattr(lib, "nfc_ping"):
            lib.nfc_ping.argtypes = []
            lib.nfc_ping.restype = ctypes.c_int

        send_log("CDLL loaded OK")

    except Exception as e:
        send_err(f"CDLL load failed: {e}")
        return

    mode = "CARD"  # "CARD" o "HCE"
    closed_for_hce = False

    last_emit = ""
    last_emit_ts = 0.0

    last_heartbeat = 0.0

    def do_close():
        nonlocal closed_for_hce
        try:
            lib.nfc_close_all()
            send_log("nfc_close_all() called")
        except Exception as e:
            send_err(f"nfc_close_all exception: {e}")
        closed_for_hce = True

    while True:
        # heartbeat cada 5s
        now = time.monotonic()
        if now - last_heartbeat > 5.0:
            send_log(f"heartbeat mode={mode} closed_for_hce={closed_for_hce}")
            last_heartbeat = now

        # 1) comandos del proceso principal
        try:
            while True:
                cmd = cmd_q.get_nowait()
                if not cmd:
                    continue

                t = cmd.get("type")
                if t == "STOP":
                    send_log("CMD STOP")
                    do_close()
                    return

                if t == "SET_MODE":
                    new_mode = cmd.get("mode", "CARD")
                    if new_mode != mode:
                        send_log(f"CMD SET_MODE {mode} -> {new_mode}")
                    mode = new_mode
                    if mode != "CARD":
                        if not closed_for_hce:
                            do_close()
                    else:
                        closed_for_hce = False

                if t == "CLOSE":
                    send_log("CMD CLOSE")
                    do_close()

        except queue.Empty:
            pass
        except Exception as e:
            send_err(f"cmd loop error: {e}")

        # 2) si estamos en HCE, no tocamos NFC
        if mode != "CARD":
            time.sleep(0.05)
            continue

        # 3) leer pack
        try:
            b = lib.ev2PackInfo()
            if b:
                s = b.decode("utf-8", "ignore").strip()
                if s:
                    # evita spam de mismo PACK en ráfaga
                    tnow = time.monotonic()
                    if s != last_emit or (tnow - last_emit_ts) > 0.05:
                        evt_q.put({"type": "PACK", "pack": s, "ts": time.time()})
                        send_log(f"PACK: {s}")
                        last_emit = s
                        last_emit_ts = tnow

        except Exception as e:
            send_err("ev2PackInfo exception: " + str(e) + " | " + traceback.format_exc())
            do_close()
            time.sleep(0.20)

        time.sleep(0.02)