"""Top-level CLI dispatcher.

`apply-engine build ...`  -> the resume/cover generator (apply_engine.builder.cli)
`apply-engine ...`        -> the stage-to-brink apply flow (apply_engine.cli), the default

Keeping `apply` as the default (no subcommand needed) preserves the original `--job` invocation.
"""

import sys


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "build":
        from .builder.cli import main as build_main
        return build_main(argv[1:])
    if argv and argv[0] == "apply":
        # explicit `apply` subcommand, equivalent to the bare default
        from .cli import main as apply_main
        return apply_main(argv[1:])
    from .cli import main as apply_main
    return apply_main(argv)
