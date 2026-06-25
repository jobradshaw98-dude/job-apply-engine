"""Top-level CLI dispatcher — the `career-engine` command.

`career-engine source ...`   -> scan public ATS boards for new postings (apply_engine.source.cli)
`career-engine qualify ...`  -> resolve URL + fetch JD + enrich-gate + fit-score + promote (apply_engine.qualify.cli)
`career-engine build ...`    -> tailored résumé/cover generator (apply_engine.builder.cli)
`career-engine apply ...`    -> stage-to-brink form fill (apply_engine.cli), the default
`career-engine engage ...`   -> autonomous career-ops orchestrator: hygiene + stage to brink (apply_engine.engage.cli)

Keeping `apply` as the default (no subcommand needed) preserves the original `--job` invocation.
The legacy `apply-engine` command resolves here too.
"""

import sys


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "source":
        from .source.cli import main as source_main
        return source_main(argv[1:])
    if argv and argv[0] == "qualify":
        from .qualify.cli import main as qualify_main
        return qualify_main(argv[1:])
    if argv and argv[0] == "build":
        from .builder.cli import main as build_main
        return build_main(argv[1:])
    if argv and argv[0] == "engage":
        from .engage.cli import main as engage_main
        return engage_main(argv[1:])
    if argv and argv[0] == "apply":
        # explicit `apply` subcommand, equivalent to the bare default
        from .cli import main as apply_main
        return apply_main(argv[1:])
    from .cli import main as apply_main
    return apply_main(argv)
