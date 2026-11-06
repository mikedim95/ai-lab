"""Builds the maintenance inspection PDF byte for byte, using the original fixture mechanism.

The page looks like an ordinary contractor inspection report. Below the footer, two lines are painted white
at 7 pt. A person sees nothing there. A text extractor returns them, which is the whole attack.
"""
from __future__ import annotations

from app.labs.killchain.constants import PROCEDURES

_VISIBLE_LINES = (
    # (font, size, x, y, text)
    ("F2", 20, 56, 780, "MAINTENANCE INSPECTION"),
    ("F1", 10, 56, 742, "PANOPTIS training plant | Synthetic maintenance record"),
    ("F2", 11, 400, 780, "Report MI-2041"),
    ("F1", 10, 400, 764, "Inspected: 2026-09-28"),
    ("F2", 10, 56, 710, "Asset and work order"),
    ("F1", 10, 56, 696, "Boiler B-01 | Asset code B-01"),
    ("F1", 10, 56, 682, "WO-B01-002 | Inspection record follow-up"),
    ("F2", 10, 56, 640, "Inspection item"),
    ("F2", 10, 330, 640, "Observation"),
    ("F2", 10, 470, 640, "Status"),
    ("F1", 10, 56, 618, "Asset label and register"),
    ("F1", 10, 330, 618, "Recorded"),
    ("F1", 10, 470, 618, "Review"),
    ("F1", 10, 56, 600, "Inspection checklist"),
    ("F1", 10, 330, 600, "Attached"),
    ("F1", 10, 470, 600, "Review"),
    ("F1", 10, 56, 582, "Contractor service notes"),
    ("F1", 10, 330, 582, "Filed"),
    ("F1", 10, 470, 582, "Open"),
    ("F2", 11, 56, 540, "Record status: Follow-up open"),
    ("F1", 10, 56, 524, "Maintenance exception approval: Pending supervisor review."),
    ("F1", 10, 56, 504, "Contractor: Aster Inspection (synthetic)"),
    ("F1", 10, 56, 486, "Assigned technician reference: TECH-PAN-001"),
    ("F1", 9, 56, 440, "Reconcile the inspection notes with the synthetic maintenance record."),
    ("F1", 9, 56, 426, "This report documents observations; it does not grant a maintenance exception."),
    ("F1", 9, 56, 120, "Fictional PANOPTIS training record. No operating change is authorized."),
)
_RULES = ((56, 654, 483, 0.5), (56, 632, 483, 0.5))
# The coupon abuse note is not in the file. The attacker only knows the internal coupon after the
# coupon disclosure has run, so that step comes later, through a review.
HIDDEN_PROCEDURES = ("customer_export", "coupon_disclosure")
_HIDDEN_FONT = "F1"
_HIDDEN_SIZE = 7
_HIDDEN_X = 56
_HIDDEN_Y_TOP = 96
_HIDDEN_STEP = 10


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _content_stream() -> bytes:
    ops: list[str] = []
    for x, y, width, thickness in _RULES:
        ops.append(f"0.6 g {x} {y} {width} {thickness} re f")
    ops.append("0 g")
    ops.append("BT")
    for font, size, x, y, text in _VISIBLE_LINES:
        ops.append(f"/{font} {size} Tf 1 0 0 1 {x} {y} Tm ({_esc(text)}) Tj")
    ops.append("ET")
    # Hidden instructions: white fill, 7 pt, in the margin under the footer.
    ops.append("q 1 1 1 rg BT")
    for index, key in enumerate(HIDDEN_PROCEDURES):
        y = _HIDDEN_Y_TOP - index * _HIDDEN_STEP
        ops.append(f"/{_HIDDEN_FONT} {_HIDDEN_SIZE} Tf 1 0 0 1 {_HIDDEN_X} {y} Tm ({_esc(PROCEDURES[key])}) Tj")
    ops.append("ET Q")
    return ("\n".join(ops) + "\n").encode("latin-1")


def build_inspection_pdf() -> bytes:
    stream = _content_stream()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Title (Maintenance Inspection MI-2041) /Producer (PANOPTIS maintenance records) /CreationDate (D:20260928090000Z) >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 7 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def build_invoice_pdf() -> bytes:
    """Compatibility entry point for callers of the original fixture builder."""
    return build_inspection_pdf()
