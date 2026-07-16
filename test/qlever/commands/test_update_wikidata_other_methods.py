import json
import os
import queue
import tempfile
import unittest
from threading import Event
from unittest.mock import patch

from qlever.commands.update_wikidata import (
    Batch,
    Outcome,
    PipelineRestart,
    Updater,
    UpdateWikidataCommand,
)
from test.qlever.commands.test_update_wikidata_execute import make_args

ENDPOINT = "http://localhost:7001"


def result_json():
    return json.dumps(
        {
            "operations": [
                {
                    "delta-triples": {
                        "after": {"inserted": 10, "deleted": 5, "total": 15},
                        "operation": {"inserted": 3, "deleted": 2, "total": 5},
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


class TestVerifyOffset(unittest.TestCase):
    def _updater(self, **over):
        command = UpdateWikidataCommand()
        args = make_args(**over)
        updater = Updater(command, args, ENDPOINT)
        updater.bind(queue.Queue(), Event())
        updater.first_batch = False  # so the check is not skipped
        return updater

    def test_equal_offset_ok(self):
        updater = self._updater()
        with patch(
            "qlever.commands.update_wikidata.get_next_offset_from_endpoint",
            return_value=200,
        ):
            self.assertTrue(updater._verify_offset(Batch(first_offset=200)))

    def test_endpoint_earlier_triggers_rewind(self):
        updater = self._updater(rewind_to_earlier_offset="yes")
        with patch(
            "qlever.commands.update_wikidata.get_next_offset_from_endpoint",
            return_value=150,
        ):
            with self.assertRaises(PipelineRestart) as cm:
                updater._verify_offset(Batch(first_offset=200))
        self.assertEqual(cm.exception.new_offset, 150)

    def test_endpoint_earlier_no_rewind_is_fatal(self):
        updater = self._updater(rewind_to_earlier_offset="no")
        with patch(
            "qlever.commands.update_wikidata.get_next_offset_from_endpoint",
            return_value=150,
        ):
            self.assertFalse(updater._verify_offset(Batch(first_offset=200)))

    def test_endpoint_later_is_fatal(self):
        updater = self._updater()
        with patch(
            "qlever.commands.update_wikidata.get_next_offset_from_endpoint",
            return_value=250,
        ):
            self.assertFalse(updater._verify_offset(Batch(first_offset=200)))

    def test_first_batch_skips_check(self):
        updater = self._updater()
        updater.first_batch = True
        # No endpoint query should happen; a mismatch would otherwise be fatal.
        with patch(
            "qlever.commands.update_wikidata.get_next_offset_from_endpoint",
            return_value=1,
        ) as m:
            self.assertTrue(updater._verify_offset(Batch(first_offset=200)))
        m.assert_not_called()

    def test_check_disabled_skips_check(self):
        updater = self._updater(check_offset_before_each_batch="no")
        with patch(
            "qlever.commands.update_wikidata.get_next_offset_from_endpoint",
            return_value=1,
        ) as m:
            self.assertTrue(updater._verify_offset(Batch(first_offset=200)))
        m.assert_not_called()


class TestUpdaterApply(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _prepare_update_file(self, offset, size):
        name = f"update.{offset}.{size}.sparql"
        with open(name, "w") as f:
            f.write("DELETE {} INSERT {} WHERE {}\n")
        with open(f"update.{offset}.{size}.meta", "w") as f:
            f.write("d - d")
        return name

    def _updater(self, **over):
        command = UpdateWikidataCommand()
        args = make_args(**over)
        updater = Updater(command, args, ENDPOINT)
        updater.bind(queue.Queue(), Event())
        return updater

    def test_apply_writes_result_and_builds_curl(self):
        name = self._prepare_update_file(100, 2)
        updater = self._updater()
        calls = []

        def fake_run_command(cmd, return_output=False, **kw):
            calls.append(cmd)
            return result_json()

        with patch(
            "qlever.commands.update_wikidata.run_command",
            side_effect=fake_run_command,
        ):
            ok = updater.apply(
                Batch(update_file_name=name, first_offset=100, batch_size=2)
            )

        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertIn(f"--data-binary @{name}", calls[0])
        self.assertIn("access-token=TOKEN", calls[0])
        self.assertIn("application/sparql-update", calls[0])
        self.assertTrue(os.path.exists("update.100.2.result"))

    def test_apply_retries_on_failed_request(self):
        name = self._prepare_update_file(100, 1)
        updater = self._updater()
        # first_batch True -> offset check skipped, so retry loop is clean.
        attempts = {"n": 0}

        def fake_run_command(cmd, return_output=False, **kw):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise Exception("connection refused")
            return result_json()

        with patch(
            "qlever.commands.update_wikidata.run_command",
            side_effect=fake_run_command,
        ):
            ok = updater.apply(
                Batch(update_file_name=name, first_offset=100, batch_size=1)
            )
        self.assertTrue(ok)
        self.assertEqual(attempts["n"], 2)

    def test_keep_update_requests_last(self):
        # Two offsets present; keep only the most recent (200).
        self._prepare_update_file(100, 1)
        name = self._prepare_update_file(200, 1)
        updater = self._updater(keep_update_requests="last")

        with patch(
            "qlever.commands.update_wikidata.run_command",
            return_value=result_json(),
        ):
            updater.apply(
                Batch(update_file_name=name, first_offset=200, batch_size=1)
            )

        # Offset 100 files pruned, offset 200 kept.
        self.assertFalse(os.path.exists("update.100.1.sparql"))
        self.assertTrue(os.path.exists("update.200.1.sparql"))
        self.assertTrue(os.path.exists("update.200.1.result"))

    def test_qlever_exception_is_not_fatal(self):
        name = self._prepare_update_file(100, 1)
        updater = self._updater()
        with patch(
            "qlever.commands.update_wikidata.run_command",
            return_value=json.dumps({"exception": "boom"}),
        ):
            ok = updater.apply(
                Batch(update_file_name=name, first_offset=100, batch_size=1)
            )
        self.assertTrue(ok)


class TestUpdaterRun(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_run_applies_then_finishes_on_eos(self):
        command = UpdateWikidataCommand()
        args = make_args()
        updater = Updater(command, args, ENDPOINT)
        q = queue.Queue()
        updater.bind(q, Event())

        with open("update.100.1.sparql", "w") as f:
            f.write("DELETE {} INSERT {} WHERE {}\n")
        q.put(
            Batch(
                update_file_name="update.100.1.sparql",
                first_offset=100,
                batch_size=1,
            )
        )
        q.put(Batch(end_of_stream=True, reason="num-messages"))

        with patch(
            "qlever.commands.update_wikidata.run_command",
            return_value=result_json(),
        ):
            outcome = updater.run()

        self.assertEqual(outcome, Outcome.FINISHED)
        self.assertEqual(updater.batch_count, 1)

    def test_run_fails_on_collector_error(self):
        command = UpdateWikidataCommand()
        args = make_args()
        updater = Updater(command, args, ENDPOINT)
        q = queue.Queue()
        updater.bind(q, Event())
        q.put(Batch(error=RuntimeError("collector blew up")))

        outcome = updater.run()
        self.assertEqual(outcome, Outcome.FAILED)


if __name__ == "__main__":
    unittest.main()
