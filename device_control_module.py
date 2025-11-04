"""Hardware control helpers for LEDs, fan relay, and buzzer."""

# Author 1: <Shawn Nabizada, 2333349>
# Author 1: <Clayton Cheung, 2332707>

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Dict, List

try:
    import board
    import digitalio
except ImportError:  # pragma: no cover - running off Pi
    board = None  # type: ignore
    digitalio = None  # type: ignore

try:
    # Pi 5 / newer (BCM2712)
    from adafruit_blinka.microcontroller.bcm2712 import pin as _pinmap
except ImportError:
    # Pi 4 / older (BCM283x)
    from adafruit_blinka.microcontroller.bcm283x import pin as _pinmap


logger = logging.getLogger(__name__)


def resolve_pin(pin_spec):
    """Return a pin object suitable for digitalio.DigitalInOut based on config.
    Accepts:
      - "D13", "D21", etc.
      - "GPIO13", "GPIO21", etc.
      - "BCM:13"
      - int 13
    We'll ultimately map to _pinmap.GPIO13 etc.
    """
    if pin_spec is None:
        raise RuntimeError("GPIO pin specification missing in config")

    def _gpio_obj_from_int(n: int):
        # turn 13 -> "GPIO13", look that up on _pinmap
        attr = f"GPIO{n}"
        if hasattr(_pinmap, attr):
            return getattr(_pinmap, attr)

        # Fallback: some Blinka/board mappings expose D<nn> or GP<nn> on the
        # `board` module instead of GPIO<nn> on the microcontroller pinmap.
        # Try a few common attribute names on `board` before failing so the
        # code is more portable across Pi models and Blinka versions.
        if board is not None:
            for cand in (f"D{n}", f"GP{n}", attr):
                if hasattr(board, cand):
                    return getattr(board, cand)

        raise RuntimeError(f"No {attr} in this Pi's pin map")

    # int form (ex: 13)
    if isinstance(pin_spec, int):
        return _gpio_obj_from_int(pin_spec)

    # string form
    if isinstance(pin_spec, str):
        spec = pin_spec.strip()

        # BCM:13
        if spec.upper().startswith("BCM:"):
            try:
                bcm_pin = int(spec.split(":",1)[1])
            except ValueError as exc:
                raise RuntimeError(f"Invalid BCM pin declaration '{spec}'") from exc
            return _gpio_obj_from_int(bcm_pin)

        # D13, D21, etc. -> strip leading D and treat rest as number
        if spec.upper().startswith("D") and spec[1:].isdigit():
            return _gpio_obj_from_int(int(spec[1:]))

        # GPIO13 direct
        if spec.upper().startswith("GPIO") and spec[4:].isdigit():
            return _gpio_obj_from_int(int(spec[4:]))

        raise RuntimeError(f"Unsupported pin string '{spec}'")

    raise TypeError(f"Unsupported pin specification type: {type(pin_spec)}")




class _OutputWrapper:
    """Adapter that harmonises real GPIO outputs and in-memory fallbacks."""

    def __init__(self, name: str, pin):
        self.name = name
        self._value = False
        self._io = None

        if digitalio is not None and not isinstance(pin, int):
            try:
                dio = digitalio.DigitalInOut(pin)
                dio.direction = digitalio.Direction.OUTPUT
                dio.value = False
                self._io = dio
                logger.info("GPIO output initialised for %s", name)
            except Exception as exc:  # pragma: no cover - hardware failure path
                logger.warning("Falling back to software stub for %s (%s)", name, exc)

    def set(self, on: bool):
        self._value = bool(on)
        if self._io is not None:
            self._io.value = self._value

    def get(self) -> bool:
        if self._io is not None:
            return bool(self._io.value)
        return self._value

    def close(self):
        if self._io is not None:
            try:
                self._io.deinit()
            except AttributeError:  # pragma: no cover - older Blinka
                self._io.value = False


