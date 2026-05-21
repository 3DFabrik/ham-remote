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

from flask import Flask, render_template, jsonify, request, session, redirect, url_for
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
# Quansheng UV-K5 Serial Protocol
# Based on QuanshengDock by nicsure (github.com/nicsure/QuanshengDock)
# Requires firmware 0.32.21q or compatible
# Protocol: 38400 baud, 8N1, XOR-encrypted packets with CRC16
# ============================================================

class QuanshengProtocol:
    """Low-level Quansheng serial protocol implementation."""
    
    # XOR encryption key
    XOR_KEY = bytes([0x16, 0x6c, 0x14, 0xe6, 0x2e, 0x91, 0x0d, 0x40,
                     0x21, 0x35, 0xd5, 0x40, 0x13, 0x03, 0xe9, 0x80])
    
    # Packet markers
    HEADER = b'\xAB\xCD'
    FOOTER = b'\xDC\xBA'
    
    # Command IDs
    CMD_HELLO            = 0x0514
    CMD_IM_HERE          = 0x0515
    CMD_READ_EEPROM      = 0x051B
    CMD_READ_EEPROM_REP  = 0x051C
    CMD_WRITE_EEPROM     = 0x051D
    CMD_WRITE_EEPROM_REP = 0x051E
    CMD_GET_RSSI         = 0x0527
    CMD_RSSI_INFO        = 0x0528
    CMD_KEY_PRESS        = 0x0801
    CMD_GET_SCREEN       = 0x0803
    CMD_SCAN             = 0x0808
    CMD_SCAN_ADJUST      = 0x0809
    CMD_SCAN_REPLY       = 0x0908
    CMD_WRITE_REGISTERS  = 0x0850
    CMD_READ_REGISTERS   = 0x0851
    CMD_REGISTER_INFO    = 0x0951
    CMD_WRITE_GPIO       = 0x0860
    CMD_READ_GPIO        = 0x0861
    CMD_GPIO_INFO        = 0x0961
    CMD_GPIO_PULSE       = 0x0862
    CMD_ENTER_HWMODE     = 0x0870
    CMD_EXIT_HWMODE      = 0x0871
    CMD_SET_REPORT_REG   = 0x0872
    
    # Key codes for KeyPress command
    KEY_0 = 0
    KEY_1 = 1
    KEY_2 = 2
    KEY_3 = 3
    KEY_4 = 4
    KEY_5 = 5
    KEY_6 = 6
    KEY_7 = 7
    KEY_8 = 8
    KEY_9 = 9
    KEY_MENU  = 10  # A
    KEY_UP    = 11  # B
    KEY_DOWN  = 12  # C
    KEY_EXIT  = 13  # D
    KEY_STAR  = 14  # *
    KEY_HASH  = 15  # #
    KEY_F1    = 16  # Side key 1 (PTT long / scan)
    KEY_F2    = 19  # Side key 2 (scan / flashlight)
    
    @staticmethod
    def crc16(data_byte, crc=0):
        """CCITT CRC16 calculation."""
        crc ^= data_byte << 8
        for _ in range(8):
            crc <<= 1
            if crc > 0xFFFF:
                crc ^= 0x1021
            crc &= 0xFFFF
        return crc
    
    @staticmethod
    def xor_encrypt(data, start_index=0):
        """XOR encrypt/decrypt data bytes."""
        key = QuanshengProtocol.XOR_KEY
        return bytes(b ^ key[(i + start_index) & 15] for i, b in enumerate(data))
    
    @staticmethod
    def build_packet(cmd, *args):
        """Build a complete Quansheng serial packet.
        
        Packet format:
        [0xAB][0xCD][len_lo][len_hi][cmd_lo][cmd_hi][plen_lo][plen_hi][params...][crc_lo][crc_hi][0xDC][0xBA]
        
        len = bytes from cmd_lo to crc_hi (inclusive)
        """
        # Build parameter payload
        params = bytearray()
        for arg in args:
            if isinstance(arg, int):
                if arg <= 0xFF:
                    params.append(arg & 0xFF)
                elif arg <= 0xFFFF:
                    params.extend([(arg >> 0) & 0xFF, (arg >> 8) & 0xFF])
                else:
                    params.extend([
                        (arg >> 0) & 0xFF, (arg >> 8) & 0xFF,
                        (arg >> 16) & 0xFF, (arg >> 24) & 0xFF
                    ])
            elif isinstance(arg, bytes) or isinstance(arg, bytearray):
                params.extend(arg)
        
        param_len = len(params)
        # Build inner data: cmd(2) + plen(2) + params
        inner = bytearray([
            cmd & 0xFF, (cmd >> 8) & 0xFF,
            param_len & 0xFF, (param_len >> 8) & 0xFF
        ])
        inner.extend(params)
        
        # Calculate CRC16 over inner data
        crc = 0
        for b in inner:
            crc = QuanshengProtocol.crc16(b, crc)
        
        # XOR encrypt from cmd onwards
        encrypted_inner = bytearray(inner)
        xor_idx = 0
        for i in range(len(encrypted_inner)):
            encrypted_inner[i] ^= QuanshengProtocol.XOR_KEY[xor_idx & 15]
            xor_idx += 1
        
        # CRC bytes also encrypted
        crc_lo = (crc & 0xFF) ^ QuanshengProtocol.XOR_KEY[xor_idx & 15]
        xor_idx += 1
        crc_hi = ((crc >> 8) & 0xFF) ^ QuanshengProtocol.XOR_KEY[xor_idx & 15]
        
        # Inner length (cmd + plen + params + crc = 4 + param_len + 2)
        inner_len = len(encrypted_inner) + 2  # +2 for CRC bytes
        
        packet = bytearray([0xAB, 0xCD])
        packet.extend([inner_len & 0xFF, (inner_len >> 8) & 0xFF])
        packet.extend(encrypted_inner)
        packet.extend([crc_lo, crc_hi, 0xDC, 0xBA])
        
        return bytes(packet)
    
    @staticmethod
    def parse_packet(data):
        """Parse a received packet. Returns (cmd, params) or None."""
        if len(data) < 12:
            return None
        if data[0] != 0xAB or data[1] != 0xCD:
            return None
        if data[-2] != 0xDC or data[-1] != 0xBA:
            return None
        
        inner_len = data[2] | (data[3] << 8)
        # Inner data starts at offset 4
        encrypted_inner = data[4:4 + inner_len - 2]  # exclude CRC
        crc_received_enc = data[4 + inner_len - 2:4 + inner_len]
        
        # Decrypt
        xor_idx = 0
        decrypted = bytearray()
        for b in encrypted_inner:
            decrypted.append(b ^ QuanshengProtocol.XOR_KEY[xor_idx & 15])
            xor_idx += 1
        
        # Verify CRC
        crc_calc = 0
        for b in decrypted:
            crc_calc = QuanshengProtocol.crc16(b, crc_calc)
        
        crc_lo = crc_received_enc[0] ^ QuanshengProtocol.XOR_KEY[xor_idx & 15]
        xor_idx += 1
        crc_hi = crc_received_enc[1] ^ QuanshengProtocol.XOR_KEY[xor_idx & 15]
        crc_received = crc_lo | (crc_hi << 8)
        
        if crc_calc != crc_received:
            logger.warning(f"CRC mismatch: calc={crc_calc:04X} recv={crc_received:04X}")
            return None
        
        if len(decrypted) < 4:
            return None
        
        cmd = decrypted[0] | (decrypted[1] << 8)
        param_len = decrypted[2] | (decrypted[3] << 8)
        params = bytes(decrypted[4:4 + param_len])
        
        return (cmd, params)


