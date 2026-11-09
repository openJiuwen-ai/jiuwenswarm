# Storyboard Agent Skill

Emit a timed camera table — **one row per clip**. Each row needs: timeline, camera framing (shot size / move as film language), action, scene change flag, and a **clip prompt**. Align actions to **character_specs** and setting to **scene_specs**.

Materialize the Brief's full narrative/content arc and speech plan into distinct sequential **clips**.
Every **clip** must advance the story or message; do not use repeated action or alternate coverage as runtime filler. Keep exact dialogue/voiceover with its speaker and timed **clip**, and honor silence.

Keep exactly one climax. Each row has start state, the new action, and an irreversible end state; the next row opens from the previous end (**shot_consistency**). Record per-clip clothing, emotion, and presence when they change, without changing the character's face.

Write the storyboard in the user's content language — the language the video's speech and on-screen
text will use. Dialogue and staging discipline: every spoken line stays speakable exact words;
signs and brand text live in the scene description, not in dialogue. A crowd or group reacts as an
off-screen group voice, never as one on-screen person speaking for everyone; a line addressed to a
group matches the headcount on screen (one person answers for themselves unless declared the
spokesperson). Each row's action stages its own opening state — who is where and what is already
true as the shot begins — instead of relying on a transition no earlier row showed.

Design the performance. When a later turn depends on an inner state (lust, fear, scheming), the
row's action stages that state as visible performance — a gaze that will not leave the other
person, daydreaming, fidgeting hands — at the strength the turn needs; a thin "absent-minded" note
cannot motivate an outburst two rows later. Speech rows carry delivery direction with the line:
tone, expression, and the accompanying movement (closing in, reaching out, pulling back), so the
model renders the intended delivery instead of a neutral shout.

Video models run a content safety review that rejects graphic violence, gore, and cruelty. Keep such events in the story, but stage them indirectly in the **clip prompt** — aftermath, reactions, silhouettes, off-screen sound, symbolic props — never as explicit description.