class device_control_module:
    """Manage actuator state for LEDs, fan, and buzzer."""

    REQUIRED_DEVICES = ("red_led", "green_led", "blue_led", "fan", "buzzer")
    CONTROLLABLE_DEVICES = ("red_led", "green_led", "blue_led", "fan")

    _setup_lock = threading.Lock()

    def __init__(self, config_file: str = "config.json"):
        self.config = self.load_config(config_file)
        self._outputs: Dict[str, _OutputWrapper] = {}
        self._states: Dict[str, str] = {}
        self._pulse_duration = float(self.config.get("buzzer_pulse_duration_s", 0.5))
        self._initialise_outputs()

    def load_config(self, config_file: str) -> dict:
        """Load configuration from JSON file."""
        try:
            with open(config_file, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError as exc:  # pragma: no cover - config enforced by caller
            raise RuntimeError(f"Config file {config_file} not found") from exc

    # ------------------------------------------------------------------
    # GPIO setup
    # ------------------------------------------------------------------
    def _initialise_outputs(self) -> None:
        pins_cfg = self.config.get("PINS", {})
        missing = [name for name in self.REQUIRED_DEVICES if pins_cfg.get(name) is None]
        if missing:
            raise RuntimeError(f"Missing pin assignments for: {', '.join(missing)}")

        with self._setup_lock:
            for name in self.REQUIRED_DEVICES:
                pin = resolve_pin(pins_cfg.get(name))
                self._outputs[name] = _OutputWrapper(name, pin)
                self._outputs[name].set(False)
                self._states[name] = "off"

    # ------------------------------------------------------------------
    # Public API used by JeefHSApp / MQTT callbacks
    # ------------------------------------------------------------------
    def set_device_state(self, device_name: str, on: bool) -> bool:
        """Toggle controllable devices; returns True if a state change occurred."""
        device = device_name.lower()
        if device not in self.CONTROLLABLE_DEVICES:
            logger.debug("Ignoring unsupported device toggle for %s", device_name)
            return False

        target_state = "on" if on else "off"
        if self._states.get(device) == target_state:
            return False

        output = self._outputs.get(device)
        if output is None:
            logger.warning("Attempted to toggle uninitialised device %s", device)
            return False

        output.set(on)
        self._states[device] = target_state
        # Avoid noisy INFO logs for individual LEDs; keep INFO for other devices.
        if device in {"red_led", "green_led", "blue_led"}:
            logger.debug("Set %s to %s", device, target_state)
        else:
            logger.info("Set %s to %s", device, target_state)
        return True

    def pulse_buzzer(self, duration: float | None = None) -> None:
        """Momentarily activate the buzzer for alerts."""
        buzzer = self._outputs.get("buzzer")
        if buzzer is None:
            logger.warning("Buzzer not configured; pulse ignored")
            return

        pulse_for = duration if duration is not None else self._pulse_duration
        logger.info("Pulsing buzzer for %.2f seconds", pulse_for)
        self._states["buzzer"] = "on"
        buzzer.set(True)
        time.sleep(max(pulse_for, 0.1))
        buzzer.set(False)
        self._states["buzzer"] = "off"

    def get_all_status(self) -> List[Dict[str, str]]:
        """Report the latest known state of every actuator."""
        timestamp = datetime.now().isoformat()
        status_report: List[Dict[str, str]] = []
        for name in self.REQUIRED_DEVICES:
            state = self._states.get(name, "off")
            status_report.append({
                "timestamp": timestamp,
                "device_name": name,
                "status": state
            })
        return status_report

    # ------------------------------------------------------------------
    # Compatibility helpers used by legacy code paths
    # ------------------------------------------------------------------
    def generate_device_status(self) -> List[Dict[str, str]]:
        return self.get_all_status()

    def get_device_status(self) -> List[Dict[str, str]]:
        try:
            report = self.get_all_status()
            logger.debug("Device status requested; %d entries", len(report))
            return report
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.error("Error gathering device status: %s", exc, exc_info=True)
            return []

    def cleanup(self) -> None:
        for output in self._outputs.values():
            output.close()
        self._outputs.clear()