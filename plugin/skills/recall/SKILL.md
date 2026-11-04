---
description: Search long-term memory (Memcairn) for a topic.
argument-hint: <what to look for>
disable-model-invocation: true
---
Call the Memcairn `recall` tool with query "$ARGUMENTS". If it says low confidence, call it
once more with mode="deep". Fetch full text with `get` only for ids that matter, then answer
from what memory holds; if nothing is found, say so.