class UVK5Radio:
    """Interface for Quansheng UV-K5 via QuanshengDock-compatible protocol.
    
    Supports firmware 0.32.21q and compatible.
    Uses BK4819 register access for direct radio control.
    """
    
    BAUD_RATE = 38400
    
    def __init__(self, port=None):
        self.port = port
        self.serial = None
        self.connected = False
        self.hw_mode = False  # Hardware mode (direct BK4819 register access)
        
        # Radio state
        self.current_freq = 145.500  # MHz
        self.current_mode = 'FM'
        self.ptt_active = False
        self.squelch = 1
        self.volume = 5
        self.tx_power = 'LOW'
        self.rssi_raw = 0
        self.rssi_pct = 0.0
        self.smeter_value = 0  # 0=S0...9=S9, 10=+20, 11=+40, 12=+60
        
        # BK4819 register cache
        self._reg_33 = 0  # GPIO/config register
        self._reg_30 = 0  # Configuration register
        self._freq_hi = 0  # High 16 bits of frequency
        
        # Threading
        self._lock = threading.Lock()
        self._rx_thread = None
        self._running = False
        
        # Response handling
        self._rx_buffer = bytearray()
        self._parse_stage = 0  # State machine for packet parsing
        self._pkt_len = 0
        self._pkt_data = bytearray()
    
    def connect(self, port=None):
        """Connect to UV-K5 and enter hardware mode."""
        if port:
            self.port = port
        
        if not self.port:
            self.port = self._find_aioc()
        
        if not self.port:
            logger.warning("No serial port found for UV-K5")
            return False
        
        try:
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.5
            )
            
            # Flush buffers
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()
            
            self.connected = True
            logger.info(f"Connected to UV-K5 on {self.port}")
            
            # Initialize: send key presses to wake up radio comms
            # QuanshengDock sends KeyPress(13)=EXIT then KeyPress(19)=F2
            self._send_packet(QuanshengProtocol.CMD_KEY_PRESS, QuanshengProtocol.KEY_EXIT)
            time.sleep(0.05)
            self._send_packet(QuanshengProtocol.CMD_KEY_PRESS, QuanshengProtocol.KEY_F2)
            time.sleep(0.1)
            
            # Enter hardware mode for direct BK4819 register access
            self._enter_hardware_mode()
            time.sleep(0.1)
            
            # Read initial register state (frequency + config)
            self._read_registers([0x38, 0x39, 0x33, 0x30, 0x31])
            time.sleep(0.2)
            
            # Start RX listener thread
            self._running = True
            self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self._rx_thread.start()
            
            return True
            
        except serial.SerialException as e:
            logger.error(f"Failed to connect to UV-K5: {e}")
            self.connected = False
            return False
    
    def disconnect(self):
        """Disconnect from radio, exit hardware mode first."""
        self._running = False
        if self.hw_mode and self.serial and self.serial.is_open:
            try:
                self._send_packet(QuanshengProtocol.CMD_EXIT_HWMODE)
            except Exception:
                pass
        if self._rx_thread:
            self._rx_thread.join(timeout=3)
        if self.serial and self.serial.is_open:
            self.serial.close()
        self.connected = False
        self.hw_mode = False
        logger.info("Disconnected from UV-K5")
    
    def _find_aioc(self):
        """Auto-detect serial port (AIOC or direct cable)."""
        ports = serial.tools.list_ports.comports()
        for port in ports:
            desc = (port.description or '').lower()
            mfg = (port.manufacturer or '').lower()
            if any(k in desc for k in ['aioc', 'stm32', 'ch340', 'cp210', 'ch910']):
                logger.info(f"Auto-detected serial adapter on {port.device}")
                return port.device
            if any(k in mfg for k in ['stm32', 'aioc', 'wch', 'silicon', 'cp210']):
                logger.info(f"Auto-detected adapter on {port.device} (manufacturer: {mfg})")
                return port.device
        if ports:
            logger.info(f"No adapter detected, trying first port: {ports[0].device}")
            return ports[0].device
        return None
    
    def _send_packet(self, cmd, *args):
        """Build and send a protocol packet."""
        if not self.connected or not self.serial:
            return
        packet = QuanshengProtocol.build_packet(cmd, *args)
        with self._lock:
            try:
                self.serial.write(packet)
            except serial.SerialException as e:
                logger.error(f"Send error: {e}")
                self.connected = False
    
    def _read_response(self, timeout=0.5):
        """Read one response packet (blocking with timeout)."""
        if not self.serial:
            return None
        
        buf = bytearray()
        deadline = time.monotonic() + timeout
        
        while time.monotonic() < deadline:
            avail = self.serial.in_waiting
            if avail > 0:
                buf.extend(self.serial.read(avail))
            else:
                b = self.serial.read(1)
                if b:
                    buf.extend(b)
                else:
                    if len(buf) > 0:
                        # Had data but timed out - try to parse
                        break
                    continue
            
            # Try to find a complete packet
            # Look for 0xAB 0xCD header
            while len(buf) >= 2:
                idx = buf.find(b'\xAB\xCD')
                if idx < 0:
                    buf.clear()
                    break
                if idx > 0:
                    buf = buf[idx:]
                
                if len(buf) < 4:
                    break
                
                pkt_len = buf[2] | (buf[3] << 8)
                total_len = 4 + pkt_len + 2  # header(4) + inner + footer(2)
                
                if len(buf) >= total_len:
                    packet_data = bytes(buf[:total_len])
                    buf = buf[total_len:]
                    return QuanshengProtocol.parse_packet(packet_data)
                else:
                    break  # Need more data
        
        return None
    
    def _rx_loop(self):
        """Background thread: continuously read and process responses."""
        while self._running and self.connected:
            try:
                result = self._read_response(timeout=1.0)
                if result:
                    cmd, params = result
                    self._handle_response(cmd, params)
            except Exception as e:
                if self._running:
                    logger.error(f"RX loop error: {e}")
                eventlet.sleep(0.1)
    
    def _handle_response(self, cmd, params):
        """Handle incoming response packets."""
        if cmd == QuanshengProtocol.CMD_REGISTER_INFO:
            self._handle_register_info(params)
        elif cmd == QuanshengProtocol.CMD_RSSI_INFO:
            self._handle_rssi_info(params)
        elif cmd == QuanshengProtocol.CMD_IM_HERE:
            logger.debug("Radio acknowledged HELLO")
        elif cmd == QuanshengProtocol.CMD_READ_EEPROM_REP:
            self._handle_eeprom_data(params)
        else:
            logger.debug(f"Unhandled response: cmd=0x{cmd:04X} len={len(params)}")
    
    def _handle_register_info(self, params):
        """Handle register info response.
        Format: count(2) + [reg_lo, reg_hi, val_lo, val_hi] * count
        """
        if len(params) < 2:
            return
        count = params[0] | (params[1] << 8)
        if len(params) < 2 + count * 4:
            return
        
        import struct
        for i in range(count):
            off = 2 + i * 4
            reg = params[off] | (params[off + 1] << 8)
            val = params[off + 2] | (params[off + 3] << 8)
            self._process_register(reg, val)
    
    def _process_register(self, reg, val):
        """Process individual BK4819 register values."""
        if reg == 0x38:
            # Frequency low 16 bits
            freq_raw = val | (self._freq_hi << 16)
            self._update_frequency_from_raw(freq_raw)
        elif reg == 0x39:
            # Frequency high 16 bits
            self._freq_hi = val
        elif reg == 0x33:
            self._reg_33 = val
        elif reg == 0x30:
            self._reg_30 = val
        elif reg == 0x67:
            # RSSI value (9-bit)
            self.rssi_raw = val & 0x1FF
            self._update_smeter()
        elif reg == 0x65:
            # Noise/AM RSSI
            noise = val & 0x7F
            # Subtract noise from raw RSSI for S-Meter
            self.rssi_pct = max(0, self.rssi_raw - noise) / 3.2
            self._update_smeter()
    
    def _update_frequency_from_raw(self, freq_raw):
        """Convert BK4819 frequency register value to MHz."""
        # BK4819 uses 10Hz units
        freq_hz = freq_raw * 10
        if freq_hz > 0:
            self.current_freq = round(freq_hz / 1e6, 4)
    
    def _update_smeter(self):
        """Convert RSSI percentage to S-Meter value (0-12)."""
        # RSSI percentage 0-100 → S0-S9+60
        # Approximate mapping based on typical UV-K5 values
        pct = min(100, max(0, self.rssi_pct))
        if pct < 5:
            self.smeter_value = 0   # S0
        elif pct < 12:
            self.smeter_value = 1   # S1
        elif pct < 20:
            self.smeter_value = 2   # S2
        elif pct < 28:
            self.smeter_value = 3   # S3
        elif pct < 36:
            self.smeter_value = 4   # S4
        elif pct < 44:
            self.smeter_value = 5   # S5
        elif pct < 52:
            self.smeter_value = 6   # S6
        elif pct < 60:
            self.smeter_value = 7   # S7
        elif pct < 70:
            self.smeter_value = 8   # S8
        elif pct < 80:
            self.smeter_value = 9   # S9
        elif pct < 90:
            self.smeter_value = 10  # +20
        elif pct < 95:
            self.smeter_value = 11  # +40
        else:
            self.smeter_value = 12  # +60
    
    def _handle_rssi_info(self, params):
        """Handle RSSI info response."""
        if len(params) >= 2:
            self.rssi_raw = params[0] | (params[1] << 8)
            self._update_smeter()
    
    def _handle_eeprom_data(self, params):
        """Handle EEPROM read response."""
        if len(params) >= 6:
            offset = params[4] | (params[5] << 8)
            size = params[6]
            data = params[8:8 + size]
            logger.debug(f"EEPROM data: offset=0x{offset:04X} size={size}")
    
    # ----------------------------------------------------------------
    # Hardware mode control (BK4819 register access)
    # ----------------------------------------------------------------
    
    def _enter_hardware_mode(self):
        """Enter hardware mode for direct BK4819 register control."""
        self._send_packet(QuanshengProtocol.CMD_ENTER_HWMODE)
        self.hw_mode = True
        # Send HELLO with timestamp (required by protocol)
        self._send_packet(QuanshengProtocol.CMD_HELLO, 0x12345678)
        logger.info("Entered hardware mode")
    
    def _exit_hardware_mode(self):
        """Exit hardware mode, return to normal radio operation."""
        self._send_packet(QuanshengProtocol.CMD_EXIT_HWMODE)
        self.hw_mode = False
        logger.info("Exited hardware mode")
    
    def _read_registers(self, registers):
        """Read BK4819 registers. Args: list of register addresses (16-bit)."""
        args = [len(registers)]
        for reg in registers:
            args.append(reg)
        self._send_packet(QuanshengProtocol.CMD_READ_REGISTERS, *args)
    
    def _write_registers(self, reg_val_pairs):
        """Write BK4819 registers. Args: list of (register, value) tuples."""
        args = [len(reg_val_pairs)]
        for reg, val in reg_val_pairs:
            args.extend([reg, val])
        self._send_packet(QuanshengProtocol.CMD_WRITE_REGISTERS, *args)
    
    def _poll_rssi(self):
        """Poll RSSI via BK4819 registers 0x67 and 0x65."""
        if self.hw_mode:
            self._read_registers([0x67, 0x65])
    
    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------
    
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
            'rssi': self.rssi_raw,
            'rssi_pct': round(self.rssi_pct, 1),
            'smeter': self.smeter_value,
            'hw_mode': self.hw_mode,
            'timestamp': datetime.now().isoformat()
        }
    
    def set_frequency(self, freq_mhz):
        """Set VFO frequency in MHz via BK4819 registers."""
        if not self.hw_mode:
            logger.warning("Not in hardware mode, cannot set frequency")
            return False
        
        # Convert MHz to BK4819 10Hz units
        freq_10hz = int(round(freq_mhz * 1e5))
        freq_lo = freq_10hz & 0xFFFF
        freq_hi = (freq_10hz >> 16) & 0xFFFF
        
        # Update reg33 band selection bit
        self._reg_33 &= ~0b11000  # Clear band bits
        if freq_10hz < 2800000:  # < 28 MHz
            self._reg_33 |= 0b100  # HF band
        else:
            self._reg_33 |= 0b1000  # VHF/UHF band
        
        # Write frequency registers + config trigger
        self._write_registers([
            (0x38, freq_lo),
            (0x39, freq_hi),
            (0x33, self._reg_33),
            (0x30, 0),        # Trigger
            (0x30, self._reg_30),  # Restore
        ])
        
        self.current_freq = freq_mhz
        logger.info(f"Frequency set to {freq_mhz:.4f} MHz (raw: {freq_10hz})")
        return True
    
    def set_ptt(self, active):
        """Key/unkey PTT via BK4819 GPIO register."""
        if not self.connected:
            return False
        self.ptt_active = active
        if self.hw_mode:
            # BK4819 register 0x33 bit 6 controls TX
            if active:
                self._reg_33 |= (1 << 6)  # Set TX bit
            else:
                self._reg_33 &= ~(1 << 6)  # Clear TX bit
            self._write_registers([(0x33, self._reg_33)])
        else:
            # Fallback: use KeyPress for PTT
            if active:
                self._send_packet(QuanshengProtocol.CMD_KEY_PRESS, QuanshengProtocol.KEY_F1)
            else:
                self._send_packet(QuanshengProtocol.CMD_KEY_PRESS, QuanshengProtocol.KEY_EXIT)
        logger.info(f"PTT {'ON' if active else 'OFF'}")
        return True
    
    def set_squelch(self, level):
        """Set squelch level (0-9) via EEPROM."""
        self.squelch = max(0, min(9, level))
        logger.info(f"Squelch set to {self.squelch}")
        return True
    
    def set_volume(self, level):
        """Set RX audio gain (0-15 maps to 0.0 - 1.5 gain multiplier)."""
        self.volume = max(0, min(15, level))
        # Map 0-15 to gain 0.0 - 1.5
        self.rx_gain = self.volume / 10.0
        logger.info(f"Volume set to {self.volume} (gain={self.rx_gain:.2f})")
        return True
    
    def set_tx_power(self, power):
        """Set TX power (LOW=1W, HIGH=5W)."""
        if power in ('LOW', 'HIGH'):
            self.tx_power = power
            logger.info(f"TX power set to {power}")
            return True
        return False
    
    def set_mode(self, mode):
        """Set modulation mode via BK4819 registers.
        
        Modes: FM=0, AM=1, USB=2
        Uses SetReportReg (0x872) + register 0x50 for IF config.
        """
        if not self.hw_mode:
            logger.warning("Not in hardware mode, cannot set mode")
            return False
        
        mode_map = {'FM': 0, 'AM': 1, 'USB': 2, 'NFM': 0}
        mode_val = mode_map.get(mode.upper(), 0)
        
        # SetReportReg for modulation
        self._send_packet(QuanshengProtocol.CMD_SET_REPORT_REG, 1, mode_val)
        
        # Register 0x50: IF filter bandwidth
        # AM mode uses wider filter (0xBB20), FM/USB narrower (0x3B20)
        if mode.upper() == 'AM':
            self._write_registers([(0x50, 0xBB20)])
        else:
            self._write_registers([(0x50, 0x3B20)])
        
        self.current_mode = mode.upper()
        logger.info(f"Mode set to {mode}")
        return True


