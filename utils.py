import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "jobs.db"

# Per-token pricing in USD (approximate — update if Anthropic changes rates)
_PRICING = {
    "claude-haiku-4-5-20251001": (0.80e-6, 4.00e-6),   # input, output
    "claude-sonnet-4-6":         (3.00e-6, 15.00e-6),
}


def log_api_call(model: str, operation: str, input_tokens: int, output_tokens: int):
    inp, out = _PRICING.get(model, (0.0, 0.0))
    cost = input_tokens * inp + output_tokens * out
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO api_usage (timestamp, model, operation, input_tokens, output_tokens, cost_usd)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [datetime.now(timezone.utc).isoformat(), model, operation,
         input_tokens, output_tokens, round(cost, 6)],
    )
    conn.commit()
    conn.close()
