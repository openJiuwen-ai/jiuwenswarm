---
name: workswarm-image-understanding
description: Inspect user-provided images with WorkSwarm native visual question answering while separating observations from uncertainty.
---

# WorkSwarm Image Understanding

Use the actual uploaded image path or URL. Call
`visual_question_answering(image_path_or_url, question)` with a question limited to
facts that can be observed in the image.

- The native tool accepts one image per call. For multiple images, call it once per
  image and consolidate only explicit observations.
- Separate confirmed observations from uncertainty and requested follow-up
  measurements.
- Do not infer private attributes, hidden conditions, exact dimensions, materials,
  safety compliance, or structural facts that the image does not establish.
- Pass the observation report to the responsible member as an internal artifact;
  do not deliver intermediate files directly to the user.
