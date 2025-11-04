# Author 1: <Shawn Nabizada, 2333349>
# Author 1: <Clayton Cheung, 2332707>

import json
import time
from datetime import datetime
from pathlib import Path
import logging
import os
import ssl
import smtplib
from typing import Callable, Optional

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

import board
import digitalio
from picamera2 import Picamera2
import cv2

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def resolve_pin(pin_spec):
    """
    Resolve a pin spec from config into an object usable by Blinka/Adafruit libs.
    - "D6"      -> getattr(board, "D6")
    - "BCM:17"  -> int(17)  (for libs that accept BCM integers)
    - 17        -> 17
    """
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


class security_module:
    """
    Reads a PIR motion sensor and, on motion, captures an image and emails an alert via Brevo SMTP.
    Requires a valid config.json (no built-in defaults).
    """

    REQUIRED_KEYS = [
        # Email (Brevo)
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "ALERT_FROM", "ALERT_TO",
        # Behavior
        "camera_enabled", "image_dir", "alert_cooldown_s",
        # Pins block
        "PINS"
    ]

    def __init__(
        self,
        config_file: str = "config.json",
        mode_getter: Optional[Callable[[], str]] = None,
        buzzer_callback: Optional[Callable[[], None]] = None,
    ):
        self.config = self._load_config(config_file)
        self._validate_required(self.REQUIRED_KEYS, self.config)
        self._validate_required(["pir"], self.config.get("PINS", {}))

        self._mode_getter = mode_getter or (lambda: "HOME")
        self._buzzer_callback = buzzer_callback

        # Resolve PIR pin
        pir_pin = resolve_pin(self.config["PINS"]["pir"])
        if pir_pin is None:
            raise RuntimeError("PINS.pir must be set in config.json")

        # Setup PIR (HIGH when motion)
        self.pir = digitalio.DigitalInOut(pir_pin)
        self.pir.direction = digitalio.Direction.INPUT

        # Camera
        self.image_dir = self.config["image_dir"]
        os.makedirs(self.image_dir, exist_ok=True)

        self.picam2 = Picamera2()
        try:
            cfg = self.picam2.create_still_configuration()
            self.picam2.configure(cfg)
        except Exception as e:
            logger.warning(f"Picamera2 still config failed: {e}")
        self.picam2.start()

        # Per-alert-type cooldown tracker
        self._last_alert_time = {}

    # -------------------- config helpers --------------------

    @staticmethod
    def _load_config(path: str) -> dict:
        try:
            with open(path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            raise RuntimeError(f"Missing {path}. Provide a valid configuration file (no defaults in code).")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON in {path}: {e}")

    @staticmethod
    def _validate_required(required_keys, cfg: dict):
        missing = [k for k in required_keys if k not in cfg]
        if missing:
            raise RuntimeError(f"Config missing required keys: {', '.join(missing)}")

    # -------------------- main API --------------------

    def get_security_data(self) -> dict:
        motion_detected = bool(self.pir.value)
        mode = (self._mode_getter() or "HOME").upper()

        buzzer_triggered = False

        image_path = None
        if motion_detected:
            if self.config["camera_enabled"]:
                image_path = self._capture_image()
            if self._mode_allows_buzzer(mode):
                buzzer_triggered = self._pulse_buzzer()

            self._send_email_alert(
                alert_type="Motion Detected",
                message="Motion sensor triggered.",
                image_path=image_path
            )

        return {
            "timestamp": datetime.now().isoformat(),
            "motion_detected": motion_detected,
            "image_path": image_path,
            "mode": mode,
            "buzzer_triggered": buzzer_triggered
        }

    # -------------------- internals --------------------

    def _mode_allows_buzzer(self, mode: str) -> bool:
        return mode.upper() == "AWAY"

    def _pulse_buzzer(self) -> bool:
        if self._buzzer_callback is None:
            return False
        try:
            self._buzzer_callback()
            return True
        except Exception as exc:  # pragma: no cover - hardware failure path
            logger.warning("Failed to pulse buzzer: %s", exc)
            return False

    def _capture_image(self) -> str:
        """Capture an image; fall back to a small text file if capture fails."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_jpg = os.path.join(self.image_dir, f"motion_{ts}.jpg")
        try:
            frame = self.picam2.capture_array()
            cv2.imwrite(out_jpg, frame)
            logger.info(f"Image captured: {out_jpg}")
            return out_jpg
        except Exception as e:
            logger.warning(f"Camera capture failed ({e}); writing placeholder")
            out_txt = os.path.join(self.image_dir, f"motion_{ts}.txt")
            with open(out_txt, "w") as f:
                f.write(f"Motion detected at {datetime.now().isoformat()}")
            return out_txt

    def _send_email_alert(self, alert_type: str, message: str = "", image_path: str | None = None) -> bool:
        """Send via Brevo SMTP with cooldown enforcement."""
        cooldown = int(self.config["alert_cooldown_s"])
        now = time.time()
        last = self._last_alert_time.get(alert_type, 0.0)
        if now - last < cooldown:
            remain = int(cooldown - (now - last))
            logger.info(f"Suppressing '{alert_type}' (cooldown {remain}s left)")
            return False

        smtp_host = self.config["SMTP_HOST"]
        smtp_port = int(self.config["SMTP_PORT"])
        smtp_user = self.config["SMTP_USER"]
        smtp_pass = self.config["SMTP_PASS"]
        sender = self.config["ALERT_FROM"]
        recipient = self.config["ALERT_TO"]

        try:
            msg = MIMEMultipart()
            msg["From"] = sender
            msg["To"] = recipient
            msg["Subject"] = f"🚨 JeefHS Alert: {alert_type}"

            body = (
                "JeefHS Security Alert\n\n"
                f"Alert Type: {alert_type}\n"
                f"Time: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                "Location: Home Security System\n\n"
                f"{message}\n\n"
                "---\nThis is an automated alert from your JeefHS IoT system."
            )
            msg.attach(MIMEText(body, "plain"))

            if image_path and Path(image_path).exists():
                try:
                    with open(image_path, "rb") as f:
                        part = MIMEImage(f.read())
                    part.add_header("Content-Disposition", "attachment", filename=Path(image_path).name)
                    msg.attach(part)
                except Exception as e:
                    logger.warning(f"Failed attaching image: {e}")

            context = ssl.create_default_context()
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls(context=context)
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)

            self._last_alert_time[alert_type] = now
            logger.info(f"Email alert sent via Brevo: {alert_type}")
            return True

        except Exception as e:
            logger.error(f"Brevo SMTP send failed: {e}", exc_info=True)
            return False

    def close(self):
        """Cleanup camera resources."""
        try:
            self.picam2.stop()
        except Exception:
            pass
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass
