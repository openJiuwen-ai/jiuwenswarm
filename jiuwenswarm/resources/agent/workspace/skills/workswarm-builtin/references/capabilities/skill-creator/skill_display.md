---
display_name_zh: 技能生成专家
description_zh: 所有 Skill Creator 的统一入口。将技能创建与编辑请求路由到正确的专项 creator。当用户想要创建新 Skill、从聊天中编辑已有 Skill，或对话场景为 create_skill / edit_skill 时使用。在 skill-creator-normal、swarmskill-creator 与 skill-omni-creation 中选择，并确保最终 Skill 包通过 send_file_to_user 交付（绝不默默安装到工作区）。在 create_skill / edit_skill 场景下，不要绕过本入口直接调用各专项 creator。
display_name_en: Skill Creator
description_en: Unified entry point for all Skill creators. Routes skill creation and editing requests to the correct specialized creator. Use whenever the user wants to create a new Skill, edit an existing Skill from chat, or when the conversation scene is create_skill / edit_skill. Chooses among skill-creator-normal, swarmskill-creator, and skill-omni-creation, then ensures the final Skill package is delivered via send_file_to_user (never silently installed to workspace). Do not bypass this entry by calling specialized creators directly for create_skill / edit_skill scenes.
---
