"""
Entrypoint for Render (or any host that requires an open HTTP port).
Render's free tier only keeps a process alive if it's registered as a
"Web Service" answering HTTP requests — a bare background script gets
killed. This just opens a tiny health-check server on $PORT and runs the
same two processes start.py normally runs, restarting them if they crash.

Pair this with a free uptime pinger (e.g. UptimeRobot) hitting your
Render URL every 5 minutes, or Render will still spin the service down
after ~15 minutes of no HTTP traffic.
"""

import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from config import PORT
from logger import log

python = sys.executable
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"selfbot is running")

    def log_message(self, *args):
        pass  # keep Render's logs from filling up with health-check noise


def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    log.ok(f"Health-check server listening on port {PORT}")
    server.serve_forever()


def spawn(script):
    return subprocess.Popen([python, os.path.join(BASE_DIR, script)])


def supervise():
    log.info("Starting SelfBot...")
    selfbot = spawn("main.py")

    time.sleep(3)

    log.info("Starting Control Bot...")
    control_bot = spawn("control_bot.py")

    log.ok("Everything started")

    while True:
        time.sleep(10)

        if selfbot.poll() is not None:
            log.error("SelfBot stopped! Restarting...")
            selfbot = spawn("main.py")

        if control_bot.poll() is not None:
            log.error("Control Bot stopped! Restarting...")
            control_bot = spawn("control_bot.py")


if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    supervise()
