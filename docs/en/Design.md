# Design

> **Goal:** Help you understand the "Design" feature in the JiuwenSwarm web UI: what it does, what to set up before you start, how to turn a one-sentence idea into a visual creative workflow, and how to inspect, edit, and re-run each step.
>
> **Simplified Chinese:** [设计](../zh/设计.md)

---

## 1. What is Design

### 1.1 In one sentence

"Design" is the JiuwenSwarm workspace for creative work such as videos and images. You describe what you want in a sentence (for example, "make a 30-second vertical short about a little bear waking up in the forest"), optionally add a few reference images, and the system breaks the idea into a **workflow canvas**:

- First it writes a creative brief, then designs the characters and scenes;
- Next it produces a storyboard table and keyframe images;
- Then it generates a video clip for each shot and finally stitches them into the finished piece.

Every step on the canvas is a **node**. You can see what each node produced, change its description or reference images, re-run just that one node, or simply tell the assistant "turn shot two into a night scene" and let it make the change.

### 1.2 What it is good for

| Scenario | Description |
|:---|:---|
| **Short videos / short films** | Script to storyboard to final cut in one go; good for narrative pieces from 15 seconds to a few minutes |
| **Character and scene design sheets** | Run only up to the character and scene design nodes to get a set of design sheets in a consistent style |
| **Storyboard and keyframes** | Produce the storyboard and keyframes first, confirm them, then decide whether to generate video |
| **Adapting from reference material** | Upload your own images, video, or audio as references so the output matches an existing style |

This version only supports **AI video creation based on characters and scenes**: it designs the characters and scenes first, then generates pictures and video shot by shot. Other video scenarios, and other multimodal creation tasks, will be supported in later versions.

### 1.3 How it differs from "Conversation"

| Aspect | Conversation | Design |
|:---|:---|:---|
| Interaction | Question and answer; results appear in the chat | One canvas; results hang on nodes |
| Suited tasks | Q&A, writing, research, working with files | Multi-step, multi-asset visual creation |
| How to revise | Keep asking follow-up questions | Edit nodes, re-run nodes, or chat with the assistant to change them |
| Output | Text, files | Images, videos, audio, briefs |

---

## 2. Before you start: what to configure

Design uses several kinds of models. Check the following in the left navigation under "More → Settings" first.

| Model | Used for | Required? | Where to configure |
|:---|:---|:---|:---|
| **Default model** | Understanding your request, writing the brief, breaking down shots, making edits | ✅ Required | Settings → Model configuration |
| **Image generation model** | Generating character sheets, scene sheets, keyframes | Required if you want images | Settings → Agent → "Configure image generation model", and switch on **Image generation** |
| **Video generation model** | Turning each shot into a video clip | Required if you want video | Settings → Agent → "Configure video generation model", and switch on **Video generation** |
| **Music model** | Background music for the final cut | Optional | A music node only appears once this is configured |

> 💡 **Tip:** Video generation uses reference-image-to-video mode. The video models tested so far are MiniMax H3, the Seedance 2.5 series, and the Wan 3.0 series. Image generation currently supports MiniMax, Volcengine / BytePlus ModelArk, Alibaba Cloud Model Studio (DashScope, e.g. the Wan series), and self-hosted vLLM-Omni. Because a reference image has to be sent along, image and video models **relayed through OpenRouter are not supported for now**.

> ⚠️ **Note:** If a model is not configured, the corresponding node fails immediately during execution with a message like "Video generation is not configured". Fix the configuration, then click "Regenerate" on the node.

For the meaning of each model configuration field, see [Configuration](Configuration.md).

---

## 3. Opening the Design page

1. Click "Tasks" in the left navigation.
2. At the top of the session sidebar there is a work-mode drop-down (it shows "Work" by default). Open it and choose **"Design"**.
3. The sidebar becomes a list of design projects, and the Design home page appears on the right.

After switching, the sidebar lists all your design projects. You can pin, rename, and delete them, and "New project" takes you back to the Design home page.

---

## 4. Creating a design project

### 4.1 Steps

