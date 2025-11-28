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

from dotenv import load_dotenv  # NEW: For loading secrets

try:
    import board
    import digitalio
except ImportError:
    board = None
    digitalio = None

try:
    from picamera2 import Picamera2
    import cv2
except ImportError:
    Picamera2 = None
    cv2 = None


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def resolve_pin(pin_spec):
    if pin_spec is None:
        return None
    if isinstance(pin_spec, int):
        return pin_spec
    if isinstance(pin_spec, str):
        if pin_spec.upper().startswith("BCM:"):
            return int(pin_spec.split(":", 1)[1])
        try:
            if board:
                return getattr(board, pin_spec)
        except AttributeError:
            raise RuntimeError(f"Unknown board pin '{pin_spec}'")
    raise TypeError(f"Unsupported pin spec type: {type(pin_spec)}")


class security_module:
    """
    Reads a PIR motion sensor and, on motion, captures an image and emails an alert.
    Uses .env for SMTP credentials.
    """

    # We now check config logic slightly differently because SMTP keys come from env
    REQUIRED_PINS = ["pir"]

    def __init__(
        self,
        config_file: str = "config.json",
        mode_getter: Optional[Callable[[], str]] = None,
        buzzer_callback: Optional[Callable[[], None]] = None,
    ):
        self.config = self._load_config(config_file)
        self._validate_pins(self.config.get("PINS", {}))

        self._mode_getter = mode_getter or (lambda: "HOME")
        self._buzzer_callback = buzzer_callback

        # Resolve PIR pin
        if board and digitalio:
            pir_pin = resolve_pin(self.config["PINS"]["pir"])
            self.pir = digitalio.DigitalInOut(pir_pin)
            self.pir.direction = digitalio.Direction.INPUT
        else:
            logger.warning("GPIO not available; Security module in simulation mode.")
            self.pir = None

        # Camera
        self.image_dir = self.config.get("image_dir", "captured_images")
        os.makedirs(self.image_dir, exist_ok=True)

        self.picam2 = None
        if self.config.get("camera_enabled") and Picamera2:
            try:
                self.picam2 = Picamera2()
                cfg = self.picam2.create_still_configuration()
                self.picam2.configure(cfg)
                self.picam2.start()
                logger.info("Picamera2 initialized.")
            except Exception as e:
                logger.warning(f"Picamera2 init failed: {e}")

        # Per-alert-type cooldown tracker
        self._last_alert_time = {}

    # -------------------- config helpers --------------------

    @staticmethod
    def _load_config(path: str) -> dict:
        """Load JSON and inject SMTP secrets from .env"""
        load_dotenv()
        
        try:
            with open(path, "r") as f:
                cfg = json.load(f)
        except FileNotFoundError:
            raise RuntimeError(f"Missing {path}")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON in {path}: {e}")

        # Inject SMTP Secrets from ENV (Safe fallback to config.json if not in env)
        cfg["SMTP_HOST"] = os.getenv("SMTP_HOST", cfg.get("SMTP_HOST"))
        cfg["SMTP_PORT"] = int(os.getenv("SMTP_PORT", cfg.get("SMTP_PORT", 587)))
        cfg["SMTP_USER"] = os.getenv("SMTP_USER", cfg.get("SMTP_USER"))
        cfg["SMTP_PASS"] = os.getenv("SMTP_PASS", cfg.get("SMTP_PASS"))
        cfg["ALERT_FROM"] = os.getenv("ALERT_FROM", cfg.get("ALERT_FROM"))
        cfg["ALERT_TO"] = os.getenv("ALERT_TO", cfg.get("ALERT_TO"))

        # Basic validation that we have credentials
        if not cfg["SMTP_USER"] or not cfg["SMTP_PASS"]:
            logger.warning("SMTP Credentials missing in .env; Email alerts will fail.")

        return cfg

    def _validate_pins(self, pins_cfg):
        if "pir" not in pins_cfg:
            raise RuntimeError("PINS.pir must be set in config.json")

    # -------------------- main API --------------------

    def get_security_data(self) -> dict:
        # Read Hardware or Simulate
        if self.pir:
            motion_detected = bool(self.pir.value)
        else:
            motion_detected = False # Or simulate random motion for testing

        mode = (self._mode_getter() or "HOME").upper()
        buzzer_triggered = False
        image_path = None

        if motion_detected:
            # Capture Image
            if self.config.get("camera_enabled"):
                image_path = self._capture_image()
            
            # Pulse Buzzer if AWAY
            if self._mode_allows_buzzer(mode):
                buzzer_triggered = self._pulse_buzzer()

            # Send Email
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
        except Exception as exc:
            logger.warning("Failed to pulse buzzer: %s", exc)
            return False

    def _capture_image(self) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # If camera not initialized, just write a text placeholder
        if not self.picam2 or not cv2:
            out_txt = os.path.join(self.image_dir, f"motion_{ts}_placeholder.txt")
            with open(out_txt, "w") as f:
                f.write(f"Motion detected at {datetime.now().isoformat()} (Camera Unavailable)")
            return out_txt

        out_jpg = os.path.join(self.image_dir, f"motion_{ts}.jpg")
        try:
            frame = self.picam2.capture_array()
            cv2.imwrite(out_jpg, frame)
            logger.info(f"Image captured: {out_jpg}")
            return out_jpg
        except Exception as e:
            logger.warning(f"Camera capture failed ({e})")
            return None

    def _send_email_alert(self, alert_type: str, message: str = "", image_path: str | None = None) -> bool:
        """Send via SMTP with cooldown."""
        cooldown = int(self.config.get("alert_cooldown_s", 300))
        now = time.time()
        last = self._last_alert_time.get(alert_type, 0.0)
        
        if now - last < cooldown:
            return False

        if not self.config.get("SMTP_HOST") or not self.config.get("SMTP_USER"):
            return False

        try:
            msg = MIMEMultipart()
            msg["From"] = self.config["ALERT_FROM"]
            msg["To"] = self.config["ALERT_TO"]
            msg["Subject"] = f"🚨 JeefHS Alert: {alert_type}"

            body = (
                f"JeefHS Security Alert\n\n"
                f"Type: {alert_type}\n"
                f"Time: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"{message}\n"
            )
            msg.attach(MIMEText(body, "plain"))

            if image_path and Path(image_path).exists() and image_path.endswith(".jpg"):
                try:
                    with open(image_path, "rb") as f:
                        part = MIMEImage(f.read())
                    part.add_header("Content-Disposition", "attachment", filename=Path(image_path).name)
                    msg.attach(part)
                except Exception as e:
                    logger.warning(f"Failed attaching image: {e}")

            context = ssl.create_default_context()
            with smtplib.SMTP(self.config["SMTP_HOST"], self.config["SMTP_PORT"]) as server:
                server.starttls(context=context)
                server.login(self.config["SMTP_USER"], self.config["SMTP_PASS"])
                server.send_message(msg)

            self._last_alert_time[alert_type] = now
            logger.info(f"Email alert sent: {alert_type}")
            return True

        except Exception as e:
            logger.error(f"SMTP send failed: {e}")
            return False

    def close(self):
        if self.picam2:
            try:
                self.picam2.stop()
            except Exception:
                pass
        if cv2:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass