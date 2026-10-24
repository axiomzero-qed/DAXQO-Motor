"""Fixed task identity, read-only DB checks, and real queued/active Motor stops."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
from motor.arbiter import ActionArbiter, Stopped
from motor.engine import Motor
from motor.mcp_server import MotorMCP
from motor.task_guard import GuardError, HarnessTaskGuard
from tests.fakes import FakeAdapter
from tests.test_mcp_cancellation import Input, Output, call


class TaskGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name); self.database = self.state / 'harness.sqlite3'
        self.sql("CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT,stop_requested INTEGER,prompt TEXT)")

    def sql(self, statement, args=()):
        with closing(sqlite3.connect(self.database)) as db:
            with db: db.execute(statement, args)

    def running(self, ident='old'):
        self.sql("INSERT INTO tasks VALUES(?,'running',0,'DO_NOT_READ')", (ident,))

    def serve(self, motor, factory):
        source, sink = Input(), Output()
        worker = threading.Thread(target=MotorMCP(motor, factory).serve, args=(source, sink)); worker.start()
        def cleanup(): source.close(); worker.join(3); self.assertFalse(worker.is_alive())
        self.addCleanup(cleanup)
        return source, sink

    def test_binding_is_unique_and_fixed_to_old_task(self):
        with self.assertRaises(GuardError): HarnessTaskGuard.bind(self.state)
        self.running(); guard = HarnessTaskGuard.bind(self.state); self.assertEqual(guard(), '')
        self.running('new')
        with self.assertRaises(GuardError): HarnessTaskGuard.bind(self.state)
        self.sql("UPDATE tasks SET status='stopped',stop_requested=1 WHERE id='old'")
        self.assertEqual(guard(), 'harness_task_stopped'); self.assertEqual(guard.task_id, 'old')
        self.sql("DELETE FROM tasks WHERE id='old'"); self.assertEqual(guard(), 'harness_task_missing')
        self.sql("UPDATE tasks SET status='queued' WHERE id='new'")
        self.assertEqual(HarnessTaskGuard.bind(self.state)(), 'harness_task_inactive')

    def test_missing_locked_wrong_owner_and_linked_database_fail_closed(self):
        self.running(); guard = HarnessTaskGuard.bind(self.state)
        with mock.patch('motor.task_guard.os.getuid', return_value=self.database.stat().st_uid + 1, create=True):
            self.assertEqual(guard(), 'harness_guard_wrong_owner')
        with closing(sqlite3.connect(self.database)) as blocker:
            with blocker:
                blocker.execute('BEGIN EXCLUSIVE')
                self.assertEqual(guard(), 'harness_guard_unavailable')
        missing = HarnessTaskGuard(self.state / 'missing', 'old')
        self.assertEqual(missing(), 'harness_guard_unavailable'); self.assertFalse(missing.database.exists())
        link = self.state / 'linked'
        try: link.symlink_to(self.state, target_is_directory=True)
        except OSError: return  # Windows may not permit symlink creation.
        self.assertEqual(HarnessTaskGuard(link, 'old')(), 'harness_guard_unsafe_path')
        link.unlink()

    def test_queries_use_read_only_connection_and_never_select_prompt(self):
        self.running(); original = sqlite3.connect; reads = []
        def connect(path, *args, **kwargs):
            self.assertIn('?mode=ro', path); self.assertTrue(kwargs['uri']); self.assertEqual(kwargs['timeout'], .1)
            db = original(path, *args, **kwargs)
            def authorize(action, table, column, *_):
                if action == sqlite3.SQLITE_READ:
                    reads.append(column)
                    if column == 'prompt': return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize); return db
        with mock.patch('motor.task_guard.sqlite3.connect', side_effect=connect):
            self.assertEqual(HarnessTaskGuard.bind(self.state)(), '')
        self.assertTrue(set(reads) <= {'id', 'status', 'stop_requested'})

    def test_guarded_dispatch_cannot_bypass_arrival_binding(self):
        result = MotorMCP(object(), task_guard_factory=lambda: None).dispatch(call(1))
        self.assertTrue(result['result']['isError'])
        self.assertIn('harness_task_guard_unbound', json.dumps(result))

    def test_guard_is_bound_on_arrival_and_cannot_cancel_a_new_owner(self):
        self.running(); blocked, resume, bound = threading.Event(), threading.Event(), threading.Event()
        class Store:
            reads = 0
            def summary(self, *_args, **_kwargs):
                self.reads += 1
                if self.reads == 1: blocked.set(); resume.wait(3)
                return {'pages': []}
        adapter = FakeAdapter(); identities, threads = [], []
        def factory():
            guard = HarnessTaskGuard.bind(self.state)
            identities.append(guard.task_id); threads.append(threading.current_thread().name); bound.set(); return guard
        source, sink = self.serve(Motor(adapter, Store()), factory)
        source.send(call(1, 'motor_map', package='fixture.app')); self.assertTrue(blocked.wait(2))
        source.send(call(2)); self.assertTrue(bound.wait(2))
        self.sql("UPDATE tasks SET status='stopped',stop_requested=1 WHERE id='old'"); self.running('new')
        lease = adapter.acquire('new-owner', 120000, 'fixture.app')
        resume.set(); sink.wait(1); result = sink.wait(2)
        self.assertIn('harness_task_stopped', json.dumps(result))
        self.assertEqual(identities, ['old']); self.assertNotIn('daxqo-motor-mcp', threads)
        self.assertEqual(adapter._lease, lease); self.assertEqual(adapter.cancel_calls, []); self.assertEqual(adapter.perform_calls, [])

    def test_stop_requested_is_seen_without_any_cancel_notification(self):
        self.running(); resume = threading.Event()
        class Adapter(FakeAdapter):
            def events(self, *args, **kwargs):
                self.wait_entered.set(); resume.wait(3); return super().events(*args, **kwargs)
        from motor.app_map import MapStore
        store = MapStore(':memory:'); self.addCleanup(store.close)
        adapter = Adapter(tabs=True)
        source, sink = self.serve(Motor(adapter, store), lambda: HarnessTaskGuard.bind(self.state))
        source.send(call(1)); self.assertTrue(adapter.wait_entered.wait(2)); actions = len(adapter.perform_calls)
        self.sql("UPDATE tasks SET stop_requested=1 WHERE id='old'"); resume.set()
        self.assertIn('harness_task_stopped', json.dumps(sink.wait(1)))
        self.assertIsNone(adapter._lease); self.assertEqual(len(adapter.perform_calls), actions)

    def test_stop_during_acquire_releases_only_that_returned_lease(self):
        self.running(); guard = HarnessTaskGuard.bind(self.state); test = self
        class Adapter(FakeAdapter):
            def acquire(self, *args, **kwargs):
                lease = super().acquire(*args, **kwargs)
                test.sql("UPDATE tasks SET stop_requested=1 WHERE id='old'"); return lease
        adapter = Adapter()
        with self.assertRaises(Stopped):
            with ActionArbiter(adapter, 'fixture.app', task_guard=guard): self.fail('Cancelled lease entered')
        self.assertIsNone(adapter._lease); self.assertTrue(adapter.cancel_calls); self.assertEqual(adapter.perform_calls, [])
