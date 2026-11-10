"""Private line-delimited JSON workers, reused until the local server stops."""
import atexit
import json
import os
from queue import Queue, Empty
import signal
import subprocess
import threading


class JSONWorker:
    def __init__(self, command, env=None):
        self.command = command
        self.env = env
        self.lock = threading.Lock()
        self.process = None

    def _start(self):
        self.responses = Queue()
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
            env=self.env, start_new_session=os.name != 'nt')
        process, responses = self.process, self.responses
        def read():
            try:
                for line in process.stdout:
                    responses.put(line)
            finally:
                responses.put(None)
        self.reader = threading.Thread(target=read, daemon=True, name='ocr-response')
        self.reader.start()

    def call(self, payload, timeout):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self._stop()
                self._start()
            try:
                self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + '\n')
                self.process.stdin.flush()
                answer = self.responses.get(timeout=timeout)
                if answer is None:
                    raise OSError('OCR worker exited')
                return json.loads(answer)
            except (OSError, ValueError, Empty):
                self._stop()
                raise

    def _stop(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # Only stop the process tree created by this worker, including Node.
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               capture_output=True, timeout=10)
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
        process.stdout.close()
        self.reader.join(timeout=1)

    def close(self):
        with self.lock:
            self._stop()


_workers = {}
_lock = threading.Lock()


def worker(key, command, env=None):
    with _lock:
        if key not in _workers:
            _workers[key] = JSONWorker(command, env)
        return _workers[key]


def shutdown_workers():
    with _lock:
        current = list(_workers.values())
        _workers.clear()
    for item in current:
        item.close()


atexit.register(shutdown_workers)
