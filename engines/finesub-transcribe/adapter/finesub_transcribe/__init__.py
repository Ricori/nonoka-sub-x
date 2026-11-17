"""The FineSub transcription package's own code (plan 4.6 item 33).

`python -m finesub_transcribe` is the process the core's engine host starts
for one transcription; `bridge` turns its launch context into what FineSub
reads, `engine` runs FineSub's recognition chain, `wire` speaks the line
protocol. Nothing here imports the core (`nonoka_x`) or another package.
"""
