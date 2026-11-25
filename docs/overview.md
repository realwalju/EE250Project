# Fortnite Desk Companion: EE250 Final Project

**Team:** Walton, Andrea
**Due:** December 7, 2026

## Summary
A desk system that reacts to your Fortnite games in real time and keeps track of your day. A program on the gaming PC watches the game screen with computer vision and detects events like eliminations, getting knocked and Victory Royales. A Raspberry Pi 5 acts as the central hub. It turns those events into lighting effects on ESP32-S3 nodes, combines them with room sensor data, and uses an LLM to give an e-ink "desk companion" a personality. The companion reacts to your matches, writes post-match coaching recaps and nudges you about study time.

## System nodes

| Node | Hardware | Role |
|---|---|---|
| Gaming PC | Windows PC | Captures the game screen, detects events with OpenCV, tracks PC activity (focused app, input rate) |
| Raspberry Pi 5 | Pi 5 + e-ink display | MQTT broker (Mosquitto), match state tracking, mood logic, SQLite database, Flask dashboard, LLM API calls, e-ink output |
| Desk sensor board | ESP32-S3 on our custom PCB | Push button, status LED, temperature sensor, vibration switch |
| Mood light | ESP32-S3 (built-in RGB LED) | Live lighting effects for game events |
| Laptop | Any browser | Web dashboard |
| Spare | ESP32-S3 | Backup, or a second mood light |

## Data flow
1. The gaming PC detects a game event and publishes it over MQTT to the Pi.
2. The Pi updates match state, logs the event and decides how to respond.
3. The Pi sends a light effect to the mood light and logs the event for the dashboard.
4. The desk sensor board sends button presses and sensor readings to the Pi.
5. After each match, the Pi sends the match timeline to the LLM API and shows the recap on the e-ink screen.

In one line: PC sees, Pi decides, ESP32s show and take input, e-ink and dashboard display.

## Desk sensor board (custom PCB)

| Part | Pin | Purpose |
|---|---|---|
| NTC thermistor | GPIO 5 (ADC) | Room temperature over a session |
| Vibration switch | GPIO 6 | Desk slam detection, used for tilt detection |
| Push button | GPIO 7 | Clip a moment, interact with the companion (short, long and double press) |
| LED | GPIO 8 (PWM) | System status and notifications |

## Data processing
- **Computer vision on the PC:** crops screen regions (health bar, elimination feed, center banner) and uses color thresholds, template matching and OCR to recognize events. An event only counts if it appears in several consecutive frames, which filters out false triggers.
- **Combining several signals:** tilt detection combines game events (knocked or eliminated) with bursts of desk vibration counted over a short time window. The companion's mood combines live game events, time of day, button presses and recent match results.
- **Sensor signal processing:** converts the thermistor reading to temperature with the Beta equation, smooths the ADC readings, and counts vibration pulses over a time window.
- **Match state tracking on the Pi:** lobby -> in match -> match ended.
- **LLM:** post-match coaching recaps and the companion's personality and nudges. It is kept out of the real-time path.

## Outputs

| Output | Speed | What it shows |
|---|---|---|
| Mood light (RGB LED) | Under 1 second | White pulse = elimination, red flash = knocked, gold = Victory Royale, orange flicker = tilt, camera flash + cyan = clip saved, blue = study mode |
| PCB status LED | Instant | Breathing = connected, fast blink = disconnected, double blink = new message, flash = button received |
| E-ink display | ~20 s during a match, instantly for recaps | Companion face (13 expressions: hype on elims, shocked when knocked, crown on a win...), LLM recap, session stats, nudges |
| Web dashboard | Real time | Event timeline, match history, sensor graphs |

## Key design choices
- **Vision runs on the PC, not the Pi.** Only small event messages cross Wi-Fi, not video.
- **Light animations are stored on the ESP32.** The Pi sends short commands, so effects stay smooth even if Wi-Fi lags.
- **The LLM stays out of the real-time path.** Lights react instantly from our own detection code, so a slow API never delays them.
- **No extra purchases.** Everything uses hardware we already have: built-in LEDs, our sensor PCB, and the Pi with its e-ink screen.

## Timeline
- **Nov 17-23:** MQTT working end to end. Sensor board code (button, LED, sensors). Mood light reacting to test messages. Pi receives and logs everything.
- **Nov 24-30:** Computer vision detection on the PC using recorded gameplay. Match state tracking and tilt detection on the Pi.
- **Dec 1-4:** LLM recaps and companion, e-ink layouts, web dashboard.
- **Dec 5-7:** Bug fixes, README, 2-page write-up with block diagram, record the 4-minute video of the system in use.

## Risks and limitations
- **Detection accuracy:** depends on game resolution and UI scale. We tune it on recorded gameplay.
- **Demo setup:** campus Wi-Fi may block device-to-device traffic, so we use a phone hotspot. The demo is live, plus the recorded video.
- **Subtle lights:** the built-in RGB LED is small. A WS2812 LED strip on the same MQTT topic would scale it up with no code changes.
- **LLM:** needs internet and costs a few dollars over the whole project. Recaps arrive a few seconds after each match.
- **Thermistor accuracy:** needs the part's Beta value. Readings are approximate.
