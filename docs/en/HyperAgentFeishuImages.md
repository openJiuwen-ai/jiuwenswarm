# HyperAgent image questions in Feishu groups

Mention the colleague, write a question, and paste the image into the **same message**. For example: `@AI colleague Read the text inside the red box and describe where the blue circle is.` Standalone image delivery follows the colleague's existing response policy. Automatic lookup of images sent in earlier messages, replies quoting older images, private chats, and video are outside this feature.

## Service configuration

Use the colleague's existing bound Feishu bot. Personal Feishu CLI authentication is not required. The bot needs access to the source message and its image resources; bulk group-history access is not required. Existing tenant permissions still apply.

Add an image-capable model in **Settings → Models → Multimodal models**, including its model ID, API address, key and image capability. Enable **Settings → Agent → Tools → Image processing** and select that model. HyperAgent uses this same saved configuration. Newly started executions use the selected model; an already running execution retains its assembled tools. Conversation-model credentials are never borrowed automatically.

Environment-only legacy deployments still support `VISION_ENABLED`, `VISION_MODEL` / `VISION_MODEL_NAME`, `VISION_BASE_URL` / `VISION_API_BASE`, and `VISION_API_KEY` until a unified catalogue exists. Once saved, the catalogue takes precedence, including a disabled image switch. Never commit credentials. A component-version error requires AgentServer, Gateway and protocol dependencies matching the deployed version's lockfile. `VISION_REQUEST_TIMEOUT_SECONDS` sets the vision wait limit (60 seconds by default).

## Behavior and limits

Only registered image nodes in the triggering group message are downloaded. Text containing image keys and arbitrary links are not attachments. Limits are eight image nodes per message, 20 MiB per image, 40 million decoded pixels, and 45 seconds for preparation. Image numbering follows message order; repeated images share a download. A failed image does not renumber successful images.

Inbound images use this request's `feishu-image:N` references, also accepted as `media:image:N` by the common execution layer. Each call rechecks message and group access. Files live in execution-owned private directories under the colleague data directory's `media-cache/executions/`, outside the office. They are deleted on completion, failure or cancellation; crash leftovers older than one day are reclaimed during later preparation. `media-cache/feishu-sources/` retains only message revision and image keys for recovery, without image bytes or a duplicate group transcript.

Tools also accept generated images in the current session's `output/`, including office-relative `sessions/<current-session>/output/...` paths. These survive permission approval and service restart. Other session files, unapproved office files and arbitrary URLs are rejected. WeLink retains access to the current received message's attachment directory. The common scope accepts channel adapters; this release adds no other channel downloads, audio or video support.

While a page conversation is running, its send button becomes Stop. Image preparation uses that existing stop action: cancellation joins the download cleanup and prevents a subsequent model execution. This feature adds no Feishu stop command.

New WeLink attachment directories use a digest of the complete message ID to prevent different messages from sharing a directory. Resend legacy attachment tasks that were running or awaiting approval before this upgrade; ambiguous old directories are not automatically authorized.

Vision-tool scoping does not change existing Bash permissions or isolate all files from a colleague already granted full access. Missing models, unreadable resources, and invalid images must produce an honest failure rather than invented image descriptions. Vision calls have a configurable 60-second wait limit. A request already sent by the underlying SDK may continue remotely after timeout or cancellation.

## Acceptance

Send a same-message image containing a random string absent from the question and distinct shapes. Check the returned string and relative positions. Then verify two-image ordering, recalled messages, malformed or oversized images, and disabled vision. Inspect actual tool results and confirm temporary image files are gone; an acknowledgment alone is insufficient.
