---
description: Save something to long-term memory (Memcairn).
argument-hint: <fact to remember>
disable-model-invocation: true
---
Save "$ARGUMENTS" with the Memcairn `remember` tool: one short atomic fact per call, `entity` =
the project, server or topic, `type` = project|feedback|reference|decision|todo|howto|fact.
Split several facts into several calls. If it reports possible duplicates, update the
existing fact instead (supersede=true when the old one is no longer true).
