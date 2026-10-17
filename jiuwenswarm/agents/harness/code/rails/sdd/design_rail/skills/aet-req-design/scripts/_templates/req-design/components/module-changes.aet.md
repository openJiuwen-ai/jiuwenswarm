---
heading_level: 2
checklist: |
**架构变更总览**

1. Mermaid 架构变更图是否绘制 (ERROR)
   - 展示新增/修改/保护/不涉及的模块及调用关系

2. 变更类型分类准确 (ERROR)
  - 保护表示与该元素相关，但元素不应被改动
  - 不涉及表示模块中关系较近，在后续实现中存在误用、误改风险的元素

3. 围栏不得有冲突 (ERROR)
   - 同一元素不得同时出现在不同类型的围栏中
   - 如仅修改某个模块的部分函数，应在约束中说明

**模块变更明细**

1. 是否只列出相关模块 (INFO)
   - 控制粒度，无需展开无关模块
---

## 架构变更

### 变更总览

<!-- policy: Ensure consistency across design document sections—design decisions, feature changes, module design, and interface design must have corresponding entries. -->
<!-- guideline: Legend—🔵External User 🟢New 🟡Modified 🔴Protected (has calls but code modifications are disallowed this iteration) ⚪Not Involved (unrelated to this change; must explicitly prevent accidental modification). -->
<!-- constraint: Interface naming: IF-{Type}{Number}, where Type = E=External, N=New Internal, M=Modified Internal, R=Reuse Internal. -->
<!-- constraint: Diagram boxes must have connections to other boxes; any orphan node requires an annotated reason in comments (nodes not involved are exempt). -->
<!-- constraint: A single file must not appear in both "Modified" and "Protected" modules. If only a portion of a file is modified (e.g., specific functions in xxx.c), explicitly clarify this in the "Constraints" column. -->

```mermaid
graph LR
    classDef ext fill:#87CEEB,stroke:#333,color:#000
    classDef add fill:#90EE90,stroke:#333,color:#000
    classDef mod fill:#FFD700,stroke:#333,color:#000
    classDef pro fill:#FF6B6B,stroke:#333,color:#000
    classDef unt fill:#E0E0E0,stroke:#999,color:#666
    
    subgraph ext_box["📦 外部"]
        U["[User] <br/>&lt;调用方&gt;"]:::ext
    end

    subgraph modA["📦 module_a"]
        A1["[Add] <br/>&lt;File/Class/Func&gt;"]:::add
        A2["[Mod] <br/>&lt;File/Class/Func&gt;"]:::mod
        A3["[Pro] <br/>&lt;File/Class/Func&gt;"]:::pro
        A4["[Unt] <br/>&lt;File/Class/Func&gt;"]:::unt
    end

    subgraph modB["📦 module_b"]
        B1["[Add] <br/>&lt;File/Class/Func&gt;"]:::add
        B2["[Mod] <br/>&lt;File/Class/Func&gt;"]:::mod
    end

    subgraph modC["📦 module_c"]
        C1["[Pro] <br/>&lt;File/Class/Func&gt;"]:::pro
        C2["[Unt] <br/>&lt;File/Class/Func&gt;"]:::unt
    end

    U  -->|"IF-E01: 发起请求"| A1
    U  -->|"IF-E02: 查询结果"| B1
    A1 -->|"IF-N01: 创建并委托处理"| B1
    A1 -->|"IF-N02: 调用工具方法"| A2
    B2 -->|"IF-M01: 适配新数据格式"| A1
    A2 -->|"IF-M02: 增加返回字段"| B2
    B1 -.->|"IF-R01: 仅调用，不修改"| C1
    A1 -.->|"IF-R02: 仅读取配置"| A3
```

### 模块变更

<!-- constraint: Only list modules truly relevant to this design, control granularity to "sufficient to explain impact scope". -->

|模块|变更|职责|接口|依赖|约束|
|-|-|-|-|-|-|
|[]|[新增/修改/保护/不涉及]|[]|[external interface]|[]|[e.g. only allow modifying certain flow, prohibit changing interface signature, etc.]|
