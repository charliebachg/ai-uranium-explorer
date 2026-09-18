"""Parse the provincial "work done" text of an assessment file.

The Mineral Assessment File Information layers store one sentence split across WORK_1..WORK_4, and
GeoDS layer 20 has a similar historic description. The text is semi-structured:

    "Drilling: 27 DH (# SB-10 to SB-16, SP-03 to SP-12, LS-87 to LS-96), 5339.3 m. Analysis: 623 ..."
    "5 ddh records, gamma and E logs (#AE-1 to 5 - Ahenakew East) Assays Exploration report ..."
    "Drilling: 4DH (#MFU-MC-006 to 009), 2468m, Analyses: all probed ..."

Selection needs only a few facts from it: how many holes, how many metres, which hole names, whether the
holes were probed, and whether assays or geochemistry are mentioned. Everything parsed here is an index
feature for choosing files; none of it is evidence about what a report prints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------- joining the split fields ----------


def join_work_fields(parts: list[str | None]) -> str:
    """Concatenate WORK_1..WORK_4.

    Historic rows were cut at a fixed width (about 104 characters), sometimes mid-word ("A" | "nalyses")
    and sometimes mid-number. Modern rows break between clauses. A direct join is used only when the cut
    looks mid-token: letter to lower-case letter, or digit to digit. Anything else gets a space.
    """
    out = ""
    for raw in parts:
        if raw is None:
            continue
        piece = raw if raw.strip() else ""
        if not piece:
            continue
        if not out:
            out = piece.strip()
            continue
        a, b = out[-1], piece[0]
        mid_word = a.isalpha() and b.isalpha() and b.islower() and not piece.startswith(" ")
        mid_number = a.isdigit() and b.isdigit()
        if (mid_word or mid_number) and not out.endswith(" "):
            out = out + piece.rstrip()
        else:
            out = out.rstrip() + " " + piece.strip()
    return re.sub(r"\s+", " ", out).strip()


# ---------- parsing ----------

_COUNT = re.compile(
    r"(?<![\w.#-])(\d{1,4})\s*"
    r"(?P<kind>ddh|d\.d\.h\.?|dh|pdh|rdh|rc\s?holes?|diamond\s+drill\s?holes?|drill\s?holes?|boreholes?|holes)"
    r"(?![a-z])",
    re.IGNORECASE,
)
_METRES = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(m|metres|meters|ft|feet)(?![a-z])",
                     re.IGNORECASE)
_SECTION_BREAK = re.compile(r"\b(analys[ie]s|analysis|geophysic|sampling|report|assays?\b|ground|airborne|note:)",
                            re.IGNORECASE)
_NAMES_BLOCK = re.compile(r"\(\s*#\s*([^()]*(?:\([^()]*\)[^()]*)*)\)|DDH\s*#\s*([^.;]*)", re.IGNORECASE)
_PROBED = re.compile(
    r"probed|probe\s+logs?|gamma[\s-]*(?:ray\s+)?(?:neutron\s+)?logged|gamma\s+logs?|radiometric(?:ally)?\s+logged"
    r"|radiometric\s+logs?|downhole\s+(?:gamma|probe)|ra\s+probe|geiger\s+probe|probe\s+records",
    re.IGNORECASE,
)
_ASSAY = re.compile(r"assay", re.IGNORECASE)
_U3O8 = re.compile(r"U\s?3\s?[O0]\s?8", re.IGNORECASE)
_GEOCHEM = re.compile(r"geochem|lithogeochem|multi-?\s?element|\bICP\b|\bINAA\b", re.IGNORECASE)
_DRILL_WORD = re.compile(r"rill|\bddh\b|\bpdh\b", re.IGNORECASE)

_KIND_NORMAL = {
    "ddh": "ddh", "d.d.h": "ddh", "d.d.h.": "ddh", "dh": "dh", "pdh": "pdh", "rdh": "rdh",
    "holes": "holes", "hole": "holes", "boreholes": "holes", "borehole": "holes",
}


@dataclass
class HoleMention:
    count: int
    kind: str  # ddh | dh | pdh | rdh | holes | rc
    text: str
    metres: float | None = None
    length_unit: str | None = None
    names_raw: str | None = None


@dataclass
class WorkText:
    text: str
    mentions: list[HoleMention] = field(default_factory=list)
    hole_count: int | None = None           # diamond and unspecified holes (the selection's "drill holes")
    percussion_count: int | None = None     # pdh / rc / rotary holes, reported separately
    metres: float | None = None
    feet: float | None = None
    hole_names: list[str] = field(default_factory=list)
    hole_names_complete: bool = False        # every name block expanded without guessing
    probed: bool = False
    assay: bool = False
    u3o8: bool = False
    geochem: bool = False
    drilling: bool = False

    def as_dict(self) -> dict:
        return {
            "hole_count": self.hole_count, "percussion_count": self.percussion_count, "metres": self.metres,
            "feet": self.feet, "hole_names": self.hole_names, "hole_names_complete": self.hole_names_complete,
            "probed": self.probed, "assay": self.assay, "u3o8": self.u3o8, "geochem": self.geochem,
            "drilling": self.drilling,
            "mentions": [m.__dict__ for m in self.mentions],
        }


def _norm_kind(k: str) -> str:
    k = re.sub(r"\s+", " ", k.lower()).strip()
    if k.startswith("rc"):
        return "rc"
    if "diamond" in k:
        return "ddh"
    if "drill" in k:
        return "holes"
    return _KIND_NORMAL.get(k, k)


def _to_float(num: str) -> float:
    return float(num.replace(",", ""))


_ITEM_RANGE = re.compile(r"^(?P<a>.+?)\s*(?:\bto\b|\s-\s|–)\s*(?P<b>.+)$", re.IGNORECASE)
_TRAIL_NUM = re.compile(r"^(?P<prefix>.*?)(?P<num>\d+)(?P<suffix>[A-Z]?)$", re.IGNORECASE)


def _expand_item(item: str) -> tuple[list[str], bool]:
    """Expand "SB-10 to SB-16", "KN-01 to -08", "MFU-MC-006 to 009", "AE-1 to 5"; (names, clean)."""
    item = item.strip().strip(".")
    if not item:
        return [], True
    m = _ITEM_RANGE.match(item)
    if not m:
        return [item], True
    a, b = m.group("a").strip(), m.group("b").strip()
    # A trailing label ("AE-1 to 5 - Ahenakew East") is dropped from the end token.
    b = re.split(r"\s+-\s+|\s{2,}", b)[0].strip()
    ma = _TRAIL_NUM.match(a)
    if not ma or ma.group("suffix"):
        return [item], False
    prefix, na = ma.group("prefix"), ma.group("num")
    b_clean = b.lstrip("-").strip()
    mb = _TRAIL_NUM.match(b_clean)
    if not mb or mb.group("suffix"):
        return [item], False
    b_prefix = mb.group("prefix")
    # The end token either repeats the prefix, or gives the number alone ("to 009", "to -08").
    if b_prefix and b_prefix.replace(" ", "") != prefix.replace(" ", ""):
        return [item], False
    start, end = int(na), int(mb.group("num"))
    if end < start or end - start > 400:
        return [item], False
    width = len(na) if na.startswith("0") or len(na) == len(mb.group("num")) else 0
    return [f"{prefix}{str(i).zfill(width)}" for i in range(start, end + 1)], True


def _parse_names(block: str) -> tuple[list[str], bool]:
    names: list[str] = []
    clean = True
    block = re.sub(r"\([^()]*\)", "", block)  # nested remarks such as "(wedged from ...)"
    for item in re.split(r",|;|\band\b|&", block):
        item = item.strip()
        # "CX-086, -086-1, -087": a leading dash repeats the previous name's letter prefix.
        if item.startswith("-") and names:
            lead = re.match(r"^([A-Z]+)", names[-1], re.IGNORECASE)
            if lead:
                item = lead.group(1) + item
        got, ok = _expand_item(item)
        clean &= ok
        for g in got:
            if g and g not in names:
                names.append(g)
    return names, clean


def parse_work_text(text: str | None) -> WorkText:
    text = re.sub(r"\s+", " ", text or "").strip()
    wt = WorkText(text=text)
    if not text:
        return wt
    wt.probed = bool(_PROBED.search(text))
    wt.assay = bool(_ASSAY.search(text))
    wt.u3o8 = bool(_U3O8.search(text))
    wt.geochem = bool(_GEOCHEM.search(text))

    seen: set[tuple] = set()
    matches = list(_COUNT.finditer(text))
    for i, m in enumerate(matches):
        count = int(m.group(1))
        kind = _norm_kind(m.group("kind"))
        # The clause runs to the next section keyword, the next hole count, or 220 characters.
        stop = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        rest = text[m.end(): min(stop, m.end() + 220)]
        brk = _SECTION_BREAK.search(rest)
        clause = rest[: brk.start()] if brk else rest
        clause = re.split(r"\bDrilling\s*:", clause, flags=re.IGNORECASE)[0]
        mention = HoleMention(count=count, kind=kind, text=text[m.start(): m.end() + len(clause)].strip())
        lm = _METRES.search(clause)
        if lm and _to_float(lm.group(1)) >= 5:
            mention.metres = _to_float(lm.group(1))
            mention.length_unit = "ft" if lm.group(2).lower() in ("ft", "feet") else "m"
        nb = _NAMES_BLOCK.search(clause)
        if nb:
            mention.names_raw = (nb.group(1) or nb.group(2) or "").strip()
        # Some rows repeat a clause verbatim ("Drilling: 52 DH, 19,849.7 m ... Drilling: 52 DH, 19,849.7 m").
        key = (count, kind, mention.metres if mention.metres is not None else (mention.names_raw or "")[:40])
        if key in seen:
            continue
        seen.add(key)
        wt.mentions.append(mention)

    diamond = [mn for mn in wt.mentions if mn.kind in ("ddh", "dh", "holes")]
    percussion = [mn for mn in wt.mentions if mn.kind in ("pdh", "rc", "rdh")]
    if diamond:
        wt.hole_count = sum(mn.count for mn in diamond)
    if percussion:
        wt.percussion_count = sum(mn.count for mn in percussion)
    metres = [mn.metres for mn in wt.mentions if mn.metres is not None and mn.length_unit == "m"]
    feet = [mn.metres for mn in wt.mentions if mn.metres is not None and mn.length_unit == "ft"]
    wt.metres = round(sum(metres), 2) if metres else None
    wt.feet = round(sum(feet), 2) if feet else None

    complete = bool(diamond)
    for mn in diamond:
        if not mn.names_raw:
            complete = False
            continue
        names, clean = _parse_names(mn.names_raw)
        complete &= clean
        for n in names:
            if n not in wt.hole_names:
                wt.hole_names.append(n)
    wt.hole_names_complete = complete and wt.hole_count == len(wt.hole_names)
    wt.drilling = bool(wt.mentions) or bool(_DRILL_WORD.search(text))
    return wt


def first_year(work_date: str | None) -> int | None:
    """First four-digit year in WORK_DATE ("2008-2009" -> 2008, "1953,55" -> 1953, "195?" -> None)."""
    if not work_date:
        return None
    m = re.search(r"(?<!\d)(1[89]\d\d|20\d\d)(?!\d)", str(work_date))
    return int(m.group(1)) if m else None
