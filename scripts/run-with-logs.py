#!/usr/bin/env python3
"""Capture daemon/bridge output with a 100,000,000-byte rotating file budget."""

from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys

FILE_BYTES = 10_000_000
FILE_COUNT = 10


class RotatingLog:
    def __init__(self, directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # One writer owns rotation, even if a second service is launched by mistake.
        self.lock = (directory / '.writer.lock').open('a')
        os.chmod(directory / '.writer.lock', 0o600)
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.path = directory / 'gigatype.log'
        self.open()

    def open(self):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.file = os.fdopen(fd, 'ab', buffering=0)
        os.fchmod(fd, 0o600)
        self.size = os.fstat(fd).st_size

    def write(self, message):
        if self.file.closed:
            self.open()
        stamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        data = (stamp + ' ' + message.rstrip('\n') + '\n').encode('utf-8', errors='replace')
        # Input reads are bounded; this cap also protects future callers.
        data = data[:65535] + b'\n' if len(data) > 65536 else data
        if self.size + len(data) > FILE_BYTES:
            self.file.close()
            self.path.with_name(self.path.name + f'.{FILE_COUNT - 1}').unlink(missing_ok=True)
            for index in range(FILE_COUNT - 2, 0, -1):
                source = self.path.with_name(self.path.name + f'.{index}')
                if source.exists():
                    source.replace(self.path.with_name(self.path.name + f'.{index + 1}'))
            self.path.replace(self.path.with_name(self.path.name + '.1'))
            self.open()
        self.file.write(data)
        self.size += len(data)


def main():
    if len(sys.argv) < 2 or not os.environ.get('GIGATYPE_LOG_DIR'):
        raise SystemExit('Usage: GIGATYPE_LOG_DIR=/path/to/logs run-with-logs.py COMMAND [ARGS...]')
    os.umask(0o077)
    log = RotatingLog(Path(os.environ['GIGATYPE_LOG_DIR']).expanduser())
    failed = False

    def record(message):
        nonlocal failed
        try:
            log.write(message)
            failed = False
        except OSError as error:
            if not failed:
                print(f'GigaType log file error: {error}', file=sys.stderr, flush=True)
            failed = True
        # Retain journal diagnostics even if the log directory becomes unwritable.
        print(message.rstrip('\n'), file=sys.stderr, flush=True)

    child = None
    stopping = None

    def stop(signum, _frame):
        nonlocal stopping
        stopping = signum
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    record(f'logger event=start pid={os.getpid()} file_bytes={FILE_BYTES} file_count={FILE_COUNT}')
    try:
        child = subprocess.Popen(sys.argv[1:], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env={**os.environ, 'PYTHONUNBUFFERED': '1'})
    except OSError as error:
        record(f'logger event=spawn_error error={error}')
        return 1
    if stopping is not None:
        stop(stopping, None)
    record(f'logger event=child_started pid={child.pid}')
    with child.stdout:
        while True:
            chunk = child.stdout.readline(65536)
            if not chunk:
                break
            record(chunk.decode('utf-8', errors='replace'))
    code = child.wait()
    record(f'logger event=exit returncode={code} stop_signal={stopping}')
    log.file.close()
    return 0 if stopping is not None else (code if code >= 0 else 128 - code)


if __name__ == '__main__':
    sys.exit(main())
