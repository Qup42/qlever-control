from __future__ import annotations

import glob
import json
import logging
import os
import queue
import re
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from threading import Event, Thread

import rdflib.term
import requests_sse
from rdflib import Graph
from termcolor import colored
from tqdm.contrib.logging import tqdm_logging_redirect

from qlever.command import QleverCommand
from qlever.log import log
from qlever.util import run_command


# Monkey patch `rdflib.term._castLexicalToPython` to avoid casting of literals
# to Python types. We do not need it (all we want it convert Turtle to N-Triples),
# and we can speed up parsing by a factor of about 2.
def custom_cast_lexical_to_python(lexical, datatype):
    return None  # Your desired behavior


rdflib.term._castLexicalToPython = custom_cast_lexical_to_python


def connect_to_sse_stream(sse_stream_url, since=None, event_id=None):
    """
    Connect to the SSE stream and return the connected EventSource.

    Args:
        sse_stream_url: URL of the SSE stream
        since: ISO date string to start from (mutually exclusive with event_id)
        event_id: Event ID to resume from (mutually exclusive with since)

    Returns:
        The connected EventSource object
    """
    if event_id:
        event_id_json = json.dumps(event_id)
        source = requests_sse.EventSource(
            sse_stream_url,
            headers={
                "Accept": "text/event-stream",
                "User-Agent": "qlever update-wikidata",
                "Last-Event-ID": event_id_json,
            },
        )
    else:
        source = requests_sse.EventSource(
            sse_stream_url,
            params={"since": since} if since else {},
            headers={
                "Accept": "text/event-stream",
                "User-Agent": "qlever update-wikidata",
            },
        )

    source.connect()
    return source


def get_next_offset_from_endpoint(sparql_endpoint):
    """Query the endpoint for the next stream offset.

    Args:
        sparql_endpoint: URL of the SPARQL endpoint

    Returns:
        int: The offset value from the endpoint

    Raises:
        Exception: If the query fails or returns no results
    """
    sparql_query_offset = (
        "PREFIX wikibase: <http://wikiba.se/ontology#> "
        "SELECT (MAX(?offset) AS ?maxOffset) WHERE { "
        "<http://wikiba.se/ontology#Dump> "
        "wikibase:updateStreamNextOffset ?offset "
        "}"
    )
    curl_cmd_check_offset = (
        f"curl -s {sparql_endpoint}"
        f' -H "Accept: text/csv"'
        f' -H "Content-type: application/sparql-query"'
        f' --data "{sparql_query_offset}"'
    )
    result = run_command(
        f"{curl_cmd_check_offset} | sed 1d",
        return_output=True,
    ).strip()
    if not result:
        raise Exception("Query returned no results")
    return int(result.strip('"'))


class PipelineRestart(Exception):
    """
    Raised by the `Updater` when the SPARQL endpoint's offset is earlier than
    the batch about to be applied (i.e., the server was restarted and lost its
    delta triples). The orchestrator in `execute` catches this, tears down the
    current collector/updater pipeline, and restarts it from `new_offset`.
    """

    def __init__(self, new_offset: int):
        super().__init__(f"rewind to offset {new_offset}")
        self.new_offset = new_offset


class Outcome(Enum):
    """Result of running the `Updater` loop for one pipeline attempt."""

    FINISHED = (
        auto()
    )  # Normal completion (until / num-messages / Ctrl+C / ...)
    FAILED = auto()  # Fatal, `execute` should return `False`


@dataclass
class Batch:
    """
    One unit of work handed from the `Collector` to the `Updater` through the
    bounded queue. It carries only the *filename* of the prepared SPARQL update
    (already written to disk by the collector); the updater applies it with
    `curl --data-binary @file`, so the update text never needs to live in
    memory. Cached batches are just a `Batch` whose `update_file_name` points at
    an existing file.

    `end_of_stream` / `error` ride the same queue channel so the consumer's
    `get()` handling stays uniform: a sentinel batch signals normal completion
    (with `reason`), and a fatal collector error travels as `error`.
    """

    update_file_name: str | None = None
    first_offset: int | None = None
    batch_size: int = 0
    end_of_stream: bool = False
    reason: str | None = None
    error: BaseException | None = None