1. In the input box on the Design home page, describe what you want in plain language.
2. (Optional) Click "+" to add reference material: at most **3 images, 1 video, and 1 audio file**, each no larger than **6MB**.
3. (Optional) Pick the default model for this project in the bottom-right corner.
4. Press Enter or click Send. The page shows "Thinking about how to build the design workflow…", and the canvas appears shortly after.

Each design project automatically creates **one project, one session, and one canvas**, bound together. The project name is generated from your description and can be renamed in the sidebar later.

### 4.2 Writing a better description

| Element | What it covers | Example |
|:---|:---|:---|
| **What to make** | A short film, design sheets, a storyboard… | "make a short film" |
| **What it is about** | Story outline, main character, mood | "a little bear wakes up in the forest and finds it has snowed" |
| **How long** | Total duration or number of shots | "about 30 seconds", "6 shots" |
| **Aspect ratio and resolution** | Landscape / portrait / square, resolution | "vertical 9:16", "720p" |
| **Style** | Art style, color palette, reference works | "watercolor picture-book style, warm tones" |

**Comparison**

| Recommended ✅ | Too vague ❌ |
|:---|:---|
| "Make a 30-second vertical short in watercolor picture-book style: a little bear wakes up in the forest, finds the first snow of the year, and runs out happily. 720p." | "make a bear video" |
| "Based on the three character images I uploaded, produce 4 design sheets in different scenes, same art style, 16:9." | "make some pictures" |

> 💡 **Tip:** The aspect ratio and resolution are detected and locked onto every image and video node. Resolution can be written as 480p, 720p, 768p, 1080p, 2K, or 4K; if it exceeds what the configured model supports, it is automatically lowered to the highest level that model supports.

---

## 5. Getting to know the canvas

### 5.1 Page layout

| Area | Position | Purpose |
|:---|:---|:---|
| Top toolbar | Top of the page | Shows the project name; run buttons such as "Execute" and "Cancel" on the right |
| Left panel | Left of the canvas | Two tabs: "Assistant" to chat and make edits, "Assets" to browse every uploaded and generated file |
| Canvas | Center | Where nodes and edges live; drag and zoom freely |
| Bottom dock | Bottom center of the canvas | Add nodes, switch mouse tools, auto-layout, open the asset pack |

### 5.2 Node types

By content type, there are 5 kinds of nodes: **text**, **table**, **image**, **video**, and **audio**.

By the role they play in the workflow, an automatically built workflow usually contains these nodes:

| Node | Type | Output |
|:---|:---|:---|
| **Brief** | Text | A Markdown document with the overall story, style, characters, pacing, and so on |
| **Character design** | Image | One design sheet per character |
| **Scene design** | Image | One design sheet per scene |
| **Storyboard** | Table | Picture, action, camera, and duration for each shot |
| **Keyframe** | Image | The first frame of each shot |
| **Clip** | Video | The generated video clip for each shot |
| **Compose** | Video | All clips stitched into the final cut |
| **Music** | Audio | Background music (only appears when a music model is configured) |

The top-right corner of each node shows its status: waiting, running, completed, or failed. Completed nodes show their output (image, video cover, text summary) directly on the card.

### 5.3 What edges mean

Edges run left to right and mean "the upstream node's output is the downstream node's input". For example, storyboard → keyframe means the keyframe is drawn according to the storyboard; keyframe → clip means the clip uses that keyframe as its first frame.

### 5.4 Bottom dock

| Button | Purpose | Shortcut |
|:---|:---|:---|
| **Add node** | Add an image / video / audio node to the center of the canvas, or import a ComfyUI workflow | — |
| **Select / Move** | Click, box-select, and drag nodes | `V`; hold Space to pan temporarily |
| **Pan** | Drag the whole canvas | `H` |
| **Auto layout** | Re-arrange nodes neatly by their edges to avoid overlap | — |
| **Asset pack** | Open the asset panel; drag assets onto the canvas to turn them into nodes | — |

### 5.5 Importing a ComfyUI workflow (advanced)