# ============================================================
# Yaesu FT-7800/8300 CAT Protocol
# Old-style 5-byte binary CAT, 9600 baud, 8N2
# ============================================================

def _freq_to_bcd(freq_hz):
    """Convert frequency in Hz to 4-byte Yaesu BCD format.
    
    Yaesu BCD: each nibble = one digit, freq in 10Hz units.
    Example: 145.5000 MHz = 14550000 Hz = 1455000 * 10 Hz
    BCD: 14 55 00 00 → bytes 0x14 0x55 0x00 0x00
    """
    freq_10hz = int(freq_hz / 10)
    # Build BCD nibble by nibble (8 digits, packed into 4 bytes)
    digits = f"{freq_10hz:08d}"
    bcd = bytes([
        (int(digits[0]) << 4) | int(digits[1]),
        (int(digits[2]) << 4) | int(digits[3]),
        (int(digits[4]) << 4) | int(digits[5]),
        (int(digits[6]) << 4) | int(digits[7]),
    ])
    return bcd


def _bcd_to_freq(bcd_bytes):
    """Convert 4-byte Yaesu BCD to frequency in Hz."""
    digits = ''
    for b in bcd_bytes:
        digits += f'{(b >> 4) & 0x0F}{b & 0x0F}'
    return int(digits) * 10  # 10Hz units → Hz


