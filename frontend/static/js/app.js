/**
 * UV-K5 Remote - Frontend Application
 * Handles UI, WebSocket communication, and audio streaming
 */

class UVK5Remote {
    constructor() {
        this.socket = null;
        this.connected = false;
        this.pttActive = false;
        this.frequency = 145.500;
        this.squelch = 1;
        this.volume = 5;
        this.txPower = 'LOW';
        this.simulate = true;
        this.audioContext = null;
        this.mediaStream = null;
        this.audioProcessor = null;
        
        this.init();
    }
    
    init() {
        this.setupUI();
        this.setupFrequencyDigits();
        this.setupPTT();
        this.setupControls();
        this.setupSettings();
        this.connectWebSocket();
    }
    
    // ============================================================
    // WebSocket
    // ============================================================
    
    connectWebSocket() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.socket = io({
            transports: ['websocket'],
            reconnection: true,
            reconnectionDelay: 1000,
            reconnectionAttempts: 10
        });
        
        this.socket.on('connect', () => {
            console.log('WebSocket connected');
            this.updateConnectionStatus(true);
        });
        
        this.socket.on('disconnect', () => {
            console.log('WebSocket disconnected');
            this.updateConnectionStatus(false);
        });
        
        this.socket.on('radio_update', (data) => {
            this.updateFromRadio(data);
        });
        
