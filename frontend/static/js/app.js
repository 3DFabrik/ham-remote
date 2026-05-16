/**
 * HAM Remote - Frontend Application
 * Handles UI, WebSocket communication, audio streaming, and level meters
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
        this.pttHotkey = localStorage.getItem('ptt_hotkey') || 'Space';
        
        // Level meter state
        this.txPeakHold = 0;
        this.rxPeakHold = 0;
        this.txClipTimeout = null;
        this.rxClipTimeout = null;
        
        this.init();
    }
    
    init() {
        this.setupUI();
        this.setupFrequencyDigits();
        this.setupPTT();
        this.setupControls();
        this.setupSettings();
        this.setupLevelMeters();
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
        
        this.socket.on('audio_tx', (data) => {
            // Incoming audio from radio RX
            if (data.data) {
                this.processRxAudio(data);
            }
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
            portSelect: document.getElementById('port-select'),
            radioTypeSelect: document.getElementById('radio-type'),
            simulateToggle: document.getElementById('simulate-toggle'),
            btnConnect: document.getElementById('btn-connect'),
            btnDisconnect: document.getElementById('btn-disconnect'),
            btnRefreshPorts: document.getElementById('btn-refresh-ports'),
            // Level bars
            smeterFill: document.getElementById('smeter-bar-fill'),
            smeterDb: document.getElementById('smeter-db'),
            smeterClip: document.getElementById('smeter-clip'),
            rxBarFill: document.getElementById('rx-bar-fill'),
            rxDb: document.getElementById('rx-db'),
            rxClip: document.getElementById('rx-clip'),
            txBarFill: document.getElementById('tx-bar-fill'),
            txDb: document.getElementById('tx-db'),
            txClip: document.getElementById('tx-clip'),
        };
    }
    
    // ============================================================
    // Level Meters
    // ============================================================
    
    setupLevelMeters() {
        this.rxAnalyser = null;
        this.txAnalyser = null;
        this.rxDataArray = null;
        this.txDataArray = null;
        
        // Mic is only activated on PTT press (user gesture)
        // TX level bar shows activity only while transmitting
        
        // Start the level meter animation loop
        this._levelMeterLoop();
    }
    
    _levelMeterLoop() {
        // Update TX level (from mic analyser)
        if (this.txAnalyser && this.txDataArray) {
            this.txAnalyser.getByteTimeDomainData(this.txDataArray);
            const peak = this._getPeakFromData(this.txDataArray);
            const db = this._amplitudeToDb(peak);
            this._updateBar('tx', db, peak);
        }
        
        // Update RX level (from received audio analyser)
        if (this.rxAnalyser && this.rxDataArray) {
            this.rxAnalyser.getByteTimeDomainData(this.rxDataArray);
            const peak = this._getPeakFromData(this.rxDataArray);
            const db = this._amplitudeToDb(peak);
            this._updateBar('rx', db, peak);
        }
        
        requestAnimationFrame(() => this._levelMeterLoop());
    }
    
    _getPeakFromData(dataArray) {
        let max = 0;
        for (let i = 0; i < dataArray.length; i++) {
            const v = Math.abs(dataArray[i] - 128) / 128;
            if (v > max) max = v;
        }
        return max;
    }
    
    _amplitudeToDb(amplitude) {
        if (amplitude <= 0) return -Infinity;
        return 20 * Math.log10(amplitude);
    }
    
    _updateBar(channel, db, linearPeak) {
        const fillEl = this.els[channel + 'BarFill'];
        const dbEl = this.els[channel + 'Db'];
        const clipEl = this.els[channel + 'Clip'];
        
        if (!fillEl) return;
        
        // Map dB to percentage: -60dB = 0%, 0dB = 100% (horizontal width)
        const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
        fillEl.style.width = pct + '%';
        
        // Display text
        if (db === -Infinity) {
            dbEl.textContent = '-∞ dB';
            dbEl.className = 'level-bar-value';
        } else {
            dbEl.textContent = db.toFixed(1) + ' dB';
            dbEl.className = 'level-bar-value';
        }
        
        // Clipping detection (above -1 dBFS or > 0.89 linear)
        const isClipping = linearPeak > 0.89;
        
        if (isClipping) {
            clipEl.classList.add('clipping');
            dbEl.classList.add('clip-warn');
            dbEl.textContent = 'CLIP!';
            
            // Auto-clear clip indicator after 1.5s
            if (channel === 'tx' && this.txClipTimeout) clearTimeout(this.txClipTimeout);
            if (channel === 'rx' && this.rxClipTimeout) clearTimeout(this.rxClipTimeout);
            
            const timeout = setTimeout(() => {
                clipEl.classList.remove('clipping');
            }, 1500);
            
            if (channel === 'tx') this.txClipTimeout = timeout;
            if (channel === 'rx') this.rxClipTimeout = timeout;
        }
    }
    
    _createAnalyser(source) {
        const analyser = this.audioContext.createAnalyser();
        analyser.fftSize = 256;
        analyser.smoothingTimeConstant = 0.5;
        source.connect(analyser);
        // Don't connect analyser to destination - it's just for measurement
        return analyser;
    }
    
    // ============================================================
    // Frequency
    // ============================================================
    
    setupFrequencyDigits() {
        const digitsContainer = this.els.freqDigits;
        const positions = [
            { type: 'digit', index: 0 },
            { type: 'digit', index: 1 },
            { type: 'digit', index: 2 },
            { type: 'separator' },
            { type: 'digit', index: 3 },
            { type: 'digit', index: 4 },
            { type: 'digit', index: 5 },
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
        
        document.querySelectorAll('.btn-freq').forEach(btn => {
            btn.addEventListener('click', () => {
                const freq = parseFloat(btn.dataset.freq);
                this.setFrequency(freq);
            });
        });
    }
    
    setFrequency(freq) {
        freq = Math.max(144.0, Math.min(146.0, freq));
        freq = Math.round(freq * 1000) / 1000;
        
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
        
        const newFreq = parseInt(digits.join('')) / 1000;
        
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
    
    // ============================================================
    // PTT
    // ============================================================
    
    setupPTT() {
        const pttBtn = this.els.pttButton;
        
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
        
        document.addEventListener('keydown', (e) => {
            if (e.code === this.pttHotkey && !e.repeat) {
                e.preventDefault();
                this.pttOn();
            }
        });
        
        document.addEventListener('keyup', (e) => {
            if (e.code === this.pttHotkey) {
                e.preventDefault();
                this.pttOff();
            }
        });
    }
    
    pttOn() {
        if (this.pttActive) return;
        this.pttActive = true;
        this.els.pttButton.classList.add('active');
        console.log('[PTT] PTT pressed – starting mic...');
        
        this.socket.emit('audio_stop_rx');
        this.socket.emit('ptt_press');
        this.startMicrophone();
    }
    
    pttOff() {
        if (!this.pttActive) return;
        this.pttActive = false;
        this.els.pttButton.classList.remove('active');
        
        this.socket.emit('ptt_release');
        this.stopMicrophone();
        this.socket.emit('audio_start_rx');
    }
    
    // ============================================================
    // Controls
    // ============================================================
    
    setupControls() {
        document.getElementById('sq-down').addEventListener('click', () => {
            this.setSquelch(Math.max(0, this.squelch - 1));
        });
        document.getElementById('sq-up').addEventListener('click', () => {
            this.setSquelch(Math.min(9, this.squelch + 1));
        });
        
        document.getElementById('vol-down').addEventListener('click', () => {
            this.setVolume(Math.max(0, this.volume - 1));
        });
        document.getElementById('vol-up').addEventListener('click', () => {
            this.setVolume(Math.min(15, this.volume + 1));
        });
        
        this.els.powerToggle.addEventListener('click', () => {
            this.setTxPower(this.txPower === 'LOW' ? 'HIGH' : 'LOW');
        });
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
    // Audio (Microphone TX + Speaker RX with Level Analysis)
    // ============================================================
    
    async startMicrophone() {
        try {
            console.log('[MIC] startMicrophone called');
            
            if (!this.audioContext) {
                this.audioContext = new (window.AudioContext || window.webkitAudioContext)({
                    sampleRate: 16000
                });
                console.log('[MIC] AudioContext created, state:', this.audioContext.state);
            }
            
            // Resume AudioContext if suspended (browser autoplay policy)
            if (this.audioContext.state === 'suspended') {
                console.log('[MIC] Resuming suspended AudioContext...');
                await this.audioContext.resume();
                console.log('[MIC] AudioContext state now:', this.audioContext.state);
            }
            
            console.log('[MIC] Requesting getUserMedia...');
            // Open mic fresh on each PTT press
            this.micStream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                    channelCount: 1,
                    sampleRate: 16000
                }
            });
            console.log('[MIC] Got mic stream:', this.micStream.getTracks().length, 'tracks');
            
            // Set up TX analyser for level monitoring
            const micSource = this.audioContext.createMediaStreamSource(this.micStream);
            this.txAnalyser = this.audioContext.createAnalyser();
            this.txAnalyser.fftSize = 256;
            this.txAnalyser.smoothingTimeConstant = 0.5;
            this.txDataArray = new Uint8Array(this.txAnalyser.frequencyBinCount);
            micSource.connect(this.txAnalyser);
            
            // Use MediaRecorder with Opus codec
            const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                ? 'audio/webm;codecs=opus'
                : 'audio/ogg;codecs=opus';
            
            this.mediaRecorder = new MediaRecorder(this.micStream, {
                mimeType: mimeType,
                audioBitsPerSecond: 24000
            });
            
            this.mediaRecorder.ondataavailable = (e) => {
                if (e.data.size > 0 && this.pttActive) {
                    const reader = new FileReader();
                    reader.onload = () => {
                        const base64 = reader.result.split(',')[1];
                        this.socket.emit('audio_rx', {
                            data: base64,
                            codec: 'opus-webm',
                            sampleRate: 16000
                        });
                    };
                    reader.readAsDataURL(e.data);
                }
            };
            
            this.mediaRecorder.start(20);
            console.log('Microphone streaming started (Opus, 24kbit/s)');
            
        } catch (err) {
            console.error('Microphone access denied:', err);
        }
    }
    
    stopMicrophone() {
        // Stop MediaRecorder
        if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
            this.mediaRecorder.stop();
            this.mediaRecorder = null;
        }
        // Stop mic stream completely
        if (this.micStream) {
            this.micStream.getTracks().forEach(t => t.stop());
            this.micStream = null;
        }
        // Clear TX analyser
        this.txAnalyser = null;
        this.txDataArray = null;
        
        // Reset TX bar
        if (this.els.txBarFill) this.els.txBarFill.style.width = '0%';
        if (this.els.txDb) {
            this.els.txDb.textContent = '-∞ dB';
            this.els.txDb.className = 'level-bar-value';
        }
        if (this.els.txClip) this.els.txClip.classList.remove('clipping');
    }
    
    processRxAudio(data) {
        if (!this.audioContext) {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)({
                sampleRate: 16000
            });
        }
        
        try {
            let float32Array = null;
            
            if (data.codec === 'opus') {
                const binary = atob(data.data);
                const bytes = new Uint8Array(binary.length);
                for (let i = 0; i < binary.length; i++) {
                    bytes[i] = binary.charCodeAt(i);
                }
                
                if (this.opusDecoder) {
                    this.opusDecoder.decodeFrame(bytes).then(pcm => {
                        this._playAndAnalyzeRx(pcm);
                    });
                    return;
                }
            } else if (data.codec === 'pcm') {
                const binary = atob(data.data);
                const bytes = new Uint8Array(binary.length);
                for (let i = 0; i < binary.length; i++) {
                    bytes[i] = binary.charCodeAt(i);
                }
                const int16 = new Int16Array(bytes.buffer);
                const float32 = new Float32Array(int16.length);
                for (let i = 0; i < int16.length; i++) {
                    float32[i] = int16[i] / 0x8000;
                }
                float32Array = float32;
            }
            
            if (float32Array) {
                this._playAndAnalyzeRx(float32Array);
            }
        } catch (err) {
            console.error('RX audio playback error:', err);
        }
    }
    
    _playAndAnalyzeRx(float32Array) {
        if (!this.audioContext) return;
        
        const buffer = this.audioContext.createBuffer(1, float32Array.length, 16000);
        buffer.copyToChannel(float32Array, 0);
        
        const source = this.audioContext.createBufferSource();
        source.buffer = buffer;
        
        // Create or reuse RX analyser
        if (!this.rxAnalyser) {
            this.rxAnalyser = this.audioContext.createAnalyser();
            this.rxAnalyser.fftSize = 256;
            this.rxAnalyser.smoothingTimeConstant = 0.5;
            this.rxDataArray = new Uint8Array(this.rxAnalyser.frequencyBinCount);
            this.rxAnalyser.connect(this.audioContext.destination);
        }
        
        source.connect(this.rxAnalyser);
        source.start();
    }
    
    // ============================================================
    // Connection Management
    // ============================================================
    
    setupSettings() {
        this.els.btnRefreshPorts.addEventListener('click', () => this.refreshPorts());
        this.els.btnConnect.addEventListener('click', () => this.connectRadio());
        this.els.btnDisconnect.addEventListener('click', () => this.disconnectRadio());
        
        this.els.audioPlayback = document.getElementById('audio-playback');
        this.els.audioCapture = document.getElementById('audio-capture');
        this.els.btnRefreshAudio = document.getElementById('btn-refresh-audio');
        
        this.els.btnRefreshAudio.addEventListener('click', () => this.refreshAudioDevices());
        this.els.audioPlayback.addEventListener('change', () => this.setAudioConfig());
        this.els.audioCapture.addEventListener('change', () => this.setAudioConfig());
        
        this.els.radioTypeSelect.addEventListener('change', () => this.setRadioType());
        
        // Tabs
        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                tab.classList.add('active');
                document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
            });
        });
        
        // PTT Hotkey
        this.els.hotkeyBtn = document.getElementById('ptt-hotkey-btn');
        this.els.hotkeyHint = document.getElementById('hotkey-hint');
        this.els.hotkeyBtn.textContent = this.pttHotkey;
        
        this.els.hotkeyBtn.addEventListener('click', () => this.startHotkeyListen());
        
        // Load ports, audio devices, and radio types on open
        this.refreshPorts();
        this.refreshAudioDevices();
        this.refreshRadioTypes();
        
        // Audio streaming state
        this.micStream = null;
        this.mediaRecorder = null;
        this.opusDecoder = null;
        this.rxAudioQueue = [];
        this.rxPlaying = false;
        
        this._initOpusDecoder();
        
        // DEBUG: Mic test button
        const micTestBtn = document.getElementById('btn-mic-test');
        const micTestResult = document.getElementById('mic-test-result');
        if (micTestBtn) {
            micTestBtn.addEventListener('click', async () => {
                micTestResult.textContent = 'Testing...';
                micTestResult.style.color = '#ffaa00';
                try {
                    console.log('[MIC-TEST] isSecureContext:', window.isSecureContext);
                    console.log('[MIC-TEST] mediaDevices:', !!navigator.mediaDevices);
                    console.log('[MIC-TEST] getUserMedia:', !!navigator.mediaDevices?.getUserMedia);
                    
                    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                        micTestResult.textContent = 'FAIL: getUserMedia not available';
                        micTestResult.style.color = '#f44';
                        return;
                    }
                    
                    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                    console.log('[MIC-TEST] Got stream:', stream.getTracks().length, 'tracks');
                    stream.getTracks().forEach(t => {
                        console.log('[MIC-TEST] Track:', t.label, t.readyState, t.kind);
                    });
                    
                    // Also test AudioContext
                    const ctx = new (window.AudioContext || window.webkitAudioContext)();
                    const source = ctx.createMediaStreamSource(stream);
                    const analyser = ctx.createAnalyser();
                    analyser.fftSize = 256;
                    source.connect(analyser);
                    const data = new Uint8Array(analyser.frequencyBinCount);
                    
                    // Read a few samples
                    let maxLevel = 0;
                    for (let i = 0; i < 10; i++) {
                        await new Promise(r => setTimeout(r, 100));
                        analyser.getByteTimeDomainData(data);
                        for (let j = 0; j < data.length; j++) {
                            const v = Math.abs(data[j] - 128) / 128;
                            if (v > maxLevel) maxLevel = v;
                        }
                    }
                    
                    const db = maxLevel > 0 ? (20 * Math.log10(maxLevel)).toFixed(1) : '-∞';
                    micTestResult.textContent = `OK! ${stream.getTracks()[0].label} | Peak: ${db} dB | Sprech mal rein!`;
                    micTestResult.style.color = '#0f0';
                    
                    // Clean up test
                    stream.getTracks().forEach(t => t.stop());
                    ctx.close();
                } catch (err) {
                    console.error('[MIC-TEST] Error:', err);
                    micTestResult.textContent = `ERROR: ${err.name}: ${err.message}`;
                    micTestResult.style.color = '#f44';
                }
            });
        }
    }
    
    async _initOpusDecoder() {
        try {
            if (typeof OpusDecoder !== 'undefined') {
                this.opusDecoder = new OpusDecoder();
                await this.opusDecoder.ready;
                console.log('Opus decoder initialized');
            } else {
                console.warn('Opus decoder library not loaded, RX will use PCM fallback');
            }
        } catch (err) {
            console.warn('Opus decoder init failed:', err);
        }
    }
    
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
                this.socket.emit('audio_start_rx');
            }
        } catch (err) {
            console.error('Connect error:', err);
        }
    }
    
    async disconnectRadio() {
        try {
            await fetch('/api/disconnect', { method: 'POST' });
            this.socket.emit('audio_stop_rx');
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
        if (data.smeter !== undefined) {
            this.updateSMeter(data.smeter);
        }
    }
    
    // ============================================================
    // S-Meter (as upward bar, unified with level bars)
    // ============================================================
    
    updateSMeter(value) {
        // value: 0-9 = S0-S9, 10-12 = +20/+40/+60
        // Map to percentage: S0=0%, S9=56%, +60=100%
        const pct = Math.min(100, (value / 12) * 100);
        this.els.smeterFill.style.width = pct + '%';
        
        let text;
        if (value === 0) {
            text = 'S0';
        } else if (value <= 9) {
            text = 'S' + value;
        } else {
            const db = (value - 9) * 20;
            text = 'S9+' + db;
        }
        this.els.smeterDb.textContent = text;
    }
    
    // ============================================================
    // Audio Device Management
    // ============================================================
    
    async refreshAudioDevices() {
        try {
            const resp = await fetch('/api/audio/devices');
            const devices = await resp.json();
            
            this.els.audioPlayback.innerHTML = '<option value="">Default</option>';
            (devices.playback || []).forEach(dev => {
                const opt = document.createElement('option');
                opt.value = dev.id;
                opt.textContent = dev.name + ' - ' + dev.detail;
                this.els.audioPlayback.appendChild(opt);
            });
            
            this.els.audioCapture.innerHTML = '<option value="">Default</option>';
            (devices.capture || []).forEach(dev => {
                const opt = document.createElement('option');
                opt.value = dev.id;
                opt.textContent = dev.name + ' - ' + dev.detail;
                this.els.audioCapture.appendChild(opt);
            });
            
            const configResp = await fetch('/api/audio/config');
            const config = await configResp.json();
            if (config.playback) this.els.audioPlayback.value = config.playback;
            if (config.capture) this.els.audioCapture.value = config.capture;
            
        } catch (err) {
            console.error('Audio devices error:', err);
        }
    }
    
    async setAudioConfig() {
        const playback = this.els.audioPlayback.value;
        const capture = this.els.audioCapture.value;
        
        try {
            await fetch('/api/audio/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ playback, capture })
            });
            console.log('Audio config saved');
        } catch (err) {
            console.error('Set audio config error:', err);
        }
    }
    
    // ============================================================
    // Radio Type Management
    // ============================================================
    
    async refreshRadioTypes() {
        try {
            const resp = await fetch('/api/radio-types');
            const types = await resp.json();
            
            this.els.radioTypeSelect.innerHTML = '';
            types.forEach(t => {
                const opt = document.createElement('option');
                opt.value = t.id;
                opt.textContent = t.label;
                opt.title = t.description;
                this.els.radioTypeSelect.appendChild(opt);
            });
            
            const currentResp = await fetch('/api/radio-type');
            const current = await currentResp.json();
            if (current.type) this.els.radioTypeSelect.value = current.type;
            
        } catch (err) {
            console.error('Radio types error:', err);
        }
    }
    
    async setRadioType() {
        const type = this.els.radioTypeSelect.value;
        try {
            const resp = await fetch('/api/radio-type', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ type })
            });
            const result = await resp.json();
            if (result.success) {
                console.log(`Switched to ${result.label}`);
                this.refreshPorts();
            }
        } catch (err) {
            console.error('Set radio type error:', err);
        }
    }
    
    // ============================================================
    // PTT Hotkey
    // ============================================================
    
    startHotkeyListen() {
        this.els.hotkeyBtn.textContent = '...';
        this.els.hotkeyBtn.classList.add('listening');
        this.els.hotkeyHint.textContent = 'Jetzt Taste drücken';
        
        const handler = (e) => {
            e.preventDefault();
            e.stopPropagation();
            
            if (['ShiftLeft', 'ShiftRight', 'ControlLeft', 'ControlRight', 'AltLeft', 'AltRight', 'MetaLeft', 'MetaRight'].includes(e.code)) {
                return;
            }
            
            this.pttHotkey = e.code;
            localStorage.setItem('ptt_hotkey', e.code);
            
            const name = e.code
                .replace('Key', '')
                .replace('Digit', '')
                .replace('Numpad', 'Num ')
                .replace('Space', 'Space')
                .replace('ArrowUp', '↑')
                .replace('ArrowDown', '↓')
                .replace('ArrowLeft', '←')
                .replace('ArrowRight', '→');
            
            this.els.hotkeyBtn.textContent = name;
            this.els.hotkeyBtn.classList.remove('listening');
            this.els.hotkeyHint.textContent = 'Klicken dann Taste drücken';
            
            document.removeEventListener('keydown', handler, true);
            console.log(`PTT hotkey set to ${e.code}`);
        };
        
        document.addEventListener('keydown', handler, true);
    }
}

// Initialize on load
document.addEventListener('DOMContentLoaded', () => {
    window.hamRemote = new UVK5Remote();
});