class YaesuCATRadio(UVK5Radio):
    """Interface for Yaesu FT-7800/FT-8300 via old-style 5-byte CAT protocol."""
    
    BAUD_RATE = 9600
    
    # All CAT commands are exactly 5 bytes: [P1 P2 P3 P4 CMD]
    CMD_FREQ_SET    = 0x01  # P1-P4 = BCD frequency in 10Hz units
    CMD_FREQ_READ   = 0x03  # P1-P4 = 0x00, reply = 5 bytes BCD freq
    CMD_MODE_SET    = 0x07  # P1-P4 = mode code
    CMD_PTT_ON      = 0x08  # P1-P4 = 0x00
    CMD_PTT_OFF     = 0x88  # P1-P4 = 0x00
    CMD_GET_STATUS  = 0xFA  # P1-P4 = 0x00, reply = 5 bytes status
    CMD_GET_SMETER  = 0xF7  # P1-P4 = 0x00, reply = 5 bytes (S-meter in byte 2)
    
    # Mode codes (4-byte, little-endian-like)
    MODE_CODES = {
        'FM':  b'\x01\x00\x00\x00',
        'NFM': b'\x02\x00\x00\x00',  # Narrow FM (FT-8300)
        'AM':  b'\x03\x00\x00\x00',
        'WFM': b'\x04\x00\x00\x00',  # Wide FM
    }
    
    def __init__(self, port=None):
        super().__init__(port)
        self.current_mode = 'FM'
    
    def _build_cmd(self, cmd, data=b'\x00\x00\x00\x00'):
        """Build a 5-byte CAT command: [P1 P2 P3 P4 CMD]."""
        assert len(data) == 4
        return data + bytes([cmd])
    
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
    
    def _send_cat(self, cmd, data=b'\x00\x00\x00\x00'):
        """Send a 5-byte CAT command and read 5-byte response."""
        if not self.serial or not self.serial.is_open:
            return None
        packet = self._build_cmd(cmd, data)
        with self._lock:
            try:
                self.serial.reset_input_buffer()
                self.serial.write(packet)
                response = self.serial.read(5)
                if len(response) == 5:
                    return response
                logger.warning(f"Yaesu short response: {len(response)} bytes")
                return None
            except serial.SerialException as e:
                logger.error(f"Yaesu CAT error: {e}")
                self.connected = False
                return None
    
    def set_frequency(self, freq_mhz):
        """Set frequency, immediately read back and push to frontend."""
        freq_hz = int(freq_mhz * 1_000_000)
        bcd = _freq_to_bcd(freq_hz)
        resp = self._send_cat(self.CMD_FREQ_SET, bcd)
        if resp:
            confirmed = self.get_frequency()
            if confirmed:
                socketio.emit('radio_update', self.get_status())
    
    def get_frequency(self):
        """Read current frequency from radio."""
        resp = self._send_cat(self.CMD_FREQ_READ)
        if resp and len(resp) == 5:
            freq_hz = _bcd_to_freq(resp[:4])
            self.current_freq = freq_hz / 1_000_000
            return self.current_freq
        return self.current_freq
    
    def set_mode(self, mode):
        """Set mode (FM, NFM, AM, WFM)."""
        mode_data = self.MODE_CODES.get(mode, self.MODE_CODES['FM'])
        resp = self._send_cat(self.CMD_MODE_SET, mode_data)
        if resp:
            self.current_mode = mode
            logger.info(f"Yaesu mode set to {mode}")
    
    def set_ptt(self, active):
        """PTT via CAT command."""
        if active:
            self._send_cat(self.CMD_PTT_ON)
        else:
            self._send_cat(self.CMD_PTT_OFF)
        self.ptt_active = active
    
    def get_status(self):
        """Get full radio status."""
        status = {
            'connected': self.connected,
            'frequency': self.current_freq,
            'mode': self.current_mode,
            'ptt': self.ptt_active,
            'smeter': self.smeter_value,
            'rssi': self._smeter_to_rssi(),
            'volume': self.volume,
            'squelch': self.squelch,
            'tx_power': self.tx_power,
            'timestamp': datetime.now().isoformat()
        }
        return status
    
    def _monitor_loop(self):
        """Background thread polling Yaesu status."""
        while self._running:
            try:
                if self.connected:
                    # Only read S-meter, NOT frequency (updated via set readback)
                    resp = self._send_cat(self.CMD_GET_STATUS)
                    if resp and len(resp) == 5:
                        raw = resp[1]
                        self.smeter_value = self._raw_to_smeter(raw)
                    socketio.emit('radio_update', self.get_status())
                    
            except Exception as e:
                logger.error(f"Yaesu monitor error: {e}")
                eventlet.sleep(5.0)
    
    def _raw_to_smeter(self, raw):
        """Convert raw S-meter byte to S0-S9+60 scale."""
        if raw < 10:
            return 0
        elif raw < 30:
            return 1 + int((raw - 10) / 4)
        elif raw < 120:
            return 3 + int((raw - 30) / 15)
        elif raw < 200:
            return 9
        else:
            return min(12, 10 + int((raw - 200) / 27))
    
    def _smeter_to_rssi(self):
        """Convert S-meter value to approximate dBm."""
        smeter_db = {
            0: -127, 1: -121, 2: -115, 3: -109, 4: -103,
            5: -97, 6: -91, 7: -85, 8: -79, 9: -73,
            10: -63, 11: -53, 12: -43
        }
        return smeter_db.get(self.smeter_value, -127)


