import json
import os
import queue
import tempfile
import time
import unittest
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

from qlever.commands.update_wikidata import (
    Collector,
    UpdateWikidataCommand,
)

TOPIC = "test-topic"
OLD_DATE = "2020-01-01T00:00:00Z"


def make_args(**over):
    d = dict(
        sse_stream_url="http://sse.example/stream",
        topic=TOPIC,
        partition=0,
        verbose="no",
        wait_between_batches=0,
        num_retries=3,
        batch_size=100000,
        num_messages=None,
        lag_seconds=1,
        until=None,
        use_cached_sparql_queries=False,
        entity_prefix="wd:",
        entity_namespace="http://www.wikidata.org/entity/",
        access_token="TOKEN",
        check_offset_before_each_batch="yes",
        rewind_to_earlier_offset="yes",
        keep_update_requests="all",
        host_name="localhost",
        port=7001,
        buffer_size=10,
        since=None,
        offset=None,
        show=False,
        wikimedia_commons=False,
    )
    d.update(over)
    return SimpleNamespace(**d)


class FakeEvent:
    def __init__(self, type, data):
        self.type = type
        self.data = data


class FakeSource:
    def __init__(self, events):
        self._events = events
        self.closed = False

    def __iter__(self):
        return iter(self._events)

    def close(self):
        self.closed = True


def triple(n):
    return (
        f"<http://example.org/s{n}> "
        f"<http://example.org/p> "
        f"<http://example.org/o{n}> .\n"
    )


def make_event(
    offset,
    dt=OLD_DATE,
    operation="add",
    added=None,
    deleted=None,
    entity_id="Q1",
    rev_id=1,
    sequence=0,
):
    data = {
        "meta": {
            "topic": TOPIC,
            "offset": offset,
            "partition": 0,
            "dt": dt,
        },
        "entity_id": entity_id,
        "operation": operation,
        "rev_id": rev_id,
        "sequence": sequence,
    }
    if added is not None:
        data["rdf_added_data"] = {"data": added}
    if deleted is not None:
        data["rdf_deleted_data"] = {"data": deleted}
    return FakeEvent("message", json.dumps(data))


def make_connect(all_events):
    """A fake `connect_to_sse_stream` that models Kafka resume: it returns the
    events whose offset is >= the requested `event_id` offset."""

    def _connect(url, since=None, event_id=None):
        if event_id:
            start = event_id[0]["offset"]
            evs = [
                e
                for e in all_events
                if json.loads(e.data)["meta"]["offset"] >= start
            ]
        else:
            evs = list(all_events)
        return FakeSource(evs)

    return _connect


def drain(q):
    items = []
    try:
        while True:
            items.append(q.get_nowait())
    except queue.Empty:
        pass
    return items


