from __future__ import annotations

import re

from qlever.command import QleverCommand
from qlever.log import log
from qlever.util import run_command


class VacuumDeltaTriplesCommand(QleverCommand):
    """
    Class for executing the `vacuum-delta-triples` command.
    """

    def __init__(self):
        pass

    def description(self) -> str:
        return "Vacuum the delta triples on the server"

    def should_have_qleverfile(self) -> bool:
        return True

    def relevant_qleverfile_arguments(self) -> dict[str, list[str]]:
        return {"server": ["host_name", "port", "access_token"]}

    def additional_arguments(self, subparser) -> None:
        subparser.add_argument(
            "--sparql-endpoint",
            help="URL of the QLever server, default is {host_name}:{port}",
        )

    def execute(self, args) -> bool:
        vacuum_cmd = "curl -s"
        if args.sparql_endpoint:
            vacuum_cmd += f" {args.sparql_endpoint}"
        else:
            vacuum_cmd += f" {args.host_name}:{args.port}"
        vacuum_cmd += f' --data-urlencode "cmd=vacuum-delta-triples" --data-urlencode "access-token={args.access_token}"'
        self.show(vacuum_cmd, only_show=args.show)
        if args.show:
            return True

        try:
            vacuum_cmd += ' -w " %{http_code}"'
            result = run_command(vacuum_cmd, return_output=True)
            match = re.match(r"^(.*) (\d+)$", result, re.DOTALL)
            if not match:
                raise Exception(f"Unexpected output:\n{result}")
            error_message = match.group(1).strip()
            status_code = match.group(2)
            if status_code != "200":
                raise Exception(error_message)
            message = "Delta triples vacuumed successfully"
            log.info(message)
            return True
        except Exception as e:
            log.error(e)
            return False