# ============================================================
# Xiegu X6100 - Icom CI-V compatible protocol
# USB-C provides both CAT serial + sound card
# ============================================================

class XieguX6100Radio(UVK5Radio):
    """Interface for Xiegu X6100 via CI-V protocol over USB serial."""
    
    BAUD_RATE = 19200
    CI_V_ADDR = 0xA4  # X6100 default CI-V address
    CONTROLLER_ADDR = 0x00  # Controller (us)
    
    # CI-V commands (Icom-compatible)
    CMD_FREQ_SET = 0x05    # Set frequency (5 bytes BCD, no subcmd)
    CMD_FREQ_READ = 0x03   # Read frequency (no subcmd)
    CMD_MODE_SET = 0x06    # Set mode
    CMD_MODE_READ = 0x04   # Read mode
    CMD_PTT_ON = 0x08     # PTT On (via 0x1C 0x00)
    CMD_PTT_OFF = 0x88    # PTT Off (via 0x1C 0x00)
    CMD_SMETER_READ = 0x15 # Read S-meter (subcmd 0x02)
    CMD_ID_READ = 0x19    # Read radio ID
    
    # Mode codes
    MODE_CODES = {
        'LSB': 0x00, 'USB': 0x01, 'AM': 0x02, 'CW': 0x03,
        'RTTY': 0x04, 'FM': 0x05, 'WFM': 0x06, 'NFM': 0x08,
        'CW-R': 0x07, 'RTTY-R': 0x09
    }
    MODE_NAMES = {v: k for k, v in MODE_CODES.items()}
    
    def __init__(self, port=None):
        super().__init__(port)
        self.current_mode = 'SSB'
        self._freq_set_at = 0  # timestamp of last freq set
    
    def _build_civ(self, cmd, subcmd=None, data=b''):
        """Build a CI-V frame: FE FE TO FROM CMD [SUBCMD] [DATA] FD"""
        frame = bytes([0xFE, 0xFE, self.CI_V_ADDR, self.CONTROLLER_ADDR, cmd])
        if subcmd is not None:
            frame += bytes([subcmd])
        frame += data + bytes([0xFD])
        return frame
    
    def _send_civ(self, cmd, subcmd=None, data=b'', expect_response=True):
        """Send CI-V command and optionally read response."""
        if not self.serial or not self.serial.is_open:
            return None
        frame = self._build_civ(cmd, subcmd, data)
        with self._lock:
            try:
                self.serial.reset_input_buffer()
                self.serial.write(frame)
                if not expect_response:
                    return None  # Fire and forget
                # Read response: FE [FE] FROM TO CMD [SUBCMD] [DATA] FD
                resp = b''
                deadline = time.time() + 1.0
                while time.time() < deadline:
                    chunk = self.serial.read(30)
                    if chunk:
                        resp += chunk
                        if b'\xFD' in resp:
                            break
                    else:
                        break
                if resp and b'\xFD' in resp:
                    end = resp.index(b'\xFD')
                    resp = resp[:end + 1]
                    # Skip preamble FE bytes
                    idx = 0
                    while idx < len(resp) and resp[idx] == 0xFE:
                        idx += 1
                    if idx < len(resp):
                        resp = resp[idx-1:]  # Keep one FE
                        resp = b'\xFE\xFE' + resp[1:]  # Normalize to 2 FE
                    if len(resp) >= 6 and resp[0] == 0xFE:
                        resp_cmd = resp[4] if len(resp) > 4 else 0
                        if resp_cmd == 0xFB:  # ACK/NACK
                            return resp
                        if resp_cmd == cmd:
                            if subcmd is not None and len(resp) > 5:
                                if resp[5] != subcmd:
                                    return None  # Wrong subcmd
                            return resp
                return None
            except serial.SerialException as e:
                logger.error(f"X6100 CI-V error: {e}")
                self.connected = False
                return None
    
    def connect(self, port=None):
        """Connect to X6100 via USB serial."""
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
                stopbits=serial.STOPBITS_ONE,
                timeout=2
            )
            self.connected = True
            logger.info(f"Connected to Xiegu X6100 on {self.port} at {self.BAUD_RATE} baud")
            
            # Verify radio ID
            resp = self._send_civ(self.CMD_ID_READ)
            if resp and len(resp) >= 7:
                radio_id = resp[5] if len(resp) > 5 else 0
                logger.info(f"X6100 radio ID: 0x{radio_id:02X}")
            
            # Read initial frequency
            self.get_frequency()
            logger.info(f"X6100 initial freq: {self.current_freq} MHz")
            
            self._running = True
            self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self._monitor_thread.start()
            return True
        except serial.SerialException as e:
            logger.error(f"Failed to connect to X6100: {e}")
            self.connected = False
            return False
    
    def set_frequency(self, freq_mhz):
        """Set frequency on radio (fire-and-forget, no blocking readback)."""
        freq_hz = int(freq_mhz * 1_000_000)
        bcd = self._freq_to_civ_bcd(freq_hz)
        self._freq_set_at = time.time()  # suppress monitor for 3s
        self.current_freq = freq_mhz
        # Send set command – no sleep, no readback, no blocking
        self._send_civ(self.CMD_FREQ_SET, data=bcd, expect_response=False)
    
    def _freq_to_civ_bcd(self, freq_hz):
        """Convert Hz to CI-V BCD (5 bytes, MSB first per X6100 manual Table 2-1).
        Byte 0: [10Hz][1Hz]
        Byte 1: [1kHz][100Hz]
        Byte 2: [100kHz][10kHz]
        Byte 3: [10MHz][1MHz]
        Byte 4: [1GHz][100MHz]
        """
        f = freq_hz
        hz1 = f % 10; f //= 10
        hz10 = f % 10; f //= 10
        hz100 = f % 10; f //= 10
        khz1 = f % 10; f //= 10
        khz10 = f % 10; f //= 10
        khz100 = f % 10; f //= 10
        mhz1 = f % 10; f //= 10
        mhz10 = f % 10; f //= 10
        mhz100 = f % 10; f //= 10
        ghz = f % 10
        return bytes([
            (hz10 << 4) | hz1,
            (khz1 << 4) | hz100,
            (khz100 << 4) | khz10,
            (mhz10 << 4) | mhz1,
            (ghz << 4) | mhz100
        ])
    
    def _civ_bcd_to_freq(self, bcd_bytes):
        """Convert CI-V BCD (5 bytes per X6100 manual Table 2-1) to Hz."""
        d = bcd_bytes
        return ((d[4] >> 4) & 0xF) * 1_000_000_000 + \
               (d[4] & 0xF) * 100_000_000 + \
               ((d[3] >> 4) & 0xF) * 10_000_000 + \
               (d[3] & 0xF) * 1_000_000 + \
               ((d[2] >> 4) & 0xF) * 100_000 + \
               (d[2] & 0xF) * 10_000 + \
               ((d[1] >> 4) & 0xF) * 1_000 + \
               (d[1] & 0xF) * 100 + \
               ((d[0] >> 4) & 0xF) * 10 + \
               (d[0] & 0xF)
    
    def get_frequency(self):
        """Read current frequency from X6100."""
        resp = self._send_civ(self.CMD_FREQ_READ)
        if resp:
            logger.info(f"X6100 freq response: {resp.hex(' ')} ({len(resp)} bytes)")
        if resp and len(resp) >= 10:
            # Response: FE FE FROM TO 03 [5 BCD] FD
            freq_hz = self._civ_bcd_to_freq(resp[5:10])
            self.current_freq = freq_hz / 1_000_000
            logger.info(f"X6100 freq decoded: {self.current_freq} MHz")
            return self.current_freq
        return self.current_freq
    
    def set_mode(self, mode):
        """Set operating mode."""
        mode_code = self.MODE_CODES.get(mode, 0x05)  # default FM
        resp = self._send_civ(self.CMD_MODE_SET, data=bytes([mode_code]))
        if resp:
            self.current_mode = mode
    
    def set_ptt(self, active):
        """PTT via CI-V (uses 0x1C 0x00 command)."""
        if active:
            self._send_civ(0x1C, subcmd=0x00, data=bytes([0x01]), expect_response=False)  # TX
        else:
            self._send_civ(0x1C, subcmd=0x00, data=bytes([0x02]), expect_response=False)  # RX
        self.ptt_active = active
    
    def get_status(self):
        """Get full radio status."""
        return {
            'connected': self.connected,
            'frequency': self.current_freq,
            'mode': self.current_mode,
            'ptt': self.ptt_active,
            'smeter': self.smeter_value,
            'rssi': self._smeter_to_rssi(),
            'volume': self.volume,
            'squelch': self.squelch,
            'tx_power': self.tx_power,
            'timestamp': datetime.now().isoformat()
        }
    
    def _monitor_loop(self):
        """Background thread polling X6100 status."""
        while self._running:
            try:
                if self.connected:
                    # Skip S-meter polling during freq changes to avoid serial collision
                    if time.time() - self._freq_set_at < 3.0:
                        eventlet.sleep(0.5)
                        continue
                    resp = self._send_civ(self.CMD_SMETER_READ, subcmd=0x02)
                    if resp and len(resp) >= 7:
                        raw = resp[5] if len(resp) > 5 else 0
                        self.smeter_value = self._raw_to_smeter(raw)
                    socketio.emit('radio_update', self.get_status())
                eventlet.sleep(2.0)
            except Exception as e:
                logger.error(f"X6100 monitor error: {e}")
                eventlet.sleep(5.0)
    
    def _raw_to_smeter(self, raw):
        """Convert raw S-meter to S0-S9+60."""
        if raw < 10: return 0
        elif raw < 30: return 1 + int((raw - 10) / 4)
        elif raw < 120: return 3 + int((raw - 30) / 15)
        elif raw < 200: return 9
        else: return min(12, 10 + int((raw - 200) / 27))
    
    def _smeter_to_rssi(self):
        """Convert S-meter to dBm."""
        db = {0:-127, 1:-121, 2:-115, 3:-109, 4:-103,
              5:-97, 6:-91, 7:-85, 8:-79, 9:-73, 10:-63, 11:-53, 12:-43}
        return db.get(self.smeter_value, -127)