class Collector:
    """
    Producer side of the pipeline: connects to the SSE stream, aggregates events
    into one batch (or reads a cached update file), builds the SPARQL update,
    writes the `update.{offset}.{size}.sparql`/`.meta` files, and puts a `Batch`
    on the bounded queue. Runs in its own thread; `execute` drives it.

    The collector guarantees offset contiguity internally (it seeds each batch's
    first offset from its own resume cursor), so it never consults the endpoint
    offset. Offset verification against the endpoint lives in the `Updater`.
    """

    def __init__(
        self,
        command,
        args,
        since,
        start_offset,
        q: queue.Queue,
        stop_event: Event,
    ):
        self.command = command
        self.args = args
        self.since = since
        self.start_offset = start_offset
        self.q = q
        self.stop_event = stop_event
        # Number of batches assembled so far (for the "Assembled batch #N" log).
        self.assembled_count = 0

    def _put(self, batch: Batch) -> bool:
        """
        Put a batch on the queue, blocking when the buffer is full but still
        observing `stop_event` (so a teardown during a full-queue wait does not
        deadlock). Returns `False` if the collector was asked to stop.
        """
        while not self.stop_event.is_set():
            try:
                self.q.put(batch, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    def run(self):
        """Thread entry point: run the collection loop, delivering unexpected
        errors to the updater through the queue."""
        try:
            self._run()
        except Exception as e:
            self._put(Batch(error=e))

    def _run(self):
        args = self.args
        since = self.since
        total_num_messages = 0
        wait_before_next_batch = False
        finished = False
        event_id_for_next_batch = (
            [
                {
                    "topic": args.topic,
                    "partition": args.partition,
                    "offset": self.start_offset,
                }
            ]
            if self.start_offset is not None
            else None
        )

        # Main event loop: Either resume from `event_id_for_next_batch` (if set),
        # or start a new connection to `args.sse_stream_url` (with URL
        # parameter `?since=`).
        while True:
            if self.stop_event.is_set():
                return
            # Optionally wait before processing the next batch (make sure that
            # the wait is interruptible by Ctrl+C).
            if wait_before_next_batch:
                if args.verbose == "yes":
                    log.info(
                        f"Waiting {args.wait_between_batches} "
                        f"second{'s' if args.wait_between_batches > 1 else ''} "
                        f"before processing the next batch"
                    )
                    log.info("")
                wait_before_next_batch = False
                self.command.ctrl_c_pressed.wait(args.wait_between_batches)
            if self.command.ctrl_c_pressed.is_set():
                log.warn(
                    "\rCtrl+C pressed while waiting in between batches, "
                    "exiting"
                )
                self._put(Batch(end_of_stream=True, reason="ctrl-c"))
                return

            # Start stream from either `event_id_for_next_batch` or `since`.
            # We'll extract the offset for first_offset_in_batch later.
            if event_id_for_next_batch:
                event_id_json = json.dumps(event_id_for_next_batch)
                if args.verbose == "yes":
                    log.info(
                        colored(
                            f"Consuming stream from event ID: {event_id_json}",
                            attrs=["dark"],
                        )
                    )
            else:
                if args.verbose == "yes":
                    log.info(
                        colored(
                            f"Consuming stream from date: {since}",
                            attrs=["dark"],
                        )
                    )

            # Connect to the SSE stream with retry logic
            try:
                source = self.command.retry_with_backoff(
                    lambda: connect_to_sse_stream(
                        args.sse_stream_url,
                        since=since if not event_id_for_next_batch else None,
                        event_id=event_id_for_next_batch,
                    ),
                    "SSE stream connection for batch processing",
                    args.num_retries,
                )
            except KeyboardInterrupt:
                log.warn(
                    "\rCtrl+C pressed while while connecting to stream, "
                    "exiting"
                )
                self._put(Batch(end_of_stream=True, reason="ctrl-c"))
                return
            except Exception as e:
                log.error(
                    f"Failed to connect to SSE stream after "
                    f"{args.num_retries} retry attempts, last error: {e}"
                )
                self._put(
                    Batch(end_of_stream=True, reason="connection-failed")
                )
                return

            # Next comes the inner loop, which processes exactly one "batch" of
            # messages. The batch is completed (simply using `break`) when either
            # `args.batch_size` messages have been processed, or when one of a
            # variety of conditions occur (Ctrl+C pressed, message within
            # `args.lag_seconds` of current time, delete operation followed by
            # insert of triple with that entity as subject).

            # Initialize all the batch variables.
            current_batch_size = 0
            # Extract the offset from the event ID to use as the starting offset
            # for this batch. This is set before processing any messages.
            if event_id_for_next_batch:
                first_offset_in_batch = event_id_for_next_batch[0]["offset"]
                event_id_for_next_batch = None
            else:
                # This should not happen since we now always determine the offset
                # before starting, but keep as fallback
                first_offset_in_batch = None

            date_list = []
            delete_entity_ids = set()
            delta_to_now_list = []
            batch_assembly_start_time = time.perf_counter()
            # Maps each triple to the `(rev_id, sequence)` of the event that
            # last wrote it. The SSE stream may deliver events out of `rev_id`
            # order (notably during a Wikidata merge, where the source's
            # DELETE and the target's ADD for a transferred article URL share
            # the same `dt` and the target often arrives first). Tracking the
            # `rev_id` per triple makes the cross-set "remove from the other
            # side" step gated by causal order, so the chronologically last
            # event for a given triple wins regardless of arrival order.
            insert_triples: dict[str, tuple[int, int]] = {}
            delete_triples: dict[str, tuple[int, int]] = {}

            # Check if we can use a cached SPARQL query file
            use_cached_file = False
            cached_file_name = None
            if (
                args.use_cached_sparql_queries
                and first_offset_in_batch is not None
            ):
                cached_update = self.command.determine_next_cached_update(
                    first_offset_in_batch, args.batch_size
                )
                if cached_update is not None:
                    cached_file_name, current_batch_size = cached_update
                    use_cached_file = True

            # Process one event at a time (unless using cached file).
            if not use_cached_file:
                with tqdm_logging_redirect(
                    loggers=[logging.getLogger("qlever")],
                    desc="Batch",
                    total=args.batch_size,
                    leave=False,
                    bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}{postfix}",
                ) as pbar:
                    for event in self.command.iter_sse_events(source):
                        # Skip events that are not of type `message` (should not
                        # happen), have no field `data` (should not happen either), or
                        # where the topic is not in `args.topics` (one topic by itself
                        # should provide all relevant updates).
                        if event.type != "message" or not event.data:
                            continue
                        event_data = json.loads(event.data)
                        topic = event_data.get("meta").get("topic")
                        if topic != args.topic:
                            continue

                        try:
                            # Extract offset, topic, and partition from the message metadata
                            # to construct a precise event ID for resuming.
                            meta = event_data.get("meta")
                            offset = meta.get("offset")
                            topic = meta.get("topic")
                            partition = meta.get("partition")

                            # Get the date (rounded *down* to seconds).
                            date = meta.get("dt")
                            date = re.sub(r"\.\d*Z$", "Z", date)

                            # Get the other relevant fields from the message.
                            entity_id = event_data.get("entity_id")
                            operation = event_data.get("operation")
                            rdf_added_data = event_data.get("rdf_added_data")
                            rdf_deleted_data = event_data.get(
                                "rdf_deleted_data"
                            )
                            rdf_linked_shared_data = event_data.get(
                                "rdf_linked_shared_data"
                            )
                            # rdf_unlinked_shared_data = event_data.get(
                            #     "rdf_unlinked_shared_data"
                            # )

                            # Causal-order key for this event (see comment at
                            # the `insert_triples` / `delete_triples`
                            # initialization above).
                            rev_key = (
                                event_data.get("rev_id", 0),
                                event_data.get("sequence", 0),
                            )

                            # Check batch completion conditions BEFORE processing the
                            # data of this message. If any of the conditions is met,
                            # we finish the batch and resume from the LAST PROCESSED
                            # message (not the current one that triggered the break).
                            #
                            # NOTE: We will update event_id_for_next_batch AFTER
                            # successfully processing each message (see below), so that
                            # when we break, it contains the last processed event ID.
                            since = None

                            # Condition 1: Delete followed by insert for same entity.
                            operation_adds_data = (
                                rdf_added_data is not None
                                or rdf_linked_shared_data is not None
                            )
                            if (
                                operation_adds_data
                                and entity_id in delete_entity_ids
                            ):
                                if args.verbose == "yes":
                                    log.warn(
                                        f"Encountered operation that adds data for "
                                        f"an entity ID ({entity_id}) that was deleted "
                                        f"earlier in this batch; finishing batch and "
                                        f"resuming from this message in the next batch"
                                    )
                                break

                            # Condition 2: Batch size or limit on number of
                            # messages reached.
                            if current_batch_size >= args.batch_size or (
                                args.num_messages is not None
                                and total_num_messages >= args.num_messages
                            ):
                                break

                            # Condition 3: Message close to current time.
                            date_obj = datetime.strptime(
                                date, "%Y-%m-%dT%H:%M:%SZ"
                            ).replace(tzinfo=timezone.utc)
                            date_as_epoch_s = date_obj.timestamp()

                            now_as_epoch_s = time.time()
                            delta_to_now_s = now_as_epoch_s - date_as_epoch_s
                            if (
                                delta_to_now_s < args.lag_seconds
                                and current_batch_size > 0
                            ):
                                if args.verbose == "yes":
                                    log.warn(
                                        f"Encountered message with date {date}, which is within "
                                        f"{args.lag_seconds} "
                                        f"second{'s' if args.lag_seconds > 1 else ''} "
                                        f"of the current time, finishing the current batch"
                                    )
                                wait_before_next_batch = (
                                    args.wait_between_batches is not None
                                    and args.wait_between_batches > 0
                                )
                                break

                            # Condition 4: Reached `--until` date and at least one
                            # message was processed.
                            if (
                                args.until
                                and date >= args.until
                                and current_batch_size > 0
                            ):
                                log.warn(
                                    f"Reached --until date {args.until} "
                                    f"(message date: {date}), that's it folks"
                                )
                                finished = True
                                break

                            # Delete operations are postponed until the end of the
                            # batch, so remember the entity ID here.
                            if operation == "delete":
                                delete_entity_ids.add(entity_id)

                            # Replace each occurrence of `\\` by `\u005C\u005C`
                            # (which is twice the Unicode for backslash).
                            #
                            # NOTE: Strictly speaking, it would be enough to do
                            # this for two backslashes followed by a `u`, but
                            # doing it for all double backslashes does not
                            # harm. When parsing a SPARQL query, then according
                            # to the standar, first all occurrences of `\uxxxx`
                            # (where `xxxx` are four hex digits) are replaced
                            # by the corresponding Unicode character. That is a
                            # problem when `\\uxxxx` occurs in a literal,
                            # because then it would be replaced by `\` followed
                            # by the Unicode character, which is invalied
                            # SPARQL. The subsitution avoids that problem.
                            def node_to_sparql(node: rdflib.term.Node) -> str:
                                return node.n3().replace(
                                    "\\\\", "\\u005C\\u005C"
                                )

                            # Process the to-be-deleted triples.
                            #
                            # NOTE: The triples from `rdf_unlinked_shared_data`
                            # must not be deleted, because they are only
                            # unlinked from the current entity, but may still
                            # be linked from other entities. If they are not
                            # linked from any other entity, they will be
                            # orphaned, but we don't mind that.
                            for rdf_to_be_deleted in (rdf_deleted_data,):
                                if rdf_to_be_deleted is not None:
                                    try:
                                        rdf_to_be_deleted_data = (
                                            rdf_to_be_deleted.get("data")
                                        )
                                        graph = Graph()
                                        log.debug(
                                            f"RDF to_be_deleted data: {rdf_to_be_deleted_data}"
                                        )
                                        graph.parse(
                                            data=rdf_to_be_deleted_data,
                                            format="turtle",
                                        )
                                        for s, p, o in graph:
                                            triple = f"{s.n3()} {p.n3()} {node_to_sparql(o)}"
                                            # NOTE: In case there was a previous `insert` of that
                                            # triple, it is safe to remove that `insert`, but not
                                            # the `delete` (in case the triple is contained in the
                                            # original data). The cross-set override only happens
                                            # if this delete is strictly newer than the existing
                                            # insert; otherwise the existing insert dominates and
                                            # the (older) delete is discarded.
                                            if triple in insert_triples:
                                                if (
                                                    rev_key
                                                    > insert_triples[triple]
                                                ):
                                                    del insert_triples[triple]
                                                    delete_triples[triple] = (
                                                        rev_key
                                                    )
                                            elif (
                                                triple not in delete_triples
                                                or rev_key
                                                > delete_triples[triple]
                                            ):
                                                delete_triples[triple] = (
                                                    rev_key
                                                )
                                    except Exception as e:
                                        log.error(
                                            f"Error reading `rdf_to_be_deleted_data`: {e}"
                                        )
                                        source.close()
                                        self._put(Batch(error=e))
                                        return

                            # Process the to-be-added triples.
                            for rdf_to_be_added in (
                                rdf_added_data,
                                rdf_linked_shared_data,
                            ):
                                if rdf_to_be_added is not None:
                                    try:
                                        rdf_to_be_added_data = (
                                            rdf_to_be_added.get("data")
                                        )
                                        graph = Graph()
                                        log.debug(
                                            "RDF to be added data: {rdf_to_be_added_data}"
                                        )
                                        graph.parse(
                                            data=rdf_to_be_added_data,
                                            format="turtle",
                                        )
                                        for s, p, o in graph:
                                            triple = f"{s.n3()} {p.n3()} {node_to_sparql(o)}"
                                            # NOTE: In case there was a previous `delete` of that
                                            # triple, it is safe to remove that `delete`, but not
                                            # the `insert` (in case the triple is not contained in
                                            # the original data). Use `>=` on the cross-set gate
                                            # so that a same-event delete-then-add (deletes are
                                            # processed first in the per-event loop) lets the
                                            # add win, matching the previous within-event
                                            # behaviour.
                                            if triple in delete_triples:
                                                if (
                                                    rev_key
                                                    >= delete_triples[triple]
                                                ):
                                                    del delete_triples[triple]
                                                    insert_triples[triple] = (
                                                        rev_key
                                                    )
                                            elif (
                                                triple not in insert_triples
                                                or rev_key
                                                > insert_triples[triple]
                                            ):
                                                insert_triples[triple] = (
                                                    rev_key
                                                )
                                    except Exception as e:
                                        log.error(
                                            f"Error reading `rdf_to_be_added_data`: {e}"
                                        )
                                        source.close()
                                        self._put(Batch(error=e))
                                        return

                        except Exception as e:
                            log.error(f"Error reading data from message: {e}")
                            log.info(event)
                            continue

                        # Message was successfully processed, update batch tracking
                        current_batch_size += 1
                        total_num_messages += 1
                        pbar_update_frequency = 100
                        if (current_batch_size % pbar_update_frequency) == 0:
                            pbar.set_postfix(
                                {
                                    "Time": date_obj.strftime(
                                        "%Y-%m-%d %H:%M:%S"
                                    )
                                }
                            )
                            pbar.update(pbar_update_frequency)
                        log.debug(
                            f"DATE: {date_as_epoch_s:.0f} [{date}], "
                            f"NOW: {now_as_epoch_s:.0f}, "
                            f"DELTA: {now_as_epoch_s - date_as_epoch_s:.0f}"
                        )
                        date_list.append(date)
                        delta_to_now_list.append(delta_to_now_s)

                        # Update the event ID for the next batch. We increment the
                        # offset by 1 so that the next batch starts with the next
                        # message (not re-processing the current one).
                        event_id_for_next_batch = [
                            {
                                "topic": topic,
                                "partition": partition,
                                "offset": offset + 1,
                            }
                        ]

                        # Ctrl+C finishes the current batch (this should come at the
                        # end of the inner event loop so that always at least one
                        # message is processed).
                        if self.command.ctrl_c_pressed.is_set():
                            log.warn(
                                "\rCtrl+C pressed while processing a batch, "
                                "finishing it and exiting"
                            )
                            break
            else:
                # Using cached file - set batch size and calculate next offset
                total_num_messages += current_batch_size
                event_id_for_next_batch = [
                    {
                        "topic": args.topic,
                        "partition": args.partition,
                        "offset": first_offset_in_batch + current_batch_size,
                    }
                ]

            # For each batch we open a new connection (either from
            # `event_id_for_next_batch` or from `since`), so close it here.
            source.close()

            # If the stream died before any events were collected (e.g.,
            # HTTP 503), skip straight to reconnecting.
            if not use_cached_file and current_batch_size == 0:
                continue

            # Assemble the current batch of messages (or skip if using cached).
            self.assembled_count += 1
            if not use_cached_file:
                batch_assembly_end_time = time.perf_counter()
                batch_assembly_time_ms = int(
                    1000
                    * (batch_assembly_end_time - batch_assembly_start_time)
                )
                date_list.sort()
                delta_to_now_list.sort()
                min_delta_to_now_s = delta_to_now_list[0]
                if min_delta_to_now_s < 10:
                    min_delta_to_now_s = f"{min_delta_to_now_s:.1f}"
                else:
                    min_delta_to_now_s = f"{int(min_delta_to_now_s):,}"
                log.info(
                    f"Assembled batch #{self.assembled_count}, "
                    f"#messages: {current_batch_size:2,}, "
                    f"date range: {date_list[0]} - {date_list[-1]}  "
                    f"[assembly time: {batch_assembly_time_ms:3,}ms, "
                    f"min delta to NOW: {min_delta_to_now_s}s]"
                )

                # Add a triples `wikibase:Dump wikibase:updatesCompleteUntil
                # DATE` and `wikibase:Dump wikibase:updateStreamNextOffset
                # OFFSET`. These are batch-level synthetic triples that
                # never conflict with stream events, so the rev_key value
                # stored alongside them is irrelevant — `(0, 0)` is fine.
                insert_triples[
                    f"<http://wikiba.se/ontology#Dump> "
                    f"<http://wikiba.se/ontology#updatesCompleteUntil> "
                    f'"{date_list[-1]}"'
                    f"^^<http://www.w3.org/2001/XMLSchema#dateTime>"
                ] = (0, 0)
                insert_triples[
                    "<http://wikiba.se/ontology#Dump> "
                    "<http://wikiba.se/ontology#updateStreamNextOffset> "
                    f'"{event_id_for_next_batch[0]["offset"]}"'
                ] = (0, 0)

                # Construct UPDATE operation.
                delete_block = " . \n  ".join(delete_triples)
                insert_block = " . \n  ".join(insert_triples)
                delete_insert_operation = (
                    f"DELETE {{\n  {delete_block} \n}} "
                    f"INSERT {{\n  {insert_block} \n}} "
                    f"WHERE {{ }}\n"
                )

                # If `delete_entity_ids` is non-empty, add a `DELETE WHERE`
                # operation that deletes all triples that are associated with only
                # those entities.
                delete_entity_ids_as_values = " ".join(
                    [f"{args.entity_prefix}{qid}" for qid in delete_entity_ids]
                )
                if len(delete_entity_ids) > 0:
                    delete_where_operation = (
                        f"PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
                        f"PREFIX wikibase: <http://wikiba.se/ontology#>\n"
                        f"PREFIX {args.entity_prefix.rstrip(':')}: <{args.entity_namespace}>\n"
                        f"DELETE {{\n"
                        f"  ?s ?p ?o .\n"
                        f"}} WHERE {{\n"
                        f"  {{\n"
                        f"    VALUES ?s {{ {delete_entity_ids_as_values} }}\n"
                        f"    ?s ?p ?o .\n"
                        f"  }} UNION {{\n"
                        f"    VALUES ?_1 {{ {delete_entity_ids_as_values} }}\n"
                        f"    ?_1 ?_2 ?s .\n"
                        f"    ?s ?p ?o .\n"
                        f"    ?s rdf:type wikibase:Statement .\n"
                        f"  }}\n"
                        f"}}\n"
                    )
                    delete_insert_operation += ";\n" + delete_where_operation

            # For batch size 1, the operation could be sent via
            # `--data-urlencode`, but we always write the update to a file and
            # send it via `--data-binary` (see the `Updater`).
            if use_cached_file:
                # Use the cached file instead of writing a new one
                update_arg_file_name = cached_file_name
            else:
                # Refuse to write `update.None.*.sparql`: the offset must
                # be a real integer here, otherwise the filename would
                # later break the cleanup pass (`int("None")`).
                if first_offset_in_batch is None:
                    log.error(
                        "Internal error: `first_offset_in_batch` is None "
                        "when trying to write the update file; refusing "
                        "to produce `update.None.*.sparql`. Please report."
                    )
                    self._put(
                        Batch(
                            error=RuntimeError(
                                "`first_offset_in_batch` is None"
                            )
                        )
                    )
                    return
                # Write the constructed SPARQL update to a file
                update_arg_file_name = f"update.{first_offset_in_batch}.{current_batch_size}.sparql"
                with open(update_arg_file_name, "w") as f:
                    f.write(delete_insert_operation)
                # Write metadata file with date range
                meta_file_name = (
                    f"update.{first_offset_in_batch}.{current_batch_size}.meta"
                )
                with open(meta_file_name, "w") as f:
                    f.write(f"{date_list[0]} - {date_list[-1]}")

            # Hand the prepared batch to the updater (blocks when the buffer is
            # full). If the pipeline is being torn down, stop.
            if not self._put(
                Batch(
                    update_file_name=update_arg_file_name,
                    first_offset=first_offset_in_batch,
                    batch_size=current_batch_size,
                )
            ):
                return

            # If Ctrl+C was pressed, we reached `--until`, or we processed
            # exactly `--num-messages`, finish: emit the end-of-stream sentinel.
            if (
                self.command.ctrl_c_pressed.is_set()
                or finished
                or (
                    args.num_messages is not None
                    and total_num_messages >= args.num_messages
                )
            ):
                reason = (
                    "ctrl-c"
                    if self.command.ctrl_c_pressed.is_set()
                    else ("until" if finished else "num-messages")
                )
                self._put(Batch(end_of_stream=True, reason=reason))
                return


