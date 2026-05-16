#!/usr/bin/env python3
"""
HAM Remote - Web Interface for Amateur Radio Transceivers
Control Yaesu, Quansheng, and other radios from a web browser.

Architecture:
- Flask web server with WebSocket support (flask-socketio)
- Serial communication with UV-K5 via AIOC
- Audio streaming via WebSocket (opus encoded)
- PTT control via serial/CAT commands
"""

import eventlet
eventlet.monkey_patch()

import os
import sys
import json
import time
import subprocess
import threading
import logging
from datetime import datetime

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
import serial
import serial.tools.list_ports

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger('uvk5-remote')

# ============================================================
# Radio Driver Registry
# ============================================================

RADIO_DRIVERS = {}


def register_radio(name, label, description, baud_rate, cls):
    """Register a radio driver."""
    RADIO_DRIVERS[name] = {
        'label': label,
        'description': description,
        'baud_rate': baud_rate,
        'cls': cls
    }


# ============================================================
# UV-K5 Serial Protocol
# Based on QuanshengDock reverse-engineered protocol
# ============================================================

class UVK5Radio:
    """Interface for Quansheng UV-K5 serial communication."""
    
    # UV-K5 serial settings (via AIOC virtual COM port)
    BAUD_RATE = 38400  # AIOC default for UV-K5
    
    # Command bytes (based on QuanshengDock protocol)
    # These are the serial commands the UV-K5 firmware understands
    CMD_VERSION = b'\x14\x05\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
    CMD_GET_FREQ = b'\x14\x05\x0a\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
    
    def __init__(self, port=None):
        self.port = port
        self.serial = None
        self.connected = False
        self.current_freq = 145.500  # Default 2m calling frequency
        self.current_mode = 'FM'
        self.ptt_active = False
        self.squelch = 1
        self.volume = 5
        self.tx_power = 'LOW'  # LOW=1W, HIGH=5W
        self.rssi = 0
        self.smeter_value = 0  # 0=S0, 1-9=S1-S9, 10=+20, 11=+40, 12=+60
        self._lock = threading.Lock()
        self._monitor_thread = None
        self._running = False
    
    def connect(self, port=None):
        """Connect to the UV-K5 via AIOC serial port."""
        if port:
            self.port = port
        
        if not self.port:
            # Try to auto-detect AIOC
            self.port = self._find_aioc()
        
        if not self.port:
            logger.warning("No AIOC/serial port found")
            return False
        
        try:
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1.0
            )
            self.connected = True
            logger.info(f"Connected to UV-K5 on {self.port}")
            
            # Start monitoring thread
            self._running = True
            self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self._monitor_thread.start()
            
            return True
        except serial.SerialException as e:
            logger.error(f"Failed to connect: {e}")
            self.connected = False
            return False
    
    def disconnect(self):
        """Disconnect from the radio."""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=3)
        if self.serial and self.serial.is_open:
            self.serial.close()
        self.connected = False
        logger.info("Disconnected from UV-K5")
    
    def _find_aioc(self):
        """Auto-detect AIOC virtual COM port."""
        ports = serial.tools.list_ports.comports()
        for port in ports:
            desc = (port.description or '').lower()
            mfg = (port.manufacturer or '').lower()
            # AIOC identifies as various things, look for common patterns
            if any(k in desc for k in ['aioc', 'stm32', 'ch340']):
                logger.info(f"Auto-detected AIOC on {port.device}")
                return port.device
            if 'stm32' in mfg or 'aioc' in mfg:
                logger.info(f"Auto-detected AIOC on {port.device} (manufacturer match)")
                return port.device
        # If nothing found, return first available COM port (for testing)
        if ports:
            logger.info(f"No AIOC detected, using first port: {ports[0].device}")
            return ports[0].device
        return None
    
    def _send_command(self, cmd_bytes):
        """Send raw command to the radio."""
        if not self.connected or not self.serial:
            return None
        with self._lock:
            try:
                self.serial.reset_input_buffer()
                self.serial.write(cmd_bytes)
                # Read response (most commands return 16+ bytes)
                response = self.serial.read(32)
                return response
            except serial.SerialException as e:
                logger.error(f"Serial communication error: {e}")
                self.connected = False
                return None
    
    def get_status(self):
        """Get current radio status."""
        return {
            'connected': self.connected,
            'port': self.port,
            'frequency': self.current_freq,
            'mode': self.current_mode,
            'ptt': self.ptt_active,
            'squelch': self.squelch,
            'volume': self.volume,
            'tx_power': self.tx_power,
            'rssi': self.rssi,
            'smeter': self.smeter_value,
            'timestamp': datetime.now().isoformat()
        }
    
    def set_frequency(self, freq_mhz):
        """Set the VFO frequency in MHz."""
        # Validate 2m band (144-146 MHz for Germany)
        if freq_mhz < 144.0 or freq_mhz > 146.0:
            logger.warning(f"Frequency {freq_mhz} MHz outside 2m band")
            return False
        
        self.current_freq = freq_mhz
        logger.info(f"Frequency set to {freq_mhz} MHz")
        # TODO: Send actual serial command to UV-K5
        # The QuanshengDock firmware supports frequency setting via serial
        return True
    
    def set_ptt(self, active):
        """Key/unkey PTT."""
        if not self.connected:
            return False
        self.ptt_active = active
        # PTT can be controlled via AIOC CM108 HID endpoint
        # or via serial command depending on firmware
        logger.info(f"PTT {'ON' if active else 'OFF'}")
        # TODO: Implement actual PTT toggle via AIOC
        return True
    
    def set_squelch(self, level):
        """Set squelch level (0-9)."""
        self.squelch = max(0, min(9, level))
        logger.info(f"Squelch set to {self.squelch}")
        return True
    
    def set_volume(self, level):
        """Set volume level (0-15)."""
        self.volume = max(0, min(15, level))
        # Note: volume control might not be available via serial
        logger.info(f"Volume set to {self.volume}")
        return True
    
    def set_tx_power(self, power):
        """Set TX power (LOW=1W, HIGH=5W)."""
        if power in ('LOW', 'HIGH'):
            self.tx_power = power
            logger.info(f"TX power set to {power}")
            return True
        return False
    
    def _monitor_loop(self):
        """Background thread to poll radio status."""
        while self._running:
            try:
                if self.connected and not SIMULATE:
                    # Poll S-Meter / signal strength from radio
                    # Yaesu CAT: read S-meter via specific command
                    # UV-K5: read RSSI from serial
                    # TODO: Implement actual polling per radio type
                    pass
                elif self.connected and SIMULATE:
                    # Simulate gentle S-meter fluctuation
                    import random
                    self.smeter_value = random.choice([3, 4, 5, 5, 6, 6, 7])
                eventlet.sleep(1.0)
            except Exception as e:
                logger.error(f"Monitor error: {e}")
                eventlet.sleep(5.0)


