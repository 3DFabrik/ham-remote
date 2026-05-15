# UV-K5 Remote

Web interface for remote operation of a Quansheng UV-K5 via AIOC (All-In-One-Cable).

## Status: 🚧 In Development (Grundgerüst)

### Current features
- ✅ Web UI with frequency display and digit-by-digit tuning
- ✅ PTT button (hold-to-talk, touch + spacebar)
- ✅ Squelch / Volume / TX Power controls
- ✅ Quick frequency buttons (DMR, Calling, Relais)
- ✅ WebSocket real-time updates
- ✅ Simulation mode (works without hardware)
- ✅ Mobile-first dark theme

### TODO
- [ ] Audio streaming (RX from radio → browser)
- [ ] Audio streaming (TX from browser microphone → radio)
- [ ] Actual UV-K5 serial protocol implementation
- [ ] PTT via AIOC CM108 HID endpoint
- [ ] Frequency setting via serial command
- [ ] RSSI / signal strength display
- [ ] Authentication (login page)
- [ ] HTTPS support
- [ ] Raspberry Pi deployment

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run in simulation mode (no hardware needed)
python backend/app.py

# Run with real hardware
UVK5_SIMULATE=false python backend/app.py
```

Open http://localhost:8080 in your browser.

## Hardware Setup

```
[UV-K5] ←→ [AIOC] ←→ USB ←→ [Server / Pi]
                                  ↕
                              Web Browser (Handy)
```

### Requirements
- Quansheng UV-K5 with QuanshengDock firmware (0.32.21q)
- AIOC (All-In-One-Cable) - github.com/skuep/AIOC
- USB connection to server
- 2m antenna
- 12V power adapter for continuous operation

## Architecture

```
frontend/          - HTML/CSS/JS (browser)
  static/css/      - Stylesheet (dark theme, mobile-first)
  static/js/       - Frontend logic + WebSocket client
  templates/       - Jinja2 templates
backend/
  app.py           - Flask + SocketIO server + UV-K5 serial interface
```
