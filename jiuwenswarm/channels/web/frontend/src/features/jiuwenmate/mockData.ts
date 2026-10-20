import type {
  JiuwenMateCapabilities,
  JiuwenMateChannel,
  JiuwenMateChannelMessage,
  JiuwenMateDailyActivity,
  JiuwenMateTask,
  JiuwenMateTemplate,
  JiuwenMateTraceRow,
} from './types';

export const JIUWENMATE_WORK_DOMAINS: readonly string[] = [
  '效率提升',
  '内容创作',
  '客户服务',
  '数据分析',
  '研发工程',
  '教育学习',
  '金融投资',
  '法律咨询',
];

export const JIUWENMATE_GENERATED_CAPABILITIES: Omit<JiuwenMateCapabilities, 'model' | 'starterPrompts'> = {
  skills: ['context-analyst', 'report-writer', 'faq-builder'],
  connectors: ['腾讯问卷'],
  plugins: ['网页搜索'],
  knowledge: [],
};

export function emptyCapabilities(): JiuwenMateCapabilities {
  return { model: '', skills: [], connectors: [], plugins: [], knowledge: [], starterPrompts: [] };
}

const DESIGN_TAGS = ['效率提升', '内容创作'];

function templateCapabilities(starterPrompts: string[]): JiuwenMateCapabilities {
  return {
    model: '',
    skills: [],
    connectors: ['腾讯问卷', '飞书文档'],
    plugins: ['网页搜索', '文件解析'],
    knowledge: [],
    starterPrompts,
  };
}

