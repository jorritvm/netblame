"""Optional plain-language explanation of the analysis by an LLM.

The deterministic analysis is the source of truth; the model only gets the
computed summary (no raw samples) and is told not to contradict the verdict.
"""

from __future__ import annotations

import json
import os
from typing import Any

SYSTEM_PROMPT = """\
You explain the results of a home-network diagnostic tool called netblame.
netblame pinged several hops in parallel (local router, ISP gateway, internet
hosts) and a deterministic analysis already produced a verdict.

Rules:
- Treat the verdict, evidence and notes as correct. Do not contradict them and
  do not invent measurements that are not in the data.
- Write for a technically capable user. Be concise: at most ~200 words of
  explanation, in short paragraphs or a few bullets.
- Explain what the numbers mean, why the verdict follows from them, and the
  2-4 most useful next steps.
- If the verdict points at the ISP line / upstream network, finish with a short
  draft message (max ~120 words) the user can send to their ISP, quoting the
  key numbers.
- Plain text only, no markdown headings.
"""


class ExplainError(RuntimeError):
    """Raised when the LLM explanation cannot be produced."""


def explain(summary: dict[str, Any], model: str) -> str:
    try:
        import anthropic
    except ImportError as exc:
        raise ExplainError(
            'The anthropic package is not installed. Install it with: pip install "netblame[llm]"'
        ) from exc

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ExplainError("Set the ANTHROPIC_API_KEY environment variable to use --explain.")

    client = anthropic.Anthropic(timeout=60.0, max_retries=2)
    try:
        message = client.messages.create(
            model=model,
            max_tokens=1200,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": "netblame analysis summary (JSON):\n"
                    + json.dumps(summary, indent=2, ensure_ascii=False),
                }
            ],
        )
    except anthropic.APIError as exc:
        raise ExplainError(f"LLM request failed: {exc}") from exc

    text = "".join(block.text for block in message.content if block.type == "text").strip()
    if not text:
        raise ExplainError("The model returned an empty explanation.")
    return text
