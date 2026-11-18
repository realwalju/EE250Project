"""Send fake game events over MQTT so the whole system can be tested without
running Fortnite (and as a fallback replay for the demo).

Interactive:  python test_publisher.py --broker <PI_IP>
Scripted:     python test_publisher.py --broker <PI_IP> --demo
Light only:   python test_publisher.py --broker <PI_IP> --effect victory

AI-assisted: initial version generated with Claude (Anthropic), then reviewed
and tested by the team.
"""

import argparse
import json
import time

import paho.mqtt.client as mqtt

T_GAME = "desk/game/event"
T_SENSORS = "desk/sensors"
T_EFFECT = "desk/leds/effect"

KEYS = {
    "s": "match_start",
    "e": "elim",
    "k": "knocked",
    "x": "eliminated",
    "v": "victory",
    "m": "match_end",
}

# (seconds to wait before, topic, payload)
DEMO_SCRIPT = [
    (0, T_GAME, {"type": "match_start"}),
    (3, T_GAME, {"type": "elim"}),
    (3, T_GAME, {"type": "elim"}),
    (3, T_GAME, {"type": "knocked"}),
    (1, T_SENSORS, {"light_pct": 40, "temp_c": 24.0, "vib": 4}),  # fake desk slams
    (1, T_SENSORS, {"light_pct": 40, "temp_c": 24.0, "vib": 5}),  # -> tilt
    (5, T_GAME, {"type": "elim"}),
    (3, T_GAME, {"type": "victory"}),
]


def send(client, topic, payload):
    client.publish(topic, json.dumps(payload))
    print(f"sent {topic} {payload}")


def interactive(client):
    print("keys: " + ", ".join(f"{k}={v}" for k, v in KEYS.items())
          + ", t=fake desk slam, q=quit")
    while True:
        key = input("> ").strip().lower()
        if key == "q":
            break
        if key in KEYS:
            send(client, T_GAME, {"type": KEYS[key]})
        elif key == "t":
            send(client, T_SENSORS, {"light_pct": 40, "temp_c": 24.0, "vib": 8})
        else:
            print("unknown key")


def demo(client):
    for delay, topic, payload in DEMO_SCRIPT:
        time.sleep(delay)
        send(client, topic, payload)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--demo", action="store_true", help="play a scripted fake match")
    parser.add_argument("--effect", help="send one effect straight to the mood light")
    args = parser.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="desk-test-publisher")
    client.connect(args.broker, args.port)
    client.loop_start()
    try:
        if args.effect:
            send(client, T_EFFECT, {"effect": args.effect})
        elif args.demo:
            demo(client)
        else:
            interactive(client)
        time.sleep(0.5)  # let the last message go out
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
