import os
import subprocess
import sys
import time

from logger import log

python = sys.executable
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def spawn(script):
    return subprocess.Popen([python, os.path.join(BASE_DIR, script)])


def main():
    log.info("Starting SelfBot...")
    selfbot = spawn("main.py")

    time.sleep(3)

    log.info("Starting Control Bot...")
    control_bot = spawn("control_bot.py")

    log.ok("Everything started")

    try:
        while True:
            time.sleep(10)

            if selfbot.poll() is not None:
                log.error("SelfBot stopped! Restarting...")
                selfbot = spawn("main.py")

            if control_bot.poll() is not None:
                log.error("Control Bot stopped! Restarting...")
                control_bot = spawn("control_bot.py")

    except KeyboardInterrupt:
        log.info("Stopping...")
        selfbot.terminate()
        control_bot.terminate()


if __name__ == "__main__":
    main()
