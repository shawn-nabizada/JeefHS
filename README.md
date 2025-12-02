# Jeef Home Security (JeefHS)

[Demo video (YouTube)](https://youtu.be/M_X60AuhMnQ?si=Q_FRKQtvKjA0x56r) \|
[Flask Dashboard](https://jeefhomesecurity.onrender.com/devices) \|
[Adafruit IO Dashboard](https://io.adafruit.com/Snab/dashboards/jeefhs)

## Table of Contents
- [Overview](#overview)
- [System Architecture](#system-architecture)
- [Quick Start](#quick-start)
- [Configuration (.env & config.json)](#configuration-env--configjson)
- [Project Structure](#project-structure)
- [Web Application](#web-application)
- [How It Works (Offline Sync)](#how-it-works-offline-sync)
- [What We Learned](#what-we-learned)


## Overview

Created by Shawn Nabizada and Clayton Cheung, **JeefHS** is a
comprehensive IoT home security system that bridges edge computing
(Raspberry Pi) with cloud services (Adafruit IO & Neon Postgres).

The system coordinates environmental sensing, device actuators (LEDs,
fan, buzzer), and security monitoring (PIR + Camera). Unlike simple
trackers, JeefHS features a **robust offline-first architecture**: it
logs data locally to SQLite when the internet is down and synchronizes
with a cloud Postgres database when connectivity returns. A custom
**Flask Web Application** provides a user interface to view historical
data, check intrusion logs, and control devices remotely.

## System Architecture

JeefHS follows a hybrid Edge/Cloud architecture:

### Edge (Raspberry Pi)

-   Runs the main event loop (`jeefHS.py`)
-   Collects sensor data (DHT22, PIR)
-   Controls actuators (LEDs, Fan, Buzzer) via GPIO
-   Publishes live telemetry to **Adafruit IO** via MQTT
-   Logs events immediately to a local **SQLite** database (offline
    cache)
-   Background sync thread uploads unsynced data to **Neon Postgres**
    when internet connectivity is restored

### Cloud

-   **Adafruit IO**: MQTT broker for real-time control and dashboards
-   **Neon Postgres**: Persistent storage for historical data and
    security logs

### User Interface (Flask)

-   Hosted locally or on a cloud platform (e.g., Render)
-   Live data fetched from Adafruit IO APIs
-   Historical data queried from Postgres using SQL

## Quick Start

### 1. Create and activate a virtual environment

``` bash
python3 -m venv venv
source venv/bin/activate
```

### 2. Install dependencies

``` bash
pip install -r requirements.txt
# or manually:
# pip install paho-mqtt flask sqlalchemy psycopg2-binary python-dotenv adafruit-circuitpython-dht picamera2
```

### 3. Set up secrets (.env)

Create a `.env` file in the project root (do not commit this file):

``` bash
ADAFRUIT_IO_USERNAME=your_username
ADAFRUIT_IO_KEY=your_aio_key
DATABASE_URL=postgresql://user:pass@ep-xyz.aws.neon.tech/neondb
```

### 4. Configure hardware (config.json)

In `config.json`, update GPIO pins, feed
names, and intervals as needed.

### 5. Run the system

**Start the IoT system (Raspberry Pi):**

``` bash
python3 jeefHS.py
```

**Start the web interface:**

``` bash
python3 web_app/app.py
```

## Configuration (.env & config.json)

### Secrets (.env)

-   `ADAFRUIT_IO_USERNAME`, `ADAFRUIT_IO_KEY`: MQTT and API access
-   `DATABASE_URL`: Cloud Postgres (Neon) connection string

### Application Settings (config.json)

-   **Feeds**: Mapping of logical names to Adafruit IO feeds
-   **Pins**: GPIO pin assignments
-   **Timers**:
    -   `security_check_interval`
    -   `env_interval`
    -   `heartbeat_interval`

## Project Structure

-   `jeefHS.py` --- Main application loop and orchestration
-   `database_interface.py` --- Local SQLite storage and cloud sync engine
-   `MQTT_communicator.py` --- MQTT publisher/subscriber handler
-   `security_module.py` --- PIR motion detection and camera capture
-   `environmental_module.py` --- Temperature and humidity sensing
-   `device_control_module.py` --- GPIO abstraction for actuators
-   `web_app/`
    -   `app.py` --- Flask application
    -   `templates/` --- HTML views (Dashboard, Environment, Security,
        Devices)

## Web Application

The Flask dashboard provides:

-   **Dashboard**: Live temperature, humidity, and system mode
-   **Environment History**: Date-based temperature and humidity charts
-   **Security Logs**: Motion detection history with timestamps and images
-   **Device Control**: Remote control of Fan, Buzzer, and Party Mode

## How It Works (Offline Sync)

1.  Sensor data and events are always written to local SQLite with `synced = 0`
2.  A background sync thread runs every 10 seconds
3.  Unsynced rows are batch-inserted into the Postgres database
4.  On success, local rows are marked as `synced = 1`
5.  Failures are retried automatically

This guarantees no data loss during internet outages.

## What We Learned

Milestone 3 highlighted the complexity of distributed systems and
offline-first design. Separating the database synchronization into a
background daemon thread was critical for system stability. The project
also demonstrated how MQTT complements SQL by handling real-time control
while SQL manages historical data. Finally, separating hardware logic
from the Flask UI made development, testing, and deployment
significantly easier.
