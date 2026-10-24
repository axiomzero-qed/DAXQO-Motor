"""Real worker/arbiter cancellation races, without phone operations."""
import io
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from motor.app_map import MapStore
from motor.engine import Motor
from motor.mcp_server import MotorMCP
from tests.fakes import FakeAdapter


class Input:
    def __init__(self): self.lines = queue.Queue()
    def readline(self, _limit): return self.lines.get()
    def send(self, value): self.lines.put((json.dumps(value) + '\n').encode())
    def close(self): self.lines.put(b'')


class Output(io.BytesIO):
    def __init__(self): super().__init__(); self.condition = threading.Condition()
    def write(self, value):
        with self.condition:
            result = super().write(value); self.condition.notify_all(); return result
    def messages(self): return [json.loads(line) for line in self.getvalue().splitlines()]
    def wait(self, ident):
        with self.condition:
            assert self.condition.wait_for(lambda: any(m['id'] == ident for m in self.messages()), 3)
        return next(m for m in self.messages() if m['id'] == ident)


class Adapter(FakeAdapter):
    def __init__(self): super().__init__(tabs=True, block_events=True); self.scopes = []
    def cancel(self, reason, lease=None):
        self.scopes.append(lease); return super().cancel(reason, lease)


def call(ident, name='motor_scout', **args):
    return {'jsonrpc': '2.0', 'id': ident, 'method': 'tools/call', 'params': {
        'name': name, 'arguments': args or {'package': 'fixture.app', 'max_scrolls': 0}}}


def notification(ident):
    return {'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': ident}}


