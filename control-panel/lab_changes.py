"""The change log for a lab: which commands the agent ran on which node, turn by turn, read from the agent's session log.

The vmrun wrapper writes one header per command: "=== <time> <node> $ <command>" (or "=== <time> $ <command>" when
the node isn't named), followed by the command's output. A script sent on stdin has the header
"=== <time> <node> $ (script on stdin)" and the script itself as its output's first lines.
"""
import re

HEADER_RE = re.compile(r"^=== (\S+) (?:(\S+) )?\$ (.*)$")
OUTPUT_EXCERPT = 400          # characters of each command's output kept on the run record
MAX_PER_TURN = 200            # commands kept per turn; the rest are counted, not stored


def parse_command_log(text):
    """Session log text -> a list of {ts, node, command, output}, in the order they ran. Output is the lines until
    the next header, trimmed to OUTPUT_EXCERPT. The node is None for logs written before nodes were named."""
    entries = []
    for line in (text or "").splitlines():
        m = HEADER_RE.match(line)
        if m:
            ts, node, command = m.groups()          # node is None for a header without one (the "$" anchors it)
            entries.append({"ts": ts, "node": node, "command": command, "output": []})
        elif entries:
            entries[-1]["output"].append(line)
    for e in entries:
        e["output"] = "\n".join(e["output"]).strip()[-OUTPUT_EXCERPT:]
    return entries


def new_entries(before_text, after_text):
    """The commands that ran between two reads of the same log."""
    return parse_command_log(after_text)[len(parse_command_log(before_text)):]
