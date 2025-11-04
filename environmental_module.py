# Author 1: <Shawn Nabizada, 2333349>
# Author 1: <Clayton Cheung, 2332707>

import json
import time
import random
import math
from datetime import datetime, timedelta
from pathlib import Path
import logging
import os

import board

# NOTE: moved DHT initialization into the class (no module-level GPIO setup)
# import adafruit_dht  # imported lazily in __init__

# Configure logging (unchanged)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def resolve_pin(pin_spec):
    """Resolve a pin spec like 'D4' or 'BCM:17' into an object usable by Blinka/Adafruit libs."""
    if pin_spec is None:
        return None
    if isinstance(pin_spec, int):
        return pin_spec
    if isinstance(pin_spec, str):
        if pin_spec.upper().startswith("BCM:"):
            return int(pin_spec.split(":", 1)[1])
        try:
            return getattr(board, pin_spec)
        except AttributeError:
            raise RuntimeError(f"Unknown board pin '{pin_spec}' (check config).")
    raise TypeError(f"Unsupported pin spec type: {type(pin_spec)}")


class environmental_module:
    def __init__(self, config_file='config.json'):
        self.config = self.load_config(config_file)
        self._dht = None

        # Only touch GPIO if explicitly enabled
        if self.config.get("use_dht", False):
            try:
                import adafruit_dht  # lazy import
                pins = self.config.get("PINS", {})
                dht_pin = resolve_pin(pins.get("dht"))
                if dht_pin is None:
                    raise RuntimeError("PINS.dht missing in config.json")

                # Use DHT22 as requested
                self._dht = adafruit_dht.DHT22(dht_pin, use_pulseio=False)
                logger.info("DHT22 initialized")
            except Exception as e:
                logger.warning(f"DHT22 init failed, will simulate env readings: {e}")
                self._dht = None

    def load_config(self, config_file):
        """Load configuration from JSON file (kept as in original)."""
        default_config = {
            "ADAFRUIT_IO_USERNAME": "username",
            "ADAFRUIT_IO_KEY": "userkey",
            "MQTT_BROKER": "io.adafruit.com",
            "MQTT_PORT": 1883,
            "MQTT_KEEPALIVE": 60,
            "devices": ["living_room_light", "bedroom_fan", "front_door", "garage_door"],
            "camera_enabled": True,
            "capturing_interval": 900,
            "flushing_interval": 10,
            "sync_interval": 300
        }

        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
                return {**default_config, **config}
        except FileNotFoundError:
            logger.warning(f"Config file {config_file} not found, using defaults")
            return default_config

    def get_environmental_data(self):
        # Minimal change: same structure; now only temperature & humidity (no pressure)
        temperature_c, humidity = 0, 0

        # Try real sensor first (if enabled/initialized)
        if self._dht is not None:
            # Make a few attempts to read the DHT sensor because DHT reads are flaky
            retries = int(self.config.get('dht_read_retries', 3))
            delay = float(self.config.get('dht_retry_delay_s', 2.0))
            last_exc = None
            for attempt in range(1, retries + 1):
                try:
                    t = self._dht.temperature
                    h = self._dht.humidity
                    if t is None or h is None:
                        raise ValueError("DHT22 returned None")
                    temperature_c = round(float(t), 1)
                    humidity = round(float(h), 1)
                    logger.info(f"Environmental reading from sensor (attempt {attempt}): {temperature_c} C, {humidity}%")
                    return {
                        'timestamp': datetime.now().isoformat(),
                        'temperature': temperature_c,
                        'humidity': humidity,
                        'source': 'sensor'
                    }
                except Exception as e:
                    last_exc = e
                    logger.debug(f"DHT read attempt {attempt} failed: {e}")
                    if attempt < retries:
                        time.sleep(delay)
            # All attempts failed; fall back to simulation
            logger.warning(f"DHT22 read failed after {retries} attempts, will simulate values: {last_exc}")

        # Simulation path (original logic preserved, minus pressure)
        try:
            # Simulate realistic temperature variations
            base_temp = 22 + 5 * math.sin(time.time() / 3600)  # Daily cycle
            temperature_c = round(base_temp + random.uniform(-2, 2), 1)

            # Humidity inversely related to temperature
            humidity = round(60 - (temperature_c - 20) * 2 + random.uniform(-5, 5), 1)
            humidity = max(30, min(90, humidity))  # Clamp between 30-90%

        except RuntimeError as error:
            # Keep original error handling style
            print(error.args[0])
            time.sleep(2.0)

        # Simulation path return value includes source info so callers and logs can distinguish
        logger.info(f"Simulated environmental reading: {temperature_c} C, {humidity}%")
        return {
            'timestamp': datetime.now().isoformat(),
            'temperature': temperature_c,
            'humidity': humidity,
            'source': 'simulated'
        }
