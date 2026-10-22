"""
Chain-of-custody certificate (PDF).

Generated the moment a transfer settles: who sent what to whom, which bags,
the courier and airway bill, and every custody step with its time. It is
stored in object storage (Amazon S3 when the AWS stack is deployed, otherwise
Supabase Storage) and both facilities receive a link by email.

The SHA-256 fingerprint printed at the bottom is computed over the canonical
JSON of the timeline, so anyone holding the certificate can check it against
the AWS custody ledger (DynamoDB) — the two are produced independently.
"""

import datetime
import hashlib
import json
from typing import List, Optional

from fpdf import FPDF

IST = datetime.timedelta(hours=5, minutes=30)

_REPLACEMENTS = {"—": "-", "–": "-", "·": "|", "’": "'", "‘": "'", "“": '"', "”": '"', "…": "...", "→": "->"}


def _latin1(text) -> str:
    """Core PDF fonts are Latin-1; swap the few typographic characters we use."""
    value = str(text if text is not None else "")
    for src, dst in _REPLACEMENTS.items():
        value = value.replace(src, dst)
    return value.encode("latin-1", "replace").decode("latin-1")


def _ist(moment: Optional[datetime.datetime]) -> str:
    if not moment:
        return "-"
    return (moment + IST).strftime("%d %b %Y, %H:%M IST")


def fingerprint(events: List[dict]) -> str:
    canonical = json.dumps(events, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_certificate(transfer, source, destination, events, bags: List[str]) -> bytes:
    """Render the certificate. ``events`` are ``TransferEvent`` rows, oldest first."""
    timeline = [
        {
            "status": e.status,
            "title": e.title,
            "actor": e.actor_bank_id,
            "at": e.occurred_at.isoformat() if e.occurred_at else None,
        }
        for e in events
    ]
    digest = fingerprint(timeline)

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_page()
    pdf.set_margins(16, 16, 16)

    # Header band
    pdf.set_fill_color(196, 18, 48)
    pdf.rect(0, 0, 210, 26, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(16, 8)
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 8, _latin1("PlateletIQ  |  Chain-of-Custody Certificate"))
    pdf.set_xy(16, 16)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 6, _latin1(f"Transfer {transfer.id}  |  issued {_ist(datetime.datetime.utcnow())}"))

    pdf.set_text_color(29, 29, 31)
    pdf.set_y(34)

    def heading(text: str) -> None:
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(196, 18, 48)
        pdf.cell(0, 7, _latin1(text.upper()), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(29, 29, 31)

    def row(label: str, value) -> None:
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(48, 6, _latin1(label))
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 6, _latin1(value), align="L", new_x="LMARGIN", new_y="NEXT")

    heading("Consignment")
    row("Product", f"{transfer.units} x {transfer.component_type} platelet concentrate"
                   + (f", group {transfer.blood_group}" if transfer.blood_group else ""))
    row("Direction", "Shortage pull" if transfer.direction == "SHORTAGE_PULL" else "Wastage push")
    row("Priority", transfer.priority)
    row("Bags", ", ".join(bags) if bags else f"{transfer.units} unit(s)")
    row("Storage", "20-24 deg C with constant agitation. Do not refrigerate.")

    heading("Parties")
    row("Sender", f"{source.name} ({source.code})")
    row("", source.address or "")
    row("Receiver", f"{destination.name} ({destination.code})")
    row("", destination.address or "")

    heading("Logistics")
    row("Courier", f"{transfer.courier_name or '-'} via {transfer.provider_label or transfer.transport_provider or '-'}")
    row("Airway bill", transfer.awb_code or "-")
    row("Route", f"{transfer.distance_km or '-'} km, planned {round(transfer.eta_minutes or 0)} min ({transfer.route_provider or '-'})")
    row("Picked up", _ist(transfer.dispatched_at))
    row("Settled", _ist(transfer.completed_at))

    heading("Custody verification")
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(
        0, 5,
        _latin1(
            "Custody left the sender only when the sender confirmed its one-time pickup code, and "
            "entered the receiver's ledger only when the receiver redeemed the sender's one-time "
            "receipt code. Both codes were single-use, stored as HMAC digests, and verified server side."
        ),
        new_x="LMARGIN", new_y="NEXT",
    )

    heading("Timeline")
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(245, 245, 247)
    pdf.cell(44, 6, "Time (IST)", fill=True)
    pdf.cell(42, 6, "Status", fill=True)
    pdf.cell(0, 6, "Event", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8)
    for e in events:
        pdf.cell(44, 5.5, _latin1(_ist(e.occurred_at)))
        pdf.cell(42, 5.5, _latin1(e.status))
        pdf.multi_cell(0, 5.5, _latin1(e.title), align="L", new_x="LMARGIN", new_y="NEXT")

    heading("Integrity")
    pdf.set_font("Courier", "", 8)
    pdf.multi_cell(0, 5, _latin1(f"SHA-256 of timeline: {digest}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(110, 110, 115)
    pdf.ln(2)
    pdf.multi_cell(
        0, 4,
        _latin1(
            "Generated automatically by PlateletIQ. The same events are recorded independently in the "
            "AWS custody ledger (SNS -> SQS -> Lambda -> DynamoDB) when that stack is deployed."
        ),
        new_x="LMARGIN", new_y="NEXT",
    )

    return bytes(pdf.output())


def object_key(transfer_id: str) -> str:
    return f"certificates/{transfer_id}.pdf"
