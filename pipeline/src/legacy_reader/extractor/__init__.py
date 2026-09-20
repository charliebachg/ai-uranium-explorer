"""The extractor as an agent (PRD §8.2): locate → read → validate → agree → file, one page at a time.

The batch reader (`legacy_reader.extract`) already reads a page under the wire schema and the assembler and
validators already turn a reading into located, checked values. What this package adds is the loop around
them as a typed state machine per page, and the two stages the batch reader does not have:

* `agree`  a second read of the same page by a different model family, compared value by value with the
           first; values both readers agree on are marked, everything else goes to the review queue;
* `file`   the agreement marks and the queue rows filed under the `read` tier beside the values.

Nothing here reads a page the batch reader has already read: a page whose first-family reading is on disk
is carried into the loop as read, so the second family can be run over the Opus readings without paying
for them again.

Modules: `states` (the page state and its transitions), `agree` (the comparison rule and its metrics),
`queue` (the review queue tables), `loop` (the run), `gold` (hand-keyed pages and the score), `cli`.
"""
