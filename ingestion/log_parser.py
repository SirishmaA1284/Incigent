"""
log_parser.py — Real pattern recognition for infrastructure logs.
Detects: DB pool exhaustion, OOM kills, deployment events, circuit breakers,
         5xx spikes, replication lag, connection leaks, memory pressure.
"""
import re
from datetime import datetime
from typing import Optional

# ── Primary timestamp patterns ─────────────────────────────────────────────────
TS_PATTERNS = [
    re.compile(r'(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})'),
    re.compile(r'(?P<ts>\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2})'),  # nginx
    re.compile(r'(?P<ts>\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})'),  # syslog
]

LEVEL_PATTERN = re.compile(
    r'\b(CRITICAL|FATAL|ERROR|ERR|WARN(?:ING)?|INFO|DEBUG|NOTICE|TRACE)\b',
    re.IGNORECASE
)

LEVEL_NORMALISE = {
    "fatal": "CRITICAL", "critical": "CRITICAL",
    "error": "ERROR", "err": "ERROR",
    "warning": "WARN", "warn": "WARN",
    "notice": "INFO", "info": "INFO",
    "debug": "DEBUG", "trace": "DEBUG",
}

# ── Semantic pattern library ───────────────────────────────────────────────────
PATTERNS = [
    # DB pool
    (re.compile(r'connection\s+pool\s+(exhausted|full|at\s+max)', re.I),           "DB_POOL_EXHAUSTED",     "CRITICAL"),
    (re.compile(r'max_connections\s+limit\s+reached',               re.I),           "DB_MAX_CONN",           "CRITICAL"),
    (re.compile(r'idle_in_transaction',                              re.I),           "DB_IDLE_LEAK",          "WARN"),
    (re.compile(r'connection\s+pool\s+at\s+(\d+)%',                 re.I),           "DB_POOL_HIGH",          "WARN"),
    (re.compile(r'slow\s+query\s+detected',                          re.I),           "DB_SLOW_QUERY",         "WARN"),
    (re.compile(r'replication\s+lag[:\s]+(\d+)',                     re.I),           "DB_REPLICATION_LAG",    "WARN"),
    (re.compile(r'replication\s+stream\s+disconnected',              re.I),           "DB_REPLICATION_BROKEN", "ERROR"),
    # Network / API
    (re.compile(r'circuit\s+breaker\s+(open|tripped)',               re.I),           "CIRCUIT_BREAKER_OPEN",  "ERROR"),
    (re.compile(r'circuit\s+breaker\s+(closed|half.open)',           re.I),           "CIRCUIT_BREAKER_CLOSE", "INFO"),
    (re.compile(r'health\s+check\s+failed',                          re.I),           "HEALTH_CHECK_FAIL",     "ERROR"),
    (re.compile(r'health\s+check\s+passed',                          re.I),           "HEALTH_CHECK_OK",       "INFO"),
    (re.compile(r'upstream\s+\w+\s+timeout',                         re.I),           "UPSTREAM_TIMEOUT",      "ERROR"),
    (re.compile(r'5\d{2}\s+(?:service\s+unavailable|internal)',      re.I),           "HTTP_5XX",              "ERROR"),
    # Memory / OOM
    (re.compile(r'out\s+of\s+memory|oom.kill',                       re.I),           "OOM_KILL",              "CRITICAL"),
    (re.compile(r'memory\s+pressure|swap\s+usage',                   re.I),           "MEMORY_PRESSURE",       "WARN"),
    # Deployment
    (re.compile(r'deploy(?:ed|ing|ment)|rolling\s+restart',          re.I),           "DEPLOY",                "INFO"),
    (re.compile(r'rollback',                                          re.I),           "ROLLBACK",              "WARN"),
    # Recovery
    (re.compile(r'(?:service|db|connection)\s+re.established|recovered|resolved', re.I), "RECOVERY",          "INFO"),
    (re.compile(r'traffic\s+restored|normalising|all\s+.*healthy',   re.I),           "RECOVERY",              "INFO"),
    # Alert
    (re.compile(r'pagerduty|alert\s+fired|on.call',                  re.I),           "ALERT_FIRED",           "INFO"),
]


def detect_patterns(content: str) -> list:
    """Return list of (tag, inferred_level) for known failure patterns."""
    hits = []
    for regex, tag, lvl in PATTERNS:
        if regex.search(content):
            hits.append((tag, lvl))
    return hits


def parse_log_line(line: str) -> Optional[dict]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # Extract timestamp
    ts = None
    for pat in TS_PATTERNS:
        m = pat.search(line)
        if m:
            raw_ts = m.group("ts")
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                        "%d/%b/%Y:%H:%M:%S", "%b %d %H:%M:%S", "%b  %d %H:%M:%S"):
                try:
                    ts = datetime.strptime(raw_ts, fmt)
                    if ts.year == 1900:
                        ts = ts.replace(year=datetime.now().year)
                    break
                except ValueError:
                    continue
            if ts:
                break

    if not ts:
        return None  # skip lines without timestamps

    # Extract log level
    lm = LEVEL_PATTERN.search(line)
    level = LEVEL_NORMALISE.get(lm.group(1).lower(), "INFO") if lm else "INFO"

    # Extract content (everything after timestamp + level)
    content = line
    for pat in TS_PATTERNS:
        content = pat.sub("", content, count=1)
    content = LEVEL_PATTERN.sub("", content, count=1)
    content = re.sub(r'^\s*[\[\]|:]+\s*', '', content).strip()
    if not content:
        content = line  # fallback

    # Pattern detection — upgrade level if pattern demands higher severity
    detected = detect_patterns(line)
    sev_rank = {"DEBUG":0,"INFO":1,"WARN":2,"ERROR":3,"CRITICAL":4}
    for tag, inferred_lvl in detected:
        if sev_rank.get(inferred_lvl, 0) > sev_rank.get(level, 0):
            level = inferred_lvl

    # Extract component/service name from [brackets] or before colon
    component = None
    comp_match = re.search(r'\[([a-zA-Z0-9_\-\.]+)\]', line)
    if comp_match:
        component = comp_match.group(1)

    return {
        "timestamp": ts,
        "source": "log",
        "level": level,
        "content": content,
        "component": component,
        "patterns": [t for t, _ in detected],
        "raw": line,
    }


def parse_log_file(filepath: str) -> list:
    events = []
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            event = parse_log_line(line)
            if event:
                events.append(event)
    return events
