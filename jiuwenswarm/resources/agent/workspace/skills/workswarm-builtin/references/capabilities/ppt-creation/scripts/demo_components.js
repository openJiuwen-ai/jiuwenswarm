// scripts/demo_components.js — full-deck demo of components.js.
// Run from anywhere: node demo_components.js  (writes demo.pptx to cwd)
// Optional component gallery. These pages demonstrate APIs; they are not a
// recommended deck outline, a layout quota, or a template contract.
// 内容页以「图文分栏页」为主（P3-P5 是单图/上下双图/并列多图三形态的黄金范例），
// 其后每种页型各示范一次；不为多样性堆组件。规范：标题无序号、横幅纯文字居中、继承母版页脚。
// Resolve pptxgenjs from the caller's cwd first (the agent workspace has node_modules),
// then from this script's own directory.
const pptxgen = require(require.resolve("pptxgenjs", { paths: [process.cwd(), __dirname] }));
const C = require(require("path").join(__dirname, "components.js"));

async function main() {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_16x9";

  C.addOpeningSlide(pres, { title: "组件库演示", department: "演示部门", author: "演示作者", date: "2026-07-02" });
  // TOC titles are informative business phrases (8-16 chars), never abstract two-word labels.
  C.addTocSlide(pres, { title: "目录", sections: [
    { num: "1", title: "多智能体商业化应用初步涌现" },
    { num: "2", title: "主流多智能体方案简介" },
    { num: "3", title: "头部公司 Agent 实践洞察" },
    { num: "4", title: "技术挑战与趋势总结" },
  ]});

  const { item, group, bandNode, groupBand } = C;
  let s;

  // ===== P3 图文分栏·单图（默认黄金页型）=====
  // 左 addSystemDiagram（分层结构）+ 右 addTextBlock(numbered)，两栏各一黑色居中 heading，
  // 右栏第 N 条逐项对应左图第 N 个 band。这是结构类内容的首选版式。
  s = pres.addSlide(); s.background = { color: C.THEME.white };
  C.addSlideTitle(pres, s, { title: "MOA 如何用分层协作突破单模型上限？", subtitle: "左图承载结构、右栏逐层解析，第 N 条对应第 N 层" });
  C.addSystemDiagram(pres, s, { x: 0.5, y: 1.05, w: 5.4, h: 3.6, heading: "示例 MOA 分层架构", spec: {
    title: "示例分层", subtitle: "", core_message: "", diagram_type: "layered",
    bands: [
      bandNode("input", "输入层", [item("uq", "用户问题 / 任务请求")], "neutral", "full_width"),
      // 平级 N 项写成一个 group 的 N 个完整 items——4+ 项引擎自动多列（4 项→2×2、6 项→2×3），
      // 不压扁、不合并、不省略。真实型号名直接用；严禁写成 "Qwen-110B/72B" 或加「等」。
      // 半宽视觉区放 4 项刚好；项更多且图文分栏装不下时，改系统图整页 w:9 承载，或按
      // over-capacity 报错引导扩区/换骨架/拆页——绝不靠合并省略。
      groupBand("ref", "参考模型层", [
        group("g_ref", "开源模型并行", [
          "WizardLM-2-8x22B", "Qwen1.5-110B-Chat", "Llama-3-70B-Instruct", "Mixtral-8x22B",
        ]),
      ], "secondary"),
      bandNode("agg", "聚合与输出", [
        item("ag", "聚合器 Aggregator", "primary"), item("fo", "最终输出", "result"),
      ], "primary", "row"),
    ],
    edges: [
      { from: "input", to: "ref", relation: "flow", direction: "forward", label: "分发", line_style: "solid", emphasis: "normal", routing: "direct", multiplicity: "single" },
      { from: "ref", to: "agg", relation: "flow", direction: "forward", label: "候选汇入", line_style: "solid", emphasis: "normal", routing: "direct", multiplicity: "single" },
    ],
  } });
  C.addTextBlock(pres, s, { x: 6.1, y: 1.05, w: 3.4, h: 3.6, heading: "逐层解析", style: "numbered", items: [
    { title: "输入层", body: "示例：接收用户问题或任务请求，统一规整成可分发的输入，是整条链路的唯一入口。" },
    { title: "参考模型层", body: "示例：多个异构模型并行产出候选分析，互为参照暴露彼此盲区，异构比排名更能提升质量。" },
    { title: "聚合与输出", body: "示例：聚合器对候选做筛选、综合与再创作，产出结构化、可追溯、可验证的最终结果，是系统上限所在。" },
  ]});
  C.addContentChrome(pres, s, { summary: "示例结论：分层协作把多模型集体智慧转化为一个可控入口的稳定产出。", pageNum: 3 });

  // ===== P4 图文分栏·上下双图 =====
  // 左视觉区用 addRegionHeading 画一个共用标题，下方摆上下两张 addDeckChart（引入前/引入后），
  // 右栏 addTextBlock 做总分对应（现状问题 / 干预机制 / 结果提升）。
  s = pres.addSlide(); s.background = { color: C.THEME.white };
  C.addSlideTitle(pres, s, { title: "引入 MOA 前后，关键指标怎么变？", subtitle: "视觉区上下双图对比，右栏解释变化来自何处" });
  {
    const gy = C.addRegionHeading(pres, s, { text: "示例 · 引入前后指标对比", x: 0.5, y: 1.05, w: 5.4 });
    C.addDeckChart(pres, s, { x: 0.5, y: gy, w: 5.4, h: 1.5,
      type: "bar", data: [{ name: "引入前", labels: ["质量", "一致性", "鲁棒性"], values: [22.2, 33.3, 28.8] }] });
    C.addDeckChart(pres, s, { x: 0.5, y: gy + 1.58, w: 5.4, h: 1.5,
      type: "bar", data: [{ name: "引入后", labels: ["质量", "一致性", "鲁棒性"], values: [66.6, 71.1, 63.3] }] });
    C.addTextBlock(pres, s, { x: 6.1, y: 1.05, w: 3.4, h: 3.55, heading: "变化解读", style: "numbered", items: [
      { title: "现状", body: "示例：单模型在质量、一致性与鲁棒性上都受限于自身知识与推理盲区，难以再靠单点优化突破。" },
      { title: "机制", body: "示例：引入多模型并行参考 + 强聚合后，候选之间互补纠错，弱项被其他模型补齐。" },
      { title: "结果", body: "示例：三项指标同步抬升，且提升幅度随任务复杂度扩大，代价是延迟与成本需提前算账。" },
    ]});
  }
  C.addContentChrome(pres, s, { summary: "示例结论：上下双图让「变了多少」和「为什么变」在同一页对齐。", pageNum: 4 });

  // ===== P5 图文分栏·并列双图 =====
  // 视觉区用 addRegionHeading 共用标题，下方并列两张小 addSystemDiagram（两种协作模式结构），
  // 右栏 addTextBlock 做多图对应（每条对应一张子图）。
  s = pres.addSlide(); s.background = { color: C.THEME.white };
  C.addSlideTitle(pres, s, { title: "路由与聚合，两种协作模式差在哪？", subtitle: "视觉区并列双图，右栏逐图对应其取舍" });
  {
    const gy = C.addRegionHeading(pres, s, { text: "示例 · 两套协作模式结构", x: 0.5, y: 1.05, w: 5.4 });
    C.addSystemDiagram(pres, s, { x: 0.5, y: gy, w: 2.55, h: 3.18, spec: {
      title: "", subtitle: "", core_message: "", diagram_type: "process",
      bands: [
        bandNode("r_in", "路由模式", [item("r_task", "任务请求")], "secondary", "full_width"),
        bandNode("r_route", "路由器", [item("r_pick", "选一个最合适模型")], "action", "full_width"),
        bandNode("r_out", "单模型执行", [item("r_done", "低时延产出")], "result", "full_width"),
      ],
      edges: [],
    } });
    C.addSystemDiagram(pres, s, { x: 3.15, y: gy, w: 2.55, h: 3.18, spec: {
      title: "", subtitle: "", core_message: "", diagram_type: "process",
      bands: [
        bandNode("a_in", "聚合模式", [item("a_task", "任务请求")], "secondary", "full_width"),
        groupBand("a_ref", "并行参考", [group("a_g", "多模型", ["模型 A", "模型 B"])], "factor_group"),
        bandNode("a_out", "聚合执行", [item("a_done", "综合后拍板")], "primary", "full_width"),
      ],
      edges: [],
    } });
    C.addTextBlock(pres, s, { x: 6.1, y: 1.05, w: 3.4, h: 3.55, heading: "两种模式", style: "numbered", items: [
      { title: "路由模式", body: "示例：按任务把请求分给单一最合适的模型，成本最优、时延最低，但没有多视角互补增益。" },
      { title: "聚合模式", body: "示例：多个模型先并行给参考，聚合器综合后再拍板，复杂任务质量更高，代价是成本与延迟成倍上升。" },
      { title: "如何选", body: "示例：高频单点任务走路由，洞察分析、评审研判等高价值复杂任务才启用聚合，按任务价值分流。" },
    ]});
  }
  C.addContentChrome(pres, s, { summary: "示例结论：并列双图把两种结构摆在一起对比，右栏逐图讲清取舍。", pageNum: 5 });

  // ===== P6 系统图整页（单图撑页）=====
  // 复杂结构（分组网格 + 红色虚线警示框 + 反馈回边）整页承载，配横幅收尾。
  s = pres.addSlide(); s.background = { color: C.THEME.white };
  C.addSlideTitle(pres, s, { title: "能力体系、风险约束与业务价值如何闭环？", subtitle: "复杂结构整页承载：构成 → 约束 → 价值 → 反馈" });
  C.addSystemDiagram(pres, s, { x: 0.5, y: 1.05, w: 9.0, h: 3.6, spec: {
    title: "示例能力体系", subtitle: "", core_message: "", diagram_type: "closed_loop",
    bands: [
      groupBand("caps", "能力构成", [
        group("g_data", "数据能力", ["采集接入"]),
        group("g_model", "模型能力", ["训练微调"]),
        group("g_eng", "工程能力", ["编排调度"]),
      ], "factor_group"),
      groupBand("risks", "风险与约束", [
        group("r1", "数据风险", ["数据孤岛", "口径不一"]),
        group("r2", "模型风险", ["幻觉未收敛", "长尾漂移"]),
        group("r3", "工程风险", ["链路耦合重", "回滚成本高"]),
      ], "warning"),
      bandNode("value", "业务价值", [
        item("v1", "效率提升"), item("v2", "质量提升"), item("v3", "成本下降"),
      ], "result", "row"),
    ],
    edges: [
      { from: "caps", to: "risks", relation: "flow", direction: "forward", label: "落地检验", line_style: "solid", emphasis: "normal", routing: "direct", multiplicity: "single" },
      { from: "risks", to: "value", relation: "flow", direction: "forward", label: "治理后产出", line_style: "solid", emphasis: "strong", routing: "direct", multiplicity: "single" },
      { from: "value", to: "caps", relation: "feedback", direction: "forward", label: "", line_style: "dashed", emphasis: "normal", routing: "right_loop", multiplicity: "single" },
    ],
  } });
  C.addContentChrome(pres, s, { summary: "示例结论：系统图把能力构成、风险约束与业务价值一屏说清，且原生可编辑。", pageNum: 6 });

  // ===== P7 表格 + 分析（数据/对比页型）=====
  s = pres.addSlide(); s.background = { color: C.THEME.white };
  C.addSlideTitle(pres, s, { title: "四类协作方案，该怎么选？", subtitle: "对比表列机制与场景，右栏给一句辨析" });
  {
    const [main, side] = C.cols(2, { weights: [2, 1] });
    C.addComparisonTable(pres, s, {
      ...main, y: 1.05, h: 3.6,
      headers: ["维度", "核心机制", "典型优势", "适合场景"],
      rows: [
        ["方案A", "按任务把请求路由给单一最合适的模型", "成本最优、响应低时延", "日常高频、单点明确的任务"],
        ["方案B", "多模型先各给参考，聚合器综合后拍板", "复杂问题输出质量更高", "洞察分析、评审与综合研判"],
        ["方案C", "多角色按工序分工，接力完成长链路", "长链路协作、吞吐更高", "开发、测试、交付流水线"],
        ["方案D", "多智能体半自治协同，边探索边修正", "开放探索与复杂协作强", "仿真推演、研究型开放任务"],
      ],
      rowIcons: [
        { initial: "由", color: "30B5C5" }, { initial: "合", color: "4472C4" },
        { initial: "编", color: "62B230" }, { initial: "群", color: "ED6D00" },
      ],
    });
    C.addPanelList(pres, s, { ...side, y: 1.05, h: 3.6, title: "一句辨析", style: "bullet", items: [
      "方案B 更像多人会审加主审拍板的机制，重在提升单次输出的质量与可靠性。",
      "方案C 更像项目团队按工序分工接力协作，重在长链路任务的吞吐与按期交付。",
      "方案A 关注的只是选谁来做这一件事，成本最优、时延最低但没有互补增益。",
      "方案D 强调群体自治与开放式探索，适合仿真推演与研究型的开放性任务。",
      "没有万能方案：按任务复杂度、容错率与预算三个维度做综合权衡与选型。",
    ]});
  }
  C.addContentChrome(pres, s, { summary: "示例结论：组合方案是位于两个极端之间的关键中间层。", pageNum: 7 });

  // ===== P8 图表 + 洞察（图表页型）=====
  s = pres.addSlide(); s.background = { color: C.THEME.white };
  C.addSlideTitle(pres, s, { title: "组合方案的得分增益该怎么解读？", subtitle: "chart with insights：信号在左、边界在右" });
  C.addChartWithInsights(pres, s, {
    chart: { type: "bar", data: [{ name: "示例得分", labels: ["方案A", "方案B", "组合方案"], values: [11.1, 22.2, 33.3] }] },
    insights: [
      "示例：组合方案得分约为单模型的三倍。",
      "示例：属高质量模式，按任务价值启用。",
      "示例：系统级编排正成为能力放大器。",
      "示例：聚合器质量决定整套系统上限。",
      "示例：收益随任务复杂度上升而扩大。",
    ],
    caveats: [
      "示例：官方口径为主，需第三方验证。",
      "示例：不同组合收益差异大，勿按均值。",
      "示例：延迟与成本成倍增加要先算账。",
      "示例：轻量问题先路由分流再决定升级。",
      "示例：候选同质化时组合基本无增益。",
    ],
  });
  C.addContentChrome(pres, s, { summary: "示例结论：增益值得重视，但要放到成本与时延中综合判断。", pageNum: 8 });

  // ===== P9 纯文字页（无结构关系时的合法页型）=====
  // 双列 addTextBlock（numbered + sectioned），白底结构化，替代灰卡矩阵。
  s = pres.addSlide(); s.background = { color: C.THEME.white };
  C.addSlideTitle(pres, s, { title: "落地 MOA，要点与场景各有哪些？", subtitle: "内容无结构关系时用双列文字块，白底不灰" });
  C.addTextBlock(pres, s, { x: 0.5, y: 1.05, w: 4.4, h: 3.55, style: "numbered", title: "落地要点", items: [
    { title: "结构化承载", body: "示例：标题 + 解释 + 要点分层，由组件负责格式，不靠手拼字符串，层级清晰便于扫读。", children: ["示例：层级清晰", "示例：便于扫读"] },
    { title: "去灰盒化", body: "示例：文字直接铺白底，只有表格与个别图示容器用灰，整屏灰盒子矩阵过不了 QA。" },
    { title: "互补优先", body: "示例：参与模型要有真实能力互补，同质化候选再多也叠加不出增益，先选异构再谈数量。" },
  ] });
  C.addTextBlock(pres, s, { x: 5.1, y: 1.05, w: 4.4, h: 3.55, style: "sectioned", title: "适用场景", items: [
    { title: "复杂研判", body: "示例：洞察分析、方案评审、综合研判等高价值任务，多视角互补能明显抬升输出质量。" },
    { title: "图文右栏", body: "示例：与系统图配对时，右栏用 numbered 逐条对应左图的每个 band，术语与编号保持一致。" },
    { title: "成本敏感", body: "示例：高频单点任务不必上聚合，先路由分流控制成本，把组合留给真正需要的复杂问题。" },
  ] });
  C.addContentChrome(pres, s, { summary: "示例结论：文字块是文字区默认，白底不灰、按栏分层。", pageNum: 9 });

  // ===== P10 参考词汇三件套（矩阵 → 漏斗 → 阶段带 = 整页结构大图配方）=====
  s = pres.addSlide(); s.background = { color: C.THEME.white };
  C.addSlideTitle(pres, s, { title: "复杂分类如何汇聚成行动路径？", subtitle: "淡彩矩阵 + 汇聚漏斗 + 阶段带，整页一张结构大图" });
  C.addTintMatrix(pres, s, {
    x: 0.5, y: 1.05, w: 9.0, h: 1.7,
    rowLabels: ["族群", "子类", "具体条目"],
    groups: [
      { name: "示例族群一", subs: [
        { name: "对象识别", items: ["示例对象", "示例特征"] },
        { name: "场景拆解", items: ["示例任务", "示例边界"] },
      ] },
      { name: "示例族群二", subs: [
        { name: "基础能力", items: ["示例能力甲", "示例能力乙"] },
        { name: "核心方法", items: ["示例框架", "示例方法"] },
        { name: "协同机制", items: ["示例分工", "示例规则"] },
      ] },
      { name: "示例族群三", subs: [
        { name: "资源底座", items: ["示例数据", "示例资产"] },
        { name: "运营支撑", items: ["示例流程", "示例保障"] },
      ] },
      { name: "示例族群四", subs: [
        { name: "治理闭环", items: ["示例规范", "示例迭代"] },
      ] },
    ],
  });
  C.addConvergeFunnel(pres, s, { x: 0.5, y: 2.78, w: 9.0, h: 0.60, label: "分类分析导出的行动路径（示例）" });
  C.addChevronStages(pres, s, {
    x: 0.5, y: 3.42, w: 9.0, h: 1.23,
    stages: [
      { name: "阶段一｜识别与聚焦", card: { title: "分类输出", items: ["示例优先级清单", "示例问题地图"] } },
      { name: "阶段二｜方案与编排", card: { title: "方案输出", items: ["示例方案组合", "示例里程碑表"] } },
      { name: "阶段三｜落地与迭代", card: { title: "成果输出", items: ["示例交付物集", "示例评估闭环"] } },
    ],
  });
  C.addContentChrome(pres, s, { summary: "示例结论：分类矩阵经漏斗汇聚为三阶段路径，一张结构大图讲完整页论证。", pageNum: 10 });

  C.addClosingSlide(pres);

  await pres.writeFile({ fileName: "demo.pptx" });
  console.log("demo.pptx written");
}

main().catch((e) => { console.error(e); process.exit(1); });
