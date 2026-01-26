from __future__ import annotations

import csv
import re
import json

import numpy as np

from qlever.command import QleverCommand
from qlever.log import log
from qlever.util import run_command


class AnalyseUpdatesCommand(QleverCommand):
    """
    Minimal skeleton for the `analyse-updates` command.
    """

    def __init__(self):
        pass

    def description(self) -> str:
        return "Analyze updates available on the QLever server (placeholder)"

    def should_have_qleverfile(self) -> bool:
        return True

    def relevant_qleverfile_arguments(self) -> dict[str, list[str]]:
        return {"server": ["host_name", "port", "access_token"]}

    def additional_arguments(self, subparser) -> None:
        subparser.add_argument(
            "--sparql-endpoint",
            help="URL of the QLever server, default is {host_name}:{port}",
        )
        subparser.add_argument(
            "analysis_parameters",
            nargs="*",
            help="Optional parameters for the analysis (placeholder)",
        )

    def execute(self, args) -> bool:
        """
        Execute the server command `get-updated-block-sizes` and print the
        returned data. Parse JSON response and extract statistics for the
        subfields (permutations found in the JSON keys).
        """
        # Build curl command to call the server API.
        analyse_cmd = "curl -s"
        endpoint = ""
        if getattr(args, "sparql_endpoint", None):
            endpoint = args.sparql_endpoint
            analyse_cmd += f" {endpoint}"
        else:
            endpoint = f"{args.host_name}:{args.port}"
            analyse_cmd += f" {endpoint}"

        analyse_cmd += (
            f' --data-urlencode "cmd=get-updated-block-sizes"'
            f' --data-urlencode "access-token={args.access_token}"'
        )

        # Show the command and return if in `--show` mode.
        self.show(analyse_cmd, only_show=args.show)
        if args.show:
            return True

        # Execute and parse HTTP status like other commands.
        try:
            analyse_cmd += ' -w " %{http_code}"'
            result = run_command(analyse_cmd, return_output=True)
            match = re.match(r"^(.*) (\d+)$", result, re.DOTALL)
            if not match:
                raise Exception(f"Unexpected output:\n{result}")
            body = match.group(1).strip()
            status_code = match.group(2)
            if status_code != "200":
                raise Exception(body)

            parsed = json.loads(body)

            # Helper to format integers with dot thousand separators
            def _fmt_int(x) -> str:
                return format(int(round(x)), ",").replace(",", ".")

            def _fetch_block_info(endpoint: str, access_token: str, permutation: str, block_index: str):
                """Fetch block inspection data from the server."""
                inspect_cmd = (
                    f"curl -s {endpoint}"
                    f' --data-urlencode "cmd=inspect-block"'
                    f' --data-urlencode "permutation={permutation}"'
                    f' --data-urlencode "blockIndex={block_index}"'
                    f' --data-urlencode "access-token={access_token}"'
                    f' -w " %{{http_code}}"'
                )

                try:
                    result = run_command(inspect_cmd, return_output=True)
                    match = re.match(r"^(.*) (\d+)$", result, re.DOTALL)
                    if not match:
                        return None

                    body = match.group(1).strip()
                    status_code = match.group(2)

                    if status_code != "200":
                        return None

                    return json.loads(body)
                except Exception:
                    return None

            def _format_block_markdown(block_info: dict, rank: str, fmt_func) -> str:
                """Format block inspection data as markdown."""
                if not block_info:
                    return f"### Block #{rank} - Inspection Failed\n\n*Data unavailable*\n\n"

                block_idx = block_info.get("blockIndex", "N/A")
                first = block_info.get("firstTriple", "N/A")
                last = block_info.get("lastTriple", "N/A")
                original = block_info.get("originalTriples", 0)

                updates = block_info.get("updates", {})
                deletes = updates.get("deletes", 0)
                inserts = updates.get("inserts", 0)
                total = updates.get("total", 0)

                ratio = f"{inserts / deletes:.2f}" if deletes > 0 else "N/A"

                return f"""### Block #{rank} (Index: {block_idx})

**First Triple:** `{first}`

**Last Triple:** `{last}`

**Original Triples:** {fmt_func(original)}

**Updates:**

| Deletes | Inserts | Total | Ratio (I/D) |
|--------:|--------:|------:|------------:|
| {fmt_func(deletes)} | {fmt_func(inserts)} | {fmt_func(total)} | {ratio} |

"""

            def _generate_markdown_files(all_stats: dict, endpoint: str, access_token: str, fmt_func):
                """Generate markdown report for each permutation."""
                for permutation, stats in all_stats.items():
                    try:
                        filename = f"update_analysis.{permutation}.md"

                        # Build metadata section
                        content = f"# Update Analysis: {permutation}\n\n"
                        content += "## Metadata\n\n"
                        content += "| Metric | Value |\n"
                        content += "|--------|------:|\n"
                        content += f"| Count | {fmt_func(stats['count'])} |\n"
                        content += f"| Sum | {fmt_func(stats['sum'])} |\n"
                        content += f"| Average | {fmt_func(stats['avg'])} |\n"
                        content += f"| Median | {fmt_func(stats['median'])} |\n"

                        # Add percentiles
                        for p in [10, 25, 75, 90, 95, 99, 99.9, 99.99]:
                            content += f"| P{p} | {fmt_func(stats[f'p{p}'])} |\n"

                        content += "\n## Selected Blocks\n\n"

                        # Fetch and format block details
                        block_ranks = list(range(1, 26)) + [50, 75, 100]
                        for rank in block_ranks:
                            block_key = f"#{rank}_block"
                            if block_key in stats:
                                block_index = stats[block_key]
                                log.debug(f"Inspecting {permutation} block #{rank} (index {block_index})...")
                                block_info = _fetch_block_info(endpoint, access_token, permutation, block_index)
                                content += _format_block_markdown(block_info, str(rank), fmt_func)

                        # Write file
                        with open(filename, "w") as f:
                            f.write(content)

                        log.info(f"Markdown report written to {filename}")

                    except Exception as e:
                        log.error(f"Failed to generate markdown for {permutation}: {e}")

            # Collect stats for CSV export
            percentiles = [10, 25, 50, 75, 90, 95, 99, 99.9, 99.99]
            row_labels = ["count", "sum", "avg", "median"] + \
                         [f"p{p}" for p in percentiles] + \
                         [f"#{i}" for i in range(1, 26)] + ["#50", "#75", "#100"]
            all_stats: dict[str, dict[str, float | str]] = {}

            for permutation, data in parsed.items():
                # Extract block indices and values as parallel arrays
                block_indices = np.array(list(data.keys()), dtype=str)
                values = np.array([int(num_changes) for num_changes in data.values()], dtype=int)

                if values.size == 0:
                    log.info(f"'{permutation}' field is present but empty; nothing to analyze.")
                    continue

                # Sort both arrays together to maintain association
                sort_indices = np.argsort(values)
                values = values[sort_indices]
                block_indices = block_indices[sort_indices]
                n = int(values.size)
                avg = float(np.mean(values))
                pct_vals = np.percentile(values, percentiles)
                pct_results = {p: float(v) for p, v in zip(percentiles, pct_vals)}
                median = pct_results[50]

                # compute sum
                sum_val = float(np.sum(values))

                # Collect stats for CSV
                field_stats: dict[str, float | str] = {
                    "count": n,
                    "sum": sum_val,
                    "avg": avg,
                    "median": median,
                }
                for p in percentiles:
                    field_stats[f"p{p}"] = pct_results[p]
                # Store both value and block index for top_n entries
                for top_n in list(range(1, 26)) + [50, 75, 100]:
                    idx = min(top_n, len(values))
                    field_stats[f"#{top_n}"] = float(values[-idx])
                    field_stats[f"#{top_n}_block"] = str(block_indices[-idx])
                all_stats[permutation] = field_stats

                formatted_count = _fmt_int(n)
                formatted_sum = _fmt_int(sum_val)
                formatted_avg = _fmt_int(avg)
                formatted_median = _fmt_int(median)
                formatted_pcts = {p: _fmt_int(pct_results[p]) for p in percentiles}

                max_width = max(
                    len(formatted_count),
                    len(formatted_sum),
                    len(formatted_avg),
                    len(formatted_median),
                    *(len(v) for v in formatted_pcts.values()),
                )

                log.info(f"Updated block sizes retrieved ({permutation} stats):")
                log.info(f"count : {formatted_count.rjust(max_width)}")
                log.info(f"sum   : {formatted_sum.rjust(max_width)}")
                log.info(f"avg   : {formatted_avg.rjust(max_width)}")
                log.info(f"median: {formatted_median.rjust(max_width)}")
                for p in percentiles:
                    log.info(f"p{p:<3}: {formatted_pcts[p].rjust(max_width)}")

                # Show top N largest entries with their block indices
                for top_n in list(range(1, 26)) + [50, 75, 100]:
                    idx = min(top_n, len(values))
                    formatted_top = _fmt_int(values[-idx])
                    block_id = block_indices[-idx]
                    log.info(f"#{top_n}: {formatted_top.rjust(max_width)} ({block_id})")
                log.info("")

            # Write stats to CSV file
            if all_stats:
                csv_filename = "update_analysis.csv"
                fields = list(all_stats.keys())
                with open(csv_filename, "w", newline="") as csvfile:
                    writer = csv.writer(csvfile)
                    # Header row: first column is stat name, then field names with block columns
                    headers = ["stat"]
                    for permutation in fields:
                        headers.append(permutation)
                        headers.append(f"{permutation}_block")
                    writer.writerow(headers)
                    # Data rows: one per stat
                    for stat_name in row_labels:
                        row = [stat_name]
                        for permutation in fields:
                            row.append(all_stats[permutation].get(stat_name, ""))
                            # Add block index if this is a top_n stat
                            if stat_name.startswith("#"):
                                row.append(all_stats[permutation].get(f"{stat_name}_block", ""))
                            else:
                                row.append("")  # Empty for non-top_n stats
                        writer.writerow(row)
                log.info(f"Statistics written to {csv_filename}")

                # Generate markdown files for each permutation
                log.info("Generating markdown reports...")
                _generate_markdown_files(all_stats, endpoint, args.access_token, _fmt_int)

            return True
        except Exception as e:
            log.error(e)
            return False

