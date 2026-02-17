# hw.py
# Singleton de hardware (GPIOHub) para TODO el proyecto.
# Evita crear múltiples instancias de RPi.GPIO en el mismo proceso.

import atexit
import logging

from gpio_hub import GPIOHub, PINMAP

_log = logging.getLogger("HW")

class _NullHub:
    """Fallback seguro si GPIOHub no pudo inicializar (no rompe imports)."""
    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            return None
        return _noop

try:
    HUB = GPIOHub(PINMAP)
    _log.info("GPIOHub singleton inicializado.")
except Exception as e:
    HUB = _NullHub()
    _log.error(f"No se pudo inicializar GPIOHub singleton: {e}")

def _cleanup():
    try:
        # Llevar a estado seguro y liberar GPIO al salir del proceso
        try:
            HUB.safe_state()
        except Exception:
            pass
        try:
            HUB.close()
        except Exception:
            pass
    except Exception:
        pass

atexit.register(_cleanup)