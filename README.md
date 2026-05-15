# 📻 HAM Remote

Web interface for remote operation of amateur radio transceivers from any browser. Mobile-first, dark theme, optimized for touch operation.

**Made by Norbot 🤖**

![HAM Remote Screenshot](frontend/static/img/screenshot.png)

## Features

### 🎛️ Controls
- Frequency display with digit-by-digit tuning
- Squelch / Volume / TX Power controls
- Quick frequency buttons (DMR, Calling, Relays)
- Live S-Meter (S0 – S9+60, animated)

### 🎙️ Audio
- Bidirectional audio streaming (RX + TX)
- Opus codec – only ~24 kbit/s, optimized for voice
- Half-duplex: RX pauses during TX
- Configurable audio devices (USB sound cards)

### ⌨️ PTT
- Hold-to-talk button (touch + mouse)
- Configurable keyboard hotkey (default: Spacebar)
- Persists across sessions

### 📡 Multi-Transceiver Support
- **Quanscheng UV-K5** – via AIOC cable, 38400 baud
- **Yaesu FT-7800/FT-8300** – CAT protocol, 9600 baud, RS232
- Radio driver registry – easy to add more radios

### 🎨 UI
- Tab navigation: Main (operation) + Setup (configuration)
- Dark theme, mobile-first design
- Transceiver and audio device selection in Setup tab
- Responsive – works on phone, tablet, and desktop

## Quick Start

```bash
# Clone
git clone https://github.com/3DFabrik/ham-remote.git
cd ham-remote

# Install dependencies
pip install -r requirements.txt

# Run in simulation mode (no hardware needed)
python backend/app.py

# Run with real hardware
UVK5_SIMULATE=false python backend/app.py
```

Open **http://localhost:8080** in your browser.

## Hardware Setup

### Yaesu FT-7800/8300
```
[FT-8300] ←RS232→ [USB Adapter] → [Server / Pi]
[FT-8300] ←Audio→ [Behringer USB] → [Server / Pi]
                                    ↕
                                Web Browser (Phone)
```

### Quansheng UV-K5
```
[UV-K5] ←→ [AIOC] → USB → [Server / Pi]
                            ↕
                        Web Browser (Phone)
```

## Architecture

```
frontend/
  static/
    css/style.css    - Dark theme stylesheet
    js/app.js        - Frontend logic, WebSocket, Opus audio
    img/logo.svg     - Logo
  templates/
    index.html       - Main page with tab navigation
backend/
  app.py             - Flask + SocketIO + radio drivers + audio engine
requirements.txt     - Python dependencies
```

## Supported Radios

| Radio | Connection | Protocol | Status |
|-------|-----------|----------|--------|
| Quansheng UV-K5 | AIOC USB | Serial 38400 baud | 🚧 In progress |
| Yaesu FT-7800/8300 | RS232 adapter | CAT 9600 baud | 🚧 In progress |

Adding a new radio: create a class extending `UVK5Radio`, register it with `register_radio()`.

## Requirements

- Python 3.10+
- USB serial adapter (for CAT control)
- USB sound card (for audio, e.g. Behringer UM2)
- Browser with WebSocket support

## Roadmap

- [ ] Hardware testing with FT-8300 + Behringer audio interface
- [ ] Authentication / login page
- [ ] HTTPS (via Caddy reverse proxy)
- [ ] Raspberry Pi Zero 2W deployment
- [ ] More radio drivers (IC-7300, FT-991, etc.)
- [ ] Memory channel management
- [ ] Band scope / waterfall display

## License

MIT