# ============================================================
# Yaesu FT-7800/8300 CAT Protocol
# Standard Yaesu CAT over 9600 baud serial
# ============================================================

class YaesuCATRadio(UVK5Radio):
    """Interface for Yaesu FT-7800/FT-8300 via CAT protocol."""
    
    BAUD_RATE = 9600
    
    # Yaesu CAT commands
    CMD_FREQ_SET = b'\x00\x00\x00\x00'  # Placeholder - 4-byte BCD frequency
    CMD_FREQ_READ = b'\x03'              # Read frequency
    CMD_MODE_SET = b'\x07'              # Set mode
    CMD_PTT_ON = b'\x08'                # PTT On
    CMD_PTT_OFF = b'\x88'               # PTT Off
    CMD_GET_STATUS = b'\xFA'            # Read status
    CMD_GET_SMETER = b'\xF7'            # Read S-meter
    
    def __init__(self, port=None):
        super().__init__(port)
        self.current_mode = 'FM'
    
    def connect(self, port=None):
        """Connect to Yaesu via serial."""
        if port:
            self.port = port
        if not self.port:
            return False
        try:
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_TWO,
                timeout=2
            )
            self.connected = True
            logger.info(f"Connected to Yaesu on {self.port} at {self.BAUD_RATE} baud")
            self._running = True
            self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self._monitor_thread.start()
            return True
        except serial.SerialException as e:
            logger.error(f"Failed to connect to Yaesu: {e}")
            self.connected = False
            return False
    
    def _send_cat(self, cmd):
        """Send a CAT command and read response."""
        if not self.serial or not self.serial.is_open:
            return None
        with self._lock:
            try:
                self.serial.reset_input_buffer()
                self.serial.write(cmd)
                response = self.serial.read(5)
                return response
            except serial.SerialException as e:
                logger.error(f"Yaesu CAT error: {e}")
                self.connected = False
                return None
    
    def set_frequency(self, freq_mhz):
        """Set frequency using Yaesu BCD format."""
        # Convert MHz to 10Hz units, then to 4-byte BCD
        freq_10hz = int(freq_mhz * 100000)
        bcd = freq_10hz.to_bytes(4, 'big')
        self._send_cat(bcd)
        self.current_freq = freq_mhz
    
    def set_ptt(self, active):
        """PTT via CAT command."""
        if active:
            self._send_cat(self.CMD_PTT_ON)
        else:
            self._send_cat(self.CMD_PTT_OFF)
        self.ptt_active = active
    
    def _monitor_loop(self):
        """Background thread polling Yaesu status."""
        while self._running:
            try:
                if self.connected and not SIMULATE:
                    resp = self._send_cat(self.CMD_GET_SMETER)
                    if resp and len(resp) >= 2:
                        # Yaesu returns S-meter as 2-byte value
                        raw = resp[1] if len(resp) > 1 else resp[0]
                        # Map 0-255 to S0-S9+60
                        if raw < 10:
                            self.smeter_value = 0
                        elif raw < 30:
                            self.smeter_value = 1 + int((raw - 10) / 4)
                        elif raw < 120:
                            self.smeter_value = 3 + int((raw - 30) / 15)
                        elif raw < 200:
                            self.smeter_value = 9
                        else:
                            self.smeter_value = min(12, 10 + int((raw - 200) / 27))
                elif self.connected and SIMULATE:
                    import random
                    self.smeter_value = random.choice([3, 4, 5, 5, 6, 6, 7])
                eventlet.sleep(1.0)
            except Exception as e:
                logger.error(f"Yaesu monitor error: {e}")
                eventlet.sleep(5.0)