export const JIUWENMATE_TEMPLATES: readonly JiuwenMateTemplate[] = [
  {
    id: 'english-coach',
    name: '英语表达教练',
    description: '基于真实写作或口语样本定位高影响问题，提供解释、自然改写与可以复测的针对性练习。',
    tags: DESIGN_TAGS,
    category: 'education',
    skillSearchTerms: [
      '英文润色',
      '语法检查',
      '英语写作',
      '口语练习',
      'english writing',
      'grammar correction',
      'language learning',
    ],
    coreAbilities: [
      { title: '问题定位', description: '从写作或口语样本中找出影响表达效果的关键问题。' },
      { title: '自然改写', description: '给出符合语境的改写，并说明改写理由。' },
      { title: '针对练习', description: '生成可复测的练习，跟踪薄弱点的改善情况。' },
    ],
    capabilities: templateCapabilities([
      '帮我润色这段英文邮件。',
      '指出这段口语稿中的语法问题。',
      '为我生成三道针对性练习。',
    ]),
  },
  {
    id: 'system-architect',
    name: '系统架构工程师',
    description: '精通后端系统架构设计与演进规划，梳理模块依赖关系，开展方案评审与风险识别，输出可落地技术改造思路。',
    tags: DESIGN_TAGS,
    category: 'development',
    skillSearchTerms: [
      '系统架构',
      '依赖分析',
      '技术方案评审',
      '代码审查',
      'backend architecture',
      'software design',
      'code review',
    ],
    coreAbilities: [
      { title: '依赖梳理', description: '梳理模块之间的调用与数据依赖，识别耦合点。' },
      { title: '方案评审', description: '对技术方案进行评审，指出风险与替代方案。' },
      { title: '演进规划', description: '输出分阶段、可落地的架构改造路线。' },
    ],
    capabilities: templateCapabilities([
      '评审这份服务拆分方案。',
      '梳理订单模块的依赖关系。',
      '输出缓存层的改造思路。',
    ]),
  },
  {
    id: 'fundamental-researcher',
    name: '基本面研究员',
    description: '区分已知事实、分析假设与待核验数据，通过业务质量、情景、风险和催化因素形成研究框架。',
    tags: DESIGN_TAGS,
    category: 'data',
    skillSearchTerms: [
      '基本面分析',
      '行业研究',
      '公司研究',
      '财务分析',
      '风险评估',
      'equity research',
      'financial analysis',
    ],
    coreAbilities: [
      { title: '事实分层', description: '区分已知事实、分析假设与待核验数据。' },
      { title: '情景分析', description: '构建多情景假设，评估关键变量的影响。' },
      { title: '研究框架', description: '围绕业务质量、风险与催化因素形成结论。' },
    ],
    capabilities: templateCapabilities([
      '整理这家公司的业务质量要点。',
      '列出需要核验的关键数据。',
      '给出三种情景下的判断。',
    ]),
  },
  {
    id: 'serial-story-planner',
    name: '连载故事策划',
    description: '用原创故事圣经、人物弧线、章节节拍和连续性检查，统筹可持续推进的长篇连载创作。',
    tags: DESIGN_TAGS,
    category: 'document',
    skillSearchTerms: [
      '小说创作',
      '故事大纲',
      '角色设定',
      '情节规划',
      'fiction writing',
      'story planning',
      'character development',
    ],
    coreAbilities: [
      { title: '故事圣经', description: '沉淀世界观、人物设定与关键事件。' },
      { title: '章节节拍', description: '规划每章的冲突、转折与情绪节奏。' },
      { title: '连续性检查', description: '检查人物与情节前后是否一致。' },
    ],
    capabilities: templateCapabilities([
      '为下一章设计三个节拍。',
      '检查主角动机是否前后一致。',
      '整理当前的人物关系。',
    ]),
  },
  {
    id: 'personal-finance-expert',
    name: '个人理财专家',
    description: '开展行业调研、竞品分析、趋势研判，输出市场洞察与决策参考，辅助业务战略判断。',
    tags: DESIGN_TAGS,
    category: 'finance',
    skillSearchTerms: [
      '个人理财',
      '预算分析',
      '支出分析',
      '投资规划',
      'personal finance',
      'budgeting',
      'investment planning',
    ],
    coreAbilities: [
      { title: '行业调研', description: '收集行业信息并提炼关键趋势。' },
      { title: '竞品分析', description: '对比主要竞品的定位与表现。' },
      { title: '决策参考', description: '输出有依据的市场洞察与建议。' },
    ],
    capabilities: templateCapabilities([
      '分析本月的支出结构。',
      '对比两种定投方案。',
      '整理近期行业动态。',
    ]),
  },
  {
    id: 'case-organizer',
    name: '案件整理顾问',
    description: '开展行业调研、竞品分析、趋势研判，输出市场洞察与决策参考，辅助业务战略判断。',
    tags: DESIGN_TAGS,
    category: 'legal',
    skillSearchTerms: [
      '案件分析',
      '法律文书',
      '证据整理',
      '时间线整理',
      'legal case analysis',
      'legal document',
      'evidence organization',
    ],
    coreAbilities: [
      { title: '材料归档', description: '按时间线与争议焦点整理案件材料。' },
      { title: '要点提炼', description: '提炼事实、证据与法律关系。' },
      { title: '文书初稿', description: '生成结构清晰的案件摘要初稿。' },
    ],
    capabilities: templateCapabilities([
      '按时间线整理这些材料。',
      '提炼本案的争议焦点。',
      '生成案件摘要初稿。',
    ]),
  },
  {
    id: 'video-storyboard',
    name: '视频分镜师',
    description: '将沟通目标转成带时长、镜头、画面、声音与制作约束的可执行分镜，并检查素材权利风险。',
    tags: DESIGN_TAGS,
    category: 'commerce',
    skillSearchTerms: [
      '视频分镜',
      '故事板',
      '镜头规划',
      '素材版权',
      'video storyboard',
      'script breakdown',
      'visual storytelling',
    ],
    coreAbilities: [
      { title: '分镜拆解', description: '把沟通目标拆成镜头、时长与画面描述。' },
      { title: '制作约束', description: '标注声音、场景与制作资源要求。' },
      { title: '权利检查', description: '检查素材使用是否存在权利风险。' },
    ],
    capabilities: templateCapabilities([
      '把这段脚本拆成分镜。',
      '估算每个镜头的时长。',
      '检查素材的授权风险。',
    ]),
  },
  {
    id: 'research-writing-advisor',
    name: '研究写作顾问',
    description: '围绕研究问题、方法匹配、证据链与论证边界，帮助形成结构清晰且可核验的研究文本。',
    tags: DESIGN_TAGS,
    category: 'content',
    skillSearchTerms: [
      '学术写作',
      '论文分析',
      '研究方法',
      '证据链',
      'academic writing',
      'research methodology',
      'literature review',
    ],
    coreAbilities: [
      { title: '问题聚焦', description: '帮助明确研究问题与边界。' },
      { title: '方法匹配', description: '为研究问题选择合适的方法。' },
      { title: '证据链检查', description: '检查论证是否有充分的证据支撑。' },
    ],
    capabilities: templateCapabilities([
      '帮我梳理这篇论文的论证结构。',
      '检查这一节的证据是否充分。',
      '为研究问题推荐方法。',
    ]),
  },
];

