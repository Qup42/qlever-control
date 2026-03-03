from __future__ import annotations

import re

from qlever.command import QleverCommand
from qlever.log import log
from qlever.util import run_command


class DumpUpdatesCommand(QleverCommand):
    """
    Command to dump all updates for a specific block in a given permutation.
    """

    def __init__(self):
        pass

    def description(self) -> str:
        return "Dump all updates for a specific block in a given permutation"

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
            help="Index of the block to dump updates for",
        )
        subparser.add_argument(
            "--sparql-endpoint",
            help="URL of the QLever server, default is {host_name}:{port}",
        )

    def execute(self, args) -> bool:
        endpoint = (
            args.sparql_endpoint
            if getattr(args, "sparql_endpoint", None)
            else f"{args.host_name}:{args.port}"
        )

        dump_cmd = (
            f"curl -s {endpoint}"
            f' --data-urlencode "cmd=dump-updates"'
            f' --data-urlencode "permutation={args.permutation}"'
            f' --data-urlencode "blockIndex={args.block_index}"'
            f' --data-urlencode "access-token={args.access_token}"'
        )

        self.show(dump_cmd, only_show=args.show)
        if args.show:
            return True

        try:
            dump_cmd += ' -w " %{http_code}"'
            result = run_command(dump_cmd, return_output=True)

            match = re.match(r"^(.*) (\d+)$", result, re.DOTALL)
            if not match:
                raise Exception(f"Unexpected output:\n{result}")

            body = match.group(1).strip()
            status_code = match.group(2)

            if status_code != "200":
                raise Exception(body)

            output_file = f"{args.block_index}.{args.permutation}"
            with open(output_file, "w") as f:
                f.write(body)
            log.info(f"Written to {output_file}")

            return True

        except Exception as e:
            log.error(e)
            return False