register_radio('xiegu-x6100', 'Xiegu X6100', 'CI-V protocol, USB-C (CAT+Audio), 19200 baud', 19200, XieguX6100Radio)

app = Flask(__name__, 
            static_folder='../frontend/static',
            template_folder='../frontend/templates')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'uvk5-remote-dev-key')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # No static file cache
app.jinja_env.auto_reload = True
app.jinja_env.cache_size = 0  # Disable Jinja template bytecode cache

# Load credentials from .settings file
import hashlib
_settings_path = os.path.join(os.path.dirname(__file__), '..', '.settings')
_auth_users = {}  # {username: password_hash}

def _load_credentials():
    global _auth_users
    try:
        with open(_settings_path) as f:
            s = json.load(f)
        for u, p in s.get('users', {}).items():
            _auth_users[u] = p  # stored as sha256 hash
    except Exception:
        pass
    # Default user if none configured
    if not _auth_users:
        _auth_users['admin'] = hashlib.sha256(b'hamremote').hexdigest()
        logger.info('Auth: using default credentials (admin / hamremote)')
    else:
        logger.info(f'Auth: {len(_auth_users)} user(s) loaded')

_load_credentials()

def _check_auth(username, password):
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    return _auth_users.get(username) == pw_hash

def _is_logged_in():
    return session.get('logged_in')

@app.before_request
def _auth_check():
    # Allow static files and login route without auth
    if request.path.startswith('/static/') or request.path == '/login':
        return None
    if not _is_logged_in():
        return redirect('/login')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if _check_auth(username, password):
            session['logged_in'] = True
            session['username'] = username
            return redirect('/')
        return render_template('login.html', error='Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

# Register all radio drivers
register_radio('uvk5', 'Quansheng UV-K5', 'Via AIOC cable, 38400 baud', 38400, UVK5Radio)
register_radio('yaesu-ft', 'Yaesu FT-7800/8300', 'CAT protocol, 9600 baud, RS232', 9600, YaesuCATRadio)

# Global radio instance
radio = UVK5Radio()
current_radio_type = 'uvk5'

# Persist radio type across restarts
_RADIO_TYPE_FILE = os.path.join(os.path.dirname(__file__), '..', '.radio-type')
if os.path.exists(_RADIO_TYPE_FILE):
    try:
        with open(_RADIO_TYPE_FILE) as f:
            saved = f.read().strip()
            if saved in RADIO_DRIVERS:
                current_radio_type = saved
                radio = RADIO_DRIVERS[saved]['cls']()
                logger.info(f"Restored radio type: {RADIO_DRIVERS[saved]['label']}")
    except Exception:
        pass

# Simulated mode for development without hardware
SIMULATE = False  # Simulation removed - real hardware only

# Settings persistence
_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), '..', '.settings')


def _load_settings():
    """Load saved settings from file."""
    try:
        if os.path.exists(_SETTINGS_FILE):
            with open(_SETTINGS_FILE) as f:
                data = json.load(f)
                # Migration: flat format → per-radio format
                if 'port' in data and 'radios' not in data:
                    migrated = {'radios': {}}
                    for key in ['port', 'audio_playback', 'audio_capture']:
                        if key in data:
                            migrated[key] = data[key]
                    return migrated
                return data
    except Exception:
        pass
    return {}


def _load_radio_settings(radio_type):
    """Load settings for a specific radio type."""
    settings = _load_settings()
    radios = settings.get('radios', {})
    return radios.get(radio_type, {})


