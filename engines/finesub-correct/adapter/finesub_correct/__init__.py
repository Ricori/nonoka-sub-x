"""The FineSub correction and translation package's own code (plan 4.6 item 33).

`python -m finesub_correct` is the process the core's engine host starts for a
task's correction, and the sidecar's resident process for single LLM calls and
the catalog. `bridge` turns its launch context into what FineSub reads,
`engine` runs FineSub's half after the stable, `llm` serves single calls,
`wire` speaks the line protocol. Nothing here imports the core (`nonoka_x`) or
another package.
"""