        // Initial status fetch
        fetch('/api/status')
            .then(r => r.json())
            .then(data => this.updateFromRadio(data))
            .catch(err => console.error('Status fetch error:', err));
    }
    
    // ============================================================
    // UI Setup
    // ============================================================
    
    setupUI() {
        // Elements
        this.els = {
            freqDisplay: document.getElementById('freq-display'),
            freqLabel: document.getElementById('freq-label'),
            freqDigits: document.getElementById('freq-digits'),
            statusDot: document.getElementById('status-indicator'),
            statusText: document.getElementById('status-text'),
            pttButton: document.getElementById('ptt-button'),
            sqValue: document.getElementById('sq-value'),
            volValue: document.getElementById('vol-value'),
            powerToggle: document.getElementById('power-toggle'),
            settingsToggle: document.getElementById('settings-toggle'),
            settingsPanel: document.getElementById('settings-panel'),
            portSelect: document.getElementById('port-select'),
            simulateToggle: document.getElementById('simulate-toggle'),
            btnConnect: document.getElementById('btn-connect'),
            btnDisconnect: document.getElementById('btn-disconnect'),
            btnRefreshPorts: document.getElementById('btn-refresh-ports'),
        };
    }
    
    setupFrequencyDigits() {
        const digitsContainer = this.els.freqDigits;
        // Format: 1 4 5 . 5 0 0
        const positions = [
            { type: 'digit', index: 0 },  // 1
            { type: 'digit', index: 1 },  // 4
            { type: 'digit', index: 2 },  // 5
            { type: 'separator' },
            { type: 'digit', index: 3 },  // 5
            { type: 'digit', index: 4 },  // 0
            { type: 'digit', index: 5 },  // 0
        ];
        
        digitsContainer.innerHTML = '';
        
        positions.forEach((pos, i) => {
            if (pos.type === 'separator') {
                const sep = document.createElement('div');
                sep.className = 'freq-digit separator';
                sep.textContent = '.';
                digitsContainer.appendChild(sep);
                return;
            }
            
            const digitEl = document.createElement('div');
            digitEl.className = 'freq-digit';
            digitEl.dataset.index = pos.index;
            
            const upBtn = document.createElement('button');
            upBtn.className = 'digit-up';
            upBtn.textContent = '▲';
            upBtn.addEventListener('click', () => this.adjustDigit(pos.index, 1));
            
            const val = document.createElement('div');
            val.className = 'digit-value';
            val.id = `digit-${pos.index}`;
            
            const downBtn = document.createElement('button');
            downBtn.className = 'digit-down';
            downBtn.textContent = '▼';
            downBtn.addEventListener('click', () => this.adjustDigit(pos.index, -1));
            
            digitEl.appendChild(upBtn);
            digitEl.appendChild(val);
            digitEl.appendChild(downBtn);
            digitsContainer.appendChild(digitEl);
        });
        
        this.updateFrequencyDisplay();
        
        // Quick frequency buttons
        document.querySelectorAll('.btn-freq').forEach(btn => {
            btn.addEventListener('click', () => {
                const freq = parseFloat(btn.dataset.freq);
                this.setFrequency(freq);
            });
        });
    }
    
    setupPTT() {
        const pttBtn = this.els.pttButton;
        
        // Touch events for mobile (hold-to-talk)
        pttBtn.addEventListener('touchstart', (e) => {
            e.preventDefault();
            this.pttOn();
        });
        
        pttBtn.addEventListener('touchend', (e) => {
            e.preventDefault();
            this.pttOff();
        });
        
        pttBtn.addEventListener('touchcancel', (e) => {
            e.preventDefault();
            this.pttOff();
        });
        
        // Mouse events for desktop
        pttBtn.addEventListener('mousedown', (e) => {
            e.preventDefault();
            this.pttOn();
        });
        
        pttBtn.addEventListener('mouseup', (e) => {
            e.preventDefault();
            this.pttOff();
        });
        
        pttBtn.addEventListener('mouseleave', () => {
            if (this.pttActive) this.pttOff();
        });
        
        // Keyboard shortcut (spacebar)
        document.addEventListener('keydown', (e) => {
            if (e.code === 'Space' && !e.repeat) {
                e.preventDefault();
                this.pttOn();
            }
        });
        
        document.addEventListener('keyup', (e) => {
            if (e.code === 'Space') {
                e.preventDefault();
                this.pttOff();
            }
        });
    }
    
    setupControls() {
        // Squelch
        document.getElementById('sq-down').addEventListener('click', () => {
            this.setSquelch(Math.max(0, this.squelch - 1));
        });
        document.getElementById('sq-up').addEventListener('click', () => {
            this.setSquelch(Math.min(9, this.squelch + 1));
        });
        
        // Volume
        document.getElementById('vol-down').addEventListener('click', () => {
            this.setVolume(Math.max(0, this.volume - 1));
        });
        document.getElementById('vol-up').addEventListener('click', () => {
            this.setVolume(Math.min(15, this.volume + 1));
        });
        
        // TX Power toggle
        this.els.powerToggle.addEventListener('click', () => {
            this.setTxPower(this.txPower === 'LOW' ? 'HIGH' : 'LOW');
        });
    }
    
    setupSettings() {
        // Toggle settings panel
        this.els.settingsToggle.addEventListener('click', () => {
            const panel = this.els.settingsPanel;
            panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
        });
        
        // Refresh ports
        this.els.btnRefreshPorts.addEventListener('click', () => this.refreshPorts());
        
        // Connect
        this.els.btnConnect.addEventListener('click', () => this.connectRadio());
        
        // Disconnect
        this.els.btnDisconnect.addEventListener('click', () => this.disconnectRadio());
        
        // Load ports on open
        this.refreshPorts();
    }
    
    // ============================================================
    // Radio Actions
    // ============================================================
    
    setFrequency(freq) {
        // Clamp to 2m band
        freq = Math.max(144.0, Math.min(146.0, freq));
        freq = Math.round(freq * 1000) / 1000; // 3 decimal places
        
        this.frequency = freq;
        this.updateFrequencyDisplay();
        
        fetch('/api/frequency', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ frequency: freq })
        }).catch(err => console.error('Set frequency error:', err));
    }
    
    adjustDigit(index, delta) {
        const freqStr = this.frequency.toFixed(3);
        const digits = freqStr.replace('.', '').split('').map(Number);
        
        digits[index] = (digits[index] + delta + 10) % 10;
        
        // Reconstruct frequency: digits are 1 4 5 5 0 0 → 145.500
        const newFreq = parseInt(digits.join('')) / 1000;
        
        // Validate band limits
        if (newFreq >= 144.0 && newFreq <= 146.0) {
            this.setFrequency(newFreq);
        }
    }
    
    updateFrequencyDisplay() {
        const freqStr = this.frequency.toFixed(3);
        this.els.freqDisplay.textContent = freqStr;
        
        const digits = freqStr.replace('.', '').split('');
        digits.forEach((d, i) => {
            const el = document.getElementById(`digit-${i}`);
            if (el) el.textContent = d;
        });
    }
    
    pttOn() {
        if (this.pttActive) return;
        this.pttActive = true;
        this.els.pttButton.classList.add('active');
        
        this.socket.emit('ptt_press');
        this.startMicrophone();
    }
    
    pttOff() {
        if (!this.pttActive) return;
        this.pttActive = false;
        this.els.pttButton.classList.remove('active');
        
        this.socket.emit('ptt_release');
        this.stopMicrophone();
    }
    
    setSquelch(level) {
        this.squelch = level;
        this.els.sqValue.textContent = level;
        
        fetch('/api/squelch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ level })
        }).catch(err => console.error('Set squelch error:', err));
    }
    
    setVolume(level) {
        this.volume = level;
        this.els.volValue.textContent = level;
        
        fetch('/api/volume', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ level })
        }).catch(err => console.error('Set volume error:', err));
    }
    
    setTxPower(power) {
        this.txPower = power;
        this.els.powerToggle.textContent = power;
        this.els.powerToggle.className = 'btn-toggle' + (power === 'HIGH' ? ' high' : '');
        
        fetch('/api/power', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ power })
        }).catch(err => console.error('Set power error:', err));
    }
    
    // ============================================================
    // Audio (Microphone for TX)
    // ============================================================
    
    async startMicrophone() {
        try {
            this.mediaStream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true
                }
            });
            console.log('Microphone activated');
            // TODO: Stream audio data to server via WebSocket
        } catch (err) {
            console.error('Microphone access denied:', err);
        }
    }
    
    stopMicrophone() {
        if (this.mediaStream) {
            this.mediaStream.getTracks().forEach(t => t.stop());
            this.mediaStream = null;
        }
    }
    
    // ============================================================
    // Connection Management
    // ============================================================
    
    async refreshPorts() {
        try {
            const resp = await fetch('/api/ports');
            const ports = await resp.json();
            
            this.els.portSelect.innerHTML = '<option value="">Auto-detect</option>';
            ports.forEach(p => {
                const opt = document.createElement('option');
                opt.value = p.device;
                opt.textContent = `${p.device} - ${p.description}`;
                this.els.portSelect.appendChild(opt);
            });
        } catch (err) {
            console.error('Refresh ports error:', err);
        }
    }
    
    async connectRadio() {
        const port = this.els.portSelect.value || undefined;
        
        try {
            const resp = await fetch('/api/connect', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ port })
            });
            const data = await resp.json();
            
            if (data.success) {
                this.updateConnectionStatus(true);
                console.log('Radio connected' + (data.simulated ? ' (simulated)' : ''));
            }
        } catch (err) {
            console.error('Connect error:', err);
        }
    }
    
    async disconnectRadio() {
        try {
            await fetch('/api/disconnect', { method: 'POST' });
            this.updateConnectionStatus(false);
        } catch (err) {
            console.error('Disconnect error:', err);
        }
    }
    
    // ============================================================
    // UI Updates
    // ============================================================
    
    updateConnectionStatus(connected) {
        this.connected = connected;
        this.els.statusDot.className = 'status-dot ' + (connected ? 'connected' : 'disconnected');
        this.els.statusText.textContent = connected ? 'Online' : 'Offline';
    }
    
    updateFromRadio(data) {
        if (data.frequency !== undefined) {
            this.frequency = data.frequency;
            this.updateFrequencyDisplay();
        }
        if (data.squelch !== undefined) {
            this.squelch = data.squelch;
            this.els.sqValue.textContent = data.squelch;
        }
        if (data.volume !== undefined) {
            this.volume = data.volume;
            this.els.volValue.textContent = data.volume;
        }
        if (data.tx_power !== undefined) {
            this.txPower = data.tx_power;
            this.els.powerToggle.textContent = data.tx_power;
            this.els.powerToggle.className = 'btn-toggle' + (data.tx_power === 'HIGH' ? ' high' : '');
        }
        if (data.connected !== undefined) {
            this.updateConnectionStatus(data.connected || data.simulated);
        }
    }
}

// Initialize on load
document.addEventListener('DOMContentLoaded', () => {
    window.uvk5 = new UVK5Remote();
});
