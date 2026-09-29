from datetime import datetime

def parse_slack_export(data: list) -> list:
    events = []
    for msg in data:
        try:
            ts = datetime.utcfromtimestamp(float(msg["ts"]))
        except (KeyError, ValueError):
            continue
        events.append({
            "timestamp": ts,
            "source": "slack",
            "level": "INFO",
            "content": f"[{msg.get('user', 'unknown')}] {msg.get('text', '')}",
            "raw": str(msg)
        })
    return events