class ChdirTmpTestCase(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()


class TestCollector(ChdirTmpTestCase):
    def _run_collector(
        self, args, events, start_offset=100, max_items=10, timeout=5
    ):
        command = UpdateWikidataCommand()
        q = queue.Queue(maxsize=args.buffer_size)
        stop_event = Event()
        collector = Collector(command, args, None, start_offset, q, stop_event)
        with patch(
            "qlever.commands.update_wikidata.connect_to_sse_stream",
            make_connect(events),
        ):
            t = Thread(target=collector.run, daemon=True)
            t.start()
            items = []
            end = time.time() + timeout
            while time.time() < end:
                try:
                    b = q.get(timeout=0.2)
                except queue.Empty:
                    if not t.is_alive():
                        break
                    continue
                items.append(b)
                if b.end_of_stream or b.error is not None:
                    break
                if len(items) >= max_items:
                    break
            stop_event.set()
            t.join(timeout=2)
        return items

    def test_single_batch_num_messages(self):
        events = [
            make_event(100, added=triple(1)),
            make_event(101, added=triple(2)),
        ]
        args = make_args(num_messages=2)
        items = self._run_collector(args, events)

        self.assertEqual(len(items), 2)
        batch, eos = items
        self.assertEqual(batch.first_offset, 100)
        self.assertEqual(batch.batch_size, 2)
        self.assertEqual(batch.update_file_name, "update.100.2.sparql")
        self.assertTrue(eos.end_of_stream)
        self.assertEqual(eos.reason, "num-messages")

        # The prepared file exists and contains both triples plus the
        # synthetic bookkeeping triple with the next offset (102).
        with open(batch.update_file_name) as f:
            content = f.read()
        self.assertIn("<http://example.org/s1>", content)
        self.assertIn("<http://example.org/s2>", content)
        self.assertIn("updateStreamNextOffset", content)
        self.assertIn('"102"', content)
        self.assertTrue(os.path.exists("update.100.2.meta"))

    def test_two_contiguous_batches(self):
        events = [
            make_event(100, added=triple(1)),
            make_event(101, added=triple(2)),
        ]
        args = make_args(num_messages=2, batch_size=1)
        items = self._run_collector(args, events)

        batches = [b for b in items if not b.end_of_stream]
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0].first_offset, 100)
        self.assertEqual(batches[0].batch_size, 1)
        # Contiguity: next batch starts exactly where the previous ended.
        self.assertEqual(
            batches[0].first_offset + batches[0].batch_size,
            batches[1].first_offset,
        )

    def test_until_finishes(self):
        events = [
            make_event(100, dt="2020-01-01T00:00:00Z", added=triple(1)),
            make_event(101, dt="2020-06-01T00:00:00Z", added=triple(2)),
        ]
        args = make_args(until="2020-03-01T00:00:00Z")
        items = self._run_collector(args, events)

        eos = items[-1]
        self.assertTrue(eos.end_of_stream)
        self.assertEqual(eos.reason, "until")
        # Only the first event (before --until) is in the batch.
        self.assertEqual(items[0].batch_size, 1)

    def test_lag_seconds_finishes_batch(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        events = [
            make_event(100, dt=OLD_DATE, added=triple(1)),
            make_event(101, dt=now, added=triple(2)),
        ]
        args = make_args(lag_seconds=100000)
        # Collector will not self-terminate here; grab the first batch only.
        items = self._run_collector(args, events, max_items=1)
        self.assertEqual(items[0].first_offset, 100)
        self.assertEqual(items[0].batch_size, 1)

    def test_delete_then_add_same_entity_splits_batch(self):
        events = [
            make_event(
                100, operation="delete", deleted=triple(1), entity_id="Q5"
            ),
            make_event(101, operation="add", added=triple(2), entity_id="Q5"),
        ]
        args = make_args(num_messages=2)
        items = self._run_collector(args, events)
        batches = [b for b in items if not b.end_of_stream]
        # The add for a just-deleted entity forces a batch boundary, so the
        # first batch has only the delete message.
        self.assertEqual(batches[0].batch_size, 1)
        self.assertEqual(batches[0].first_offset, 100)

    def test_cached_file_path(self):
        # Pre-create a cached update file for offset 100, size 3.
        with open("update.100.3.sparql", "w") as f:
            f.write("DELETE {} INSERT {} WHERE {}\n")
        with open("update.100.3.meta", "w") as f:
            f.write(f"{OLD_DATE} - {OLD_DATE}")
        args = make_args(
            use_cached_sparql_queries=True, num_messages=3, verbose="no"
        )
        items = self._run_collector(args, [], start_offset=100)
        batch = items[0]
        self.assertEqual(batch.update_file_name, "update.100.3.sparql")
        self.assertEqual(batch.first_offset, 100)
        self.assertEqual(batch.batch_size, 3)


class TestExecutePipeline(ChdirTmpTestCase):
    def _result_json(self):
        return json.dumps(
            {
                "operations": [
                    {
                        "delta-triples": {
                            "after": {
                                "inserted": 10,
                                "deleted": 5,
                                "total": 15,
                            },
                            "operation": {
                                "inserted": 3,
                                "deleted": 2,
                                "total": 5,
                            },
                        },
                        "time": {
                            "total": 50,
                            "planning": 5,
                            "execution": {
                                "computeIds": {"total": 5},
                                "evaluateWhere": 5,
                                "insertTriples": {"total": 5},
                                "deleteTriples": {"total": 5},
                            },
                            "updateMetadata": 5,
                        },
                    }
                ],
                "time": {
                    "parsing": 5,
                    "metadataUpdateForSnapshot": 5,
                    "snapshotCreation": 5,
                    "diskWriteback": 5,
                    "operations": 50,
                    "total": 100,
                },
            }
        )

    def test_execute_normal_run(self):
        events = [
            make_event(100, added=triple(1)),
            make_event(101, added=triple(2)),
        ]
        args = make_args(
            num_messages=2, batch_size=1, since="2020-01-01T00:00:00Z"
        )
        args.offset = 100

        command = UpdateWikidataCommand()
        run_command_calls = []

        def fake_run_command(cmd, return_output=False, **kw):
            run_command_calls.append(cmd)
            return self._result_json()

        with (
            patch(
                "qlever.commands.update_wikidata.connect_to_sse_stream",
                make_connect(events),
            ),
            patch(
                "qlever.commands.update_wikidata.run_command",
                side_effect=fake_run_command,
            ),
            patch(
                "qlever.commands.update_wikidata.get_next_offset_from_endpoint",
                return_value=101,
            ),
        ):
            ok = command.execute(args)

        self.assertTrue(ok)
        # Two update batches -> two curl POSTs.
        update_posts = [c for c in run_command_calls if "sparql-update" in c]
        self.assertEqual(len(update_posts), 2)
        self.assertTrue(os.path.exists("update.100.1.result"))
        self.assertTrue(os.path.exists("update.101.1.result"))


if __name__ == "__main__":
    unittest.main()
