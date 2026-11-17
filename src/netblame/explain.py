"""Build a ready-to-paste prompt for an LLM of your choice.

netblame does not call any LLM itself. ``--explain`` turns the analysis into
a self-contained prompt that you paste into Claude, ChatGPT, Gemini, a local
model, ... The deterministic analysis stays the source of truth: the prompt
contains only the computed summary (no raw samples) and tells the model not
to contradict the verdict.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

INSTRUCTIONS = """\
You are helping me understand the result of a home-network diagnostic tool
called netblame. It pinged several hops of my network path at the same moments
(local router, ISP gateway, internet hosts), and a deterministic analysis
already produced a verdict. The full summary is in the JSON below.

How to read the data:
- "level" is the hop order from my PC outwards (1 = local network, 2 = ISP
  gateway, 3 = internet). Targets with the same level are the same hop.
- A "bad sample" is a lost ping or a reply slower than the spike limit.
- "bad_while_closer_hops_healthy" counts problems that START at that hop;
  "bad_while_closer_hops_also_bad" counts problems inherited from closer hops.

Please:
1. Treat the verdict, evidence and notes as correct. Do not contradict them
   and do not invent measurements that are not in the data.
2. Explain in plain language (at most ~200 words) what the numbers mean and
   why the verdict follows from them.
3. Give the 2-4 most useful next steps for my situation.
4. If the verdict points at the ISP line / upstream network, finish with a
   short draft message (max ~120 words) I can send to my ISP, quoting the key
   numbers (loss %, latency, that it was measured with closer hops healthy).
"""


def build_prompt(summary: dict[str, Any]) -> str:
    """Return the complete prompt: instructions followed by the JSON summary."""
    return (
        INSTRUCTIONS
        + "\nnetblame summary (JSON):\n```json\n"
        + json.dumps(summary, indent=2, ensure_ascii=False)
        + "\n```\n"
    )


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the Windows clipboard with ``clip``. Returns True on success."""
    try:
        # clip.exe reads UTF-16 LE correctly when the BOM is present.
        subprocess.run(
            ["clip"],
            input=text.encode("utf-16"),
            check=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True