# ============================================================

app = Flask(__name__, 
            static_folder='../frontend/static',
            template_folder='../frontend/templates')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'uvk5-remote-dev-key')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # No static file cache
app.jinja_env.auto_reload = True
app.jinja_env.cache_size = 0  # Disable Jinja template bytecode cache

socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

# Register all radio drivers
register_radio('uvk5', 'Quansheng UV-K5', 'Via AIOC cable, 38400 baud', 38400, UVK5Radio)
register_radio('yaesu-ft', 'Yaesu FT-7800/8300', 'CAT protocol, 9600 baud, RS232', 9600, YaesuCATRadio)

# Global radio instance
radio = UVK5Radio()
current_radio_type = 'uvk5'

# Simulated mode for development without hardware
SIMULATE = os.environ.get('UVK5_SIMULATE', 'true').lower() == 'true'

# Active audio devices
audio_playback_dev = os.environ.get('AUDIO_PLAYBACK', None)
audio_capture_dev = os.environ.get('AUDIO_CAPTURE', None)


def background_status_push():
    """Periodically push status updates to all connected clients."""
    while True:
        if radio.connected:
            socketio.emit('radio_update', radio.get_status())
        eventlet.sleep(2.0)


# Start background push thread
eventlet.spawn(background_status_push)


