# Test the Windows-only _winapi module

import random
import threading
import time
import unittest
from test.support import import_helper

_winapi = import_helper.import_module('_winapi', required_on=['win'])

MAXIMUM_WAIT_OBJECTS = 64
MAXIMUM_BATCHED_WAIT_OBJECTS = (MAXIMUM_WAIT_OBJECTS - 1) ** 2

class WinAPIBatchedWaitForMultipleObjectsTests(unittest.TestCase):
    def _events_waitall_test(self, n):
        evts = [_winapi.CreateEventW(0, False, False, None) for _ in range(n)]

        with self.assertRaises(TimeoutError):
            _winapi.BatchedWaitForMultipleObjects(evts, True, 100)

        # Ensure no errors raised when all are triggered
        for e in evts:
            _winapi.SetEvent(e)
        try:
            _winapi.BatchedWaitForMultipleObjects(evts, True, 100)
        except TimeoutError:
            self.fail("expected wait to complete immediately")

        # Choose 8 events to set, distributed throughout the list, to make sure
        # we don't always have them in the first chunk
        chosen = [i * (len(evts) // 8) for i in range(8)]

        # Replace events with invalid handles to make sure we fail
        for i in chosen:
            old_evt = evts[i]
            evts[i] = -1
            with self.assertRaises(OSError):
                _winapi.BatchedWaitForMultipleObjects(evts, True, 100)
            evts[i] = old_evt


    def _events_waitany_test(self, n):
        evts = [_winapi.CreateEventW(0, False, False, None) for _ in range(n)]

        with self.assertRaises(TimeoutError):
            _winapi.BatchedWaitForMultipleObjects(evts, False, 100)

        # Choose 8 events to set, distributed throughout the list, to make sure
        # we don't always have them in the first chunk
        chosen = [i * (len(evts) // 8) for i in range(8)]

        # Trigger one by one. They are auto-reset events, so will only trigger once
        for i in chosen:
            with self.subTest(f"trigger event {i} of {len(evts)}"):
                _winapi.SetEvent(evts[i])
                triggered = _winapi.BatchedWaitForMultipleObjects(evts, False, 10000)
                self.assertSetEqual(set(triggered), {i})

        # Trigger all at once. This may require multiple calls
        for i in chosen:
            _winapi.SetEvent(evts[i])
        triggered = set()
        while len(triggered) < len(chosen):
            triggered.update(_winapi.BatchedWaitForMultipleObjects(evts, False, 10000))
        self.assertSetEqual(triggered, set(chosen))

        # Replace events with invalid handles to make sure we fail
        for i in chosen:
            with self.subTest(f"corrupt event {i} of {len(evts)}"):
                old_evt = evts[i]
                evts[i] = -1
                with self.assertRaises(OSError):
                    _winapi.BatchedWaitForMultipleObjects(evts, False, 100)
                evts[i] = old_evt


    def test_few_events_waitall(self):
        self._events_waitall_test(16)

    def test_many_events_waitall(self):
        self._events_waitall_test(256)

    def test_max_events_waitall(self):
        self._events_waitall_test(MAXIMUM_BATCHED_WAIT_OBJECTS)


    def test_few_events_waitany(self):
        self._events_waitany_test(16)

    def test_many_events_waitany(self):
        self._events_waitany_test(256)

    def test_max_events_waitany(self):
        self._events_waitany_test(MAXIMUM_BATCHED_WAIT_OBJECTS)