class CancellationTests(unittest.TestCase):
    def start(self, adapter=None):
        adapter = adapter or Adapter(); store = MapStore(':memory:'); motor = Motor(adapter, store)
        source, sink = Input(), Output(); server = MotorMCP(motor)
        worker = threading.Thread(target=server.serve, args=(source, sink)); worker.start()
        self.addCleanup(store.close)
        def cleanup(): source.close(); worker.join(4); self.assertFalse(worker.is_alive())
        self.addCleanup(cleanup)
        return adapter, motor, source, sink, worker

    def test_notification_old_unknown_duplicate_and_explicit_cancel(self):
        adapter, motor, source, sink, _ = self.start()
        source.send(call(1)); self.assertTrue(adapter.wait_entered.wait(2))
        source.send(notification(1)); source.send(call(100, 'motor_map', package='fixture.app')); sink.wait(100)
        self.assertFalse(any(m['id'] == 1 for m in sink.messages()))
        self.assertTrue(all(scope is not None for scope in adapter.scopes))
        adapter.release_wait.clear(); adapter.wait_entered.clear()
        source.send(call(2)); self.assertTrue(adapter.wait_entered.wait(2)); actions = len(adapter.perform_calls)
        source.send(notification(1)); source.send(notification(999)); source.send(notification(1))
        malformed = notification(2); malformed['params']['reason'] = 42; source.send(malformed)
        source.send(call(1)); source.send({'jsonrpc': '2.0', 'id': 3, 'method': 'ping'})
        sink.wait(3); self.assertFalse(motor._active.cancelled.is_set())
        self.assertTrue(any(m.get('error', {}).get('code') == -32600 for m in sink.messages()))
        source.send(call(4, 'motor_cancel', reason='explicit'))
        sink.wait(4); source.send(call(5, 'motor_map', package='fixture.app')); sink.wait(5)
        self.assertFalse(any(m['id'] == 2 for m in sink.messages()))
        self.assertEqual(len(adapter.perform_calls), actions)

    def test_eof_cancels_active_and_queued_requests_without_global_cancel(self):
        adapter, _, source, sink, worker = self.start()
        source.send(call(1)); self.assertTrue(adapter.wait_entered.wait(2)); actions = len(adapter.perform_calls)
        source.send(call(2)); source.close(); worker.join(3)
        self.assertFalse(worker.is_alive()); self.assertIsNone(adapter._lease)
        self.assertEqual(len(adapter.perform_calls), actions)
        self.assertEqual(sink.messages(), [])
        self.assertTrue(all(scope is not None for scope in adapter.scopes))

    def test_pending_queue_is_bounded_and_cancel_remains_responsive(self):
        adapter, _, source, sink, worker = self.start()
        source.send(call(1)); self.assertTrue(adapter.wait_entered.wait(2)); actions = len(adapter.perform_calls)
        for ident in range(2, 18): source.send(call(ident))
        self.assertEqual(sink.wait(17)['error']['code'], -32000)
        source.send(call(99, 'motor_cancel', reason='clear queue')); sink.wait(99)
        source.close(); worker.join(3)
        self.assertFalse(worker.is_alive()); self.assertEqual(len(adapter.perform_calls), actions)
        self.assertIsNone(adapter._lease)

    def test_cancel_while_acquire_is_pending_prevents_the_first_action(self):
        class SlowAcquire(Adapter):
            def __init__(self): super().__init__(); self.entered = threading.Event(); self.resume = threading.Event()
            def acquire(self, *args, **kwargs):
                self.entered.set(); self.resume.wait(3); return super().acquire(*args, **kwargs)
        adapter = SlowAcquire(); _, motor, source, sink, _ = self.start(adapter)
        source.send(call(1)); self.assertTrue(adapter.entered.wait(2)); self.assertIsNotNone(motor._active)
        source.send(notification(1)); source.send({'jsonrpc': '2.0', 'id': 2, 'method': 'ping'})
        sink.wait(2); adapter.resume.set(); source.send(call(3, 'motor_map', package='fixture.app')); sink.wait(3)
        self.assertFalse(any(m['id'] == 1 for m in sink.messages()))
        self.assertEqual(adapter.perform_calls, []); self.assertIsNone(adapter._lease)
        self.assertTrue(adapter.scopes and all(scope is not None for scope in adapter.scopes))

    def test_signal_handler_cleans_up_and_restores_previous_handlers(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum), MapStore(':memory:') as store:
                adapter, sink = Adapter(), Output(); old = signal.getsignal(signum)
                class SignalInput:
                    reads = 0
                    def readline(self, _limit):
                        self.reads += 1
                        if self.reads == 1: return (json.dumps(call(1)) + '\n').encode()
                        assert adapter.wait_entered.wait(2)
                        signal.getsignal(signum)(signum, None)
                with self.assertRaises(SystemExit): MotorMCP(Motor(adapter, store)).serve(SignalInput(), sink)
                self.assertEqual(signal.getsignal(signum), old); self.assertIsNone(adapter._lease)
                self.assertTrue(adapter.scopes and all(scope is not None for scope in adapter.scopes))

    @unittest.skipIf(os.name == 'nt', 'Windows TerminateProcess does not deliver Python SIGTERM handlers')
    def test_linux_sigterm_subprocess_releases_its_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            active, cancelled = Path(temp) / 'active', Path(temp) / 'cancelled'
            program = '''from pathlib import Path
import sys
from motor.mcp_server import MotorMCP
from motor.engine import Motor
from motor.app_map import MapStore
from tests.fakes import FakeAdapter
class Adapter(FakeAdapter):
    def events(self, *args, **kwargs):
        Path(sys.argv[1]).touch(); return super().events(*args, **kwargs)
    def cancel(self, reason, lease=None):
        assert lease is not None
        Path(sys.argv[2]).touch(); return super().cancel(reason, lease)
MotorMCP(Motor(Adapter(tabs=True, block_events=True), MapStore(':memory:'))).serve()
'''
            process = subprocess.Popen([sys.executable, '-c', program, str(active), str(cancelled)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=Path(__file__).resolve().parents[1])
            try:
                process.stdin.write((json.dumps(call(1)) + '\n').encode()); process.stdin.flush()
                deadline = time.monotonic() + 3
                while not active.exists() and time.monotonic() < deadline: threading.Event().wait(.01)
                self.assertTrue(active.exists()); process.send_signal(signal.SIGTERM)
                process.wait(timeout=4)  # Keep stdin open: SIGTERM itself must trigger cleanup.
                output, errors = process.communicate(timeout=4)
                self.assertEqual(process.returncode, 0, errors); self.assertTrue(cancelled.exists())
                self.assertEqual(output, b'')
            finally:
                if process.poll() is None: process.kill(); process.wait()