@app.route('/api/radio-types')
def api_radio_types():
    """List available radio types."""
    types = []
    for key, info in RADIO_DRIVERS.items():
        types.append({
            'id': key,
            'label': info['label'],
            'description': info['description'],
            'baud_rate': info['baud_rate']
        })
    return jsonify(types)


@app.route('/api/radio-type', methods=['GET', 'POST'])
def api_radio_type():
    """Get or set the current radio type."""
    global radio, current_radio_type
    
    if request.method == 'GET':
        info = RADIO_DRIVERS.get(current_radio_type, {})
        return jsonify({
            'type': current_radio_type,
            'label': info.get('label', ''),
            'description': info.get('description', '')
        })
    
    data = request.json or {}
    new_type = data.get('type')
    
    if new_type not in RADIO_DRIVERS:
        return jsonify({'success': False, 'error': f'Unknown radio type: {new_type}'}), 400
    
    if new_type == current_radio_type:
        return jsonify({'success': True, 'type': current_radio_type})
    
    # Disconnect current radio
    if radio.connected:
        radio.disconnect()
    
    # Create new instance
    driver_info = RADIO_DRIVERS[new_type]
    radio = driver_info['cls']()
    current_radio_type = new_type
    
    logger.info(f"Switched radio type to {driver_info['label']}")
    return jsonify({'success': True, 'type': current_radio_type, 'label': driver_info['label']})


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    """Get current radio status as JSON."""
    if SIMULATE and not radio.connected:
        return jsonify({
            'connected': True,
            'simulated': True,
            'port': 'SIMULATED',
            'frequency': radio.current_freq,
            'mode': radio.current_mode,
            'ptt': radio.ptt_active,
            'squelch': radio.squelch,
            'volume': radio.volume,
            'tx_power': radio.tx_power,
            'rssi': -87,
            'smeter': 5,  # Simulated: S5
            'timestamp': datetime.now().isoformat()
        })
    return jsonify(radio.get_status())


@app.route('/api/connect', methods=['POST'])
def api_connect():
    """Connect to the radio."""
    data = request.json or {}
    port = data.get('port')
    
    if SIMULATE and not port:
        radio.connected = True
        return jsonify({'success': True, 'simulated': True, 'port': 'SIMULATED'})
    
    success = radio.connect(port)
    return jsonify({'success': success, 'port': radio.port})


@app.route('/api/simulate', methods=['POST'])
def api_set_simulate():
    """Toggle simulation mode."""
    global SIMULATE
    data = request.json or {}
    SIMULATE = data.get('enabled', True)
    logger.info(f"Simulation mode set to: {SIMULATE}")
    return jsonify({'success': True, 'simulate': SIMULATE})


@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    """Disconnect from the radio."""
    radio.disconnect()
    return jsonify({'success': True})


@app.route('/api/frequency', methods=['POST'])
def api_set_frequency():
    """Set the radio frequency."""
    data = request.json or {}
    freq = data.get('frequency')
    
    if freq is None:
        return jsonify({'success': False, 'error': 'No frequency provided'}), 400
    
    try:
        freq = float(freq)
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'Invalid frequency'}), 400
    
    success = radio.set_frequency(freq)
    if success:
        socketio.emit('radio_update', radio.get_status())
    return jsonify({'success': success, 'frequency': radio.current_freq})


@app.route('/api/ptt', methods=['POST'])
def api_ptt():
    """Toggle PTT."""
    data = request.json or {}
    active = data.get('active', False)
    
    success = radio.set_ptt(active)
    if success:
        socketio.emit('radio_update', radio.get_status())
    return jsonify({'success': success, 'ptt': radio.ptt_active})


@app.route('/api/squelch', methods=['POST'])
def api_squelch():
    """Set squelch level."""
    data = request.json or {}
    level = data.get('level', 1)
    success = radio.set_squelch(int(level))
    return jsonify({'success': success, 'squelch': radio.squelch})


