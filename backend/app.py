#!/usr/bin/env python3
"""
UV-K5 Remote Transceiver - Web Interface
Control a Quansheng UV-K5 via AIOC (All-In-One-Cable) from a web browser.

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
                if self.connected:
                    # Poll RSSI and other status
                    # TODO: Implement actual status polling
                    pass
                eventlet.sleep(1.0)
            except Exception as e:
                logger.error(f"Monitor error: {e}")
                eventlet.sleep(5.0)


# ============================================================
# Web Application
# ============================================================

app = Flask(__name__, 
            static_folder='../frontend/static',
            template_folder='../frontend/templates')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'uvk5-remote-dev-key')

socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

# Global radio instance
radio = UVK5Radio()

# Simulated mode for development without hardware
SIMULATE = os.environ.get('UVK5_SIMULATE', 'true').lower() == 'true'


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


# WebSocket events for real-time updates

@socketio.on('connect')
def ws_connect():
    logger.info(f"WebSocket client connected: {request.sid}")
    emit('radio_update', radio.get_status())


@socketio.on('ptt_press')
def ws_ptt_press():
    radio.set_ptt(True)
    emit('radio_update', radio.get_status(), broadcast=True)


@socketio.on('ptt_release')
def ws_ptt_release():
    radio.set_ptt(False)
    emit('radio_update', radio.get_status(), broadcast=True)


@socketio.on('set_frequency')
def ws_set_frequency(data):
    freq = data.get('frequency')
    if freq:
        radio.set_frequency(float(freq))
        emit('radio_update', radio.get_status(), broadcast=True)


@socketio.on('audio_rx')
def ws_audio_rx(data):
    """Receive audio from browser microphone and forward to radio."""
    # TODO: Forward audio data to AIOC sound card
    pass


@socketio.on('disconnect')
def ws_disconnect():
    logger.info(f"WebSocket client disconnected: {request.sid}")


# ============================================================
# Main
# ============================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    host = os.environ.get('HOST', '0.0.0.0')
    
    logger.info(f"Starting UV-K5 Remote on {host}:{port}")
    logger.info(f"Simulation mode: {SIMULATE}")
    
    socketio.run(app, host=host, port=port, debug=False)
