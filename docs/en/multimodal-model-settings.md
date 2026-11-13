# Multimodal model settings

In the personal edition, manage models under **Settings > Models > Multimodal models** and select them under **Settings > Agent > Tools**. Chat models, model groups, and decision models keep their existing settings.

## Add and select models

Choose a purpose and one or more capabilities:

| Purpose | Capabilities |
| --- | --- |
| Multimodal understanding | Image processing, audio processing, video understanding |
| Multimodal generation | Image generation, video generation |

Select only capabilities the model actually supports. Existing provider and connection validation rules apply. For example, vLLM-Omni allows an empty API key and model name; other providers retain their existing requirements. The model display name fills when you leave the model-name field or select a model. Customized names are preserved; clearing the display name enables automatic filling again. Switching providers with an empty model name does not fill the provider name.

Saving automatically selects the model and enables matching tools that have no model selected. Existing selections and manually disabled tools are preserved. Editing a model to add a capability follows the same rule.

Each tool dropdown lists matching models. Turning a tool off retains its selection; turning it back on uses that selection. Turning on an unconfigured tool opens the add dialog with its purpose and capability preselected. You can change both. If the saved model excludes the originating capability, it is still added and the originating tool stays unchanged.

Display names can repeat. Matching candidates with the same name also show their actual model name and address. If those are identical too, the model ID distinguishes them.

## Edit and delete

Connection edits synchronize to every bound tool, including disabled tools. Removing a capability or changing purpose requires confirmation when an existing selection is affected. The confirmation lists those tools.

Deletion also requires confirmation and lists every bound tool, including disabled ones. After deletion or capability removal:

- Tools with another matching model select the first one in list order and retain their on/off state.
- Tools without a matching model have their selection and model configuration cleared and are turned off.

Cancelling confirmation does not save and retains the editing draft.

## Existing configuration and save results

The first Runtime startup after upgrading imports the five previous tool configurations. Opening settings only reads the migrated result. Models merge only when their purpose, full connection configuration, and advanced options are equal. Their capabilities are combined, while each tool retains its own switch. Incomplete historical values are preserved; editing and saving still requires the original form validation to pass.

Image, audio, and video configurations whose model names exactly equal their respective template placeholders (`your-vision-model-name`, `your-audio-model-name`, and `your-video-model-name`) are not imported, regardless of other connection values. These tools remain unbound and disabled. Adding a real matching model then selects it and enables the tool normally.

Historical environment-variable references resolve to their effective values using the existing rules. Equivalent address and image-model aliases are consolidated into the unified connection fields, so old aliases cannot override later edits. Separate historical audio transcription and question-answering model names are preserved until you change the unified model name, at which point both use the new name.

The server saves the catalogue, selections, and original execution fields together. **Saved, but could not be applied** means the files were saved; use **Retry** to apply them again. Existing hot-reload behavior applies, without a new configuration-snapshot guarantee for running tasks.

Saving follows the ordinary model settings flow: the server reads the current configuration and updates the model or tool targeted by the request. When multiple pages edit the same model, the last successful save wins. Every successful save triggers a reload, including saves with unchanged content. Deleting a model or removing capabilities still checks the currently affected tools and requires confirmation again if that list changes.

Web, AgentServer, the separate Gateway, and the protocol package require matching versions. The shared backend startup migration is upgraded for both editions. Enterprise settings do not gain model or tool entries, and the unmaintained TUI is outside this change. Gateway retains its existing edition routing without adding restrictions to the six new methods. Once the catalogue is initialized, old clients cannot directly overwrite the five managed model configurations.

## Matching dependencies and startup

The main repository's dependency declarations and lockfile reference available commits on the personal origin repositories: protocol `267c077fa832b25f7f64a913461c2124353bc911` and Gateway `21ffb3861c4907389ab300d8ca0106257e731132`. Both are on `feat/multimodal-model-settings`. Dependency synchronization installs these Git versions.

```bash
uv sync --locked
uv run --locked jiuwenswarm-app
```

Update the main source, pyproject.toml, and uv.lock together. For debug startup, use `uv run --locked jiuwenswarm-start debug --skip-build`. The dependency commits are in `xiaoyao42/agent-protocol` and `xiaoyao42/agent-gateway`; this change does not push or merge into upstream.