export const JIUWENMATE_CUSTOM_SUGGESTIONS: readonly { name: string; description: string }[] = [
  {
    name: '客服小助手',
    description: '群内专属客服小助，负责解答业务咨询、查询信息、接收问题反馈，简洁高效回复群成员问题，不闲聊无关内容。',
  },
  {
    name: '办公专家',
    description: '高效处理邮件、报告、会议纪要等各类文档的撰写与润色。',
  },
];

export const JIUWENMATE_CHANNELS: readonly JiuwenMateChannel[] = [
  { id: 'robot-trial-group', name: '扫地机器人试用群', platform: 'feishu', artifacts: 3, tokenUsage: 108000, lastResponseMinutes: 3 },
  { id: 'new-product-feedback-group', name: '新品售后反馈群', platform: 'feishu', artifacts: 3, tokenUsage: 112000, lastResponseMinutes: 3 },
  { id: 'veteran-user-group', name: '老用户交流群', platform: 'feishu', artifacts: 3, tokenUsage: 108000, lastResponseMinutes: 3 },
  { id: 'premium-user-group', name: '高端机型用户专属群', platform: 'feishu', artifacts: 3, tokenUsage: 8000, lastResponseMinutes: 3 },
];

export const JIUWENMATE_CHANNEL_MESSAGES: readonly JiuwenMateChannelMessage[] = [
  { id: 'message-shen-1', sender: '沈女士', fromColleague: false, mentionColleague: true, content: '我的扫地机器人回充经常失败是什么情况？' },
  {
    id: 'message-bot-1',
    sender: '',
    fromColleague: true,
    mentionColleague: false,
    content: '您好，回充失败可以先检查充电座周边有没有杂物遮挡。如果依旧异常，可以发送【反馈问题】，我帮你记录提交人工。',
  },
  { id: 'message-zhou-1', sender: '周女士', fromColleague: false, mentionColleague: true, content: '扫地机能不能设置分区定时清扫？' },
  {
    id: 'message-bot-2',
    sender: '',
    fromColleague: true,
    mentionColleague: false,
    content: '支持分区定时清扫，在设备 APP 清扫设置里可以配置对应房间与时间。有其他问题欢迎继续说明。',
  },
  { id: 'message-zhou-2', sender: '周女士', fromColleague: false, mentionColleague: true, content: '这个机器避障太差了，老是撞椅子腿，希望后续固件优化一下。' },
  {
    id: 'message-bot-3',
    sender: '',
    fromColleague: true,
    mentionColleague: false,
    content: '收到你的使用建议，我帮你完整登记这条诉求。',
    attachment: { title: 'Altrenddashboard', kind: '工单' },
  },
];

const TRACE_USER_PREFIX = '你收到一条消息：{"source":"web","timestamp":"2026-09-22';
const TRACE_ASSISTANT_INTRO =
  '我是**JiuwenSwarm**，能帮你完成以下几类工作： ## 软件工程 – **写代码**：从零搭建项目、实现新功能、编辑脚本 – **调试修复**：定位问题、分析报错、修复Bug';

