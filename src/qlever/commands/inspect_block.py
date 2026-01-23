from __future__ import annotations

import json
import re

from qlever.command import QleverCommand
from qlever.log import log
from qlever.util import run_command


class InspectBlockCommand(QleverCommand):
    """
    Command to inspect detailed information about a specific block.
    """

    def __init__(self):
        pass

    def description(self) -> str:
        return "Inspect details about a specific block in a given permutation"

    def should_have_qleverfile(self) -> bool:
        return True

    def relevant_qleverfile_arguments(self) -> dict[str, list[str]]:
        return {"server": ["host_name", "port", "access_token"]}

    def additional_arguments(self, subparser) -> None:
        subparser.add_argument(
            "permutation",
            type=str,
            help="Permutation identifier (e.g., PSO, POS, SPO)",
        )
        subparser.add_argument(
            "block_index",
            type=int,
            help="Index of the block to inspect",
        )
        subparser.add_argument(
            "--sparql-endpoint",
            help="URL of the QLever server, default is {host_name}:{port}",
        )

    def execute(self, args) -> bool:
        # Build curl command
        endpoint = (
            args.sparql_endpoint
            if getattr(args, "sparql_endpoint", None)
            else f"{args.host_name}:{args.port}"
        )

        inspect_cmd = (
            f"curl -s {endpoint}"
            f' --data-urlencode "cmd=inspect-block"'
            f' --data-urlencode "permutation={args.permutation}"'
            f' --data-urlencode "blockIndex={args.block_index}"'
            f' --data-urlencode "access-token={args.access_token}"'
        )

        # Show command if --show flag is set
        self.show(inspect_cmd, only_show=args.show)
        if args.show:
            return True

        try:
            # Execute with HTTP status code
            inspect_cmd += ' -w " %{http_code}"'
            result = run_command(inspect_cmd, return_output=True)

            # Parse HTTP status and body
            match = re.match(r"^(.*) (\d+)$", result, re.DOTALL)
            if not match:
                raise Exception(f"Unexpected output:\n{result}")

            body = match.group(1).strip()
            status_code = match.group(2)

            if status_code != "200":
                raise Exception(body)

            # Parse JSON response
            parsed = json.loads(body)

            # Helper function for formatting integers with dot separators
            def _fmt_int(x) -> str:
                return format(int(x), ",").replace(",", ".")

            # Extract and format data
            block_index = parsed.get("blockIndex", "N/A")
            permutation = parsed.get("permutation", "N/A")
            first_triple = parsed.get("firstTriple", "N/A")
            last_triple = parsed.get("lastTriple", "N/A")
            original_triples = parsed.get("originalTriples", 0)

            updates = parsed.get("updates", {})
            deletes = updates.get("deletes", 0)
            inserts = updates.get("inserts", 0)
            total = updates.get("total", 0)

            # Calculate ratio (inserts/deletes)
            if deletes > 0:
                ratio = inserts / deletes
                ratio_str = f"{ratio:.2f}"
            else:
                ratio_str = "N/A (no deletes)"

            # Display output
            log.info(f"Block Index: {block_index}")
            log.info(f"Permutation: {permutation}")
            log.info(f"First Triple: {first_triple}")
            log.info(f"Last Triple: {last_triple}")
            log.info(f"Original Triples: {_fmt_int(original_triples)}")
            log.info(
                f"Updates: {_fmt_int(inserts)} inserts / "
                f"{_fmt_int(deletes)} deletes / "
                f"{_fmt_int(total)} total (ratio: {ratio_str})"
            )

            return True

        except Exception as e:
            log.error(e)
            return False
