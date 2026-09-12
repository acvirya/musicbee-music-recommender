"""
MusicBee Telemetry Bridge Server
Receives playback, rating, player mode, and queue events from the MusicBee Recommender plugin
and displays them in real time in the console.

Features:
- Real-time event formatted logging
- Integrated player mode & queue snapshot inside TrackStarted
- Watermark Buffer evaluation (Recommender trigger detection)
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

# Watermark buffer threshold:
# If 1 or fewer upcoming tracks remain in the queue, trigger recommender
BUFFER_THRESHOLD = 1

# Global state tracking
tracked_musicbee_pid = None
watchdog_started = False
last_player_mode = {}
last_current_track = {}

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

def evaluate_recommender(track: dict, mode: dict, queue: dict):
    """
    Evaluates whether the Recommendation Engine should trigger based on:
    1. Player Mode eligibility (Repeat != One and AutoDJ == False)
    2. Queue Watermark Buffer (remaining tracks <= BUFFER_THRESHOLD)
    """
    eligible = mode.get("eligible", False)
    rep = mode.get("repeat", "None")
    adj = mode.get("auto_dj", False)
    
    total = queue.get("total_tracks", 0)
    curr_idx = queue.get("current_index", -1)
    remaining = max(0, total - (curr_idx + 1)) if (total > 0 and curr_idx >= 0) else 0

    print("\n  " + "-" * 55)
    print("  RECOMMENDER DECISION ENGINE")
    print("  " + "-" * 55)

    if not eligible:
        reasons = []
        if rep == "One":
            reasons.append("Repeat One is ON (song will loop)")
        if adj:
            reasons.append("Auto DJ is ON (MusicBee manages queue)")
        print(f"  Status    : [IDLE / PAUSED]")
        print(f"  Reason    : Ineligible mode ({', '.join(reasons)})")

    elif remaining <= BUFFER_THRESHOLD:
        song_name = track.get("path", "Unknown").replace("\\", "/").split("/")[-1]
        print(f"  Status    : >>> [TRIGGERED] <<<")
        print(f"  Condition : Queue low! ({remaining} upcoming track(s) <= threshold of {BUFFER_THRESHOLD})")
        print(f"  Message   : Recommendation Engine Triggered! Fetching Recommended Tracks...")
        print(f"  Seed Track: \"{song_name}\"")
        print(f"  Action    : [Mock] Queried section embeddings -> 2 recommended songs queued to end.")

    else:
        print(f"  Status    : [IDLE]")
        print(f"  Condition : Queue healthy ({remaining} upcoming tracks > threshold of {BUFFER_THRESHOLD})")
        print(f"  Reason    : User queue has plenty of songs. Respecting user playlist without intrusion.")

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
        global tracked_musicbee_pid, last_player_mode, last_current_track
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

            settings = data.get("settings", {})
            if settings:
                skip_pct = settings.get("skip_percent", 0)
                skip_sec = settings.get("skip_seconds", 0)
                play_pct = settings.get("play_percent", 0)
                play_sec = settings.get("play_seconds", 0)
                skip_desc = []
                if skip_pct > 0: skip_desc.append(f"<{skip_pct}%")
                if skip_sec > 0: skip_desc.append(f"<{skip_sec}s")
                if not skip_desc and play_pct > 0: skip_desc.append(f"<{play_pct}% (from play trigger)")
                if not skip_desc and play_sec > 0: skip_desc.append(f"<{play_sec}s (from play trigger)")
                if not skip_desc: skip_desc.append("<80% (default fallback)")

                print(f"  MusicBee Preferences Detected:")
                print(f"    - Skip Threshold : {', '.join(skip_desc)}")
                print(f"    - Play Trigger   : {play_pct}% / {play_sec}s")

        elif event_name == "TrackStarted":
            track = data.get("track", {})
            dur_ms = track.get("duration_ms", 0)
            last_current_track = track
            print(f"  Track Path : {track.get('path', 'N/A')}")
            print(f"  Duration   : {format_ms(dur_ms)} ({dur_ms} ms)")
            
            # Player mode info
            mode = data.get("player_mode", {})
            last_player_mode = mode
            if mode:
                rep = mode.get("repeat", "None")
                shuf = "ON" if mode.get("shuffle") else "OFF"
                adj = "ON" if mode.get("auto_dj") else "OFF"
                print(f"  Mode       : Repeat={rep} | Shuffle={shuf} | AutoDJ={adj}")

            # Embedded queue snapshot
            queue = data.get("queue", {})
            if queue:
                total = queue.get("total_tracks", 0)
                curr_idx = queue.get("current_index", -1)
                upcoming = queue.get("upcoming_tracks", [])
                remaining = max(0, total - (curr_idx + 1)) if (total > 0 and curr_idx >= 0) else 0
                print(f"  Queue      : Playing {curr_idx + 1} of {total} (Remaining in queue: {remaining})")
                if upcoming:
                    print(f"  Upcoming ({len(upcoming)} tracks shown):")
                    for i, path in enumerate(upcoming, 1):
                        filename = path.replace("\\", "/").split("/")[-1]
                        print(f"    {i}. {filename}")

            # Evaluate recommender activation
            evaluate_recommender(track, mode, queue)
            
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
            remaining = max(0, total - (curr_idx + 1)) if (total > 0 and curr_idx >= 0) else 0
            print(f"  [Manual Queue Edit]")
            print(f"  Queue      : Playing {curr_idx + 1} of {total} (Remaining in queue: {remaining})")
            if upcoming:
                print(f"  Upcoming ({len(upcoming)} tracks shown):")
                for i, path in enumerate(upcoming, 1):
                    filename = path.replace("\\", "/").split("/")[-1]
                    print(f"    {i}. {filename}")

            # Evaluate recommender activation on queue changes as well
        elif event_name == "SettingsChanged":
            settings = data.get("settings", {})
            skip_pct = settings.get("skip_percent", 0)
            skip_sec = settings.get("skip_seconds", 0)
            play_pct = settings.get("play_percent", 0)
            play_sec = settings.get("play_seconds", 0)
            skip_desc = []
            if skip_pct > 0: skip_desc.append(f"<{skip_pct}%")
            if skip_sec > 0: skip_desc.append(f"<{skip_sec}s")
            if not skip_desc and play_pct > 0: skip_desc.append(f"<{play_pct}% (from play trigger)")
            if not skip_desc and play_sec > 0: skip_desc.append(f"<{play_sec}s (from play trigger)")
            if not skip_desc: skip_desc.append("<80% (default fallback)")

            print("  MusicBee Preferences Updated by User!")
            print(f"    - New Skip Threshold : {', '.join(skip_desc)}")
            print(f"    - New Play Trigger   : {play_pct}% / {play_sec}s")

        elif event_name == "MusicBeeClosing":
            print("  MusicBee is closing cleanly.")
            print("  Auto-closing bridge server. Goodbye!")
            sys.stdout.flush()
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
        print(f" Buffer Threshold: {BUFFER_THRESHOLD} (triggers when remaining queue <= {BUFFER_THRESHOLD})")
        print(" Waiting for events from MusicBee... (Press Ctrl+C to stop)")
        print("=" * 65)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server...")

if __name__ == "__main__":
    run_server()
