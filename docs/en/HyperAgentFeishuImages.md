# HyperAgent image questions in Feishu groups

Mention the AI colleague, paste a screenshot and ask your question in the same group message. For example, “What does the red box say, and where is the blue circle?” Images in rich-text posts retain their order. To ask about an earlier image, send it again with the question.

**Administrator setup**

1. Give the colleague’s bound Feishu bot access to the message and its image resources.
2. Under **Settings → Models → Multimodal models**, add an image-capable model with its model ID, API address and key.
3. Under **Settings → Agent → Tools → Image processing**, enable the tool and select the model.

Each message supports up to eight images, with a limit of 20 MiB and 40 million pixels per image. Image preparation has a 45-second timeout. Vision calls default to 60 seconds, configurable through `VISION_REQUEST_TIMEOUT_SECONDS`.

After upgrading, resend legacy WeLink attachment tasks that were running or awaiting approval.