@app.route('/api/volume', methods=['POST'])
def api_volume():
    """Set volume level."""
    data = request.json or {}
    level = data.get('level', 5)
    success = radio.set_volume(int(level))
    return jsonify({'success': success, 'volume': radio.volume})


@app.route('/api/power', methods=['POST'])
def api_power():
    """Set TX power."""
    data = request.json or {}
    power = data.get('power', 'LOW')
    success = radio.set_tx_power(power)
    return jsonify({'success': success, 'tx_power': radio.tx_power})


@app.route('/api/ports')
def api_ports():
    """List available serial ports."""
    ports = serial.tools.list_ports.comports()
    return jsonify([{
        'device': p.device,
        'description': p.description,
        'manufacturer': p.manufacturer,
        'hwid': p.hwid
    } for p in ports])


@app.route('/api/audio/devices')
def api_audio_devices():
    """List available audio devices (playback and capture)."""
    devices = {'playback': [], 'capture': []}

    try:
        # Get playback devices
        result = subprocess.run(['aplay', '-l'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if line.startswith('card') or line.startswith('Karte'):
                    # Parse: card 0: NVidia [HDA NVidia], device 0: ALC889 Analog [ALC889 Analog]
                    import re
                    # Parse both English and German output formats
                    # EN: card 0: CODEC [USB Audio CODEC], device 0: USB Audio [USB Audio]
                    # DE: Karte 0: CODEC [USB Audio CODEC], Gerät 0: USB Audio [USB Audio]
                    match = re.search(r'(?:card|Karte) (\d+).*?(?:device|Gerät) (\d+)', line)
                    if match:
                        card_num = match.group(1)
                        dev_num = match.group(2)
                        # Extract name between first pair of brackets
                        name_match = re.search(r'\[([^\]]+)\]', line)
                        name = name_match.group(1) if name_match else f"Card {card_num}"
                        hw_id = f"hw:{card_num},{dev_num}"
                        devices['playback'].append({
                            'id': hw_id,
                            'name': name,
                            'detail': f"hw:{card_num},{dev_num}",
                            'card': int(card_num),
                            'device': int(dev_num)
                        })
    except (subprocess.TimeoutExpired, FileNotFoundError):
        logger.warning("aplay not available")

    try:
        # Get capture devices
        result = subprocess.run(['arecord', '-l'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if line.startswith('card') or line.startswith('Karte'):
                    import re
                    match = re.search(r'(?:card|Karte) (\d+).*?(?:device|Gerät) (\d+)', line)
                    if match:
                        card_num = match.group(1)
                        dev_num = match.group(2)
                        name_match = re.search(r'\[([^\]]+)\]', line)
                        name = name_match.group(1) if name_match else f"Card {card_num}"
                        hw_id = f"hw:{card_num},{dev_num}"
                        devices['capture'].append({
                            'id': hw_id,
                            'name': name,
                            'detail': f"hw:{card_num},{dev_num}",
                            'card': int(card_num),
                            'device': int(dev_num)
                            })
    except (subprocess.TimeoutExpired, FileNotFoundError):
        logger.warning("arecord not available")

    return jsonify(devices)


@app.route('/api/audio/config', methods=['GET', 'POST'])
def api_audio_config():
    """Get or set the active audio devices."""
    global audio_playback_dev, audio_capture_dev

    if request.method == 'GET':
        return jsonify({
            'playback': audio_playback_dev,
            'capture': audio_capture_dev
        })

    data = request.json or {}
    if 'playback' in data:
        audio_playback_dev = data['playback']
    if 'capture' in data:
        audio_capture_dev = data['capture']

    logger.info(f"Audio config: playback={audio_playback_dev}, capture={audio_capture_dev}")
    return jsonify({
        'success': True,
        'playback': audio_playback_dev,
        'capture': audio_capture_dev
    })


# ============================================================
# Audio Stream Manager
# WebSocket + Opus, 16kHz Mono
# ============================================================

class AudioStreamManager:
    """Manages bidirectional audio streaming between browser and sound card.
    Uses Opus codec: 16kHz mono, ~24 kbit/s, 20ms frames."""
    
    SAMPLE_RATE = 16000
    CHANNELS = 1
    FRAME_SIZE = 320  # 20ms @ 16kHz = 320 samples
    FRAME_BYTES = 640  # 320 samples * 2 bytes (16-bit)
    
    def __init__(self):
        self.tx_active = False
        self.rx_active = False
        self.rx_clients = set()
        self._capture_process = None
        self._capture_thread = None
        self._running = False
        
        # Opus encoder for RX (radio → browser)
        import opuslib
        self.opus_encoder = opuslib.Encoder(self.SAMPLE_RATE, self.CHANNELS, opuslib.APPLICATION_VOIP)
        self.opus_decoder = opuslib.Decoder(self.SAMPLE_RATE, self.CHANNELS)
        logger.info("Audio: Opus codec initialized (16kHz mono, VOIP mode)")
    
    def start_tx(self):
        """Start TX: prepare to receive mic audio from browser."""
        self.tx_active = True
        logger.info("Audio TX started")
    
    def stop_tx(self):
        """Stop TX."""
        self.tx_active = False
        logger.info("Audio TX stopped")
    
    def handle_tx_audio(self, data):
        """Handle incoming Opus audio from browser, decode and play to sound card."""
        if not self.tx_active:
            return
        try:
            import base64
            import numpy as np
            
            opus_data = base64.b64decode(data.get('data', ''))
            if not opus_data:
                return
            
            # Decode Opus → PCM
            pcm = self.opus_decoder.decode(opus_data, self.FRAME_SIZE)
            
            # Play to sound card
            if not SIMULATE and audio_playback_dev:
                # Write PCM to aplay subprocess
                if not hasattr(self, '_playback_proc') or self._playback_proc is None:
                    cmd = [
                        'aplay',
                        '-D', audio_playback_dev,
                        '-f', 'S16_LE',
                        '-c', '1',
                        '-r', str(self.SAMPLE_RATE),
                        '-t', 'raw'
                    ]
                    self._playback_proc = subprocess.Popen(
                        cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE
                    )
                try:
                    self._playback_proc.stdin.write(pcm)
                    self._playback_proc.stdin.flush()
                except BrokenPipeError:
                    logger.warning("aplay pipe broken, restarting")
                    self._playback_proc = None
        except Exception as e:
            logger.error(f"TX audio error: {e}")
    
    def start_rx_stream(self, client_sid):
        """Start streaming RX audio to a browser client."""
        self.rx_clients.add(client_sid)
        if not self.rx_active:
            self.rx_active = True
            self._running = True
            self._capture_thread = threading.Thread(target=self._rx_capture_loop, daemon=True)
            self._capture_thread.start()
            logger.info("Audio RX stream started (Opus)")
    
    def stop_rx_stream(self, client_sid):
        """Stop streaming to a specific client."""
        self.rx_clients.discard(client_sid)
        if not self.rx_clients:
            self.rx_active = False
            self._running = False
            logger.info("Audio RX stream stopped (no clients)")
    
    def _rx_capture_loop(self):
        """Capture audio from sound card, encode to Opus, stream to clients."""
        import base64
        
        while self._running:
            try:
                if SIMULATE:
                    eventlet.sleep(0.02)  # 20ms frames
                    # Simulate silence frame
                    silence = b'\x00' * self.FRAME_BYTES
                    opus_frame = self.opus_encoder.encode(silence, self.FRAME_SIZE)
                    encoded = base64.b64encode(opus_frame).decode()
                    for sid in list(self.rx_clients):
                        socketio.emit('audio_tx', {
                            'data': encoded,
                            'codec': 'opus',
                            'sampleRate': self.SAMPLE_RATE
                        }, room=sid)
                else:
                    # Real capture via arecord
                    device = audio_capture_dev or 'default'
                    cmd = [
                        'arecord',
                        '-D', device,
                        '-f', 'S16_LE',
                        '-c', '1',
                        '-r', str(self.SAMPLE_RATE),
                        '-t', 'raw'
                    ]
                    self._capture_process = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                    )
                    
                    while self._running and self._capture_process.poll() is None:
                        chunk = self._capture_process.stdout.read(self.FRAME_BYTES)
                        if len(chunk) == self.FRAME_BYTES:
                            # Encode PCM → Opus
                            opus_frame = self.opus_encoder.encode(chunk, self.FRAME_SIZE)
                            encoded = base64.b64encode(opus_frame).decode()
                            for sid in list(self.rx_clients):
                                socketio.emit('audio_tx', {
                                    'data': encoded,
                                    'codec': 'opus',
                                    'sampleRate': self.SAMPLE_RATE
                                }, room=sid)
                    
                    if self._capture_process.poll() is not None:
                        logger.warning("arecord process died, restarting...")
                        eventlet.sleep(1)
                        
            except Exception as e:
                logger.error(f"RX capture error: {e}")
                eventlet.sleep(1)
        
        # Cleanup
        if self._capture_process and self._capture_process.poll() is None:
            self._capture_process.terminate()
            self._capture_process = None
        if hasattr(self, '_playback_proc') and self._playback_proc:
            self._playback_proc.terminate()
            self._playback_proc = None


audio_stream_manager = AudioStreamManager()


# WebSocket events for real-time updates

@socketio.on('connect')
def ws_connect():
    logger.info(f"WebSocket client connected: {request.sid}")
    emit('radio_update', radio.get_status())


@socketio.on('ptt_press')
def ws_ptt_press():
    radio.set_ptt(True)
    if audio_stream_manager:
        audio_stream_manager.start_tx()
    emit('radio_update', radio.get_status(), broadcast=True)


@socketio.on('ptt_release')
def ws_ptt_release():
    radio.set_ptt(False)
    if audio_stream_manager:
        audio_stream_manager.stop_tx()
    emit('radio_update', radio.get_status(), broadcast=True)


@socketio.on('set_frequency')
def ws_set_frequency(data):
    freq = data.get('frequency')
    if freq:
        radio.set_frequency(float(freq))
        emit('radio_update', radio.get_status(), broadcast=True)


@socketio.on('audio_rx')
def ws_audio_rx(data):
    """Receive audio from browser microphone and forward to playback device."""
    # Audio data comes as base64-encoded opus frames
    # Forward to the selected playback device (radio's audio input)
    if audio_stream_manager and audio_stream_manager.tx_active:
        audio_stream_manager.handle_tx_audio(data)


@socketio.on('audio_start_tx')
def ws_audio_start_tx():
    """Browser starts transmitting audio."""
    if audio_stream_manager:
        audio_stream_manager.start_tx()


@socketio.on('audio_stop_tx')
def ws_audio_stop_tx():
    """Browser stops transmitting audio."""
    if audio_stream_manager:
        audio_stream_manager.stop_tx()


@socketio.on('audio_start_rx')
def ws_audio_start_rx():
    """Browser wants to receive audio stream."""
    if audio_stream_manager:
        audio_stream_manager.start_rx_stream(request.sid)


@socketio.on('audio_stop_rx')
def ws_audio_stop_rx():
    """Browser stops receiving audio."""
    if audio_stream_manager:
        audio_stream_manager.stop_rx_stream(request.sid)


@socketio.on('disconnect')
def ws_disconnect():
    logger.info(f"WebSocket client disconnected: {request.sid}")


# ============================================================
# Main
# ============================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    host = os.environ.get('HOST', '0.0.0.0')
    
    logger.info(f"Starting HAM Remote on {host}:{port}")
    logger.info(f"Simulation mode: {SIMULATE}")
    
    socketio.run(app, host=host, port=port, debug=False)
