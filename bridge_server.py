"""
MusicBee Telemetry Bridge Server
Receives playback, rating, player mode, and queue events from the MusicBee Recommender plugin
and displays them in real time in the console.

Features:
- Real-time event formatted logging
- Integrated player mode & queue snapshot inside TrackStarted
- Auto-close watchdog: terminates bridge server when MusicBee closes or crashes
"""

import ctypes
from ctypes import wintypes
import http.server
import json
import os
import socketserver
import sys
import threading
import time
from datetime import datetime

PORT = 5005

# Global process tracking for watchdog
tracked_musicbee_pid = None
watchdog_started = False

def format_ms(ms: int) -> str:
    """Formats milliseconds into mm:ss or hh:mm:ss string."""
    if ms is None or ms < 0:
        return "00:00"
    total_seconds = int(ms / 1000)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"

def is_pid_alive(pid: int) -> bool:
    """Checks if a Windows process ID is still alive using kernel32 API (0% CPU, instant)."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == STILL_ACTIVE
        return False
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)

def start_watchdog():
    """Monitors the MusicBee process in background. Auto-exits server if MusicBee terminates."""
    global watchdog_started
    if watchdog_started:
        return
    watchdog_started = True

    def run_watchdog():
        while True:
            time.sleep(2)
            if tracked_musicbee_pid is not None:
                if not is_pid_alive(tracked_musicbee_pid):
                    print(f"\n[WATCHDOG] MusicBee (PID {tracked_musicbee_pid}) terminated.")
                    print("[WATCHDOG] Auto-closing bridge server. Goodbye!")
                    sys.stdout.flush()
                    os._exit(0)

    t = threading.Thread(target=run_watchdog, daemon=True)
    t.start()

class TelemetryHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/event":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            
            try:
                data = json.loads(body.decode("utf-8"))
                
                # Send HTTP 200 response first
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
                
                # Process the event
                self.handle_telemetry_event(data)
            except Exception as e:
                print(f"[ERROR] Failed to process incoming event: {e}")
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"status":"error"}')
        else:
            self.send_response(404)
            self.end_headers()

    def handle_telemetry_event(self, data: dict):
        global tracked_musicbee_pid
        event_name = data.get("event", "UNKNOWN")
        time_str = datetime.now().strftime("%H:%M:%S")
        
        separator = "=" * 65
        print(f"\n{separator}")
        print(f"[{time_str}] EVENT: {event_name}")
        print(separator)

        if event_name == "PluginStartup":
            pid = data.get("pid")
            if pid:
                tracked_musicbee_pid = pid
                start_watchdog()
                print(f"  Connected to MusicBee (PID: {pid})")
                print("  [WATCHDOG ACTIVE] Server will auto-close when MusicBee exits.")
            else:
                print("  MusicBee Recommender Plugin connected successfully!")

        elif event_name == "TrackStarted":
            track = data.get("track", {})
            dur_ms = track.get("duration_ms", 0)
            print(f"  Track Path : {track.get('path', 'N/A')}")
            print(f"  Duration   : {format_ms(dur_ms)} ({dur_ms} ms)")
            
            # Player mode info
            mode = data.get("player_mode", {})
            if mode:
                rep = mode.get("repeat", "None")
                shuf = "ON" if mode.get("shuffle") else "OFF"
                adj = "ON" if mode.get("auto_dj") else "OFF"
                eligible = mode.get("eligible", False)
                status_rec = "ACTIVE" if eligible else "PAUSED (Auto DJ or Repeat One is active)"
                print(f"  Mode       : Repeat={rep} | Shuffle={shuf} | AutoDJ={adj}")
                print(f"  Recommender: [{status_rec}]")

            # Embedded queue snapshot
            queue = data.get("queue", {})
            if queue:
                total = queue.get("total_tracks", 0)
                curr_idx = queue.get("current_index", -1)
                upcoming = queue.get("upcoming_tracks", [])
                print(f"  Queue      : Index {curr_idx + 1}/{total} (0-indexed: {curr_idx})")
                if upcoming:
                    print(f"  Upcoming ({len(upcoming)} tracks):")
                    for i, path in enumerate(upcoming, 1):
                        filename = path.replace("\\", "/").split("/")[-1]
                        print(f"    {i}. {filename}")
            
        elif event_name == "TrackEnded":
            track = data.get("track", {})
            dur_ms = track.get("duration_ms", 0)
            listened_ms = track.get("listened_ms", 0)
            skipped = track.get("skipped", False)
            pct = (listened_ms / dur_ms * 100) if dur_ms > 0 else 0
            
            status_text = "SKIPPED [User skipped track]" if skipped else "FINISHED [Completed playback]"
            print(f"  Track Path : {track.get('path', 'N/A')}")
            print(f"  Listened   : {format_ms(listened_ms)} / {format_ms(dur_ms)} ({pct:.1f}%)")
            print(f"  Status     : {status_text}")

        elif event_name == "RatingChanged":
            print(f"  Track Path : {data.get('path', 'N/A')}")
            print(f"  Rating     : {data.get('rating', '')} / 5.0")
            print(f"  Love/Ban   : {data.get('love', 'None')}")

        elif event_name == "QueueChanged":
            total = data.get("total_tracks", 0)
            curr_idx = data.get("current_index", -1)
            upcoming = data.get("upcoming_tracks", [])
            print(f"  [Manual Queue Edit]")
            print(f"  Queue      : Index {curr_idx + 1}/{total} (0-indexed: {curr_idx})")
            if upcoming:
                print(f"  Upcoming ({len(upcoming)} tracks):")
                for i, path in enumerate(upcoming, 1):
                    filename = path.replace("\\", "/").split("/")[-1]
                    print(f"    {i}. {filename}")

        elif event_name == "MusicBeeClosing":
            print("  MusicBee is closing cleanly.")
            print("  Auto-closing bridge server. Goodbye!")
            sys.stdout.flush()
            # Exit cleanly after short pause
            threading.Timer(0.3, lambda: os._exit(0)).start()
            
        else:
            print(f"  Raw Payload: {json.dumps(data, indent=2)}")

        sys.stdout.flush()

    def log_message(self, format, *args):
        # Suppress default HTTP logging to keep console clean
        return

def run_server():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), TelemetryHandler) as httpd:
        print("=" * 65)
        print(f" MusicBee Telemetry Bridge Server running on http://127.0.0.1:{PORT}")
        print(" Waiting for events from MusicBee... (Press Ctrl+C to stop)")
        print("=" * 65)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server...")

if __name__ == "__main__":
    run_server()
