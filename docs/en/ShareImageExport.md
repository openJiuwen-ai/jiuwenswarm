# Conversation image export

When Web generates an image in a separate browser tab, manually closing that tab stops its own export. This also applies when the server has claimed the task but the acknowledgement has not reached the tab. The main page shows a non-blocking stopped-export notification with the conversation name and restores the Export button. Clicking it again creates a new task.

Closing a duplicate tab that did not acquire the task does not stop the active export tab or exports from other conversations. A tab that closes automatically after a successful download does not show a stopped-export notification. Retrying a download still reuses the generated file.