class Updater:
    """
    Consumer side of the pipeline: takes a prepared `Batch` off the queue,
    verifies the endpoint offset (moved here from the collection path — this is
    the only place where `endpoint_offset == batch.first_offset` is guaranteed,
    because a single consumer applies batches strictly in FIFO order), applies
    the update via `curl`, writes the `.result` file, prunes old update files,
    and parses/logs the JSON statistics. Runs in the main thread.

    Constructed once and reused across pipeline restarts (via `bind`) so the
    running totals persist, matching the pre-refactor behavior.
    """

    def __init__(self, command, args, sparql_endpoint):
        self.command = command
        self.args = args
        self.sparql_endpoint = sparql_endpoint
        # Running totals (persist across pipeline restarts).
        self.batch_count = 0
        self.total_update_time = 0
        self.start_time = time.perf_counter()
        # Per-attempt state, (re)set by `bind`.
        self.q = None
        self.stop_event = None
        self.first_batch = True

    def bind(self, q: queue.Queue, stop_event: Event):
        """Attach a fresh queue + stop event for one pipeline attempt and reset
        the first-batch flag (the first batch after a restart is again seeded at
        the endpoint offset, so its offset check is skipped)."""
        self.q = q
        self.stop_event = stop_event
        self.first_batch = True

    def _get(self) -> Batch:
        """Block until a batch is available, polling so a Ctrl+C (which makes the
        collector emit an end-of-stream sentinel) is observed promptly."""
        while True:
            try:
                return self.q.get(timeout=0.5)
            except queue.Empty:
                continue

    def run(self) -> Outcome:
        """Consume and apply batches until end-of-stream or a fatal error. May
        raise `PipelineRestart` (caught by the orchestrator in `execute`)."""
        while True:
            batch = self._get()
            if batch.error is not None:
                log.error(f"Collection failed: {batch.error}")
                return Outcome.FAILED
            if batch.end_of_stream:
                return Outcome.FINISHED
            try:
                applied = self.apply(batch)
            except KeyboardInterrupt:
                log.warn(
                    "\r  \nCtrl+C pressed while executing update, exiting"
                )
                return Outcome.FINISHED
            if not applied:
                return Outcome.FAILED
            self.batch_count += 1
            self.first_batch = False

    def _verify_offset(self, batch: Batch) -> bool:
        """
        Check that the endpoint's next offset matches the batch about to be
        applied. Returns `True` if the batch may be applied, `False` on a fatal
        mismatch. Raises `PipelineRestart` when the endpoint offset is earlier
        than the batch (server restart) and rewinding is enabled.
        """
        args = self.args
        if (
            args.check_offset_before_each_batch != "yes"
            or self.first_batch
            or batch.first_offset is None
        ):
            return True

        # Verify offset with retry logic
        try:
            endpoint_offset = self.command.retry_with_backoff(
                lambda: get_next_offset_from_endpoint(self.sparql_endpoint),
                "Offset verification",
                args.num_retries,
            )
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.error(
                f"Failed to retrieve offset from endpoint "
                f"after {args.num_retries} retries: {e}. "
                f"This might be the first update, or the offset triple is missing."
            )
            return False

        if endpoint_offset < batch.first_offset:
            # Stream offset is LATER than endpoint offset
            if args.rewind_to_earlier_offset == "yes":
                log.info(
                    colored(
                        f"Stream offset {batch.first_offset} is later "
                        f"than offset {endpoint_offset} from endpoint; "
                        f"this can happen after a server restart; "
                        f"rewinding to offset {endpoint_offset} from endpoint",
                        "cyan",
                    )
                )
                log.info("")
                raise PipelineRestart(endpoint_offset)
            else:
                log.error(
                    f"Offset mismatch: stream offset {batch.first_offset} "
                    f"is later than offset {endpoint_offset} from endpoint; "
                    f"rewind disabled by --rewind-to-earlier-offset=no"
                )
                return False
        elif endpoint_offset > batch.first_offset:
            # Stream offset is EARLIER than endpoint offset - this is bad
            log.error(
                f"Offset mismatch: stream offset {batch.first_offset} "
                f"is earlier than offset {endpoint_offset} from endpoint; "
                f"this indicates that updates may have been applied "
                f"out of order or some updates are missing"
            )
            return False
        return True

    def apply(self, batch: Batch) -> bool:
        """
        Apply one prepared batch against the SPARQL endpoint. Returns `True` on
        success, `False` on a fatal error. Raises `PipelineRestart` on a server
        restart and `KeyboardInterrupt` on Ctrl+C during application.
        """
        args = self.args
        sparql_endpoint = self.sparql_endpoint

        # Verify the offset and send the UPDATE request. If the request fails,
        # re-verify the offset (which detects a server restart and rewinds) and
        # retry the same, already-prepared batch.
        while True:
            if not self._verify_offset(batch):
                return False

            # Construct curl command and send the update via `--data-binary`.
            curl_cmd = (
                f"curl -s -X POST"
                f' "{sparql_endpoint}?access-token={args.access_token}"'
                f" -H 'Content-Type: application/sparql-update'"
            )
            curl_cmd += f" --data-binary @{batch.update_file_name}"
            if args.verbose == "yes":
                log.info(colored(curl_cmd, "blue"))

            try:
                result = run_command(curl_cmd, return_output=True)
                break
            except Exception:
                if self.command.ctrl_c_pressed.is_set():
                    raise KeyboardInterrupt()
                else:
                    log.warn(
                        "\r  \nUpdate request failed; will re-verify and retry"
                    )
                    continue

        result_file_name = (
            f"update.{batch.first_offset}.{batch.batch_size}.result"
        )
        with open(result_file_name, "w") as f:
            f.write(result)

        # Clean up old update request files according to --keep-update-requests
        if args.keep_update_requests != "all":
            # Find all update.*.{sparql,meta,result} files
            update_files = {}
            for ext in ["sparql", "meta", "result"]:
                for file_path in glob.glob(f"update.*.*.{ext}"):
                    # Extract offset from filename (update.OFFSET.SIZE.ext).
                    # Skip files whose middle component is not a numeric
                    # offset (e.g. a stale `update.None.*.sparql` from a
                    # prior crashed run), so they cannot derail `int(x)`
                    # below.
                    parts = Path(file_path).stem.split(".")
                    if len(parts) >= 3 and parts[1].isdigit():
                        offset = parts[1]
                        if offset not in update_files:
                            update_files[offset] = []
                        update_files[offset].append(file_path)

            # Sort by offset (newest last)
            sorted_offsets = sorted(update_files.keys(), key=lambda x: int(x))

            # Determine which to keep
            if args.keep_update_requests == "none":
                files_to_keep = []
            elif args.keep_update_requests == "last":
                files_to_keep = (
                    update_files[sorted_offsets[-1]] if sorted_offsets else []
                )
            elif args.keep_update_requests == "last-three":
                files_to_keep = []
                for offset in sorted_offsets[-3:]:
                    files_to_keep.extend(update_files[offset])

            # Delete files not in the keep list
            for offset, files in update_files.items():
                for file_path in files:
                    if file_path not in files_to_keep:
                        try:
                            os.remove(file_path)
                        except Exception:
                            pass  # Ignore errors during cleanup

        # Results should be a JSON, parse it.
        try:
            result = json.loads(result)
        except Exception as e:
            log.error(
                f"Error parsing JSON result: {e}. "
                f"The first 1000 characters are: {result[:1000]}"
            )
            return False

        # Check if the result contains a QLever exception.
        if "exception" in result:
            error_msg = result["exception"]
            log.error(f"QLever exception: {error_msg}")
            log.info("")
            return True

        # Helper function for getting the value of `stats["time"][...]`
        # without the "ms" suffix. If the extraction fails, return 0

        # (and optionally log the failure).
        class FailureMode(Enum):
            LOG_ERROR = auto()
            SILENTLY_RETURN_ZERO = auto()
            THROW_EXCEPTION = auto()

        def get_time_ms(
            stats, *keys: str, failure_mode=FailureMode.LOG_ERROR
        ) -> int:
            try:
                value = stats["time"]
                for key in keys:
                    value = value[key]
                value = int(value)
            except Exception:
                if failure_mode == FailureMode.THROW_EXCEPTION:
                    raise
                elif failure_mode == FailureMode.LOG_ERROR:
                    log.error(
                        f"Error extracting time from JSON statistics, "
                        f"keys: {keys}"
                    )
                value = 0
            return value

        # Check for old JSON format (no `operations` or `time` on top level).
        old_json_message_template = (
            "Result JSON does not contain `{}` field, you are "
            "probably using an old version of QLever"
        )
        for field in ["operations", "time"]:
            if field not in result:
                raise RuntimeError(old_json_message_template.format(field))

        # Get the per-operation statistics.
        for i, stats in enumerate(result["operations"]):
            try:
                ins_after = stats["delta-triples"]["after"]["inserted"]
                del_after = stats["delta-triples"]["after"]["deleted"]
                ops_after = stats["delta-triples"]["after"]["total"]
                num_ins = int(stats["delta-triples"]["operation"]["inserted"])
                num_del = int(stats["delta-triples"]["operation"]["deleted"])
                num_ops = int(stats["delta-triples"]["operation"]["total"])
                time_op_total = get_time_ms(stats, "total")
                time_us_per_op = (
                    int(1000 * time_op_total / num_ops) if num_ops > 0 else 0
                )
                if args.verbose == "yes":
                    log.info(
                        colored(
                            f"TRIPLES: {num_ops:+10,} -> {ops_after:10,}, "
                            f"INS: {num_ins:+10,} -> {ins_after:10,}, "
                            f"DEL: {num_del:+10,} -> {del_after:10,}, "
                            f"TIME: {time_op_total:7,}ms, "
                            f"TIME/TRIPLE: {time_us_per_op:6,}µs",
                            attrs=["bold"],
                        )
                    )

                time_planning = get_time_ms(stats, "planning")
                time_compute_ids = get_time_ms(
                    stats,
                    "execution",
                    "computeIds",
                    "total",
                )
                time_where = get_time_ms(
                    stats,
                    "execution",
                    "evaluateWhere",
                )
                time_metadata = get_time_ms(
                    stats,
                    "updateMetadata",
                )
                time_insert = get_time_ms(
                    stats,
                    "execution",
                    "insertTriples",
                    "total",
                    failure_mode=FailureMode.SILENTLY_RETURN_ZERO,
                )
                time_delete = get_time_ms(
                    stats,
                    "execution",
                    "deleteTriples",
                    "total",
                    failure_mode=FailureMode.SILENTLY_RETURN_ZERO,
                )
                # Time spent in `consolidateAll()` after the buffer
                # push_backs. Only emitted by servers built with the
                # `SortedLocatedTriplesVector` block layout (the
                # `update/replaceSet` change); zero on older servers.
                time_consolidate = get_time_ms(
                    stats,
                    "execution",
                    "consolidateSortedDeltaTriples",
                    failure_mode=FailureMode.SILENTLY_RETURN_ZERO,
                )
                time_unaccounted = time_op_total - (
                    time_planning
                    + time_compute_ids
                    + time_where
                    + time_metadata
                    + time_delete
                    + time_insert
                    + time_consolidate
                )
                if args.verbose == "yes":
                    log.info(
                        f"METADATA: {100 * time_metadata / time_op_total:2.0f}%, "
                        f"PLANNING: {100 * time_planning / time_op_total:2.0f}%, "
                        f"WHERE: {100 * time_where / time_op_total:2.0f}%, "
                        f"IDS: {100 * time_compute_ids / time_op_total:2.0f}%, "
                        f"DELETE: {100 * time_delete / time_op_total:2.0f}%, "
                        f"INSERT: {100 * time_insert / time_op_total:2.0f}%, "
                        f"CONSOLIDATE: {100 * time_consolidate / time_op_total:2.0f}%, "
                        f"UNACCOUNTED: {100 * time_unaccounted / time_op_total:2.0f}%",
                    )

            except Exception as e:
                log.warn(
                    f"Error extracting statistics: {e}, "
                    f"curl command was: {curl_cmd}"
                )
                # Show traceback for debugging.
                import traceback

                traceback.print_exc()
                log.info("")
                continue

        # Get times for the whole request (not per operation).
        time_parsing = get_time_ms(
            result,
            "parsing",
        )
        time_metadata = get_time_ms(
            result,
            "metadataUpdateForSnapshot",
        )
        time_snapshot = get_time_ms(
            result,
            "snapshotCreation",
        )
        time_writeback = get_time_ms(
            result,
            "diskWriteback",
        )
        time_operations = get_time_ms(
            result,
            "operations",
        )
        time_total = get_time_ms(
            result,
            "total",
        )
        time_unaccounted = time_total - (
            time_parsing
            + time_metadata
            + time_snapshot
            + time_writeback
            + time_operations
        )

        # Update the totals.
        self.total_update_time += time_total / 1000.0
        total_elapsed_time = time.perf_counter() - self.start_time

        # Show statistics for the completed batch.
        if args.verbose == "yes":
            log.info(
                colored(
                    f"TOTAL UPDATE TIME SO FAR: {self.total_update_time:4.0f}s, "
                    f"TOTAL ELAPSED TIME SO FAR: {total_elapsed_time:4.0f}s, "
                    f"TOTAL TIME FOR THIS UPDATE REQUEST: {time_total:7,}ms, ",
                    attrs=["bold"],
                )
            )
            log.info(
                f"PARSING: {100 * time_parsing / time_total:2.0f}%, "
                f"OPERATIONS: {100 * time_operations / time_total:2.0f}%, "
                f"METADATA: {100 * time_metadata / time_total:2.0f}%, "
                f"SNAPSHOT: {100 * time_snapshot / time_total:2.0f}%, "
                f"WRITEBACK: {100 * time_writeback / time_total:2.0f}%, "
                f"UNACCOUNTED: {100 * time_unaccounted / time_total:2.0f}%",
            )
            log.info("")

        return True


