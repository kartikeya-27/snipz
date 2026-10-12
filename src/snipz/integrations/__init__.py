"""Provider integrations — budget-guarded wrappers around LLM SDK clients.

Each provider lives in its own module and needs that provider's SDK,
installed through the matching extra:

* :mod:`snipz.integrations.anthropic` — ``pip install snipz[anthropic]``
* :mod:`snipz.integrations.openai` — ``pip install snipz[openai]`` (Responses API)

Importing this package never imports a provider SDK. The errors below
are shared by every provider wrapper.
"""

from __future__ import annotations

from snipz.integrations._guard import DuplicateRequestError, SettlementError

__all__ = ["DuplicateRequestError", "SettlementError"]