If you prefer tuning parameters in ComfyUI, you can import its exported `.json` workflow: bottom dock "Add node" → "ComfyUI" → choose the file.

Keep in mind:

- Only **vLLM-Omni Generate Image / Generate Video nodes** in the workflow are recognized; other nodes are skipped with a notice;
- Imported nodes carry a ComfyUI badge, and their prompt and sampling parameters (steps, guidance scale, seed, and so on) are **sent to vLLM-Omni as-is**, without being rewritten by the agent;
- These nodes need the vLLM-Omni service address filled in under "Configure video generation model / Configure image generation model".

---

## 6. Running the workflow

### 6.1 Run buttons

The run button at the top right changes with the current state:

| Button | When it appears | What it does |
|:---|:---|:---|
| **Execute** | Canvas just built, never run | Runs the whole workflow from the start |
| **Running…** | A run is in progress | Disabled; wait for it to finish |
| **Continue** | A previous run was cancelled, or some nodes have not run | Runs the remaining nodes |
| **Retry failed nodes** | Some node failed | Re-runs only the failed node, then continues downstream |
| **Cancel** | While running | Stops the current run; outputs of completed nodes are kept |
| **Rerun workflow** (in the drop-down) | Any time | Clears all run state and starts over |

> 💡 **Tip:** "Execute" runs every runnable node in one go; you do not need to keep clicking "Continue". You can switch to other pages during a run; the state syncs when you come back.

### 6.2 What happens during a run

After you click "Execute", the backend runs the nodes layer by layer, following the edges:

1. Nodes with no upstream run first (usually the brief);
2. Nodes whose upstream nodes are all complete move into the next round and run in parallel;
3. Each node is handled by a node agent: it reads upstream outputs, assembles the prompt, calls the image or video model, and saves the result as a file;
4. When everything is done, the final cut hangs on the "Compose" node.

The "Assistant" tab on the left shows what is happening in real time (thinking, which tool is being called, which node is running).

### 6.3 Re-running a single node

Select a node and click **"Regenerate"** in its toolbar. The system re-runs only that node using the current upstream outputs, without touching the others. This suits cases like "I don't like shot three, change only shot three".

### 6.4 Choosing between old and new versions

After regenerating, the system keeps both the **original** and the **incoming** content:

- For documents you may have edited by hand, such as the **brief** and the **storyboard**, a "choose the version to keep" dialog pops up and you decide;
- For other nodes such as images and videos, the new content is adopted automatically when the run finishes.

You can also click "Compare versions" on a node to open this dialog at any time.

---

## 7. Editing and fine-tuning

### 7.1 Chat with the assistant

The "Assistant" tab on the left is this project's chat. Ask in plain language, for example:

- "Turn shot two into a night scene and add some snowflakes"
- "Add a closing shot where the bear looks back at the camera"
- "Switch the whole thing to a cyberpunk style"

**Select a node before sending** to make the request apply only to that node; with nothing selected, it applies to the whole canvas. You can attach reference material with your message as well (same limits as 4.1).

After the assistant finishes, the canvas updates automatically and the chat tells you what changed (nodes added, edges added, outputs replaced, and so on).

### 7.2 Editing a node by hand

Click an image, video, or audio node and a toolbar appears next to it with three modes:

| Mode | Purpose | Adjustable |
|:---|:---|:---|
| **Generate** | Let the model generate this node's content | Prompt, aspect ratio, resolution, duration, whether it has sound, how many to generate |
| **Upload** | Use your own file as this node's output | Browse or drag and drop |
| **Edit** | Edit text directly (text and table nodes only) | Text content |

The toolbar also has a row of **material slots** where you can attach extra reference images to this node (for example, an extra costume reference for one shot). After changing the settings, click "Generate" or "Regenerate" to apply them.

> 💡 **Tip:** The prompt you write in "Generate" mode is sent to the model together with the reference images above it; leave it empty to use the description from the storyboard.

### 7.3 Adding and removing nodes and edges

