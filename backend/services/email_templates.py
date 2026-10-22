"""
HTML email bodies.

Table-based layout with inline styles, because that is what Gmail, Outlook and
phone mail clients reliably render. Every email also gets a plain-text part.
"""

import html
from typing import Iterable, List, Optional, Sequence, Tuple

SEVERITY_COLOURS = {
    "info": "#0071E3",
    "success": "#1A8A2C",
    "warning": "#B25000",
    "critical": "#C41230",
}


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""))


def render(
    title: str,
    paragraphs: Iterable[str],
    details: Optional[Sequence[Tuple[str, object]]] = None,
    cta: Optional[Tuple[str, str]] = None,
    severity: str = "info",
    facility_name: Optional[str] = None,
    tables: Optional[List[dict]] = None,
) -> Tuple[str, str]:
    """
    Build ``(html, text)``.

    ``tables`` is a list of ``{"heading", "columns", "rows"}`` for the digest.
    """
    colour = SEVERITY_COLOURS.get(severity, SEVERITY_COLOURS["info"])
    paragraphs = [p for p in paragraphs if p]

    body_html = "".join(
        f'<p style="margin:0 0 12px;font-size:14px;line-height:21px;color:#1D1D1F">{_e(p)}</p>'
        for p in paragraphs
    )

    details_html = ""
    if details:
        rows = "".join(
            f'<tr><td style="padding:6px 12px 6px 0;font-size:12px;color:#6E6E73;white-space:nowrap;'
            f'vertical-align:top">{_e(k)}</td><td style="padding:6px 0;font-size:13px;color:#1D1D1F;'
            f'font-weight:600">{_e(v)}</td></tr>'
            for k, v in details
        )
        details_html = (
            '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:6px 0 16px;'
            f'border-top:1px solid #E5E5E7;width:100%">{rows}</table>'
        )

    tables_html = ""
    for table in tables or []:
        head = "".join(
            f'<th align="left" style="padding:7px 8px;font-size:11px;color:#6E6E73;text-transform:uppercase;'
            f'letter-spacing:.04em;border-bottom:1px solid #E5E5E7">{_e(c)}</th>'
            for c in table.get("columns", [])
        )
        body = "".join(
            "<tr>" + "".join(
                f'<td style="padding:7px 8px;font-size:13px;color:#1D1D1F;border-bottom:1px solid #F0F0F2">{_e(cell)}</td>'
                for cell in row
            ) + "</tr>"
            for row in table.get("rows", [])
        ) or (
            f'<tr><td colspan="{len(table.get("columns", [])) or 1}" style="padding:8px;font-size:13px;'
            f'color:#6E6E73">{_e(table.get("empty", "Nothing to report."))}</td></tr>'
        )
        tables_html += (
            f'<p style="margin:18px 0 6px;font-size:12px;font-weight:700;color:#1D1D1F;text-transform:uppercase;'
            f'letter-spacing:.06em">{_e(table.get("heading", ""))}</p>'
            '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
            f'style="border-collapse:collapse"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'
        )

    cta_html = ""
    if cta and cta[1]:
        cta_html = (
            f'<a href="{_e(cta[1])}" style="display:inline-block;margin-top:8px;padding:11px 20px;'
            f'background:{colour};color:#ffffff;text-decoration:none;border-radius:10px;font-size:14px;'
            f'font-weight:600">{_e(cta[0])}</a>'
        )

    who = f" because you are a member of {_e(facility_name)}" if facility_name else ""
    html_doc = f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#F5F5F7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F5F5F7;padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #E5E5E7">
<tr><td style="height:5px;background:{colour}"></td></tr>
<tr><td style="padding:22px 28px 6px">
<p style="margin:0;font-size:12px;font-weight:700;letter-spacing:.08em;color:{colour};text-transform:uppercase">PlateletIQ</p>
<h1 style="margin:6px 0 14px;font-size:21px;line-height:27px;color:#1D1D1F">{_e(title)}</h1>
{body_html}{details_html}{tables_html}{cta_html}
</td></tr>
<tr><td style="padding:20px 28px 24px;font-size:11px;line-height:17px;color:#AEAEB2">
You are receiving this{who}. Change what you receive under Notifications in the PlateletIQ console.
One-time custody codes are never sent by email or chat.
</td></tr>
</table></td></tr></table></body></html>"""

    text_lines = [title, "=" * len(title), ""] + paragraphs
    if details:
        text_lines += [""] + [f"{k}: {v}" for k, v in details]
    for table in tables or []:
        text_lines += ["", table.get("heading", "").upper()]
        text_lines += [" | ".join(str(c) for c in row) for row in table.get("rows", [])] or [
            table.get("empty", "Nothing to report.")
        ]
    if cta and cta[1]:
        text_lines += ["", f"{cta[0]}: {cta[1]}"]
    return html_doc, "\n".join(text_lines)
