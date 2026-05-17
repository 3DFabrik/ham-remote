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
- RF / RX / TX level meter bars with LED-strip style gradient

### 🎙️ Audio
- **Bidirectional audio streaming (RX + TX)**
- **Opus codec end-to-end** – ~24 kbit/s, 20ms frames, WASM decoder in browser
- RX: Sound card → Opus encode → WebSocket → Browser WASM decode → Speaker
- TX: Browser mic → Opus/WebM → WebSocket → Server → Sound card → Radio
- Half-duplex: RX pauses during TX
- Configurable audio devices (USB sound cards)
- Audio level metering with peak-hold and clipping detection

### ⌨️ PTT
- Hold-to-talk button (touch + mouse)
- Configurable keyboard hotkey (default: Spacebar)
- Persists across sessions

### 📡 Multi-Transceiver Support
- **Quansheng UV-K5** – via AIOC cable, 38400 baud
- **Yaesu FT-7800/FT-8300** – CAT protocol, 9600 baud, RS232
- **Xiegu X6100** – USB-C (CAT + sound card), planned
- **Kenwood TS-2000** – RS232 CAT, planned
- Radio driver registry – easy to add more radios

### 🌐 Remote Access
- Works behind a reverse proxy (Caddy, nginx, etc.)
- Let's Encrypt TLS support via reverse proxy
- Accessible from anywhere with proper DNS setup

### 🎨 UI
- Tab navigation: Main (operation) + Setup (configuration)
- Dark theme, mobile-first design
- Transceiver and audio device selection in Setup tab
- Audio level bars: RF (signal), RX (received audio), TX (microphone)
- Responsive – works on phone, tablet, and desktop

## Quick Start

```bash
# Clone
git clone https://github.com/3DFabrik/ham-remote.git
cd ham-remote

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install libopus (required for Opus codec)
# Debian/Ubuntu: apt install libopus0
# macOS: brew install opus

# Run
python backend/app.py
```

Open **http://localhost:8080** in your browser.

### HTTPS Setup

HTTPS is required for microphone access from a remote device. Use a reverse proxy:

**Option A: Caddy with local TLS**
```
https://your-server:8444 {
    tls internal
    reverse_proxy 127.0.0.1:8080 {
        flush_interval -1
    }
}
```

**Option B: Caddy with Let's Encrypt (public domain)**
```
ham.yourdomain.com {
    reverse_proxy 127.0.0.1:8080 {
        flush_interval -1
    }
}
```

## Hardware Setup

### Yaesu FT-7800/8300
```
[FT-8300] ←RS232→ [FTDI USB Adapter] → [Server]
[FT-8300] ←Audio→ [USB Sound Card]   → [Server]
                                        ↕
                                   Web Browser
```

### Quansheng UV-K5
```
[UV-K5] ←→ [AIOC] → USB → [Server]
                            ↕
                        Web Browser
```

## Architecture

```
frontend/
  static/
    css/style.css              - Dark theme, LED-strip level bars
    js/app.js                  - Frontend logic, WebSocket, audio playback
    js/opus-decoder.min.js     - WASM Opus decoder (self-hosted)
    img/logo.svg               - Logo
  templates/
    index.html                 - Main page with tab navigation
backend/
  app.py                       - Flask + SocketIO + radio drivers + audio engine
requirements.txt               - Python dependencies
```

### Audio Pipeline (RX) — Opus
```
Radio → USB Sound Card → arecord (16kHz S16_LE)
    → Opus encode (20ms frames, opuslib)
    → base64 → Socket.IO audio_tx
    → Browser WASM Opus decode → AudioContext → Speaker
```

### Audio Pipeline (TX)
```
Browser Mic → MediaRecorder (WebM/Opus)
    → Socket.IO audio_rx → Server → Opus decode → aplay → Sound Card → Radio
```

## Supported Radios

| Radio | Connection | Protocol | Status |
|-------|-----------|----------|--------|
| Quansheng UV-K5 | AIOC USB | Serial 38400 baud | 🚧 In progress |
| Yaesu FT-7800/8300 | RS232 / FTDI | CAT 9600 baud | ✅ Working |
| Xiegu X6100 | USB-C | CAT + Sound card | 📋 Planned |
| Kenwood TS-2000 | RS232 | CAT 9600 baud | 📋 Planned |

Adding a new radio: create a class extending `UVK5Radio`, register it with `register_radio()`.

## Requirements

- Python 3.10+
- libopus (system library for Opus codec)
- USB serial adapter (FTDI/CH340 for CAT control)
- USB sound card for audio I/O
- Browser with WebSocket and WASM support

## Roadmap

- [x] ~~Audio level metering (RF/RX/TX bars)~~
- [x] ~~Live audio streaming (RX from radio)~~
- [x] ~~HTTPS via Caddy reverse proxy~~
- [x] ~~Yaesu FT-7800/8300 CAT driver~~
- [x] ~~Opus end-to-end audio codec~~
- [x] ~~Remote access via public domain~~
- [ ] TX audio path (browser mic → radio)
- [ ] Xiegu X6100 support
- [ ] Kenwood TS-2000 support
- [ ] Authentication / login page
- [ ] Raspberry Pi deployment
- [ ] Docker packaging
- [ ] Hamlib integration
- [ ] Memory channel management
- [ ] Band scope / waterfall display

## Known Issues

- **Zombie processes**: Server restarts may leave old Python processes. Use `fuser -k 8080/tcp` before starting.
- **Audio dropout**: Occasional brief dropouts due to clock drift between capture and playback. Pre-buffering and drift correction mitigate but don't fully eliminate this.

## License

MIT
