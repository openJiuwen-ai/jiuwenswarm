# Swarm Skill · 运行时流程（v2）

> 取代 M1/M2 中 `entry=dynamic_skill` / Cold A 叙事。产品真相源：
> `创意实现/需求/swarm-skill需求设计文档-v2.md`。

```mermaid
flowchart TB
  subgraph install [1. 安装与落盘]
    Hub["SwarmSkills Hub / 本地导入"]
    Disk["物理库 get_agent_skills_dir()"]
    Hub --> Disk
  end

  subgraph boot [2. Bootstrap 钉库]
    Pin["configure_global_skills_dir(...)"]
    Pin --> Disk
  end

  subgraph assemble [3. 成员装配 同轨]
    Spec["折叠为唯一 core.team.skill_use"]
    Rail["TeamSkillUseRail 扫同一物理库"]
    Vis["空 allow = 全可见"]
    Soft["soft-link 路1 并存"]
    Spec --> Rail --> Vis
    Spec -.-> Soft
  end

  subgraph open [4. 开班与使用 v2]
    Bound{"已绑 team_name?"}
    Bound -->|是 · 目录团 / 无技 auto-bind| Ord["仅普通 Skill；禁 Swarm（须 dissolve）"]
    Bound -->|是 · session_generated 且同 skill_id| Cont["允许同 Swarm 续聊（幂等）"]
    Bound -->|否 + Swarm| Auto["chat.send → auto-bind / generated<br/>team_origin=session_generated"]
    Auto --> Clear["agents.*.skills=[] 冻结<br/>跳过 live 模板 reconcile"]
    Clear --> Hint["Leader SOP 提示"]
    Hint --> Rail
    Cont --> Rail
    Ord --> Rail
  end

  install --> boot --> assemble --> open
```

## 路径速查

| 角色 | 路径 |
|------|------|
| 物理库 | `get_agent_skills_dir()` |
| 可见性 | `skills-visibility.json`；无团 Swarm：**空 allow**，不写 `allow=[swarm]` |
| 选团 | `team.session.bind` **只认** `team_name`（无 dynamic entry） |
| 解绑 | `team.runtime.dissolve`；先无技 auto-bind 后再选 Swarm 须先解绑 |

## 与 develop / 旧 M1 的差异

- **有意不**采用 develop「选技写入 allow 收窄」。
- **已废** `entry=dynamic_*`、`TEAM_COLD_REOPEN_REQUIRED` sticky Cold A。
- Session-generated 团：**跳过** live preset skills 漂移恢复，避免空 allow 被写回。
