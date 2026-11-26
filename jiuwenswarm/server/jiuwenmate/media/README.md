# HyperAgent media

- `feishu_inbound_media.py` validates the source message, downloads its images and cleans up private files.
- `execution_media.py` resolves authorized message and session images. New channels implement the `InboundMedia` protocol and keep channel API calls in their adapter.
- `vision_runtime.py` resolves the configured model and passes authorized images to the question-answering and OCR tools.

The execution driver owns the media lifetime. The Bash runtime registers the vision tools with the current execution scope.
