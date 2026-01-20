from __future__ import annotations

import os
import shlex
import tempfile
import time
import traceback

from qlever.command import QleverCommand
from qlever.log import log
from qlever.util import run_command


class DeleteTestTriplesCommand(QleverCommand):
    """
    Class for deleting test triples of the form <a> <b> "{n}".

    The command generates a SPARQL DELETE DATA statement with the specified
    number of test triples and sends it to a SPARQL endpoint.
    """

    def __init__(self):
        pass

    def description(self) -> str:
        return "Delete 100k test triples of the form <a> <b> \"{n}\""

    def should_have_qleverfile(self) -> bool:
        return False

    def relevant_qleverfile_arguments(self) -> dict[str, list[str]]:
        return {"server": ["host_name", "port", "access_token"]}

    def additional_arguments(self, subparser) -> None:
        subparser.add_argument(
            "--sparql-endpoint",
            type=str,
            help="URL of the SPARQL endpoint",
        )
        subparser.add_argument(
            "--count",
            type=int,
            default=100000,
            help="Number of triples to delete (default: 100000)",
        )

    def execute(self, args) -> bool:
        sparql_endpoint = (
            args.sparql_endpoint if args.sparql_endpoint else f"{args.host_name}:{args.port}"
        )

        # Generate SPARQL UPDATE string with DELETE DATA
        sparql_update = "DELETE DATA {\n"
        for i in range(args.count):
            sparql_update += f"  <a> <b> \"{i}\" .\n"
        sparql_update += "}"

        # Write to temporary file
        temp_file = None
        temp_file_path = None
        try:
            temp_file = tempfile.NamedTemporaryFile(mode='w', delete=False)
            temp_file.write(sparql_update)
            temp_file.flush()
            temp_file_path = temp_file.name

            # Build curl command
            curl_cmd = (
                f"curl -s {sparql_endpoint} -X POST "
                f"-H 'Authorization: Bearer {args.access_token}' "
                f"-H 'Content-Type: application/sparql-update' "
                f"--data-binary @{shlex.quote(temp_file_path)}"
            )

            # Show and exit if requested
            self.show(curl_cmd, only_show=args.show)
            if args.show:
                return True

            # Execute update
            try:
                start_time = time.time()
                run_command(curl_cmd)
                time_msecs = round(1000 * (time.time() - start_time))
                if args.log_level != "NO_LOG":
                    log.info("")
                    log.info(
                        f"Delete processing time (end-to-end): {time_msecs:,d} ms"
                    )
            except Exception as e:
                if args.log_level == "DEBUG":
                    traceback.print_exc()
                log.error(e)
                return False

            return True

        finally:
            # Clean up temporary file
            if temp_file:
                temp_file.close()
            if temp_file_path and os.path.exists(temp_file_path):
                os.unlink(temp_file_path)
