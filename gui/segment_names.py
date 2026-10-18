"""FFmpeg segment names use the local wall-clock time when each file opens."""
from pathlib import Path
import re

TIME_PATTERN = "%Y-%m-%d_%H-%M-%S"
OLD_SUFFIX = re.compile(r"^(.*)_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_%0?\d*d(\.[^.]+)$")


def timestamp_segments(command):
    result = list(command)
    segmented = any(result[i] == "-f" and result[i + 1] in ("segment", "stream_segment", "ssegment")
                    for i in range(len(result) - 1))
    match = OLD_SUFFIX.fullmatch(result[-1]) if result and segmented else None
    if not match:
        return result
    # Escape percent signs in the literal author/title/directory portion.
    result[-1] = match[1].replace("%", "%%") + "_" + TIME_PATTERN + match[2]
    if "-strftime" in result:
        result[result.index("-strftime") + 1] = "1"
    else:
        result[-1:-1] = ["-strftime", "1"]
    return result


def pattern_directory(pattern):
    parent = str(Path(pattern).parent)
    if TIME_PATTERN in pattern:
        parent = parent.replace("%%", "%")
    return Path(parent).resolve()


def filename_matcher(pattern):
    name = Path(pattern).name
    timestamped = TIME_PATTERN in pattern
    expression, cursor = [], 0
    tokens = {"%Y": r"\d{4}", "%m": r"\d{2}", "%d": r"\d{2}", "%H": r"\d{2}",
              "%M": r"\d{2}", "%S": r"\d{2}", "%%": "%"}
    for match in re.finditer(r"%%|%Y|%m|%H|%M|%S|%0?\d*d", name):
        expression.append(re.escape(name[cursor:match.start()]))
        token = match.group()
        expression.append(tokens[token] if timestamped and token in tokens else r"\d+" if token.endswith("d") else re.escape(token))
        cursor = match.end()
    expression.append(re.escape(name[cursor:]))
    return re.compile("".join(expression)).fullmatch
