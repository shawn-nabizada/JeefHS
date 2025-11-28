# Author 1: <Shawn Nabizada, 2333349>
# Author 1: <Clayton Cheung, 2332707>

import json
import logging
import ssl
import os
from typing import Callable, Dict, Optional

from dotenv import load_dotenv  # NEW: For loading secrets
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
        """Load configuration from JSON and inject secrets from .env"""
        load_dotenv()  # Load .env variables

        default_config = {
            "MQTT_BROKER": "io.adafruit.com",
            "MQTT_PORT": 1883,
            "MQTT_KEEPALIVE": 60,
            "use_tls": False
        }

        try:
            with open(config_file, 'r') as f:
                json_config = json.load(f)
        except FileNotFoundError:
            logger.warning(f"Config file {config_file} not found, using defaults")
            json_config = {}

        # Merge Defaults + JSON
        config = {**default_config, **json_config}

        # Inject Credentials from ENV (Overrides JSON)
        if os.getenv("ADAFRUIT_IO_USERNAME"):
            config["ADAFRUIT_IO_USERNAME"] = os.getenv("ADAFRUIT_IO_USERNAME")
        if os.getenv("ADAFRUIT_IO_KEY"):
            config["ADAFRUIT_IO_KEY"] = os.getenv("ADAFRUIT_IO_KEY")

        return config

    def setup_mqtt(self):
        """Setup MQTT client for Adafruit IO (with optional TLS)"""
        if not self.config.get("ADAFRUIT_IO_USERNAME") or not self.config.get("ADAFRUIT_IO_KEY"):
            logger.error("Missing Adafruit IO credentials in .env or config.json")
            return

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
                self.mqtt_client.tls_set(context=ssl.create_default_context())
                port = int(self.config.get("MQTT_PORT", 8883))
            else:
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
        if rc == 0:
            self.mqtt_connected = True
            logger.info("Connected to MQTT broker")
            self._subscribe_control_feeds()
        else:
            self.mqtt_connected = False
            logger.error(f"Failed to connect to MQTT broker, return code {rc}")

    def on_mqtt_disconnect(self, client, userdata, rc):
        self.mqtt_connected = False
        if rc != 0:
            logger.warning(f"Unexpected disconnection from MQTT broker (rc={rc})")
        else:
            logger.info("Disconnected from MQTT broker")

    def on_mqtt_publish(self, client, userdata, mid):
        logger.debug(f"Message {mid} published successfully")

    def on_mqtt_message(self, client, userdata, message):
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
            if self._on_set_mode:
                try:
                    self._on_set_mode(payload)
                except Exception as exc:
                    logger.warning("Mode control handler failed: %s", exc)
            return

        desired_state = payload.upper() in {"ON", "1", "TRUE", "HIGH"}
        if self._on_set_device_state:
            try:
                self._on_set_device_state(device_or_mode, desired_state)
            except Exception as exc:
                logger.exception("Device control handler failed for %s: %s", device_or_mode, exc)

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

    def close(self):
        try:
            if self.mqtt_client is not None:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
        except Exception:
            pass

    def _subscribe_control_feeds(self) -> None:
        if not self.mqtt_client or not self.control_feeds:
            return
        username = self.config["ADAFRUIT_IO_USERNAME"]
        for device, feed in self.control_feeds.items():
            topic = f"{username}/feeds/{feed}"
            result, _ = self.mqtt_client.subscribe(topic)
            if result == mqtt.MQTT_ERR_SUCCESS:
                logger.info("Subscribed to control feed %s (%s)", feed, device)
            else:
                logger.warning("Failed to subscribe to %s (code %s)", topic, result)