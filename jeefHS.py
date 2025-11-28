"""JeefHS application entry point with unified logging, database sync, and device control."""

# Author 1: <Shawn Nabizada, 2333349>
# Author 1: <Clayton Cheung, 2332707>

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv  # NEW: For loading secrets

from MQTT_communicator import MQTT_communicator
from environmental_module import environmental_module
from security_module import security_module
from device_control_module import device_control_module
from mode_manager import ModeManager
from database_interface import DatabaseInterface  # NEW: For cloud sync

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class JeefHSApp:
    """Coordinates sensors, actuators, MQTT, database sync, and daily logging."""

    def __init__(self, config_file: str = 'config.json'):
        self.config = self.load_config(config_file)

        self.security_check_interval = self.config["security_check_interval"]
        self.security_send_interval = self.config["security_send_interval"]
        self.env_interval = self.config["env_interval"]
        self.flush_interval = self.config.get("flushing_interval", 10)

        self.env_feeds = self.config.get("ENV_FEEDS", {})
        self.security_feeds = self.config.get("SECURITY_FEEDS", {})
        self.status_feeds = self.config.get("STATUS_FEEDS", {})
        self.heartbeat_feed = self.config.get("HEARTBEAT_FEED")
        self.heartbeat_interval = int(self.config.get("heartbeat_interval", 30))

        # --- NEW: Database Interface for SQLite <-> Neon Sync ---
        self.db = DatabaseInterface(self.config)

        self.mode_manager = ModeManager()
        self.device_controller = device_control_module(config_file)

        # Party mode state
        self._party_thread = None
        self._party_stop_event = threading.Event()
        self._party_lock = threading.Lock()
        
        self.mqtt_agent = MQTT_communicator(
            config_file,
            on_set_device_state=self._handle_remote_device_state,
            on_set_mode=self._handle_remote_mode_request,
        )

        self.mode_manager.register_callback(self._on_mode_change)

        self.env_data = environmental_module(config_file)
        self.security_data = security_module(
            config_file,
            mode_getter=self.mode_manager.get_mode,
            buzzer_callback=self.device_controller.pulse_buzzer,
        )

        self.running = True
        self._log_lock = threading.Lock()
        self._log_handle: Optional[object] = None
        self._log_date: Optional[str] = None
        self._needs_flush = False
        self._last_flush_time = time.time()
        self._last_heartbeat_time = 0.0

        self.last_env_data: Dict[str, Optional[str]] = {}
        self.last_security_data: Dict[str, Optional[str]] = {
            "timestamp": None,
            "motion_detected": False,
            "image_path": None,
            "mode": self.mode_manager.get_mode(),
            "buzzer_triggered": False,
        }

        # Publish initial mode state
        self._on_mode_change(self.mode_manager.get_mode())

    # ------------------------------------------------------------------
    # Configuration helpers (Updated for .env)
    # ------------------------------------------------------------------
    def load_config(self, config_file: str) -> dict:
        load_dotenv()  # Load variables from .env

        default_config = {
            "MQTT_BROKER": "io.adafruit.com",
            "MQTT_PORT": 1883,
            "MQTT_KEEPALIVE": 60,
        }

        try:
            with open(config_file, 'r', encoding='utf-8') as handle:
                user_config = json.load(handle)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Config file {config_file} not found") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in {config_file}: {exc}") from exc
        
        # Merge JSON over defaults
        config = {**default_config, **user_config}

        # Inject Secrets from ENV (This overrides JSON)
        if os.getenv("ADAFRUIT_IO_USERNAME"):
            config["ADAFRUIT_IO_USERNAME"] = os.getenv("ADAFRUIT_IO_USERNAME")
        if os.getenv("ADAFRUIT_IO_KEY"):
            config["ADAFRUIT_IO_KEY"] = os.getenv("ADAFRUIT_IO_KEY")

        return config

    # ------------------------------------------------------------------
    # Cloud publishing
    # ------------------------------------------------------------------
    def send_to_cloud(self, data: dict, feeds: dict[str, str]) -> bool:
        success = True
        for key, feed in feeds.items():
            if key not in data:
                continue
            value = data.get(key)
            if value is None:
                continue
            ok = self.mqtt_agent.send_to_adafruit_io(feed, value)
            success = success and ok
            time.sleep(0.5)
        return success

    # ------------------------------------------------------------------
    # Data collection
    # ------------------------------------------------------------------
    def collect_environmental_data(self, current_time: float, timers: dict) -> None:
        if current_time - timers['env_check'] < self.env_interval:
            return

        env_data = self.env_data.get_environmental_data()
        self.last_env_data = env_data

        # 1. Log to local JSONL file
        self._write_log_entry(event_type='environmental', env_data=env_data)

        # 2. Log to Database (SQLite -> Cloud Sync)
        self.db.log_environment(env_data)

        # 3. Publish Live Data to Adafruit IO
        source = env_data.get('source')
        if source in {'sensor', 'simulated'}:
            if self.send_to_cloud(env_data, self.env_feeds):
                logger.info("Environmental data sent to MQTT (%s)", source)
            else:
                logger.warning("Failed to send environmental data via MQTT")
        else:
            logger.warning("Unknown environmental data source '%s'", source)

        timers['env_check'] = current_time

    def collect_security_data(self, current_time: float, timers: dict, security_counts: dict) -> None:
        if current_time - timers['security_check'] >= self.security_check_interval:
            sec_data = self.security_data.get_security_data()
            sec_data.setdefault('mode', self.mode_manager.get_mode())
            self.last_security_data = sec_data

            if sec_data.get('motion_detected'):
                security_counts['motion'] += 1
                logger.warning("Motion detected! Total: %s", security_counts['motion'])
                
                # 1. Log to local JSONL
                self._write_log_entry(event_type='motion', security_data=sec_data)
                
                # 2. Log to Database (SQLite -> Cloud Sync)
                self.db.log_security(sec_data, event_type="motion")

            timers['security_check'] = current_time

        if current_time - timers['security_send'] >= self.security_send_interval:
            summary = {
                'timestamp': datetime.now().isoformat(),
                'motion_count': security_counts['motion'],
            }
            if self.send_to_cloud(summary, self.security_feeds):
                logger.info("Security summary sent (motion=%s)", security_counts['motion'])
            else:
                logger.warning("Failed to publish security summary")

            security_counts['motion'] = 0
            timers['security_send'] = current_time

    # ------------------------------------------------------------------
    # Logging utilities
    # ------------------------------------------------------------------
    def _ensure_log_file(self, timestamp_iso: str) -> None:
        try:
            log_dt = datetime.fromisoformat(timestamp_iso.replace('Z', '+00:00'))
        except ValueError:
            log_dt = datetime.now()

        date_str = log_dt.strftime('%Y%m%d')
        if self._log_date == date_str and self._log_handle is not None:
            return

        if self._log_handle is not None:
            self._log_handle.flush()
            os.fsync(self._log_handle.fileno())
            self._log_handle.close()
            self._needs_flush = False

        log_path = Path(f"{date_str}_jeefhs_log.jsonl").resolve()
        self._log_handle = open(log_path, 'a', buffering=1, encoding='utf-8')
        self._log_date = date_str
        logger.info("Logging to %s", log_path)

    def _current_actuator_states(self) -> Dict[str, str]:
        status = {}
        for item in self.device_controller.get_all_status():
            status[item['device_name']] = item['status']
        return status

    def _write_log_entry(
        self,
        *,
        event_type: Optional[str] = None,
        env_data: Optional[dict] = None,
        security_data: Optional[dict] = None,
    ) -> None:
        env = env_data or self.last_env_data or {}
        sec = security_data or self.last_security_data or {}

        timestamp = env.get('timestamp') or sec.get('timestamp') or datetime.now().isoformat()
        with self._log_lock:
            self._ensure_log_file(timestamp)

            mode_value = sec.get('mode') or self.mode_manager.get_mode()
            entry = {
                "timestamp": timestamp,
                "temperature": env.get('temperature'),
                "humidity": env.get('humidity'),
                "motion_detected": sec.get('motion_detected', False),
                "image_path": sec.get('image_path'),
                "mode": mode_value,
                "actuators": self._current_actuator_states(),
                "buzzer_triggered": sec.get('buzzer_triggered', False),
            }
            if env.get('source'):
                entry['environment_source'] = env.get('source')
            if event_type:
                entry['event'] = event_type

            self._log_handle.write(json.dumps(entry) + '\n')
            self._needs_flush = True

    def _flush_log(self) -> None:
        with self._log_lock:
            if self._log_handle is None:
                return
            self._log_handle.flush()
            os.fsync(self._log_handle.fileno())
            self._needs_flush = False

    # ------------------------------------------------------------------
    # Mode and MQTT callbacks
    # ------------------------------------------------------------------
    def _on_mode_change(self, new_mode: str) -> None:
        status_feed = self.status_feeds.get('mode')
        if status_feed:
            self.mqtt_agent.send_to_adafruit_io(status_feed, new_mode)
        self.last_security_data['mode'] = new_mode
        self._write_log_entry(event_type='mode_change')

    def _handle_remote_device_state(self, device_name: str, new_state: bool) -> None:
        if device_name == 'party_mode':
            if new_state:
                self._start_party_mode()
            else:
                self._stop_party_mode()
            return

        if device_name == 'buzzer':
            if new_state:
                try:
                    self.device_controller.pulse_buzzer()
                    self._write_log_entry(event_type='device_buzzer')
                except Exception:
                    logger.exception("Failed to pulse buzzer")
            return

        changed = self.device_controller.set_device_state(device_name, new_state)
        if changed:
            if device_name not in {'red_led', 'green_led', 'blue_led'}:
                self._write_log_entry(event_type=f"device_{device_name}")
        return

    # ------------------------------------------------------------------
    # Party mode implementation
    # ------------------------------------------------------------------
    def _party_worker(self, stop_event: threading.Event) -> None:
        seq = [
            (('red_led',), 0.3),
            (('green_led',), 0.3),
            (('blue_led',), 0.3),
            (('red_led','green_led'), 0.25),
            (('green_led','blue_led'), 0.25),
            (('red_led','blue_led'), 0.25),
            (('red_led','green_led','blue_led'), 0.5),
            ((), 0.2),
        ]
        try:
            while not stop_event.is_set():
                for leds, delay in seq:
                    if stop_event.is_set():
                        break
                    for name in ('red_led', 'green_led', 'blue_led'):
                        self.device_controller.set_device_state(name, name in leds)
                    time.sleep(delay)
        except Exception as exc:
            logger.exception("Party worker failed: %s", exc)
        finally:
            for name in ('red_led', 'green_led', 'blue_led'):
                try:
                    self.device_controller.set_device_state(name, False)
                except Exception:
                    pass

    def _start_party_mode(self) -> None:
        with self._party_lock:
            if self._party_thread and self._party_thread.is_alive():
                return
            states = self._current_actuator_states()
            for led in ('red_led', 'green_led', 'blue_led'):
                if states.get(led) != 'off':
                    logger.info("Cannot start party mode: %s is not off", led)
                    return
            self._party_stop_event.clear()
            t = threading.Thread(target=self._party_worker, args=(self._party_stop_event,), name="PartyMode")
            t.daemon = True
            t.start()
            self._party_thread = t
            logger.info("Party mode started")
            self._write_log_entry(event_type='party_mode_on')

    def _stop_party_mode(self) -> None:
        with self._party_lock:
            if not (self._party_thread and self._party_thread.is_alive()):
                return
            self._party_stop_event.set()
            self._party_thread.join(timeout=5)
            logger.info("Party mode stopped")
            self._write_log_entry(event_type='party_mode_off')

    def _handle_remote_mode_request(self, requested_mode: str) -> None:
        self.mode_manager.set_mode(requested_mode)

    # ------------------------------------------------------------------
    # Loop workers
    # ------------------------------------------------------------------
    def _maybe_send_heartbeat(self, current_time: float) -> None:
        if not self.heartbeat_feed:
            return
        if current_time - self._last_heartbeat_time < self.heartbeat_interval:
            return
        payload = datetime.now().isoformat()
        if self.mqtt_agent.send_to_adafruit_io(self.heartbeat_feed, payload):
            logger.debug("Heartbeat published")
        self._last_heartbeat_time = current_time

    def data_collection_loop(self) -> None:
        timers = {
            'env_check': 0.0,
            'security_check': 0.0,
            'security_send': 0.0,
        }
        security_counts = {'motion': 0}

        try:
            while self.running:
                try:
                    current_time = time.time()
                    self.collect_security_data(current_time, timers, security_counts)
                    self.collect_environmental_data(current_time, timers)
                    self._maybe_send_heartbeat(current_time)

                    if self._needs_flush and current_time - self._last_flush_time >= self.flush_interval:
                        self._flush_log()
                        self._last_flush_time = current_time

                    time.sleep(1)
                except Exception as exc:
                    logger.error("Error in data collection loop: %s", exc, exc_info=True)
                    time.sleep(5)
        finally:
            with self._log_lock:
                if self._log_handle is not None:
                    self._log_handle.flush()
                    os.fsync(self._log_handle.fileno())
                    self._log_handle.close()
                    self._log_handle = None
                    self._needs_flush = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self.running = True
        logger.info("Starting JeefHS application")

        data_thread = threading.Thread(target=self.data_collection_loop, name="JeefHSLoop")
        data_thread.start()

        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutdown requested")
        finally:
            self.running = False
            data_thread.join(timeout=10)
            
            # --- NEW: Close DB Sync ---
            logger.info("Stopping database sync...")
            self.db.close()
            
            try:
                self.security_data.close()
            except Exception:
                pass
            try:
                self.device_controller.cleanup()
            except Exception:
                pass
            try:
                self.mqtt_agent.close()
            except Exception:
                pass
            logger.info("JeefHS stopped")


if __name__ == '__main__':
    JeefHSApp(config_file='config.json').start()