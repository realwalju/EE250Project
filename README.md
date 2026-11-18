# Fortnite Desk Companion (EE250 Final Project)

**Team:** Walton, Andrea

A desk system that reacts to Fortnite games in real time. The gaming PC detects
game events from the screen with computer vision, a Raspberry Pi 5 hub turns them
into lighting effects and LLM-written recaps on an e-ink "desk companion", and our custom ESP32-S3 sensor PCB adds a button, a status LED and
room sensors (light, temperature, desk vibration).

See [`docs/overview.md`](docs/overview.md) for the full design.

> Submission note: the course asks for a `README.txt`. Before submitting,
> export this file to `README.txt` (team names, run instructions, libraries).

## Repository layout

```
esp32/                ESP-IDF v5/v6 projects
  components/desk_net/  Shared Wi-Fi + MQTT code for both boards
  sensor_board/   ESP32-S3 on our custom PCB: button, LED, light, temp, vibration
  mood_light/     ESP32-S3 using its built-in RGB LED for live game effects
pi/
  hub.py          Central hub: MQTT logic, match state, tilt detection, SQLite log
  companion.py    Companion mood, quick reactions, LLM post-match recaps
  eink.py         Draws the companion screen on the Waveshare 7.3" e-ink display
  web.py          Flask dashboard
  mosquitto.conf  MQTT broker config for the Pi
pc/
  test_publisher.py  Sends fake game events so everything can be tested without Fortnite
  detector.py        Screen-capture event detector (template matching) + PC activity
  templates/         Cropped images of on-screen messages, one folder per event
tests/
  test_system.py  Automated tests (no hardware needed): python -m pytest tests -q
docs/
  overview.md     Project overview / design doc
  video_plan.md   Shot list for the 4-minute demo video
```

## MQTT topics

| Topic | From -> to | Example payload |
|---|---|---|
| `desk/game/event` | PC -> Pi | `{"type":"elim"}` (`match_start`, `elim`, `knocked`, `eliminated`, `victory`, `match_end`) |
| `desk/pc/activity` | PC -> Pi | `{"app":"FortniteClient","input_rate":42}` |
| `desk/leds/effect` | Pi -> mood light | `{"effect":"victory"}` |
| `desk/input/button` | Sensor board -> Pi | `{"id":"clip","press":"short"}` |
| `desk/sensors` | Sensor board -> Pi | `{"light_pct":63,"temp_c":23.4,"vib":2}` |
| `desk/notify` | Pi -> sensor board | `{"msg":1}` (LED double-blinks) |

## Setup

All devices must be on the same network. Use a phone hotspot for demos, since
campus Wi-Fi usually blocks device-to-device traffic. Give the Pi a fixed IP
(or note it) so the ESP32s can reach the broker.

### 1. Raspberry Pi (broker + hub)

```bash
sudo apt install mosquitto mosquitto-clients
sudo cp pi/mosquitto.conf /etc/mosquitto/conf.d/ee250.conf
sudo systemctl restart mosquitto

cd pi
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # optional: add an Anthropic API key for LLM recaps
python hub.py --broker localhost
```

**E-ink display (Waveshare).** Turn on SPI (`sudo raspi-config` -> Interface
Options -> SPI). If the old InkyPi software is installed, stop it so it doesn't
fight for the display: `sudo systemctl disable --now inkypi`. Then install
Waveshare's driver into the venv:

```bash
git clone --depth 1 https://github.com/waveshareteam/e-Paper ~/e-Paper
pip install ~/e-Paper/RaspberryPi_JetsonNano/python
ls ~/e-Paper/RaspberryPi_JetsonNano/python/lib/waveshare_epd   # driver names
```

The hub drives our Waveshare 7.3" Spectra 6 panel (`epd7in3e`) by default, so
`python hub.py` is enough. For a different panel pass its driver name with
`--eink-model` (or set `EINK_MODEL` in `pi/.env`).
Black/white panels are thresholded to pure black; 7-colour panels (names ending
in `f` or `e`) get colour. Force either with `--eink-color mono|color`. Without
a display (or with `--no-eink`) the hub saves `pi/eink_preview.png` instead. For a Pimoroni
Inky, use `--eink-model inky`.

The hub also serves the dashboard at `http://<PI_IP>:5000/` (open it on the laptop).

### 2. ESP32-S3 boards (ESP-IDF v5 or v6)

Both boards are ESP-IDF projects that share `esp32/components/desk_net`
(Wi-Fi + MQTT). In each project folder, copy `secrets_example.h` to `secrets.h`
and fill in your hotspot name, password and the Pi's IP (`secrets.h` is
gitignored). The ESP32-S3 only does 2.4 GHz Wi-Fi: on an iPhone hotspot, turn on
**Maximize Compatibility**.

From the ESP-IDF terminal, with the board on its **COM** (UART) USB port:

```
cd esp32\sensor_board
idf.py set-target esp32s3
idf.py build
idf.py -p COM5 flash monitor
```

Replace `COM5` with your port; exit the monitor with Ctrl+]. Do the same in
`esp32\mood_light` for the second board (its first build downloads Espressif's
`led_strip` driver). If flashing fails to connect, hold BOOT, tap RST, release
BOOT and try again.

The YD-ESP32-S3's built-in RGB LED is on GPIO 48. On some board revisions it
only works after bridging the small solder pad labeled **RGB**.

### 3. Test without the game

```bash
cd pc
pip install -r requirements.txt
python test_publisher.py --broker <PI_IP>          # interactive keys
python test_publisher.py --broker <PI_IP> --demo   # scripted fake match
```

Watch everything with `mosquitto_sub -h <PI_IP> -t 'desk/#' -v`.

### 4. Game detection (gaming PC)

1. `python detector.py --save-every 2` while playing saves screenshots to `pc/recordings/`.
2. Crop each on-screen message (Victory Royale, knocked, eliminated, your elims)
   into `pc/templates/<event>/` - see [`pc/templates/README.md`](pc/templates/README.md).
3. `python detector.py --test recordings` scores the screenshots so you can check
   templates and thresholds offline.
4. `python detector.py --broker <PI_IP>` runs it live (`--debug` prints scores).

## External libraries

- **Python:** paho-mqtt, mss, opencv-python, numpy
- **ESP32:** ESP-IDF v5/v6 (Wi-Fi, esp-mqtt, LEDC, GPIO interrupts, ADC oneshot + calibration), Espressif `led_strip`
- **Pi:** Mosquitto MQTT broker, SQLite (Python standard library), Flask, Pillow,
  Waveshare `waveshare_epd` (or Pimoroni `inky`), gpiozero, lgpio, spidev, Anthropic Python SDK

## AI tool use

Initial project structure and starter code were generated with assistance from
Claude (Anthropic). AI-assisted files are marked with a comment at the top. We
reviewed, tested and modified the code ourselves.
