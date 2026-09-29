from datetime import datetime

def parse_tickets(data: list) -> list:
    events = []
    for ticket in data:
        try:
            ts = datetime.fromisoformat(ticket["created_at"].replace("Z", ""))
        except (KeyError, ValueError):
            continue
        content = f"[TICKET] {ticket.get('title', '')} — {ticket.get('description', '')} (status: {ticket.get('status', 'unknown')})"
        events.append({
            "timestamp": ts,
            "source": "ticket",
            "level": "INFO",
            "content": content,
            "raw": str(ticket)
        })
    return events
