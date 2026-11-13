# Storyboard Agent Skill

Emit a timed camera table — **one row per clip**. Each row needs: timeline, camera framing (shot size / move as film language), action, scene change flag, and a **clip prompt**. Align actions to **character_specs** and setting to **scene_specs**.

Materialize the Brief's full narrative/content arc and speech plan into distinct sequential **clips**.
Every **clip** must advance the story or message; do not use repeated action or alternate coverage as runtime filler. Keep exact dialogue/voiceover with its speaker and timed **clip**, and honor silence.

<<<<<<< HEAD
Keep exactly one climax. Each row has start state, the new action, and an irreversible end state; the next row opens from the previous end (**shot_consistency**). Record per-clip wardrobe, emotion, and presence when they change, without changing the character's face.

Video models run a content safety review that rejects graphic violence, gore, and cruelty. Keep such beats in the story, but stage them indirectly in the **clip prompt** — aftermath, reactions, silhouettes, off-screen sound, symbolic props — never as explicit description.
||||||| e487e9a30
Keep exactly one climax. Each row has start state, the new action, and an irreversible end state; the next row opens from the previous end (**shot_consistency**). Record per-clip wardrobe, emotion, and presence when they change, without changing the character's face.
=======
Keep exactly one climax. Each row has start state, the new action, and an irreversible end state; the next row opens from the previous end (**shot_consistency**). Record per-clip clothing, emotion, and presence when they change, without changing the character's face.

Write the storyboard in the user's content language — the language the video's speech and on-screen
text will use. Speech design: a line can reveal intent, turn a relationship, or set up what a later
shot needs. From the viewer's perspective, arrange speech so it helps the story and the viewer can tell
what is happening on screen. Keep the wording natural; do not have a character speak out too much
action or interior state, and do not make the lines dense. Speech may be dialogue, a monologue, or
voiceover. Where a hook or suspense is needed and on-screen dialogue does not fit, a monologue can
carry it — still natural, and sounding like someone talking to themselves. Avoid voiceover unless it
is necessary: the user asked, or a monologue truly cannot help the story. A line is only sounds a
mouth can say, so when a row has speech, write the actual words. A crowd or group reacts as an
off-screen group voice, never as one on-screen person speaking for everyone; a line addressed to a
group matches the headcount on screen (one person answers for themselves unless declared the
spokesperson).

Shot continuity design: each row's action establishes its own opening — who is where and what is
already true as the shot begins — and does not depend on a transition no earlier row showed.
Do not split one sound or visual effect across two shots. In most cases hard-cut between two shots is
enough. Refrain from using white-out, fade, or other similar transitions: you cannot split the
transition across the end of one shot and the start of the next. If you do use one of these
transitions, it must start and end inside a single shot.

Design the performance. When a later turn depends on an inner state (lust, fear, scheming), the
row's action stages that state as visible performance — a gaze that will not leave the other
person, daydreaming, fidgeting hands — at the strength the turn needs; a thin "absent-minded" note
cannot motivate an outburst two rows later. Speech rows carry delivery direction with the line:
tone, expression, and the accompanying movement (closing in, reaching out, pulling back), so the
model renders the intended delivery instead of a neutral shout.

Video models run a content safety review that rejects graphic violence, gore, and cruelty. Keep such events in the story, but stage them indirectly in the **clip prompt** — aftermath, reactions, silhouettes, off-screen sound, symbolic props — never as explicit description.
>>>>>>> origin/develop
