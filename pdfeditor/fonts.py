"""Font-style helpers shared by the text object and lifting logic.

PyMuPDF span ``flags`` bits: 1=superscript, 2=italic, 4=serifed, 8=monospaced,
16=bold. We map a span's style to (a) a Base-14 PDF font alias for baking, and
(b) a Qt family name for the on-screen preview. Base-14 fonts have full Latin
coverage, so newly typed characters always render (an embedded subset font
might not), while still preserving serif/mono + bold + italic.
"""
from __future__ import annotations

from typing import Dict

# (family, bold, italic) -> PyMuPDF Base-14 alias
_BASE14 = {
    ("sans", False, False): "helv", ("sans", True, False): "hebo",
    ("sans", False, True): "heit", ("sans", True, True): "hebi",
    ("serif", False, False): "tiro", ("serif", True, False): "tibo",
    ("serif", False, True): "tiit", ("serif", True, True): "tibi",
    ("mono", False, False): "cour", ("mono", True, False): "cobo",
    ("mono", False, True): "coit", ("mono", True, True): "cobi",
}

_QT_FAMILY = {"sans": "Sans Serif", "serif": "Serif", "mono": "Monospace"}


def style_from_flags(flags: int, fontname: str = "") -> Dict:
    name = (fontname or "").lower()
    italic = bool(flags & 2) or "italic" in name or "oblique" in name
    serif = bool(flags & 4)
    mono = bool(flags & 8)
    bold = bool(flags & 16) or "bold" in name or "black" in name or "heavy" in name
    family = "mono" if mono else ("serif" if serif else "sans")
    return {"bold": bold, "italic": italic, "serif": serif, "mono": mono,
            "family": family}


def base14_alias(bold: bool, italic: bool, serif: bool, mono: bool) -> str:
    family = "mono" if mono else ("serif" if serif else "sans")
    return _BASE14[(family, bool(bold), bool(italic))]


def qt_family(serif: bool, mono: bool) -> str:
    family = "mono" if mono else ("serif" if serif else "sans")
    return _QT_FAMILY[family]