| Action | How |
|:---|:---|
| Add a standalone node | Bottom dock "Add node" → choose image / video / audio |
| Add a node after an existing one | Hover the node and click "Add next node" on its right; the new node is connected automatically |
| Connect | Drag from a node's right handle to another node's left handle |
| Delete a node | Select it and press `Delete` / `Backspace`, or click the delete button on its toolbar; connected edges are removed too |
| Delete an edge | Hover the edge and click the delete button that appears |
| Resize a node | Drag the node's bottom-right corner |

These manual changes are recorded, and the assistant tries to preserve them when it edits the canvas later.

### 7.4 Viewing and editing outputs

- Click **"View materials"** on a node to see the full-size image, play the video, or read the text, and use the left/right arrows to move between materials.
- The **brief** and the **storyboard** can be edited directly in the viewer by clicking "Edit" and then saving. After saving, re-running downstream nodes picks up the new content.
- If the file was updated by another action while you were editing, the viewer asks whether to "overwrite with draft" or discard.

### 7.5 Asset pack

The "Assets" tab on the left and the "Asset pack" button in the bottom dock open the same list, which collects every file in this project:

- **Source**: uploaded by you (labelled "Uploaded") or generated by a model (labelled "Generated");
- **Status**: assets attached to a node show "On canvas"; those only in the library show "Library only";
- **Drag an asset onto the canvas** to create an upload node of the matching type automatically;
- Assets you no longer need can be deleted.

---

## 8. Where files are stored

All files live under the JiuwenSwarm data directory (default `~/.jiuwenswarm`, changeable with the `JIUWENSWARM_DATA_DIR` environment variable):

| Content | Path | Description |
|:---|:---|:---|
| Project outputs | `agent/workspace/design/<project-name-8-char-id>/assets/` | Generated images, videos, the brief `.md`, the storyboard, and so on, plus the material you uploaded |
| Canvas definition | `agent/designer/graphs/<graph_xxx>.json` | Nodes, edges, and settings |
| Run records | `agent/designer/runs/<run_xxx>.json` | Node states for each run |
| Review feedback | `agent/designer/feedback/<graph_xxx>/` | The director agent's self-review of each run, used to improve the next one |
| Trajectory | `.trace/designer/<project-id>.otlp.jsonl`, `.design.jsonl` | Written only when `trajectory_ui.enabled` is on in the main configuration. The project id is the last segment of the address bar, `/design/proj_xxxxxxxx`. The two files hold different contents; see below |

The two trajectory files:

- **`.otlp.jsonl`**: one record per line, with the prompt of each model call, the tool arguments and results, and how long it took;
- **`.design.jsonl`**: the design process itself, including the basics of each run, how the director and the nodes worked together, review feedback, and a run summary for each node agent.

Both can be viewed in the trajectory panel.

> 💡 **Tip:** Copy the `assets/` directory to take away everything a project produced.

---

## 9. FAQ

**A node fails with "not configured" or "switched off"**

The corresponding image / video generation model is not configured or its switch is off. Fix it following section 2, then click "Regenerate" on that node.

**I wrote 480p, why isn't the output 480p?**

First make sure the description uses the `480p` form with the "p" (case does not matter). If the configured model does not support that level, the system automatically switches to the closest level it does support.

**Why is there no narration / voice-over node?**

The current design workflow does not generate separate narration; sound in the video comes from the video model's own audio capability (tick "Has sound" in the node toolbar). A music node for background music only appears after a music model is configured.

**The run has been going for a long time with no response**

Video generation is asynchronous, and one shot usually takes several minutes. The system waits up to 30 minutes (2 hours for self-hosted vLLM-Omni) before marking a node as failed. You can switch to other pages meanwhile; the state syncs when you come back.

**I deleted a node by accident**

Tell the assistant "add back keyframe 3 that I just deleted", or add one manually with "Add node" in the bottom dock and reconnect it.

**I want to re-run with a different model**

Choose the default model in the bottom-right corner of the Design home page or change it in "Settings"; change image and video models under "Settings → Agent". Then click "Regenerate" on the relevant nodes.

---

## Back to navigation

- [Back to docs home](../README_EN.md)
- [Back to project home](../../README.md)
