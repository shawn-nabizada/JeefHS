import json
import time
import random
import math
from datetime import datetime
import logging
import board

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Robust Pin Resolution ---
try:
    # Pi 5 / newer (BCM2712)
    from adafruit_blinka.microcontroller.bcm2712 import pin as _pinmap
except ImportError:
    try:
        # Pi 4 / older (BCM283x)
        from adafruit_blinka.microcontroller.bcm283x import pin as _pinmap
    except ImportError:
        _pinmap = None

def resolve_pin(pin_spec):
    if pin_spec is None:
        return None

    def _gpio_obj_from_int(n: int):
        if _pinmap:
            attr = f"GPIO{n}"
            if hasattr(_pinmap, attr):
                return getattr(_pinmap, attr)
        for cand in (f"D{n}", f"GP{n}", f"GPIO{n}"):
            if hasattr(board, cand):
                return getattr(board, cand)
        raise RuntimeError(f"Could not resolve pin {n} on this device.")

    if isinstance(pin_spec, int):
        return _gpio_obj_from_int(pin_spec)

    if isinstance(pin_spec, str):
        spec = pin_spec.strip().upper()
        if spec.startswith("BCM:"):
            try:
                bcm_pin = int(spec.split(":", 1)[1])
                return _gpio_obj_from_int(bcm_pin)
            except ValueError:
                raise RuntimeError(f"Invalid BCM format: {pin_spec}")
        if spec.startswith("D") and spec[1:].isdigit():
            return _gpio_obj_from_int(int(spec[1:]))
        if spec.startswith("GPIO") and spec[4:].isdigit():
            return _gpio_obj_from_int(int(spec[4:]))
        if hasattr(board, spec):
            return getattr(board, spec)

    raise ValueError(f"Unsupported pin specification: {pin_spec}")


class environmental_module:
    def __init__(self, config_file='config.json'):
        self.config = self.load_config(config_file)
        self._dht = None

        if self.config.get("use_dht", False):
            try:
                import adafruit_dht
                pins = self.config.get("PINS", {})
                
                dht_pin_spec = pins.get("dht")
                if not dht_pin_spec:
                    raise ValueError("Key 'dht' is missing in config.json PINS")
                
                dht_pin = resolve_pin(dht_pin_spec)
                logger.info(f"Initializing DHT11 on pin: {dht_pin}")

                # --- CHANGE IS HERE: DHT11 instead of DHT22 ---
                self._dht = adafruit_dht.DHT11(dht_pin, use_pulseio=False)
                logger.info("DHT11 Sensor successfully initialized.")

            except ImportError:
                logger.error("Failed to import 'adafruit_dht'. Is the library installed?")
                self._dht = None
            except Exception as e:
                logger.warning(f"DHT11 init failed ({type(e).__name__}): {e}")
                logger.warning("System will fall back to SIMULATED data.")
                self._dht = None
        else:
            logger.info("DHT Sensor disabled in config. Using simulation.")

    def load_config(self, config_file):
        default_config = {
            "dht_read_retries": 3,
            "dht_retry_delay_s": 2.0
        }
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
                return {**default_config, **config}
        except FileNotFoundError:
            logger.warning(f"Config file {config_file} not found, using defaults")
            return default_config

    def get_environmental_data(self):
        temperature_c, humidity = 0, 0
        source = 'simulated'

        if self._dht:
            retries = int(self.config.get('dht_read_retries', 3))
            delay = float(self.config.get('dht_retry_delay_s', 2.0))
            
            for attempt in range(1, retries + 1):
                try:
                    t = self._dht.temperature
                    h = self._dht.humidity
                    
                    if t is not None and h is not None:
                        temperature_c = round(float(t), 1)
                        humidity = round(float(h), 1)
                        source = 'sensor'
                        return {
                            'timestamp': datetime.now().isoformat(),
                            'temperature': temperature_c,
                            'humidity': humidity,
                            'source': source
                        }
                except RuntimeError as e:
                    logger.warning(f"DHT Read attempt {attempt} failed: {e}")
                    time.sleep(delay)
                except Exception as e:
                    logger.error(f"Unexpected DHT error: {e}")
                    break
            
            logger.warning("All DHT read attempts failed. Returning simulated data.")

        # Fallback Simulation
        try:
            base_temp = 22 + 5 * math.sin(time.time() / 3600)
            temperature_c = round(base_temp + random.uniform(-2, 2), 1)
            humidity = round(60 - (temperature_c - 20) * 2 + random.uniform(-5, 5), 1)
            humidity = max(30, min(90, humidity))
        except Exception:
            pass

        return {
            'timestamp': datetime.now().isoformat(),
            'temperature': temperature_c,
            'humidity': humidity,
            'source': source
        }