export const JIUWENMATE_TRACE_ROWS: readonly JiuwenMateTraceRow[] = [
  {
    id: 'span-system',
    traceId: 'trace-a',
    turn: 0,
    role: 'system',
    lane: 'input',
    content:
      'You are JiuwenSwarm, an interactive agent that helps users with software engineering tasks. IMPORTANT: Assist with authorized security testing only.',
    startMs: 0,
    durationMs: 1200,
    inputTokens: 1170,
    outputTokens: 0,
  },
  {
    id: 'span-t1-context',
    traceId: 'trace-a',
    turn: 1,
    role: 'context',
    lane: 'input',
    content:
      "<system-reminder> The following content does not represent the user's intent and is not a direct instruction from the user. It is dynamic context.",
    startMs: 1300,
    durationMs: 1100,
    inputTokens: 820,
    outputTokens: 0,
  },
  {
    id: 'span-t1-user',
    traceId: 'trace-a',
    turn: 1,
    role: 'user',
    lane: 'input',
    content: `${TRACE_USER_PREFIX} 14:38:59 (UTC+08:00, Asia/Shanghai)"}`,
    startMs: 2500,
    durationMs: 1000,
    inputTokens: 117,
    outputTokens: 0,
  },
  {
    id: 'span-t1-assistant',
    traceId: 'trace-a',
    turn: 1,
    role: 'assistant',
    lane: 'model',
    content: TRACE_ASSISTANT_INTRO,
    startMs: 3600,
    durationMs: 1900,
    inputTokens: 0,
    outputTokens: 111,
  },
  {
    id: 'span-t2-user',
    traceId: 'trace-a',
    turn: 2,
    role: 'user',
    lane: 'input',
    content: `${TRACE_USER_PREFIX} 14:39:22 (UTC+08:00, Asia/Shanghai)"}`,
    startMs: 5600,
    durationMs: 900,
    inputTokens: 121,
    outputTokens: 0,
  },
  {
    id: 'span-t2-assistant',
    traceId: 'trace-a',
    turn: 2,
    role: 'assistant',
    lane: 'model',
    content: TRACE_ASSISTANT_INTRO,
    startMs: 6600,
    durationMs: 1600,
    inputTokens: 0,
    outputTokens: 104,
  },
  {
    id: 'span-t3-user',
    traceId: 'trace-b',
    turn: 3,
    role: 'user',
    lane: 'input',
    content: `${TRACE_USER_PREFIX} 15:03:35 (UTC+08:00, Asia/Shanghai)"}`,
    startMs: 8300,
    durationMs: 1000,
    inputTokens: 126,
    outputTokens: 0,
  },
  {
    id: 'span-t3-assistant-plan',
    traceId: 'trace-b',
    turn: 3,
    role: 'assistant',
    lane: 'model',
    content: '我来帮你搜索杭州今日的天气。',
    startMs: 9400,
    durationMs: 1400,
    inputTokens: 0,
    outputTokens: 36,
  },
  {
    id: 'span-t3-tool',
    traceId: 'trace-b',
    turn: 3,
    role: 'tool',
    lane: 'tools',
    content:
      'paid_search（"query": "杭州今日天气 2026年9月22日"） → Paid search provider: bocha Answer: 2026年09月22日杭州今天预报：09/22（周二）多云到阴',
    startMs: 10900,
    durationMs: 1300,
    inputTokens: 64,
    outputTokens: 320,
  },
  {
    id: 'span-t3-assistant',
    traceId: 'trace-b',
    turn: 3,
    role: 'assistant',
    lane: 'model',
    content:
      '## 杭州今日天气（2026年9月22日 周二）| 项目 | 详情 | | **天气** | 多云到阴 | | **最高气温** | 31°C | | **最低气温** | 23°C | | **风向风力** | 东北风 3 级 |',
    startMs: 12300,
    durationMs: 1500,
    inputTokens: 0,
    outputTokens: 214,
  },
];

export const JIUWENMATE_TRACE_STARTED_AT = '2026-09-22T14:38:04.764+08:00';

const DAILY_ACTIVITY_ROWS: ReadonlyArray<readonly [string, number, number]> = [
  ['2025-11-12', 24, 0.8],
  ['2025-11-19', 66, 1.9],
  ['2025-11-20', 38, 1.2],
  ['2025-11-26', 92, 2.6],
  ['2025-11-27', 41, 1.3],
  ['2025-12-03', 18, 0.6],
  ['2025-12-10', 56, 1.7],
  ['2025-12-11', 33, 1.0],
  ['2025-12-17', 120, 3.4],
  ['2025-12-18', 74, 2.1],
  ['2025-12-24', 29, 0.9],
  ['2025-12-31', 47, 1.4],
  ['2026-01-07', 84, 2.4],
  ['2026-01-08', 36, 1.1],
  ['2026-01-14', 97, 2.8],
  ['2026-01-15', 58, 1.8],
  ['2026-01-16', 112, 3.1],
  ['2026-01-21', 131, 3.6],
  ['2026-01-22', 63, 1.9],
  ['2026-01-28', 44, 1.3],
  ['2026-02-04', 52, 1.6],
  ['2026-02-11', 27, 0.9],
  ['2026-02-18', 39, 1.2],
  ['2026-03-04', 21, 0.7],
  ['2026-03-11', 35, 1.1],
  ['2026-04-15', 18, 0.6],
  ['2026-05-20', 26, 0.8],
  ['2026-07-08', 31, 1.0],
  ['2026-08-12', 22, 0.7],
  ['2026-09-16', 48, 1.5],
  ['2026-09-23', 64, 2.0],
  ['2026-09-30', 40, 1.3],
  ['2026-10-07', 73, 2.2],
  ['2026-10-08', 28, 0.9],
];