def _save_radio_settings(radio_type, data):
    """Save settings for a specific radio type."""
    try:
        current = _load_settings()
        if 'radios' not in current:
            current['radios'] = {}
        radio_cfg = current['radios'].get(radio_type, {})
        for key in ['port', 'audio_playback', 'audio_capture']:
            if key in data:
                radio_cfg[key] = data[key]
        current['radios'][radio_type] = radio_cfg
        with open(_SETTINGS_FILE, 'w') as f:
            json.dump(current, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save settings: {e}")


@app.route('/api/settings')
def api_get_settings():
    """Get saved settings for the current radio type."""
    radio_settings = _load_radio_settings(current_radio_type)
    radio_settings['radio_type'] = current_radio_type
    return jsonify(radio_settings)

# Active audio devices
audio_playback_dev = os.environ.get('AUDIO_PLAYBACK', None)
audio_capture_dev = os.environ.get('AUDIO_CAPTURE', None)

# Restore audio settings from per-radio config
_saved = _load_radio_settings(current_radio_type)
if not audio_playback_dev and _saved.get('audio_playback'):
    audio_playback_dev = _saved['audio_playback']
if not audio_capture_dev and _saved.get('audio_capture'):
    audio_capture_dev = _saved['audio_capture']


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
    
    # Persist
    try:
        with open(_RADIO_TYPE_FILE, 'w') as f:
            f.write(new_type)
    except Exception:
        pass
    
    logger.info(f"Switched radio type to {driver_info['label']}")
    saved = _load_radio_settings(new_type)
    return jsonify({'success': True, 'type': current_radio_type, 'label': driver_info['label'], 'settings': saved})


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    """Get current radio status as JSON."""
    return jsonify(radio.get_status())


@app.route('/api/connect', methods=['POST'])
def api_connect():
    """Connect to the radio."""
    data = request.json or {}
    port = data.get('port')
    
    success = radio.connect(port)
    
    # Save settings per radio type
    _save_radio_settings(current_radio_type, data)
    
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
    """Set volume / RX gain level."""
    data = request.json or {}
    level = data.get('level', 5)
    success = radio.set_volume(int(level))
    # Apply gain to audio stream manager
    audio_stream_manager.rx_gain = radio.rx_gain
    return jsonify({'success': success, 'volume': radio.volume, 'gain': round(radio.rx_gain, 2)})


@app.route('/api/power', methods=['POST'])
def api_power():
    """Set TX power."""
    data = request.json or {}
    power = data.get('power', 'LOW')
    success = radio.set_tx_power(power)
    return jsonify({'success': success, 'tx_power': radio.tx_power})


@app.route('/api/mode', methods=['POST'])
def api_mode():
    """Set modulation mode (FM, AM, USB)."""
    data = request.json or {}
    mode = data.get('mode', 'FM')
    success = radio.set_mode(mode)
    return jsonify({'success': success, 'mode': radio.current_mode})


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


@app.route('/api/audio/rx/start', methods=['POST'])
def api_audio_rx_start():
    """Start RX audio stream via REST (fallback if Socket.IO fails)."""
    from flask import request as req
    client_id = req.json.get('clientId', 'rest-client') if req.json else 'rest-client'
    logger.info(f"REST RX start from {client_id}")
    if audio_stream_manager:
        audio_stream_manager.start_rx_stream(client_id)
        return jsonify({'success': True, 'message': 'RX stream started'})
    return jsonify({'success': False, 'message': 'No stream manager'}), 500


@app.route('/api/audio/rx/stop', methods=['POST'])
def api_audio_rx_stop():
    """Stop RX audio stream via REST."""
    from flask import request as req
    client_id = req.json.get('clientId', 'rest-client') if req.json else 'rest-client'
    logger.info(f"REST RX stop from {client_id}")
    if audio_stream_manager:
        audio_stream_manager.stop_rx_stream(client_id)
        return jsonify({'success': True, 'message': 'RX stream stopped'})
    return jsonify({'success': False}), 500


# ============================================================
# Audio Stream Manager
# WebSocket + Opus, 16kHz Mono
# ============================================================

class AudioStreamManager:
    """Manages bidirectional audio streaming between browser and sound card.
    Uses Opus codec: 8kHz mono, ~12 kbit/s, 20ms frames."""
    
    SAMPLE_RATE = 8000
    CHANNELS = 1
    FRAME_SIZE = 160  # 20ms @ 8kHz = 160 samples (Opus only supports 2.5/5/10/20/40/60ms)
    FRAME_BYTES = 320  # 160 samples * 2 bytes (16-bit)
    # Send larger chunks to reduce WebSocket overhead
    CHUNK_FRAMES = 4  # 4 frames = 80ms per chunk
    CHUNK_BYTES = FRAME_BYTES * 4  # 1280 bytes per chunk
    
    def __init__(self):
        self.tx_active = False
        self.rx_active = False
        self.rx_clients = set()
        self._capture_process = None
        self._capture_thread = None
        self._running = False
        self.rx_gain = 1.0  # RX audio gain multiplier (0.0 - 2.0, default 1.0)
        
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
            if audio_playback_dev:
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
                        # Read 5 frames worth of PCM (5 x 20ms = 100ms) and encode each
                        pcm_chunk = self._capture_process.stdout.read(self.FRAME_BYTES * 5)
                        if len(pcm_chunk) >= self.FRAME_BYTES:
                            num_frames = len(pcm_chunk) // self.FRAME_BYTES
                            for i in range(num_frames):
                                pcm_frame = pcm_chunk[i * self.FRAME_BYTES:(i + 1) * self.FRAME_BYTES]
                                opus_data = self.opus_encoder.encode(pcm_frame, self.FRAME_SIZE)
                                encoded = base64.b64encode(opus_data).decode()
                                for sid in list(self.rx_clients):
                                    socketio.emit('audio_tx', {
                                        'data': encoded,
                                        'codec': 'opus',
                                        'sampleRate': self.SAMPLE_RATE
                                    }, room=sid)
                    
                    if self._capture_process.poll() is not None:
                        # Force-kill and wait to free the audio device
                        try:
                            self._capture_process.kill()
                        except OSError:
                            pass
                        self._capture_process.wait()
                        stderr = self._capture_process.stderr.read().decode()
                        if stderr.strip():
                            logger.warning(f"arecord stderr: {stderr.strip()}")
                        logger.warning("arecord process died (rc=%d), restarting...", self._capture_process.returncode)
                        self._capture_process = None
                        # Also kill any stray arecord processes on this device
                        subprocess.run(['pkill', '-9', '-f', f'arecord.*{device}'], 
                                      capture_output=True, timeout=2)
                        eventlet.sleep(2)  # Give ALSA time to release the device
                        
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


try:
    audio_stream_manager = AudioStreamManager()
except Exception as e:
    logger.warning(f"Audio: Opus not available, audio features disabled ({e})")
    audio_stream_manager = None


# WebSocket events for real-time updates

@socketio.on('connect')
def ws_connect():
    if not _is_logged_in():
        logger.warning(f"WebSocket connect rejected (not authenticated): {request.sid}")
        return False  # Reject connection
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


@socketio.on('radio_connect')
def ws_radio_connect(data):
    """Connect to radio via socket. Returns fast, init runs async."""
    port = data.get('port')
    audio_pb = data.get('audio_playback')
    audio_cap = data.get('audio_capture')

    # Apply audio config immediately
    global audio_playback_dev, audio_capture_dev
    if audio_pb:
        audio_playback_dev = audio_pb
    if audio_cap:
        audio_capture_dev = audio_cap

    # Save settings
    _save_radio_settings(current_radio_type, data or {})

    # Acknowledge immediately
    emit('radio_connecting', {'status': 'connecting'})

    # Run connect in background so we don't block
    def _do_connect():
        success = radio.connect(port)
        socketio.emit('radio_update', radio.get_status())
        if success:
            logger.info(f"Radio connected via socket: {radio.port}")
        else:
            logger.warning("Radio connect failed")
    eventlet.spawn(_do_connect)


@socketio.on('radio_disconnect')
def ws_radio_disconnect():
    """Disconnect from radio via socket."""
    radio.disconnect()
    socketio.emit('radio_update', radio.get_status())
    emit('radio_disconnected', {'success': True})


@socketio.on('set_frequency')
def ws_set_frequency(data):
    freq = data.get('frequency')
    if freq:
        radio.set_frequency(float(freq))
        emit('radio_update', radio.get_status(), broadcast=True)
        # Schedule async readback after 1s to confirm with radio
        def _readback():
            eventlet.sleep(1.0)
            confirmed = radio.get_frequency()
            if confirmed and abs(confirmed - float(freq)) > 0.001:
                # Radio disagrees – trust the radio
                socketio.emit('radio_update', radio.get_status())
        eventlet.spawn(_readback)


@socketio.on('set_mode')
def ws_set_mode(data):
    mode = data.get('mode', 'FM')
    radio.set_mode(mode)
    socketio.emit('radio_update', radio.get_status())


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
    logger.info(f"audio_start_rx received from {request.sid}")
    if audio_stream_manager:
        audio_stream_manager.start_rx_stream(request.sid)


@socketio.on('audio_stop_rx')
def ws_audio_stop_rx():
    """Browser stops receiving audio."""
    logger.info(f"audio_stop_rx received from {request.sid}")
    if audio_stream_manager:
        audio_stream_manager.stop_rx_stream(request.sid)


@socketio.on('disconnect')
def ws_disconnect():
    logger.info(f"WebSocket client disconnected: {request.sid}")
    # Stop test tone if running for this client
    _test_tone_clients.pop(request.sid, None)


# ============================================================
# Test Tone Generator (for audio pipeline debugging)
# Generates a clean 1kHz sine wave as raw PCM Int16 binary frames
# No Opus, no base64 – pure binary WebSocket for minimal latency
# ============================================================

_test_tone_clients = {}  # sid -> {'codec': 'pcm'|'opus'}
_test_tone_thread = None


def _test_tone_loop():
    """Generate 440Hz sine wave and stream via WebSocket (PCM or Opus per client)."""
    import struct
    import math
    import base64
    
    SAMPLE_RATE = 8000
    FREQ = 440  # 440 Hz (Kammerton A)
    FRAME_MS = 20  # 20ms per frame
    FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 160 samples
    AMPLITUDE = 16000  # ~50% of Int16 max to avoid clipping
    
    # Pre-generate one full period of the sine wave as lookup table
    period_samples = SAMPLE_RATE  # 1 second = exact multiple of any integer-Hz frequency
    lookup = struct.pack(f'<{period_samples}h', *[
        int(AMPLITUDE * math.sin(2.0 * math.pi * FREQ * i / SAMPLE_RATE))
        for i in range(period_samples)
    ])
    
    # Try to init Opus encoder
    opus_encoder = None
    try:
        import opuslib
        opus_encoder = opuslib.Encoder(SAMPLE_RATE, 1, opuslib.APPLICATION_VOIP)
        logger.info("Test tone: Opus encoder available")
    except Exception as e:
        logger.warning(f"Test tone: Opus not available ({e}), opus mode will fail")
    
    logger.info("Test tone: started (440Hz, 8kHz mono, 20ms frames, pre-computed)")
    
    pos = 0  # Position in lookup table (in bytes)
    frame_bytes = FRAME_SAMPLES * 2  # 320 bytes per frame (160 samples * 2)
    
    # Use wall-clock timing to avoid drift
    next_send = time.monotonic()
    
    while _test_tone_clients:
        # Extract frame from pre-computed lookup (wrap around)
        if pos + frame_bytes <= len(lookup):
            frame_pcm = lookup[pos:pos + frame_bytes]
            pos += frame_bytes
        else:
            # Wrap around
            remaining = len(lookup) - pos
            frame_pcm = lookup[pos:] + lookup[:frame_bytes - remaining]
            pos = frame_bytes - remaining
        
        # Encode Opus frame once (if any client needs it)
        opus_frame = None
        opus_clients = [sid for sid, cfg in list(_test_tone_clients.items()) if cfg.get('codec') == 'opus']
        if opus_clients and opus_encoder:
            try:
                opus_frame = opus_encoder.encode(frame_pcm, FRAME_SAMPLES)
            except Exception as e:
                logger.error(f"Test tone: Opus encode error: {e}")
        
        # Send to all clients in their requested format
        for sid, cfg in list(_test_tone_clients.items()):
            try:
                if cfg.get('codec') == 'opus':
                    if opus_frame:
                        encoded = base64.b64encode(opus_frame).decode()
                        socketio.emit('audio_tx', {
                            'data': encoded,
                            'codec': 'opus',
                            'sampleRate': SAMPLE_RATE
                        }, room=sid)
                    elif not opus_encoder:
                        # Fallback: tell client Opus unavailable, send PCM
                        socketio.emit('audio_tx', frame_pcm, room=sid)
                else:
                    # Raw PCM binary
                    socketio.emit('audio_tx', frame_pcm, room=sid)
            except Exception as e:
                logger.warning(f"Test tone: emit failed for {sid}: {e}")
                _test_tone_clients.pop(sid, None)
        
        # Wall-clock sleep to maintain steady 20ms frame rate
        next_send += FRAME_MS / 1000.0
        sleep_time = next_send - time.monotonic()
        if sleep_time > 0:
            eventlet.sleep(sleep_time)
        else:
            # We're behind schedule - skip sleep but don't accumulate debt beyond 100ms
            if sleep_time < -0.1:
                next_send = time.monotonic()
    
    logger.info("Test tone: stopped (no clients)")


@socketio.on('test_tone_start')
def ws_test_tone_start(data=None):
    """Start sending a test tone to this client."""
    global _test_tone_thread
    sid = request.sid
    codec = (data or {}).get('codec', 'pcm')
    logger.info(f"Test tone requested by {sid} (codec={codec})")
    _test_tone_clients[sid] = {'codec': codec}
    
    # Start generator thread if not running
    if _test_tone_thread is None or not _test_tone_thread:
        _test_tone_thread = eventlet.spawn(_test_tone_loop)


@socketio.on('test_tone_stop')
def ws_test_tone_stop():
    """Stop sending test tone to this client."""
    sid = request.sid
    _test_tone_clients.pop(sid, None)
    logger.info(f"Test tone stopped for {sid}")


# ============================================================
# Main
# ============================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    host = os.environ.get('HOST', '0.0.0.0')
    
    logger.info(f"Starting HAM Remote on {host}:{port}")
    logger.info(f"HAM Remote starting on 0.0.0.0:8080")
    
    socketio.run(app, host=host, port=port, debug=False)
