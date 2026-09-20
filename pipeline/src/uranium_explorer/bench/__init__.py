"""UraniumBench: a frozen, anonymised set of grid cells an analyst agent can be scored on.

The point of a benchmark is that it does not move. Every draw here comes from one seed in one TOML spec, every
file is hashed into a manifest, and the answer key is hashed separately so a pack can never carry it. The
next version is a new spec under `configs/bench/` and the same three commands: build, audit, show.

What a benchmark cell carries, and what it must not:

* an **evidence pack** — the tool results a v0 agent would see, with the cell id replaced by a bench id and
  every value id rewritten so the fabrication gate still resolves it, and with nothing that places the cell:
  no coordinates, no NTS sheet, no file number, no company, no property, no deposit name;
* a **map card** — the evidence layers around the cell, drawn with no basemap and no labels, so a model that
  reads maps is scored on what the layers show and not on what a place name gives away;
* a **blind-list** — the assessment files near the cell, which retrieval must never return for it;
* **passages** — what retrieval returns once those files are excluded, scrubbed of names and numbers that
  would identify the ground.

Effort features (how many holes, how many surveys) are left out of the pack by default: the headline finding
of this project is that they predict the labels better than geology does, and a benchmark that hands them
over would be measuring where people looked.
"""
