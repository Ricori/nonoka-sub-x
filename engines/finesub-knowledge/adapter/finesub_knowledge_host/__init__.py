"""The FineSub knowledge package's process (plan 4.6 item 35a).

`python -m finesub_knowledge_host` is started by the core's engine host: for a
task, it prepares the task's knowledge subjects and then serves the task's
transcription and correction processes as their knowledge process
(`finesub_knowledge.server`); for the knowledge page, it answers one list,
read or save (`finesub_knowledge.manage.handle`). Nothing here imports the
core (`nonoka_x`) or another package.
"""
