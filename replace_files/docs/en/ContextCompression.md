# Context compression & offload

Long chats hit **context window limits**: the model slows, forgets early details, or makes mistakes on dense material. JiuwenClaw uses openJiuwen’s **context offload** to **archive bulky content** when counts or token thresholds are exceeded—like clearing a cluttered desk: large or secondary material is summarized and indexed with `[[OFFLOAD:...]]` markers so the active window stays focused.

## Configuration (high level)

| Parameter | Role |
| :--- | :--- |
| **Trigger** | When message count exceeds `messages_threshold` (e.g. 3) **or** total tokens exceed `tokens_threshold` (default 20000), offload runs. |
| **Large messages** | `large_message_threshold` (e.g. 1000 tokens) marks messages to prioritize for offload. |
| **What to offload** | `offload_message_type` can target specific roles (e.g. compress long tool output, keep user/assistant dialogue). |
| **How** | Sampling or summarization; indexed with `[[OFFLOAD:...]]` for later retrieval. |
| **Protect recent** | `messages_to_keep` or `keep_last_round` keeps the latest user–assistant turn intact. |

[Demo video](../assets/videos/compression.mp4)

Together, this keeps important facts (billing, contracts) while trimming noise so long sessions stay coherent.
