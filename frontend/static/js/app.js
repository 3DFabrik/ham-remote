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
        this.mode = 'FM';
        this.simulate = false;
        this._rxStarted = false;
        this._wantRx = false;
        this._userInitiatedConnect = false;
        this._freqSetAt = null;
        this.audioContext = null;
        this.mediaStream = null;
        this.audioProcessor = null;
        this.pttHotkey = localStorage.getItem('ptt_hotkey') || 'Space';
        
        // Level meter state
        this.txPeakHold = 0;
        this.rxPeakHold = 0;
        this.txClipTimeout = null;
        this.rxClipTimeout = null;
        this._txLoggedOnce = false;
        this._micReady = false;
        
        this.init();
    }
    
    _log(msg) {
        console.log('[HAM]', msg);
    }
    
    init() {
        this.setupUI();
        this.setupFrequencyDigits();
        this.setupPTT();
        this.setupControls();
        this.setupSettings();
        this.setupTestTone();
        this.setupLevelMeters();
        this.connectWebSocket();
    }
    
    // ============================================================
    // WebSocket
    // ============================================================
    
    connectWebSocket() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.socket = io({
            transports: ['websocket', 'polling'],
            reconnection: true,
            reconnectionDelay: 1000,
            reconnectionAttempts: 10
        });
        
        this.socket.on('connect', () => {
            console.log('WebSocket connected, id:', this.socket.id);
            // Don't flip UI to connected on page load - user must click Connect
            if (this._userInitiatedConnect) {
                this.updateConnectionStatus(true);
            }
        });
        
        this.socket.on('disconnect', () => {
            console.log('WebSocket disconnected');
            this.updateConnectionStatus(false);
        });
        
        this.socket.on('radio_update', (data) => {
            this.updateFromRadio(data);
            // Only manage RX based on user intent, not background pushes
            if (data.connected && this._wantRx && !this._rxStarted) {
                this.startRxAudio();
            }
            // Always stop RX if radio disconnects
            if (!data.connected && this._rxStarted) {
                this.stopRxAudio();
                this._wantRx = false;
            }
        });
        
        // LCD Display state
        this._lcdActive = false;
        this._lcdCtx = null;
        
        this.socket.on('display_update', (data) => {
            // display_update logging disabled
            this._handleDisplayCmd(data);
        });
        
        this.socket.on('audio_tx', (data) => {
            // Binary frame (ArrayBuffer) or legacy JSON
            if (data instanceof ArrayBuffer) {
                this._playBinaryPCM(data);
            } else if (data && data.data) {
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
            statusDot: document.getElementById('status-indicator'),
            statusText: document.getElementById('status-text'),
            pttButton: document.getElementById('ptt-button'),
            sqValue: document.getElementById('sq-value'),
            volValue: document.getElementById('vol-value'),
            powerToggle: document.getElementById('power-toggle'),
            portSelect: document.getElementById('port-select'),
            radioTypeSelect: document.getElementById('radio-type'),
            pttHotkeyBtn: document.getElementById('ptt-hotkey-btn'),
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
        
        // Draw pixel-perfect scale lines via DOM
        this._drawScaleLines();
        window.addEventListener('resize', () => this._drawScaleLines());
        
        // Start the level meter animation loop
        this._levelMeterLoop();
    }
    
    _drawScaleLines() {
        // Audio scale: -50 dB to +4 dB (54 dB range)
        // Marks at: -40, -30, -20, -10, 0 (red), +2
        const FLOOR = -50;
        const CEIL = 4;
        const RANGE = CEIL - FLOOR;
        const AUDIO_MARKS = [
            { db: -40, cls: '' },
            { db: -30, cls: '' },
            { db: -20, cls: '' },
            { db: -10, cls: '' },
            { db: 0,   cls: 'zero-db' },
            { db: +2,  cls: '' },
        ];
        
        // S-meter scale: -60 dB to 0 dB
        // Marks at: S3(19.7%), S5(39.7%), S7(59.7%), S9(79.7%)
        const RF_MARKS = [
            { pct: 0.197 },
            { pct: 0.397 },
            { pct: 0.597 },
            { pct: 0.797 },
        ];
        
        const audioTracks = ['rx-track', 'tx-track'];
        const rfTrack = document.getElementById('rf-track');
        
        // Draw audio scale lines for RX and TX
        audioTracks.forEach(trackId => {
            const track = document.getElementById(trackId);
            if (!track) return;
            // Remove old scale lines
            track.querySelectorAll('.scale-line').forEach(el => el.remove());
            
            const trackWidth = track.offsetWidth;
            if (trackWidth === 0) return;
            
            AUDIO_MARKS.forEach(mark => {
                const frac = (mark.db - FLOOR) / RANGE;
                const px = Math.round(frac * trackWidth);
                const line = document.createElement('div');
                line.className = 'scale-line' + (mark.cls ? ' ' + mark.cls : '');
                line.style.left = px + 'px';
                track.appendChild(line);
            });
        });
        
        // Draw RF S-meter scale lines
        if (rfTrack) {
            rfTrack.querySelectorAll('.scale-line').forEach(el => el.remove());
            const trackWidth = rfTrack.offsetWidth;
            if (trackWidth > 0) {
                RF_MARKS.forEach(mark => {
                    const px = Math.round(mark.pct * trackWidth);
                    const line = document.createElement('div');
                    line.className = 'scale-line';
                    line.style.left = px + 'px';
                    rfTrack.appendChild(line);
                });
            }
        }
    }
    
    _levelMeterLoop() {
        // Update TX level (from mic analyser)
        // Only show TX level while PTT is active
        if (this.txAnalyser && this.txDataArray) {
            if (this.pttActive) {
                this.txAnalyser.getByteTimeDomainData(this.txDataArray);
                const peak = this._getPeakFromData(this.txDataArray);
                const db = this._amplitudeToDb(peak);
                this._updateBar('tx', db, peak);
                // Log first significant TX reading
                if (db > -50 && !this._txLoggedOnce) {
                    this._log('[TX-METER] First reading: ' + db.toFixed(1) + ' dB, peak=' + peak.toFixed(3));
                    this._txLoggedOnce = true;
                }
            } else {
                // PTT off: force TX bar to zero
                this._updateBar('tx', -Infinity, 0);
            }
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
        const maskEl = this.els[channel + 'BarFill'];  // now .level-bar-mask
        const dbEl = this.els[channel + 'Db'];
        const clipEl = this.els[channel + 'Clip'];
        const peakEl = document.getElementById(channel + '-peak');
        const trackEl = document.getElementById(channel === 'smeter' ? 'rf-track' : channel + '-track');
        
        if (!maskEl) return;
        
        // Map dB to percentage
        // TX/RX: level meter -50dB to +4dB (54 dB range)
        // SMeter: -60dB to 0dB (original mapping)
        let pct;
        if (channel === 'tx' || channel === 'rx') {
            const floor = -50;
            const ceil = 4;
            const range = ceil - floor; // 54 dB range
            pct = Math.max(0, Math.min(100, ((db - floor) / range) * 100));
        } else {
            pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
        }
        
        // Mask covers (100 - pct)% from the right, revealing the gradient underneath
        maskEl.style.width = (100 - pct) + '%';
        
        // TX track: grey when mic on but not transmitting
        if (channel === 'tx' && trackEl) {
            if (this.pttActive) {
                trackEl.classList.remove('tx-idle');
            } else {
                trackEl.classList.add('tx-idle');
            }
        }
        
        // Peak-hold
        const peakKey = channel + 'PeakHold';
        const peakTimeKey = channel + 'PeakTime';
        const now = Date.now();
        if (!this[peakKey]) this[peakKey] = 0;
        if (!this[peakTimeKey]) this[peakTimeKey] = 0;
        
        if (pct > this[peakKey]) {
            this[peakKey] = pct;
            this[peakTimeKey] = now;
        } else if (now - this[peakTimeKey] > 1500) {
            this[peakKey] = Math.max(pct, this[peakKey] - 0.1);
        }
        
        if (peakEl) {
            if (this[peakKey] > 2) {
                peakEl.style.left = this[peakKey] + '%';
                peakEl.style.opacity = '0.9';
            } else {
                peakEl.style.opacity = '0';
            }
        }
        
        // Display text
        if (db === -Infinity || pct < 1) {
            dbEl.textContent = '-∞ dB';
            dbEl.className = 'level-bar-value';
        } else {
            dbEl.textContent = db.toFixed(1) + ' dB';
            dbEl.className = 'level-bar-value';
        }
        
        // Clipping
        const timeoutKey = channel + 'ClipTimeout';
        if (linearPeak > 0.89) {
            clipEl.classList.add('clipping');
            dbEl.classList.add('clip-warn');
            dbEl.textContent = 'CLIP!';
            // Reset any existing timeout
            if (this[timeoutKey]) clearTimeout(this[timeoutKey]);
            // Auto-clear after 2 seconds
            this[timeoutKey] = setTimeout(() => {
                clipEl.classList.remove('clipping');
                dbEl.classList.remove('clip-warn');
            }, 2000);
        }
    }
    
    _createAnalyser(source) {
        const analyser = this.audioContext.createAnalyser();
        analyser.fftSize = 256;
        analyser.smoothingTimeConstant = 0.85;
        source.connect(analyser);
        // Don't connect analyser to destination - it's just for measurement
        return analyser;
    }
    
    // ============================================================
    // Frequency
    // ============================================================
    
    setupFrequencyDigits() {
        const upContainer = document.getElementById('freq-digits-up');
        const valContainer = document.getElementById('freq-digit-values');
        const downContainer = document.getElementById('freq-digits-down');
        if (!upContainer || !valContainer || !downContainer) return;
        
        const positions = [
            { type: 'digit', index: 0 },
            { type: 'digit', index: 1 },
            { type: 'digit', index: 2 },
            { type: 'separator' },
            { type: 'digit', index: 3 },
            { type: 'digit', index: 4 },
            { type: 'digit', index: 5 },
            { type: 'unit' },  // MHz label
        ];
        
        upContainer.innerHTML = '';
        valContainer.innerHTML = '';
        downContainer.innerHTML = '';
        
        positions.forEach((pos) => {
            if (pos.type === 'separator') {
                const sepUp = document.createElement('div');
                sepUp.className = 'freq-digit-cell spacer';
                upContainer.appendChild(sepUp);
                
                const sepVal = document.createElement('div');
                sepVal.className = 'freq-digit-cell dot';
                sepVal.textContent = '.';
                valContainer.appendChild(sepVal);
                
                const sepDown = document.createElement('div');
                sepDown.className = 'freq-digit-cell spacer';
                downContainer.appendChild(sepDown);
                return;
            }
            
            if (pos.type === 'unit') {
                const u1 = document.createElement('div');
                u1.className = 'freq-digit-cell spacer';
                upContainer.appendChild(u1);
                
                const uVal = document.createElement('div');
                uVal.className = 'freq-unit-label';
                uVal.textContent = 'MHz';
                valContainer.appendChild(uVal);
                
                const u3 = document.createElement('div');
                u3.className = 'freq-digit-cell spacer';
                downContainer.appendChild(u3);
                return;
            }
            
            // UP button
            const upBtn = document.createElement('button');
            upBtn.className = 'freq-digit-btn';
            upBtn.textContent = '▲';
            upBtn.addEventListener('click', () => this.adjustDigit(pos.index, 1));
            upContainer.appendChild(upBtn);
            
            // Value
            const val = document.createElement('div');
            val.className = 'freq-digit-cell value';
            val.id = `digit-${pos.index}`;
            val.textContent = '0';
            valContainer.appendChild(val);
            
            // DOWN button
            const downBtn = document.createElement('button');
            downBtn.className = 'freq-digit-btn';
            downBtn.textContent = '▼';
            downBtn.addEventListener('click', () => this.adjustDigit(pos.index, -1));
            downContainer.appendChild(downBtn);
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
        freq = Math.max(0.1, Math.min(500.0, freq));
        freq = Math.round(freq * 1000) / 1000; // kHz resolution
        
        this.frequency = freq;
        this._freqSetAt = Date.now();
        this.updateFrequencyDisplay();
        
        if (this.socket) {
            this.socket.emit('set_frequency', { frequency: freq });
        } else {
            fetch('/api/frequency', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ frequency: freq })
            }).catch(err => console.error('Set frequency error:', err));
        }
    }
    
    adjustDigit(index, delta) {
        const freqStr = this.frequency.toFixed(3);
        const parts = freqStr.split('.');
        const intDigits = parts[0].padStart(3, '0').split('').map(Number);
        const decDigits = parts[1].split('').map(Number);
        const digits = [...intDigits, ...decDigits]; // 6 digits
        
        digits[index] = (digits[index] + delta + 10) % 10;
        
        const intPart = parseInt(digits.slice(0, 3).join(''));
        const decPart = digits.slice(3).join('');
        const newFreq = parseFloat(`${intPart}.${decPart}`);
        
        if (newFreq >= 0.001 && newFreq <= 999.999) {
            this.setFrequency(newFreq);
        }
    }
    
    updateFrequencyDisplay() {
        const freqStr = this.frequency.toFixed(3);
        // Extract digits, pad to 6 (XXX.XXX format)
        const parts = freqStr.split('.');
        const intDigits = parts[0].padStart(3, '0').split('');  // 3 digits before dot
        const decDigits = parts[1].split('');                     // 3 digits after dot
        const allDigits = [...intDigits, ...decDigits];           // 6 digits total
        allDigits.forEach((d, i) => {
            const el = document.getElementById(`digit-${i}`);
            if (el) el.textContent = d;
        });
    }
    
    // ============================================================
    // PTT
    // ============================================================
    
    setupPTT() {
        const pttBtn = this.els.pttButton;
        
        // Mic must be activated via click first (browser requires it in click context)
        const micBtn = document.getElementById('btn-mic-enable');
        const micStatus = document.getElementById('mic-enable-status');
        
        if (micBtn) {
            micBtn.addEventListener('click', async () => {
                try {
                    micStatus.textContent = 'Activating...';
                    micStatus.style.color = '#ffaa00';
                    
                    if (!this.audioContext) {
                        this.audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
                    }
                    if (this.audioContext.state === 'suspended') await this.audioContext.resume();
                    
                    this.micStream = await navigator.mediaDevices.getUserMedia({
                        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 }
                    });
                    
                    // TX analyser
                    const micSource = this.audioContext.createMediaStreamSource(this.micStream);
                    this.txAnalyser = this.audioContext.createAnalyser();
                    this.txAnalyser.fftSize = 256;
                    this.txAnalyser.smoothingTimeConstant = 0.5;
                    this.txDataArray = new Uint8Array(this.txAnalyser.frequencyBinCount);
                    micSource.connect(this.txAnalyser);
                    
                    this._micReady = true;
                    micBtn.textContent = '🎤 Mic ON';
                    micBtn.classList.add('active');
                    micStatus.textContent = this.micStream.getTracks()[0].label;
                    this._log('[MIC] ready: ' + this.micStream.getTracks()[0].label);
                } catch (err) {
                    micStatus.textContent = err.name + ': ' + err.message;
                    micStatus.style.color = '#f44';
                    this._log('[MIC] FAILED: ' + err.message);
                }
            });
        }
        
        // PTT - hold to talk, release to stop
        // Using pointer events (unified mouse+touch+pen)
        // Also prevents context menu and text selection on long press
        pttBtn.addEventListener('pointerdown', (e) => {
            e.preventDefault();
            pttBtn.setPointerCapture(e.pointerId); // ensures pointerup fires even if pointer leaves button
            this.pttOn();
        });
        pttBtn.addEventListener('pointerup', (e) => {
            e.preventDefault();
            this.pttOff();
        });
        pttBtn.addEventListener('pointercancel', (e) => {
            this.pttOff();
        });
        // Prevent context menu on long press (mobile)
        pttBtn.addEventListener('contextmenu', (e) => {
            e.preventDefault();
        });
        
        document.addEventListener('keydown', (e) => {
            if (e.code === this.pttHotkey && !e.repeat) { e.preventDefault(); this.pttOn(); }
        });
        document.addEventListener('keyup', (e) => {
            if (e.code === this.pttHotkey) { e.preventDefault(); this.pttOff(); }
        });
    }
    
    pttOn() {
        if (this.pttActive) return;
        if (!this._micReady) {
            this._log('[PTT] Mic not enabled - click mic button first!');
            return;
        }
        this.pttActive = true;
        this.els.pttButton.classList.add('active');
        this._log('[PTT] TX on, micReady=' + this._micReady + ', stream=' + !!this.micStream);
        
        // Start recording from already-open mic stream
        try {
            this._log('[PTT] creating MediaRecorder...');
            const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                ? 'audio/webm;codecs=opus' : 'audio/ogg;codecs=opus';
            this._log('[PTT] mimeType: ' + mimeType);
            this._log('[PTT] micStream: ' + this.micStream.getTracks().length + ' tracks, active=' + this.micStream.active);
            this.mediaRecorder = new MediaRecorder(this.micStream, { mimeType, audioBitsPerSecond: 24000 });
            this._log('[PTT] MediaRecorder state: ' + this.mediaRecorder.state);
            this.mediaRecorder.ondataavailable = (e) => {
                if (e.data.size > 0 && this.pttActive) {
                    const reader = new FileReader();
                    reader.onload = () => {
                        this.socket.emit('audio_rx', {
                            data: reader.result.split(',')[1],
                            codec: 'opus-webm',
                            sampleRate: 16000
                        });
                    };
                    reader.readAsDataURL(e.data);
                }
            };
            this.mediaRecorder.start(20);
            this._log('[PTT] recording started');
        } catch (err) {
            this._log('[PTT] ERROR: ' + err.name + ': ' + err.message);
        }
        
        this.stopRxAudio();
        this.socket.emit('ptt_press');
    }
    
    pttOff() {
        if (!this.pttActive) return;
        this.pttActive = false;
        this._log('[PTT] TX off');
        this.els.pttButton.classList.remove('active');
        
        this.socket.emit('ptt_release');
        
        // Stop recorder but keep mic stream open
        if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
            this.mediaRecorder.stop();
            this.mediaRecorder = null;
        }
        
        // Restart RX after PTT release if still connected
        if (this.connected) {
            this._rxStarted = false;  // force restart
            this.startRxAudio();
        }
    }
    
    // ============================================================
    // Controls
    // ============================================================
    
    setupControls() {
        // Mode buttons
        document.querySelectorAll('.btn-mode').forEach(btn => {
            btn.addEventListener('click', () => {
                this.setMode(btn.dataset.mode);
            });
        });
        
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
    
    setMode(mode) {
        this.mode = mode;
        // Update button states
        document.querySelectorAll('.btn-mode').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.mode === mode);
        });
        
        // Send to backend
        if (this.socket && this.socket.connected) {
            this.socket.emit('set_mode', { mode });
        } else {
            fetch('/api/mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode })
            }).catch(err => console.error('Set mode error:', err));
        }
    }
    
    // ============================================================
    // Audio (Microphone TX + Speaker RX with Level Analysis)
    // ============================================================
    
    _playBinaryPCM(buffer) {
        // Fast path: ArrayBuffer from binary WebSocket → Int16 → Float32 → play
        if (!this.audioContext) {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)({
                sampleRate: 16000
            });
        }
        const int16 = new Int16Array(buffer);
        const float32 = new Float32Array(int16.length);
        for (let i = 0; i < int16.length; i++) {
            float32[i] = int16[i] / 0x8000;
        }
        this._playAndAnalyzeRx(float32);
    }
    
    // G.711 µ-law standard decode table (ITU-T G.711)
    static _ULAW_DECODE = null;
    
    static _buildUlawTable() {
        // Standard ITU-T µ-law decode: 256-entry lookup
        const table = new Int16Array(256);
        const BIAS = 0x84; // 132
        for (let i = 0; i < 256; i++) {
            let ulaw = (~i) & 0xFF;
            let sign = ulaw & 0x80 ? -1 : 1;
            let segment = (ulaw >> 4) & 0x07;
            let mantissa = ulaw & 0x0F;
            let value = ((0x10 | mantissa) << (segment + 2)) - BIAS;
            table[i] = value * sign;
        }
        return table;
    }
    
    static _ulawDecode(byte) {
        if (!UVK5Remote._ULAW_DECODE) UVK5Remote._ULAW_DECODE = UVK5Remote._buildUlawTable();
        return UVK5Remote._ULAW_DECODE[byte];
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
                    const result = this.opusDecoder.decodeFrame(bytes);
                    const audio = result.channelData ? result.channelData[0] : result;
                    if (audio && audio.length > 0) {
                        this._playAndAnalyzeRx(audio);
                    }
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
            } else if (data.codec === 'g711') {
                // G.711 µ-law: 8-bit → 16-bit linear → float32
                const binary = atob(data.data);
                const ulawBytes = new Uint8Array(binary.length);
                for (let i = 0; i < binary.length; i++) {
                    ulawBytes[i] = binary.charCodeAt(i);
                }
                const float32 = new Float32Array(ulawBytes.length);
                for (let i = 0; i < ulawBytes.length; i++) {
                    float32[i] = UVK5Remote._ulawDecode(ulawBytes[i]) / 0x8000;
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
    
    _initAudioPipeline() {
        if (!this.audioContext) {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)({
                sampleRate: 16000
            });
        }
        
        // Scheduled playback: each chunk is scheduled at a precise future time
        // No ring buffer, no ScriptProcessor – gap-free by design
        this._rxNextTime = 0;  // When the next chunk should start playing
        this._rxSampleRate = 16000;
        
        // RX analyser for level meter
        this.rxAnalyser = this.audioContext.createAnalyser();
        this.rxAnalyser.fftSize = 256;
        this.rxAnalyser.smoothingTimeConstant = 0.5;
        this.rxDataArray = new Uint8Array(this.rxAnalyser.frequencyBinCount);
        this.rxAnalyser.connect(this.audioContext.destination);
        
        this._rxPipelineReady = true;
        console.log('RX audio pipeline initialized (scheduled playback)');
    }
    
    _playAndAnalyzeRx(float32Array) {
        if (!this._rxPipelineReady) {
            this._initAudioPipeline();
        }
        
        const ctx = this.audioContext;
        const now = ctx.currentTime;
        
        // Schedule this chunk right after the previous one ends
        // If we've fallen behind (underrun), reset to slightly in the future
        if (this._rxNextTime < now) {
            // Add small latency buffer (80ms) to absorb jitter
            this._rxNextTime = now + 0.08;
        }
        
        // Create AudioBuffer and fill with samples
        const audioBuffer = ctx.createBuffer(1, float32Array.length, this._rxSampleRate);
        audioBuffer.getChannelData(0).set(float32Array);
        
        // Create source node and schedule it
        const source = ctx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(this.rxAnalyser);
        source.start(this._rxNextTime);
        
        // Advance play cursor by chunk duration
        this._rxNextTime += float32Array.length / this._rxSampleRate;
    }
    
    // ============================================================
    // Test Tone (backend → frontend audio pipeline test)
    // ============================================================
    
    setupTestTone() {
        const btnPcm = document.getElementById('btn-test-tone-pcm');
        const btnOpus = document.getElementById('btn-test-tone-opus');
        const status = document.getElementById('test-tone-status');
        
        this._testToneActive = false;
        this._testToneCodec = null;
        
        const startTone = (codec, btn) => {
            // Need user gesture to start AudioContext
            if (!this.audioContext) {
                this.audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
            }
            if (this.audioContext.state === 'suspended') {
                this.audioContext.resume();
            }
            this.socket.emit('test_tone_start', { codec });
            this._testToneActive = true;
            this._testToneCodec = codec;
            btn.classList.add('active');
            if (status) status.textContent = `440Hz, 8kHz ${codec.toUpperCase()}`;
        };
        
        const stopTone = () => {
            this.socket.emit('test_tone_stop');
            this._testToneActive = false;
            this._testToneCodec = null;
            if (btnPcm) btnPcm.classList.remove('active');
            if (btnOpus) btnOpus.classList.remove('active');
            if (status) status.textContent = '';
        };
        
        if (btnPcm) {
            btnPcm.addEventListener('click', () => {
                if (this._testToneActive) {
                    stopTone();
                    if (this._testToneCodec === 'pcm') return;
                }
                startTone('pcm', btnPcm);
            });
        }
        
        if (btnOpus) {
            btnOpus.addEventListener('click', () => {
                if (this._testToneActive) {
                    stopTone();
                    if (this._testToneCodec === 'opus') return;
                }
                startTone('opus', btnOpus);
            });
        }
    }
    
    // ============================================================
    // Connection Management
    // ============================================================
    
    setupSettings() {
        this.els.btnRefreshPorts.addEventListener('click', () => this.refreshPorts());
        this.els.btnConnect.addEventListener('click', () => this.connectRadio());
        this.els.btnDisconnect.addEventListener('click', () => this.disconnectRadio());

        // Main page connect/disconnect button
        const btnMain = document.getElementById('btn-connect-main');
        const radioMain = document.getElementById('radio-type-main');
        if (btnMain) {
            btnMain.addEventListener('click', () => {
                if (this.connected) {
                    this.disconnectRadio();
                } else {
                    // Sync radio type from main dropdown to setup dropdown
                    if (radioMain && this.els.radioType) {
                        this.els.radioType.value = radioMain.value;
                    }
                    this.connectRadio();
                }
            });
        }
        // Sync setup dropdown → main dropdown
        if (this.els.radioType && radioMain) {
            this.els.radioType.addEventListener('change', () => {
                radioMain.value = this.els.radioType.value;
            });
            radioMain.addEventListener('change', () => {
                this.els.radioType.value = radioMain.value;
            });
        }
        
        this.els.audioPlayback = document.getElementById('audio-playback');
        this.els.audioCapture = document.getElementById('audio-capture');
        this.els.audioCodec = document.getElementById('audio-codec');
        this.els.btnRefreshAudio = document.getElementById('btn-refresh-audio');
        
        this.els.btnRefreshAudio.addEventListener('click', () => this.refreshAudioDevices());
        this.els.audioPlayback.addEventListener('change', () => this.setAudioConfig());
        this.els.audioCapture.addEventListener('change', () => this.setAudioConfig());
        if (this.els.audioCodec) this.els.audioCodec.addEventListener('change', () => this.setAudioConfig());
        
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
    }
    
    async _initOpusDecoder() {
        try {
            // opus-decoder UMD exports to window["opus-decoder"].OpusDecoder
            const OpusDecoderClass = (window['opus-decoder'] && window['opus-decoder'].OpusDecoder)
                || (typeof OpusDecoder !== 'undefined' ? OpusDecoder : null);
            if (OpusDecoderClass) {
                this.opusDecoder = new OpusDecoderClass({ sampleRate: 16000, channels: 1 });
                await this.opusDecoder.ready;
                console.log('Opus decoder initialized (8kHz mono)');
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
        const audioPlayback = this.els.audioPlayback?.value || '';
        const audioCapture = this.els.audioCapture?.value || '';
        
        // Auto-enable mic on connect
        await this._enableMic();
        
        if (this.socket) {
            // Show connecting state, but don't set this.connected yet
            this.els.statusDot.className = 'status-dot connecting';
            this.els.statusText.textContent = 'Connecting...';
            this._wantRx = true;  // User wants RX after connect
            this._userInitiatedConnect = true;
            this.socket.emit('radio_connect', {
                port,
                audio_playback: audioPlayback,
                audio_capture: audioCapture
            });
            // Radio update will set this.connected and auto-start RX
        } else {
            // Fallback to HTTP
            try {
                const resp = await fetch('/api/connect', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ port, audio_playback: audioPlayback, audio_capture: audioCapture })
                });
                const data = await resp.json();
                if (data.success) {
                    this.updateConnectionStatus(true);
                    this.startRxAudio();
                }
            } catch (err) { console.error('Connect error:', err); }
        }
    }
    
    async _enableMic() {
        if (this._micReady) return; // Already enabled
        try {
            if (!this.audioContext) {
                this.audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
            }
            if (this.audioContext.state === 'suspended') await this.audioContext.resume();
            
            this.micStream = await navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 }
            });
            
            const micSource = this.audioContext.createMediaStreamSource(this.micStream);
            this.txAnalyser = this.audioContext.createAnalyser();
            this.txAnalyser.fftSize = 256;
            this.txAnalyser.smoothingTimeConstant = 0.5;
            this.txDataArray = new Uint8Array(this.txAnalyser.frequencyBinCount);
            micSource.connect(this.txAnalyser);
            
            this._micReady = true;
            this._log('[MIC] auto-enabled on connect');
        } catch (err) {
            this._log('[MIC] FAILED: ' + err.message);
        }
    }
    
    async startRxAudio() {
        if (this._rxStarted) return;
        this._rxStarted = true;  // Set immediately to prevent race
        try {
            const resp = await fetch('/api/audio/rx/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ clientId: this.socket?.id || 'browser' })
            });
            const data = await resp.json();
            console.log('RX audio:', data.message);
        } catch (err) {
            this._rxStarted = false;  // Reset on failure
            console.error('RX audio start error:', err);
        }
    }
    
    async stopRxAudio() {
        if (!this._rxStarted) return;
        try {
            await fetch('/api/audio/rx/stop', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ clientId: this.socket?.id || 'browser' })
            });
            this._rxStarted = false;
        } catch (err) {
            console.error('RX audio stop error:', err);
        }
    }
    
    async disconnectRadio() {
        this._wantRx = false;  // User doesn't want RX
        this._userInitiatedConnect = false;
        // Disable mic
        if (this.micStream) {
            this.micStream.getTracks().forEach(t => t.stop());
            this.micStream = null;
            this._micReady = false;
            this._log('[MIC] disabled on disconnect');
        }
        try {
            await this.stopRxAudio();
            if (this.socket) {
                this.socket.emit('radio_disconnect');
            } else {
                await fetch('/api/disconnect', { method: 'POST' });
            }
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

        // Update main page button
        const btnMain = document.getElementById('btn-connect-main');
        if (btnMain) {
            btnMain.textContent = connected ? 'Disconnect' : 'Connect';
            btnMain.className = 'btn-connect-main ' + (connected ? 'connected' : 'connect');
        }
    }
    

    // === LCD Display Rendering ===
    _initLCD() {
        const canvas = document.getElementById('lcd-canvas');
        if (!canvas) return;
        this._lcdCtx = canvas.getContext('2d');
        this._lcdCtx.fillStyle = '#1a1a1a';
        this._lcdCtx.fillRect(0, 0, 256, 128);
    }
    
    _handleDisplayCmd(data) {
        if (!this._lcdActive) return;
        if (!this._lcdCtx) this._initLCD();
        if (!this._lcdCtx) return;
        const ctx = this._lcdCtx;
        const cmd = data.cmd;
        const SCALE = 2;
        const ROW_H = 8;
        const LCD_W = 128;
        const LCD_H = 64;
        function unpackXY(rawX, rawRow) {
            let x = rawX;
            let row = rawRow;
            while (x >= LCD_W) { row += 1; x -= LCD_W; }
            return { x: x * SCALE, y: (row + 1) * ROW_H * SCALE };
        }
        if (cmd === 'clear_screen') {
            ctx.fillStyle = '#1a1a1a';
            ctx.fillRect(0, 0, LCD_W * SCALE, LCD_H * SCALE);
        } else if (cmd === 'clear_lines') {
            // Don't clear immediately - text handler clears its own area
            // This prevents flickering when clear comes but no text follows
        } else if (cmd === 'text') {
            // Suppress signal strength text on LCD (e.g. ' -55 21')
            if (/^\s*-\d+\s+\d+/.test(data.text || '')) {
                return;
            }
            const pos = unpackXY(data.x || 0, data.y || 0);
            const fontVal = data.font || 6;
            const text = data.text || '';
            const type = data.type || 0;
            const charScale = fontVal > 0 ? fontVal / 6 : 1;
            const charH = Math.max(6, Math.round(ROW_H * charScale * SCALE));
            const charW = Math.round(charH * 0.6);
            const textW = text.length * charW;
            ctx.fillStyle = '#1a1a1a';
            ctx.fillRect(pos.x, pos.y, textW + 2, charH + 2);
            // Preserve cursor area: redraw cursor if text overwrites it
            if (type === 2) {
                ctx.fillStyle = '#33ff33';
                ctx.fillRect(pos.x, pos.y, textW + 2, charH);
                ctx.fillStyle = '#1a1a1a';
            } else {
                ctx.fillStyle = '#33ff33';
            }
            ctx.font = 'bold ' + charH + 'px "Courier New", monospace';
            ctx.textBaseline = 'top';
            ctx.fillText(text, pos.x, pos.y + 1);
        } else if (cmd === 'cursor') {
            const row = data.row || 0;
            const state = data.state || 0;
            const y = (row + 1) * ROW_H * SCALE;
            const w = 4 * SCALE;
            const h = ROW_H * SCALE;
            if (state === 1) {
                ctx.fillStyle = '#33ff33';
                ctx.fillRect(0, y, w, h);
            } else {
                ctx.strokeStyle = '#33ff33';
                ctx.strokeRect(1, y + 0.5, w - 1, h - 1);
            }
        } else if (cmd === 'status_bar') {
            // Render status bar in Row 0
            ctx.fillStyle = '#1a1a1a';
            ctx.fillRect(0, 0, LCD_W * SCALE, ROW_H * SCALE);
            let statusText = '';
            if (data.char) statusText += data.char + ' ';
            if (data.battery_v) statusText += data.battery_v.toFixed(1) + 'V';
            if (data.flags1 & 0x01) statusText += ' TX';
            if (data.flags1 & 0x02) statusText += ' RX';
            if (statusText) {
                ctx.fillStyle = '#33ff33';
                ctx.font = (ROW_H * SCALE) + 'px "Courier New", monospace';
                ctx.textBaseline = 'top';
                ctx.fillText(statusText, 2, 1);
            }
        } else if (cmd === 'signal') {
            // v1 = signal bar segments (maps to S0-S9+), v2 = secondary bar
            const sVal = Math.max(0, Math.min(12, data.v1 || 0));
            this.updateSMeter(sVal);
        }
    }
    
    setLCDActive(active) {
        this._lcdActive = active;
        const freqSection = document.getElementById('freq-section');
        const lcdSection = document.getElementById('lcd-section');
        if (freqSection) freqSection.style.display = active ? 'none' : '';
        if (lcdSection) lcdSection.style.display = active ? '' : 'none';
        if (active && !this._lcdCtx) this._initLCD();
    }

    updateFromRadio(data) {
        if (data.frequency !== undefined) {
            // Ignore stale freq updates right after user changed it
            const now = Date.now();
            if (this._freqSetAt && (now - this._freqSetAt) < 1500) {
                // User recently changed freq – only accept if it matches
                if (Math.abs(data.frequency - this.frequency) < 0.001) {
                    // Confirmed by radio, clear debounce
                    this._freqSetAt = null;
                }
                // else: ignore, keep local value
            } else {
                this.frequency = data.frequency;
                this._freqSetAt = null;
            }
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
        if (data.mode !== undefined) {
            this.mode = data.mode;
            document.querySelectorAll('.btn-mode').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.mode === data.mode);
            });
        }
        if (data.connected !== undefined) {
            this.updateConnectionStatus(data.connected);
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
        const pct = Math.min(100, (value / 12) * 100);
        this.els.smeterFill.style.width = (100 - pct) + '%';
        
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
            if (config.codec && this.els.audioCodec) this.els.audioCodec.value = config.codec;
            
        } catch (err) {
            console.error('Audio devices error:', err);
        }
    }
    
    async setAudioConfig() {
        const playback = this.els.audioPlayback.value;
        const capture = this.els.audioCapture.value;
        const codec = this.els.audioCodec?.value || 'opus';
        
        try {
            await fetch('/api/audio/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ playback, capture, codec })
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
                // Toggle LCD display for Quansheng
                this.setLCDActive(type === "uvk5");
                // Apply saved settings for this radio type
                const saved = result.settings || {};
                // Refresh ports first, then set saved port after
                await this.refreshPorts();
                if (saved.port) {
                    this.els.portSelect.value = saved.port;
                }
                // Refresh audio devices, then apply saved audio settings + send to backend
                await this.refreshAudioDevices();
                const pb = saved.audio_playback;
                const cap = saved.audio_capture;
                if (pb && this.els.audioPlayback) {
                    this.els.audioPlayback.value = pb;
                }
                if (cap && this.els.audioCapture) {
                    this.els.audioCapture.value = cap;
                }
                // Send audio config to backend so it takes effect
                if (pb || cap) {
                    try {
                        await fetch('/api/audio/config', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ playback: pb || '', capture: cap || '' })
                        });
                    } catch (e) { console.error('Audio config restore error:', e); }
                }
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
    // Set initial button state
    const btnMain = document.getElementById('btn-connect-main');
    if (btnMain) {
        btnMain.textContent = 'Connect';
        btnMain.className = 'btn-connect-main connect';
    }

    window.hamRemote = new UVK5Remote();
    
    // Restore saved settings
    fetch('/api/settings').then(r => r.json()).then(settings => {
        if (settings.port) {
            const portSel = document.getElementById('port-select');
            if (portSel) portSel.value = settings.port;
        }
        // Audio settings restored by refreshAudioDevices() from /api/audio/config
        if (settings.radio_type) {
            const rt = document.getElementById('radio-type');
            if (rt) rt.value = settings.radio_type;
            const rtMain = document.getElementById('radio-type-main');
            if (rtMain) rtMain.value = settings.radio_type;
            // Toggle LCD display for Quansheng
            hamRemote.setLCDActive(settings.radio_type === "uvk5");
        }
    }).catch(() => {});
});
