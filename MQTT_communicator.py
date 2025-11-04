# Author 1: <Shawn Nabizada, 2333349>
# Author 1: <Clayton Cheung, 2332707>

import json
import logging
import ssl
from typing import Callable, Dict, Optional

import paho.mqtt.client as mqtt

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class MQTT_communicator:
    def __init__(
        self,
        config_file: str = 'config.json',
        on_set_device_state: Optional[Callable[[str, bool], None]] = None,
        on_set_mode: Optional[Callable[[str], None]] = None,
    ):
        self.config = self.load_config(config_file)
        self.mqtt_client = None
        self.mqtt_connected = False
        self._on_set_device_state = on_set_device_state
        self._on_set_mode = on_set_mode
        self.control_feeds: Dict[str, str] = self.config.get("CONTROL_FEEDS", {})
        self._feed_to_device = {feed: device for device, feed in self.control_feeds.items()}
        self.setup_mqtt()

    def load_config(self, config_file):
        """Load configuration from JSON file"""
        default_config = {
            "ADAFRUIT_IO_USERNAME": "username",
            "ADAFRUIT_IO_KEY": "userkey",
            "MQTT_BROKER": "io.adafruit.com",
            # NOTE: port can be overridden by config; see setup_mqtt for TLS-aware default
            "MQTT_PORT": 1883,
            "MQTT_KEEPALIVE": 60,
            "devices": ["living_room_light", "bedroom_fan", "front_door", "garage_door"],
            "camera_enabled": True,
            "capturing_interval": 900,
            "flushing_interval": 10,
            "sync_interval": 300,
            # NEW: toggle TLS for MQTT
            "use_tls": False
        }

        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
                return {**default_config, **config}
        except FileNotFoundError:
            logger.warning(f"Config file {config_file} not found, using defaults")
            return default_config

    def setup_mqtt(self):
        """Setup MQTT client for Adafruit IO (with optional TLS)"""
        try:
            self.mqtt_client = mqtt.Client()

            # Username / password (Adafruit IO requires both)
            self.mqtt_client.username_pw_set(
                self.config["ADAFRUIT_IO_USERNAME"],
                self.config["ADAFRUIT_IO_KEY"]
            )

            # Set up callbacks
            self.mqtt_client.on_connect = self.on_mqtt_connect
            self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
            self.mqtt_client.on_publish = self.on_mqtt_publish
            self.mqtt_client.on_message = self.on_mqtt_message

            # TLS (optional)
            use_tls = bool(self.config.get("use_tls", False))
            if use_tls:
                # Use system CA certificates; set TLS context
                self.mqtt_client.tls_set(context=ssl.create_default_context())
                # If no explicit port provided, default to 8883 for TLS
                port = int(self.config.get("MQTT_PORT", 8883))
            else:
                # Non-TLS default 1883 unless overridden
                port = int(self.config.get("MQTT_PORT", 1883))

            # Connect to broker
            self.mqtt_client.connect(
                self.config["MQTT_BROKER"],
                port,
                int(self.config.get("MQTT_KEEPALIVE", 60))
            )

            # Start the network loop in a separate thread
            self.mqtt_client.loop_start()
            logger.info(f"MQTT client setup completed (TLS={'on' if use_tls else 'off'})")

        except Exception as e:
            logger.error(f"Failed to setup MQTT client: {e}")
            self.mqtt_connected = False

    def on_mqtt_connect(self, client, userdata, flags, rc):
        """Callback for when MQTT client connects"""
        if rc == 0:
            self.mqtt_connected = True
            logger.info("Connected to MQTT broker")
            self._subscribe_control_feeds()
        else:
            self.mqtt_connected = False
            logger.error(f"Failed to connect to MQTT broker, return code {rc}")

    def on_mqtt_disconnect(self, client, userdata, rc):
        """Callback for when MQTT client disconnects"""
        self.mqtt_connected = False
        if rc != 0:
            logger.warning(f"Unexpected disconnection from MQTT broker (rc={rc})")
        else:
            logger.info("Disconnected from MQTT broker")

    def on_mqtt_publish(self, client, userdata, mid):
        """Callback for when message is published"""
        logger.debug(f"Message {mid} published successfully")

    def on_mqtt_message(self, client, userdata, message):
        """Handle inbound control messages from Adafruit IO."""
        try:
            payload = message.payload.decode("utf-8").strip()
        except UnicodeDecodeError:
            logger.warning("Dropping non-text MQTT payload from %s", message.topic)
            return

        feed_key = message.topic.split("/")[-1]
        device_or_mode = self._feed_to_device.get(feed_key)
        if device_or_mode is None:
            logger.debug("Ignoring MQTT message for untracked feed %s", feed_key)
            return

        if device_or_mode == "mode":
            if self._on_set_mode is None:
                logger.warning("Mode control received but no handler registered")
                return
            try:
                self._on_set_mode(payload)
            except ValueError as exc:
                logger.warning("Rejected mode command '%s': %s", payload, exc)
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.exception("Mode control handler failed: %s", exc)
            return

        desired_state = payload.upper() in {"ON", "1", "TRUE", "HIGH"}
        if self._on_set_device_state is None:
            logger.warning("Device control received for %s but no handler registered", device_or_mode)
            return
        try:
            self._on_set_device_state(device_or_mode, desired_state)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Device control handler failed for %s: %s", device_or_mode, exc)

    # Send data to Adafruit IO
    def send_to_adafruit_io(self, feed_name, value):
        if not self.mqtt_client or not self.mqtt_connected:
            logger.warning("MQTT client not connected")
            return False

        try:
            topic = f"{self.config['ADAFRUIT_IO_USERNAME']}/feeds/{feed_name}"
            result, mid = self.mqtt_client.publish(topic, str(value))
            if result == mqtt.MQTT_ERR_SUCCESS:
                logger.info(f"Published {value} to {topic}")
                return True
            else:
                logger.error(f"Failed to publish {value} to {topic}, result={result}")
                return False

        except Exception as e:
            logger.error(f"Error publishing to MQTT: {e}")
            return False

    # Optional: call when shutting down your app
    def close(self):
        try:
            if self.mqtt_client is not None:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _subscribe_control_feeds(self) -> None:
        if not self.mqtt_client or not self.control_feeds:
            return
        username = self.config["ADAFRUIT_IO_USERNAME"]
        for device, feed in self.control_feeds.items():
            topic = f"{username}/feeds/{feed}"
            result, _ = self.mqtt_client.subscribe(topic)
            if result == mqtt.MQTT_ERR_SUCCESS:
                logger.info("Subscribed to control feed %s (%s)", feed, device)
            else:  # pragma: no cover - network failure path
                logger.warning("Failed to subscribe to %s (code %s)", topic, result)
