#!/bin/bash
# HAM Remote - Start/Stop/Restart Script
# Prevents zombie processes on port 8080

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$SCRIPT_DIR/ham-remote.pid"
PORT=8080

die() { echo "❌ $1"; exit 1; }

check_running() {
    if [ -f "$PIDFILE" ]; then
        local pid=$(cat "$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$PIDFILE"
    fi
    return 1
}

kill_zombies() {
    # Kill ALL processes listening on port 8080
    local pids=$(lsof -t -i :$PORT 2>/dev/null)
    if [ -n "$pids" ]; then
        echo "🧟 Killing zombie processes on port $PORT: $pids"
        echo "$pids" | xargs kill -9 2>/dev/null
        sleep 1
    fi
    # Also kill stray arecord/aplay
    pkill -9 -f "arecord.*hw:" 2>/dev/null
    pkill -9 -f "aplay.*hw:" 2>/dev/null
}

do_start() {
    if check_running; then
        echo "✅ Already running (PID $(cat "$PIDFILE"))"
        return 0
    fi
    
    kill_zombies
    
    cd "$SCRIPT_DIR"
    source venv/bin/activate
    
    echo "🚀 Starting HAM Remote on port $PORT..."
    UVK5_SIMULATE=${SIMULATE:-true} python backend/app.py &
    local pid=$!
    echo "$pid" > "$PIDFILE"
    
    # Wait and verify
    sleep 2
    if kill -0 "$pid" 2>/dev/null; then
        echo "✅ Started (PID $pid)"
        echo "   http://localhost:$PORT"
        echo "   https://192.168.1.113:8444 (via Caddy)"
        return 0
    else
        echo "❌ Failed to start"
        rm -f "$PIDFILE"
        return 1
    fi
}

do_stop() {
    if check_running; then
        local pid=$(cat "$PIDFILE")
        echo "🛑 Stopping HAM Remote (PID $pid)..."
        kill "$pid" 2>/dev/null
        sleep 2
        # Force kill if still running
        if kill -0 "$pid" 2>/dev/null; then
            echo "🧟 Force killing..."
            kill -9 "$pid" 2>/dev/null
        fi
        rm -f "$PIDFILE"
    fi
    kill_zombies
    echo "✅ Stopped"
}

do_restart() {
    do_stop
    do_start
}

do_status() {
    if check_running; then
        local pid=$(cat "$PIDFILE")
        echo "✅ Running (PID $pid)"
        curl -s "http://localhost:$PORT/api/status" 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "   (API not responding)"
    else
        echo "⏹️ Not running"
    fi
}

case "${1:-start}" in
    start)   do_start   ;;
    stop)    do_stop    ;;
    restart) do_restart ;;
    status)  do_status  ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        echo ""
        echo "  SIMULATE=false $0 start  - Start with real hardware"
        exit 1
        ;;
esac
