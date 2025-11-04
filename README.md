# JeefHS

[Demo video (YouTube)](https://youtube.com/placeholder)

## Table of contents
- [Overview](#overview)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Project structure (files & purpose)](#project-structure-files--purpose)
- [How it works (module responsibilities)](#how-it-works-module-responsibilities)
- [Contract (inputs/outputs)](#contract-inputsoutputs)
- [What we learned](#what-we-learned)

## Overview

Created by Shawn Nabizada and Clayton Cheung, JeefHS is a lightweight, modular Python home system that coordinates environmental sensing, device control (LEDs, fan, buzzer), security monitoring (PIR + camera), and cloud communication via MQTT (Adafruit IO compatible). The entry point is `jeefHS.py`, which reads a JSON configuration, wires together the modules, then runs a continuous data-collection loop that logs events, publishes telemetry, and responds to remote control messages.

This README documents how to set up and run the project, the configuration options it expects, the roles of the main modules, and the system I/O contract.

## Quick start

1. Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

2. Install runtime dependencies. The code uses `paho-mqtt` (MQTT client) and hardware-specific libraries when running on a Raspberry Pi. Install the minimum set below and add more as needed for your hardware (e.g., `adafruit-circuitpython-dht`, `picamera2`, `opencv-python`):

```bash
pip install paho-mqtt
# optional (hardware and camera):
# pip install adafruit-circuitpython-dht picamera2 opencv-python
```

3. Copy the example configuration and edit values for your environment:

```bash
cp sampleConfig.json config.json
# then edit config.json and set ADafRUIT/MQTT/SMTP credentials and pins
```

4. Run the app:

```bash
python3 jeefHS.py
```

Notes:
- `jeefHS.py` expects `config.json` by default. You can supply another file by editing the script's call in `if __name__ == '__main__'` or modify instantiation.
- If you run off a Raspberry Pi, install the hardware-related packages above. The code falls back to simulated environmental readings and software stubs for GPIO if hardware libraries are not present.

## Configuration

Configuration is read from `config.json` (use `sampleConfig.json` as a template). Key configuration values used by the code are:

- MQTT / Adafruit IO
	- `ADAFRUIT_IO_USERNAME` (string) — Adafruit IO username used to build publish/subscribe topics
	- `ADAFRUIT_IO_KEY` (string) — Adafruit IO key / password
	- `MQTT_BROKER` (string) — MQTT broker host (default `io.adafruit.com` in samples)
	- `MQTT_PORT` (int) — MQTT port (default 1883; TLS may use 8883)
	- `MQTT_KEEPALIVE` (int) — MQTT keepalive seconds
	- `use_tls` (bool) — enable TLS for MQTT connections

- Feeds and topics
	- `CONTROL_FEEDS` (object) — map of logical device names to feed keys (e.g. `"party_mode": "party_mode_control"`). These feed keys are subscribed by `MQTT_communicator` and mapped back to device names.
	- `ENV_FEEDS` (object) — mapping of environmental keys to feed names (e.g. `"temperature": "temperature"`). Used when publishing sensor telemetry.
	- `SECURITY_FEEDS` (object) — mapping used for publishing security summaries (e.g. motion counts).
	- `STATUS_FEEDS` (object) — e.g. `mode` status feed name
	- `HEARTBEAT_FEED` (string) — topic/feed name used to publish periodic heartbeats
	- `heartbeat_interval` (int) — seconds between heartbeats

- Timing / intervals
	- `security_check_interval` (int) — how often (s) to read security sensors
	- `security_send_interval` (int) — how often (s) to publish security summaries
	- `env_interval` (int) — how often (s) to publish environmental readings
	- `flushing_interval` (int) — how frequently (s) the local log buffer is flushed to disk

- Devices and pins
	- `devices` (array) — logical device list (legacy usage)
	- `PINS` (object) — mapping of required pin names to pin specifiers. Required PINS in the code: `pir`, `dht`, `red_led`, `green_led`, `blue_led`, `fan`, `buzzer`. Pin specifiers may be strings like `"D13"`, `"BCM:17"` or integers depending on the module.
	- `use_dht` (bool) — whether to initialize a DHT22 sensor; otherwise environmental readings are simulated

- Security / camera / alerts
	- `camera_enabled` (bool) — enable image capture on motion
	- `image_dir` (string) — directory to write captured images
	- `alert_cooldown_s` (int) — minimum seconds between repeated email alerts of the same type
	- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `ALERT_FROM`, `ALERT_TO` — SMTP configuration used by `security_module` to send alerts via Brevo (or another SMTP relay)

Tip: Keep secrets (MQTT keys, SMTP password) out of source control. Consider using environment-variable injection or a separate excluded config for credentials.

## Project structure (files & purpose)

- `jeefHS.py` — Main application: loads configuration, composes modules, runs the data collection loop, handles logging and high-level orchestration (including party-mode behavior).
- `MQTT_communicator.py` — MQTT client wrapper using `paho-mqtt`. Subscribes to control feeds (`CONTROL_FEEDS`), decodes messages, and forwards device or mode commands to registered callbacks. Also provides `send_to_adafruit_io` for publishing telemetry.
- `device_control_module.py` — Abstraction over GPIO outputs (LEDs, fan, buzzer). Resolves pin specs, initialises outputs (or uses software fallbacks when run off-Pi), tracks states, and offers `set_device_state`, `pulse_buzzer`, and status reporting.
- `environmental_module.py` — Reads a DHT22 sensor when configured (with retries) and otherwise simulates temperature and humidity values. Exposes `get_environmental_data()` which returns a dict with `timestamp`, `temperature`, `humidity`, and `source`.
- `mode_manager.py` — Small thread-safe manager for global modes: `HOME`, `AWAY`, `NIGHT`. Allows registering callbacks to react to mode changes.
- `security_module.py` — Monitors a PIR input, captures images with Picamera2 on motion, sends email alerts via SMTP with cooldown handling, and returns structured security event data.
- `sampleConfig.json` — Example configuration to copy into `config.json` and edit.
- `config.json` — Runtime configuration (not committed by convention; present here if you configured it locally).

## How it works (module responsibilities)

- jeefHS.py (JeefHSApp)
	- Reads `config.json` and composes the system: `ModeManager`, `device_control_module`, `environmental_module`, `security_module`, and `MQTT_communicator`.
	- Registers callbacks: mode changes are published; MQTT device commands are handled and routed.
	- Runs `data_collection_loop()` which periodically collects environmental and security data, writes JSONL log lines to daily files, publishes to cloud feeds, and optionally sends heartbeat messages.
	- Implements a `party_mode` feature that animates the three LEDs in a background thread when triggered via the `party_mode` control feed.

- MQTT_communicator
	- Loads `CONTROL_FEEDS` and subscribes to the corresponding Adafruit IO topics. When a message arrives it maps the feed key back to a logical device or to `mode`, and calls the registered callbacks:
		- For device feeds: calls `on_set_device_state(device_name, bool)`
		- For mode feed: calls `on_set_mode(mode_string)`
	- Provides `send_to_adafruit_io(feed_name, value)` to publish telemetry and status updates.

- device_control_module
	- Resolves configured pins and creates output objects. If Blinka/digitalio is unavailable the module uses in-memory fallbacks so the code can run on non-Pi environments for testing.
	- Exposes `set_device_state(device_name, on)` to toggle devices and `pulse_buzzer()` for momentary buzzer activation.

- environmental_module
	- If `use_dht` is true and the DHT library is present, attempts a robust read with retries.
	- Otherwise returns a simulated but realistic temperature/humidity reading.

- mode_manager
	- Maintains a canonical uppercase mode value in `{'HOME','AWAY','NIGHT'}` and notifies registered callbacks on changes.

- security_module
	- Reads PIR input and, on motion, optionally captures a photo with Picamera2 and saves to `image_dir`.
	- Sends email alerts using SMTP (Brevo by default in samples) with cooldowns to avoid alert storms. Also reports whether the buzzer was triggered.

## Contract (inputs/outputs)

- Inputs:
	- `config.json` — JSON configuration file read at startup (contains credentials, pins, feed mappings, and intervals).
	- MQTT control messages on feeds mapped by `CONTROL_FEEDS`. Payloads like `ON`, `OFF`, `1`, `0` are interpreted as boolean device commands. Mode strings are passed through to the mode manager.
	- Local sensors: PIR (security), DHT22 (environmental) when enabled and hardware is present.

- Outputs:
	- MQTT publications to Adafruit IO-style feeds via `send_to_adafruit_io` (environmental telemetry, security summaries, mode status, heartbeat).
	- Device actuator changes (GPIO outputs for LEDs, fan, buzzer) via `device_control_module`.
	- Local JSONL logs written per-day containing timestamped events, environmental readings, security events, mode, and actuator snapshot.
	- Email alerts (SMTP) with optional image attachment for security motion events.

- Error modes and behavior:
	- Missing or invalid `config.json` causes `jeefHS.py` to raise a clear `RuntimeError` at startup; many modules provide fallbacks for sensor libraries but `security_module` requires a valid config with SMTP keys.
	- MQTT failures are logged. `MQTT_communicator` attempts to maintain a connection and will report publish/subscribe errors.
	- Sensor read failures: `environmental_module` retries DHT reads then falls back to simulated data. `security_module` writes placeholders if the camera capture fails.

---

If you'd like, I can now:
- add a `requirements.txt` with the minimal dependencies found in the code,
- add a short example `config.example.json` that removes secrets and keeps placeholders,
- or create a separate developer README for testing and unit tests.
Tell me which you'd like next and I'll proceed.

## What we learned

Building JeefHS reinforced a few practical lessons about designing small IoT systems. First, modularity pays off: separating MQTT, device control, sensing, security, and mode management made it much easier to develop and test each area independently. Second, defensive programming and graceful fallbacks (simulated sensors, software GPIO stubs) are essential when hardware dependencies are unreliable or absent during development. Third, configuration should be explicit and validated early, mapping feed names, pins, and credentials in JSON made wiring the system straightforward but also highlighted the need to protect secrets. Fourth, a robust logging and local JSONL history proved invaluable for diagnosing intermittent network, sensor, or camera issues. Finally, simple UX choices, clear feed naming, concise mode semantics, and cooldowns for alerts, significantly reduce false positives and make the whole system easier to operate and extend.
