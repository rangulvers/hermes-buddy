# hermes-buddy

Hermes plugin — OLED status display showing active tool, session state,
and a buddy companion on a D1 Mini + SSD1306 128×64.

Designed to share hardware with [claude-buddy](https://github.com/rangulvers/claude-buddy):
one device, two agents, priority-based display.

## Install

```bash
hermes plugins install rangulvers/hermes-buddy
hermes plugins enable hermes-buddy
```

Optional config in `~/.hermes/.env`:
```
HERMES_BUDDY_PORT=3004
HERMES_BUDDY_TOKEN=your-secret-token
```

On next `hermes` start the server starts automatically on port 3004.

## Hardware

- Wemos D1 Mini (ESP8266)
- SSD1306 0.96" OLED 128×64 I2C
- Wiring: D1→SCL, D2→SDA, 3V3→VCC, GND→GND

## ESPHome setup

1. Open `esphome/hermes-buddy-example.yaml`
2. Replace `YOUR_CLAUDE_SERVER_IP` and `YOUR_HERMES_SERVER_IP` with the IP of the machine running each agent (often the same IP if both run on one host)
3. Add WiFi credentials + secrets to your ESPHome `secrets.yaml`
4. Flash via ESPHome dashboard → Install

The firmware polls Claude Buddy (:3003) and Hermes Buddy (:3004) every 5 seconds. Claude takes display priority when active; Hermes shows when Claude is idle; both agents show their buddy in alternating idle mode.

## Verify

```bash
# Health check
curl http://localhost:3004/health

# Status (no device — just agent activity)
curl http://localhost:3004/status | python3 -m json.tool

# List hatched buddies (replace token)
curl http://localhost:3004/admin/buddies \
  -H "Authorization: Bearer hermes-buddy-changeme" | python3 -m json.tool
```

## Buddy types

BOT · CAT · OWL · GHOST · ALIEN · BEAR · FOX · DRAGON · BUNNY · CRYSTAL

Rarities: Common · Uncommon · Rare · Epic · Legendary · Mystical

Level progression: exponential ×2 per level, driven by token usage.
