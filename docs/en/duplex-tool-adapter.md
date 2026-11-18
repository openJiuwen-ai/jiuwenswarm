# vLLM-Omni duplex tool calling

The video duplex interface can connect to a vLLM-Omni Realtime WebSocket backend. This integration requires the tool capabilities from [PR #9909](https://github.com/vllm-project/vllm-omni/pull/9909); use a verified deployment revision. HTTP tool calling and in-process inference are not supported.

Both `ws://<host>/v1/realtime?duplex=1` and `ws://<host>/v1/duplex` are supported. In the verified vLLM-Omni revision, `/v1/duplex` is an alias for the same Realtime duplex protocol. Set either URL in the existing WebSocket URL field; the adapter preserves its path and adds the served model, `duplex=1` and `autostart=0` for an explicit session handshake.

In Settings → Experimental, configure the vLLM-Omni WebSocket URL, served model, optional API key and tool timeout. Select **vLLM-Omni WebSocket**, then open a new duplex session. Existing sessions keep their original connection. The ordinary duplex connection test checks the saved provider and its declared tool capabilities.

For managed deployments the equivalent environment settings are:

```sh
VIDEO_LIVE_MODE=realtime
VIDEO_REALTIME_PROVIDER=vllm_omni_ws
VLLM_OMNI_REALTIME_URL=ws://127.0.0.1:8099/v1/realtime
VLLM_OMNI_MODEL_NAME=<served-model-id>
VLLM_OMNI_TOOL_TIMEOUT_S=120
# Set VLLM_OMNI_API_KEY only if the backend requires authentication.
```

The backend must advertise declared tools and either client-response-create or automatic result continuation. Tool-less, delegate-only and next-observation backends are rejected for this integration. The first supported workflow exposes Jiuwen task delegation and task queue management. Core executes delegated work with the existing permissions and approval cards; voice/model text cannot answer those cards through this adapter.

Audio and video continue while a tool runs. The timeout includes approval waiting. Only one tool runs at a time. Failed, cancelled and late results are not passed back as successful output. A stop or disconnect cancels this connection's owned work; committed external effects cannot be undone. There is no automatic reconnection or execution replay. Reopen duplex after a connection failure and inspect the task panel before retrying.

Credentials remain on the server. Browser connection tickets are temporary and must not be shared. Qwen cloud and JoyAI retain their existing paths.
