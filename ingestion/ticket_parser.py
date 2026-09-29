from datetime import datetime

def parse_tickets(data: list) -> list:
    events = []
    for ticket in data:
        try:
            ts = datetime.fromisoformat(ticket["created_at"].replace("Z", ""))
        except (KeyError, ValueError, AttributeError, TypeError):
            continue
        ticket_id = str(ticket.get("id") or "").strip()
        label = f"[TICKET {ticket_id}]" if ticket_id else "[TICKET]"
        content = f"{label} {ticket.get('title', '')} — {ticket.get('description', '')} (status: {ticket.get('status', 'unknown')})"
        events.append({
            "timestamp": ts,
            "source": "ticket",
            "level": "INFO",
            "content": content,
            "ticket_id": ticket_id or None,
            "raw": str(ticket)
        })
    return events
