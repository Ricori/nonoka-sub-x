"""The FineSub knowledge package's protocol (WP-K; plan section 0.1 rule 5, 4.6 item 35a).

Only the knowledge package touches knowledge data. The transcription and
correction halves reach it through this protocol: the locked engine files take
the knowledge functions that open the store (`load_entry_texts`,
`KnowledgeRepo`, the write lock, ...) from `finesub_knowledge.client`, which
runs FineSub's own knowledge code in a paired knowledge process
(`finesub_knowledge.server`). What is selected, matched, rendered and written
does not change: the server executes the original functions; only where they
run does.

- `protocol`: the wire (one `multiprocessing.connection` per client thread,
  JSON frames) and the codec, shared by both ends.
- `client`: what the engine files import; finds or starts the paired server.
- `server`: the allow-list and the process that executes it.
- `manage`: the knowledge page's operations and the subject preparation a task
  needs, run inside a knowledge process.

Written to move into the packages as they are (plan 4.6 item 35): nothing here
imports `nonoka_x`. WP-P1 puts `client` and `protocol` into the transcription
and correction packages, `server`, `manage` and `protocol` into the knowledge
package.
"""
