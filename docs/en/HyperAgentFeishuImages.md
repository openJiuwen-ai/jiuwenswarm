# HyperAgent image questions in Feishu groups

Mention the colleague, write a question, and paste the image into the **same message**. For example: `@AI colleague Read the text inside the red box and describe where the blue circle is.` Standalone image delivery follows the colleague's existing response policy. Automatic lookup of images sent in earlier messages, replies quoting older images, private chats, and video are outside this feature.

## Service configuration

Use the colleague's existing bound Feishu bot. Personal Feishu CLI authentication is not required. The bot needs access to the source message and its image resources; bulk group-history access is not required. Existing tenant permissions still apply.

Configure the service data directory's `config/.env`, then restart the service:

```dotenv
VISION_ENABLED=true
VISION_MODEL=qwen4-max
VISION_BASE_URL=https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
VISION_API_KEY=<authorized vision-model key>
VISION_MAX_RETRIES=1
VISION_REQUEST_TIMEOUT_SECONDS=60
```

The existing `VISION_MODEL_NAME` and `VISION_API_BASE` aliases also work. This feature adds neither a settings page nor configuration hot reload. The conversation model remains separate from the vision tool. Credentials are never automatically borrowed from the conversation configuration; explicitly configure any authorized reuse. See [Alibaba's Qwen4-Max documentation](https://help.aliyun.com/zh/model-studio/qwen4-max), and verify model access with the deployment account.

## Behavior and limits

Only registered image nodes in the triggering group message are downloaded. Text containing image keys and arbitrary links are not attachments. Limits are eight image nodes per message, 20 MiB per image, 40 million decoded pixels, and 45 seconds for preparation. Image numbering follows message order; repeated images share a download. A failed image does not renumber successful images.

Vision tools accept only this request's `feishu-image:N` references and check the source again before each call. Execution-owned files are deleted on completion, failure, or cancellation. Crash leftovers older than one day are removed when that session next prepares images. Small source records retain message revision and image keys, without image bytes, for recovery checks and remain with the session directory.

Vision-tool scoping does not change existing Bash permissions or isolate all files from a colleague already granted full access. Missing models, unreadable resources, and invalid images must produce an honest failure rather than invented image descriptions. Vision calls have a configurable 60-second wait limit. A request already sent by the underlying SDK may continue remotely after timeout or cancellation.

## Acceptance

Send a same-message image containing a random string absent from the question and distinct shapes. Check the returned string and relative positions. Then verify two-image ordering, recalled messages, malformed or oversized images, and disabled vision. Inspect actual tool results and confirm temporary image files are gone; an acknowledgment alone is insufficient.