export const JIUWENMATE_DAILY_ACTIVITY: readonly JiuwenMateDailyActivity[] = DAILY_ACTIVITY_ROWS.map(
  ([date, tasks, hours]) => ({ date, tasks, hours }),
);

export const JIUWENMATE_HOURLY_ACTIVITY: ReadonlyArray<readonly number[]> = [
  [23, 0, 0, 0, 0, 0, 0, 0, 0, 0, 15, 16, 0, 32, 32, 0, 30, 31, 0, 0, 0, 0, 0, 0],
  [30, 16, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 62, 0, 36, 62, 32, 0, 0, 0, 32, 0, 52, 49],
  [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 24, 26, 0, 0, 70, 32, 36, 36, 67, 28, 38, 0, 58, 46],
  [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 24, 36, 24, 0, 70, 0, 32, 37, 70, 70, 70, 36, 63, 0],
  [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 36, 24, 32, 35, 0, 36, 36, 66, 32, 46, 28, 44, 38],
  [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 36, 36, 36, 0, 33, 32, 45, 0, 42, 54, 23, 0, 0],
  [0, 0, 0, 0, 0, 0, 0, 0, 0, 22, 36, 0, 0, 26, 29, 0, 16, 12, 0, 0, 82, 74, 0, 0],
];

export const JIUWENMATE_MEMBER_STATS: ReadonlyArray<{
  tokens: number;
  companionDays: number;
  chats: number;
  lastResponseMinutes: number;
}> = [
  { tokens: 108000, companionDays: 5, chats: 1, lastResponseMinutes: 3 },
  { tokens: 112000, companionDays: 5, chats: 5, lastResponseMinutes: 3 },
  { tokens: 108000, companionDays: 2, chats: 2, lastResponseMinutes: 3 },
  { tokens: 8000, companionDays: 6, chats: 6, lastResponseMinutes: 3 },
  { tokens: 8000, companionDays: 3, chats: 3, lastResponseMinutes: 3 },
  { tokens: 8000, companionDays: 3, chats: 3, lastResponseMinutes: 3 },
];

export const JIUWENMATE_TASKS: readonly JiuwenMateTask[] = [
  { id: 'task-waiting-1', title: '需求分析建立目标', group: '扫地机器人试用群', status: 'waiting', progress: 0, duration: '0', tokens: '0' },
  { id: 'task-waiting-2', title: '需求分析建立目标', group: '扫地机器人试用群', status: 'waiting', progress: 0, duration: '0', tokens: '0' },
  { id: 'task-running-1', title: '市场研究与大盘洞察', group: '扫地机器人试用群', status: 'running', progress: 70, duration: '5分30秒', tokens: '1.05K' },
  { id: 'task-running-2', title: '营销渠道转化分析', group: '扫地机器人试用群', status: 'running', progress: 40, duration: '5分30秒', tokens: '1.05K' },
  { id: 'task-running-3', title: '定价策略分析', group: '扫地机器人试用群', status: 'running', progress: 65, duration: '5分30秒', tokens: '1.05K' },
  { id: 'task-done-1', title: '需求分析建立目标', group: '扫地机器人试用群', status: 'done', progress: 100, duration: '5分30秒', tokens: '1.05K' },
  { id: 'task-done-2', title: '下发任务与分工', group: '扫地机器人试用群', status: 'done', progress: 100, duration: '5分30秒', tokens: '1.05K' },
];