class UpdateWikidataCommand(QleverCommand):
    """
    Class for executing the `update` command.
    """

    def __init__(self):
        # SPARQL query to get the date until which the updates of the
        # SPARQL endpoint are complete.
        self.sparql_updates_complete_until_query = (
            "PREFIX wikibase: <http://wikiba.se/ontology#> "
            "PREFIX schema: <http://schema.org/> "
            "SELECT * WHERE { "
            "{ SELECT (MIN(?date_modified) AS ?updates_complete_until) { "
            "wikibase:Dump schema:dateModified ?date_modified } } "
            "UNION { wikibase:Dump wikibase:updatesCompleteUntil ?updates_complete_until } "
            "} ORDER BY DESC(?updates_complete_until) LIMIT 1"
        )
        # URL of the Wikidata SSE stream.
        self.wikidata_update_stream_url = (
            "https://stream.wikimedia.org/v2/"
            "stream/rdf-streaming-updater.mutation.v2"
        )
        # Remember if Ctrl+C was pressed, so we can handle it gracefully.
        self.ctrl_c_pressed = Event()

    def description(self) -> str:
        return "Update from given SSE stream"

    def should_have_qleverfile(self) -> bool:
        return True

    def relevant_qleverfile_arguments(self) -> dict[str, list[str]]:
        return {"server": ["host_name", "port", "access_token"]}

    def additional_arguments(self, subparser) -> None:
        subparser.add_argument(
            "sse_stream_url",
            nargs="?",
            type=str,
            default=None,
            help="URL of the SSE stream to update from (default: the"
            " Wikidata stream, or the Wikimedia Commons stream with"
            " `--wikimedia-commons`)",
        )
        subparser.add_argument(
            "--batch-size",
            type=int,
            default=100000,
            help="Group this many messages together into one update "
            "(default: one update for each message); NOTE: this simply "
            "concatenates the `rdf_added_data` and `rdf_deleted_data` fields, "
            "which is not 100%% correct; as soon as chaining is supported, "
            "this will be fixed",
        )
        subparser.add_argument(
            "--lag-seconds",
            type=int,
            default=1,
            help="When a message is encountered that is within this many "
            "seconds of the current time, finish the current batch "
            "(and show a warning that this happened)",
        )
        subparser.add_argument(
            "--since",
            type=str,
            help="Consume stream messages since this date "
            "(default: determine automatically from the SPARQL endpoint)",
        )
        subparser.add_argument(
            "--until",
            type=str,
            help="Stop consuming stream messages when reaching this date "
            "(default: continue indefinitely)",
        )
        subparser.add_argument(
            "--offset",
            type=int,
            help="Consume stream messages starting from this offset "
            "(default: not set)",
        )
        subparser.add_argument(
            "--topic",
            type=str,
            default=None,
            help="The topic to consume from the SSE stream (default: "
            "eqiad.rdf-streaming-updater.mutation, or "
            "eqiad.mediainfo-streaming-updater.mutation with "
            "`--wikimedia-commons`)",
        )
        subparser.add_argument(
            "--partition",
            type=int,
            default=0,
            help="The partition to consume from the SSE stream (default: 0)",
        )
        subparser.add_argument(
            "--entity-prefix",
            type=str,
            default=None,
            help="Prefix used to format entity IDs in the entity-delete"
            " operation (default: wd:, or sdc: with `--wikimedia-commons`)",
        )
        subparser.add_argument(
            "--entity-namespace",
            type=str,
            default=None,
            help="IRI namespace bound to `--entity-prefix` in the entity-delete"
            " operation (default: http://www.wikidata.org/entity/, or"
            " https://commons.wikimedia.org/entity/ with `--wikimedia-commons`)",
        )
        subparser.add_argument(
            "--wikimedia-commons",
            action="store_true",
            default=False,
            help="Update from the Wikimedia Commons (mediainfo) stream"
            " instead of Wikidata; sets `sse_stream_url`, `--topic`,"
            " `--entity-prefix`, and `--entity-namespace` to their"
            " Commons-appropriate values, unless those are overridden"
            " explicitly",
        )
        subparser.add_argument(
            "--wait-between-batches",
            type=int,
            default=5,
            help="Wait this many seconds between batches that were "
            "finished due to a message that is within `lag_seconds` of "
            "the current time (default: 5 seconds)",
        )
        subparser.add_argument(
            "--num-messages",
            type=int,
            help="Process exactly this many messages and then exit "
            "(default: no bound on the number of messages)",
        )
        subparser.add_argument(
            "--buffer-size",
            type=int,
            default=2,
            help="Number of prepared update batches the collector may run "
            "ahead of the updater (the bounded buffer size). Larger values "
            "speed up the catch-up phase (default: 2)",
        )
        subparser.add_argument(
            "--verbose",
            choices=["no", "yes"],
            default="yes",
            help='Verbose logging, "yes" or "no" (default: "yes")',
        )
        subparser.add_argument(
            "--use-cached-sparql-queries",
            action="store_true",
            help="Use cached SPARQL query files if they exist with matching "
            "offset and target batch size (default: off)",
        )
        subparser.add_argument(
            "--check-offset-before-each-batch",
            choices=["yes", "no"],
            default="yes",
            help="Before each batch, verify that the stream offset matches the "
            "offset from the endpoint (default: yes)",
        )
        subparser.add_argument(
            "--rewind-to-earlier-offset",
            choices=["yes", "no"],
            default="yes",
            help="When the stream offset is later than the offset from the "
            "endpoint (e.g., after a server restart), rewind to the endpoint "
            "offset and reprocess messages (default: yes)",
        )
        subparser.add_argument(
            "--num-retries",
            type=int,
            default=10,
            help="Number of retries for offset verification queries when they fail "
            "(default: 10)",
        )
        subparser.add_argument(
            "--keep-update-requests",
            choices=["none", "all", "last", "last-three"],
            default="last",
            help="Which update request files (update.*.{sparql,meta,result}) to keep: "
            "none (delete all), all (keep all), last (keep only the most recent), "
            "last-three (keep the three most recent) (default: last)",
        )

    def retry_with_backoff(self, operation, operation_name, max_retries):
        """
        Retry an operation with exponential backoff, see backoff intervals below
        (in seconds). Returns the result of the operation if successful, or raises
        the last exception.
        """
        backoff_intervals = [5, 10, 30, 60, 300, 900, 1800, 3600]

        for attempt in range(max_retries):
            try:
                return operation()
            except Exception as e:
                if self.ctrl_c_pressed.is_set():
                    raise KeyboardInterrupt()
                if attempt < max_retries - 1:
                    # Use the appropriate backoff interval (once we get to the end
                    # of the list, keep using the last interval).
                    retry_delay = (
                        backoff_intervals[attempt]
                        if attempt < len(backoff_intervals)
                        else backoff_intervals[-1]
                    )
                    # Show the delay as seconds, minutes, or hours.
                    if retry_delay >= 3600:
                        delay_str = f"{retry_delay // 3600}h"
                    elif retry_delay >= 60:
                        delay_str = f"{retry_delay // 60}min"
                    else:
                        delay_str = f"{retry_delay}s"
                    log.warn(
                        f"{operation_name} failed (attempt {attempt + 1}/{max_retries}): {e}. "
                        f"Retrying in {delay_str} ..."
                    )
                    # Returns true if the wait ended because of the flag being set.
                    if self.ctrl_c_pressed.wait(timeout=retry_delay):
                        raise KeyboardInterrupt()
                else:
                    # If this was the last attempt, re-raise the exception.
                    raise

    # Handle Ctrl+C gracefully by finishing the current batch and then exiting.
    def handle_ctrl_c(self, signal_received, frame):
        if self.ctrl_c_pressed.is_set():
            pass
            # log.warn("\rCtrl+C pressed again, watch your blood pressure")
        else:
            self.ctrl_c_pressed.set()

    @staticmethod
    def iter_sse_events(source):
        """
        Yield events from the SSE stream. If the stream connection drops (e.g.
        HTTP 503), log a warning and stop the iteration so the caller can
        reconnect.
        """
        try:
            yield from source
        except Exception as e:
            log.warn(f"SSE stream connection lost ({e}), will reconnect ...")

    def determine_batch_size_for_cached_update(
        self, offset: int, batch_size: int
    ) -> int | None:
        options = list(Path.cwd().glob(f"update.{offset}.*.sparql"))
        if len(options) == 0:
            log.warn(
                "Found no cached SPARQL update. Continuing with update stream."
            )
            return None
        elif len(options) > 1:
            log.warn(
                f"Found {len(options)} candidates for cached SPARQL update. Using {options[0].name}."
            )
        return int(
            re.search(r"update\.\d+\.(\d+)\.sparql", options[0].name).group(1)
        )

    def determine_next_cached_update(
        self, first_offset_in_batch: int, batch_size: int
    ) -> tuple[str, int] | None:
        batch_size = self.determine_batch_size_for_cached_update(
            first_offset_in_batch, batch_size
        )
        if batch_size is None:
            return None
        cached_file_name = (
            f"update.{first_offset_in_batch}.{batch_size}.sparql"
        )
        cached_meta_file_name = (
            f"update.{first_offset_in_batch}.{batch_size}.meta"
        )

        # Try to read metadata file for date range
        cached_date_range = None
        if os.path.exists(cached_meta_file_name):
            try:
                with open(cached_meta_file_name, "r") as f:
                    cached_date_range = f.read().strip()
            except Exception:
                pass

        log_msg = f"Using cached SPARQL query file: {cached_file_name}"
        if cached_date_range:
            log_msg += f" [date range: {cached_date_range}]"
        log.debug(colored(log_msg, "cyan"))

        return cached_file_name, batch_size

    def execute(self, args) -> bool:
        # Resolve the four args that depend on `--wikimedia-commons`. A
        # `None` value means the user did not pass that arg, so fill in
        # the source-appropriate default; any explicit value wins.
        commons_defaults = {
            "sse_stream_url": "https://stream.wikimedia.org/v2/stream/"
            "mediainfo-streaming-updater.mutation.v2",
            "topic": "eqiad.mediainfo-streaming-updater.mutation",
            "entity_prefix": "sdc:",
            "entity_namespace": "https://commons.wikimedia.org/entity/",
        }
        wikidata_defaults = {
            "sse_stream_url": self.wikidata_update_stream_url,
            "topic": "eqiad.rdf-streaming-updater.mutation",
            "entity_prefix": "wd:",
            "entity_namespace": "http://www.wikidata.org/entity/",
        }
        defaults = (
            commons_defaults if args.wikimedia_commons else wikidata_defaults
        )
        for arg_name, default_value in defaults.items():
            if getattr(args, arg_name) is None:
                setattr(args, arg_name, default_value)

        # cURL command to get the date until which the updates of the
        # SPARQL endpoint are complete.
        sparql_endpoint = f"http://{args.host_name}:{args.port}"
        curl_cmd_updates_complete_until = (
            f"curl -s {sparql_endpoint}"
            f' -H "Accept: text/csv"'
            f' -H "Content-type: application/sparql-query"'
            f' --data "{self.sparql_updates_complete_until_query}"'
        )

        # Construct the command and show it.
        cmd_description = []
        if args.since:
            cmd_description.append(f"SINCE={args.since}")
        else:
            cmd_description.append(
                f"SINCE=$({curl_cmd_updates_complete_until} | sed 1d)"
            )
        if args.until:
            cmd_description.append(f"UNTIL={args.until}")
        cmd_description.append(
            f"Process SSE stream from {args.sse_stream_url} "
            f"in batches of up to {args.batch_size:,} messages "
        )
        self.show("\n".join(cmd_description), only_show=args.show)
        if args.show:
            return True

        # Compute the `since` date if not given.
        if args.since:
            since = args.since
        else:
            try:
                since = run_command(
                    f"{curl_cmd_updates_complete_until} | sed 1d",
                    return_output=True,
                ).strip()
            except Exception as e:
                log.error(
                    f"Error running `{curl_cmd_updates_complete_until}`: {e}"
                )
                return False

        # Special handling of Ctrl+C, see `handle_ctrl_c` above.
        signal.signal(signal.SIGINT, self.handle_ctrl_c)
        log.warn("Press Ctrl+C to finish and exit gracefully")
        log.info("")

        # If no `--offset` is provided, try to get the offset from
        # the endpoint.
        if args.offset is None:
            try:
                args.offset = get_next_offset_from_endpoint(sparql_endpoint)
                log.info(f"Resuming from offset from endpoint: {args.offset}")
            except Exception as e:
                log.debug(
                    f"Could not retrieve offset from endpoint: {e}. "
                    f"Will determine offset from date instead."
                )

        # If the offset was neither provided via `--offset` nor could
        # be retrieved from the endpoint, determine it by reading a
        # single message from the SSE stream at the `since` date.
        if args.offset is None:
            try:
                source = self.retry_with_backoff(
                    lambda: connect_to_sse_stream(
                        args.sse_stream_url, since=since
                    ),
                    "SSE stream connection",
                    args.num_retries,
                )
                offset = None
                for event in source:
                    if event.type == "message" and event.data:
                        event_data = json.loads(event.data)
                        event_topic = event_data.get("meta").get("topic")
                        if event_topic == args.topic:
                            offset = event_data.get("meta").get("offset")
                            log.debug(
                                f"Determined offset from date: {since} -> {offset}"
                            )
                            break
                source.close()
                if offset is None:
                    raise Exception(
                        f"No event with topic {args.topic} found in stream"
                    )
                args.offset = offset
            except KeyboardInterrupt:
                log.warn(
                    "\rCtrl+C pressed while determine current state, exiting"
                )
                return True
            except Exception as e:
                log.error(f"Error determining offset from stream: {e}")
                return False

        # Run the collector (producer thread) and updater (this thread),
        # connected by a bounded blocking queue. On a genuine server restart the
        # updater raises `PipelineRestart`; we then tear the pipeline down and
        # restart the collector from the endpoint offset.
        updater = Updater(self, args, sparql_endpoint)
        offset = args.offset
        current_since = since
        while True:
            q: queue.Queue = queue.Queue(maxsize=args.buffer_size)
            stop_event = Event()
            collector = Collector(
                self, args, current_since, offset, q, stop_event
            )
            collector_thread = Thread(target=collector.run, daemon=True)
            collector_thread.start()
            updater.bind(q, stop_event)
            try:
                outcome = updater.run()
            except PipelineRestart as restart:
                # Stop the collector (may be blocked on a full-queue put),
                # discard the now-invalid queued-ahead batches, and restart
                # from the endpoint offset.
                stop_event.set()
                collector_thread.join()
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
                offset = restart.new_offset
                current_since = None
                continue
            stop_event.set()
            collector_thread.join()
            if outcome == Outcome.FAILED:
                return False
            break

        # Final message after all batches have been processed.
        log.info(
            f"Processed {updater.batch_count} "
            f"{'batches' if updater.batch_count > 1 else 'batch'} "
            f"terminating update command"
        )
        return True
