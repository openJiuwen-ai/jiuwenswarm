import type { Surface } from './appContract';

/** The texts that name the document, by what the host edits. */
export interface DocumentCopy {
  noun: string;
  welcomeBody: string;
  suggestions: readonly string[];
}

export const documentCopy: Record<Surface, DocumentCopy> = {
  presentation: {
    noun: '演示文稿',
    welcomeBody: '我可以直接新建、修改和检查这份演示文稿。',
    suggestions: ['总结这份演示文稿的结构', '把每页标题改成一句结论', '检查版面有没有重叠和越界'],
  },
  document: {
    noun: '文档',
    welcomeBody: '我可以在这份文档里查找、修改和审阅文字，改动较大的地方会先给你建议，由你决定是否采用。',
    suggestions: ['总结这份文档的要点', '改写选中的段落，让它更简洁', '检查错别字和编号是否连续'],
  },
  workbook: {
    noun: '工作簿',
    welcomeBody: '我可以在这个工作簿里读数据、写公式、整理格式和做图表。',
    suggestions: ['总结选中区域的数据', '给这张表加合计行', '检查公式里的错误值'],
  },
};

/** Every text the task pane shows, in one place. */
export const copy = {
  history: '本文档的对话',
  openFiles: (noun: string) => `打开的${noun}`,
  thisFile: '本文件',
  justNow: '刚刚',
  minutesAgo: (minutes: number) => `${minutes}分钟`,
  hoursAgo: (hours: number) => `${hours}小时`,
  daysAgo: (days: number) => `${days}天`,
  untitledConversation: '新对话',
  newConversation: '新对话',
  more: '更多',
  downloadTranscript: '下载对话记录',
  transcriptFile: '对话记录',
  transcriptUser: '你',
  transcriptAssistant: 'WorkSwarm',
  transcriptSteps: (steps: string) => `过程：${steps}`,
  model: '模型',
  modelOf: (name: string) => (name ? `模型：${name}` : '模型'),
  newConversationFailed: (reason: string) => `新建对话失败：${reason}`,
  historyFailed: (reason: string) => `历史消息暂时无法加载：${reason}`,
  loadingHistory: '正在加载历史消息…',
  dismiss: '关闭提示',
  transcript: '对话记录',

  welcomeTitle: '你好，我是 WorkSwarm',

  connecting: (noun: string) => `正在连接${noun}…`,
  connectionDown: (seconds: number, attempt: number) => `连接已中断，${seconds} 秒后重连（第 ${attempt} 次）`,
  reconnecting: '连接已中断，正在重连…',
  displaced: (noun: string) => `这份${noun}已在另一个窗口中打开，那里会继续执行它的操作。`,
  continueHere: '在这里继续',
  failed: (noun: string, reason: string) => `无法连接${noun}：${reason}`,
  retry: '重试',
  paneFailed: (reason: string) => `WorkSwarm 无法在这份演示文稿中打开：${reason}`,
  presentationClosed: '这个窗格的演示文稿已经关闭。',
  presentationShared: '有两份打开的演示文稿带有同一个标识（一份是另一份的副本）。关闭其中一份后点“重试”。',

  placeholder: '需要我帮你做些什么？',
  placeholderRunning: '补充要求，会加入当前任务',
  placeholderDisabled: (noun: string) => `${noun}连接后即可发送`,
  placeholderPrompt: '请先处理上方的请求',
  placeholderDisplaced: '点上方的“在这里继续”后可发送',
  placeholderOffline: '连接恢复后即可发送',
  placeholderLoading: '正在加载历史消息…',
  messageLabel: '给 WorkSwarm 的消息，Enter 发送，Shift+Enter 换行',
  send: '发送',
  stop: '停止',
  addImages: '添加图片',
  removeImage: (name: string) => `移除图片 ${name}`,
  imageType: (name: string) => `图片类型不支持：${name}（支持 PNG、JPEG、WebP、GIF）`,
  imageSize: (name: string, megabytes: number) => `图片太大：${name}（单张最大 ${megabytes} MB）`,
  imageCount: (limit: number) => `每条消息最多 ${limit} 张图片`,
  imageRead: (name: string) => `图片读取失败：${name}`,
  imagesWait: '图片要等这一轮结束后发送',
  transcriptImage: (name: string) => `[图片：${name}]`,
  sendFailed: (reason: string) => `发送失败：${reason}`,
  stopFailed: (reason: string) => `停止任务失败：${reason}`,
  stopped: '已停止任务。',
  turnError: (reason: string) => `出错了：${reason}`,
  turnErrorFallback: '执行失败',
  replied: (text: string) => `WorkSwarm 回复：${text}`,

  seconds: (seconds: number) => `${seconds} 秒`,
  process: '查看过程',
  slide: (index: number) => `第 ${index} 页`,
  copyReply: '复制',
  copied: '已复制',
  thinking: '正在思考',
  undo: '撤销本轮修改',
  undoChanged: '后续修改会被覆盖',
  undoAnyway: '仍然撤销',
  cancel: '取消',
  undoGone: '已无法撤销',
  undoFailed: (reason: string) => `撤销失败：${reason}`,
  /** The line the next message starts with, so the agent rereads the slides the undo gave new ids. */
  undoNote: (positions: readonly number[]) => `（已撤销你此前的修改${
    positions.length ? `，恢复了第 ${positions.join('、')} 页` : ''}；这些页的 id 已变化，请先重新读取演示文稿。）`,

  approvalTitle: '需要你的授权',
  approvalAsk: (action: string) => `允许${action}？`,
  approvalOptions: { reject: '拒绝', session_allow: '本对话内始终允许', allow_once: '允许一次' } as Record<string, string>,
  approvalKeys: { reject: 'Esc', allow_once: 'Enter' } as Record<string, string>,
  approvalArguments: '查看完整参数',
  /** The names of the arguments an approval shows in words; the others show in the full arguments only. */
  payloadLabels: {
    target_session_id: '目标文档', message: '内容', slide_id: '页面', ref: '位置', explanation: '说明',
  } as Record<string, string>,
  approvalNeeded: '需要你的授权才能继续',
  questionAsked: '智能体在等你回答问题',
  askTitle: '请回答问题',
  customAnswer: '其他回答（可选）',
  submitAnswer: '提交',
  answerFailed: (reason: string) => `提交回答失败：${reason}`,
  askedInHistory: (questions: string) => `已询问：${questions}`,
  answeredInHistory: (answers: string) => `已回答：${answers}`,

  editCards: '修改建议',
  editSuggested: '建议',
  editApply: '直接修改',
  editTracked: '作为修订',
  editComment: '加为批注',
  editSkip: '跳过',
  editUndo: '撤销',
  editBusy: '处理中',
  editApplied: { edit: '已修改', tracked: '已作为修订', comment: '已加批注' },
  editStatus: {
    pending: '待处理', dismissed: '已跳过', withdrawn: '已撤回', undone: '已撤销', failed: '未能应用',
  } as Record<string, string>,

  incomingFrom: (source: string) => `来自「${source}」的任务`,
  outgoingTo: (target: string) => `发给「${target}」的任务`,
  otherConversation: '另一个对话',
  messageStatus: {
    sending: '发送中',
    sent: '已发送',
    queued: '排队中',
    running: '处理中',
    waiting_user: '等待你确认',
    delivered: '已送达',
    succeeded: '已处理',
    failed: '失败',
    cancelled: '已取消',
    unknown: '结果未知',
    expired: '已过期',
  } as Record<string, string>,

  /** What each tool does, as a step of the turn says it; a tool not listed shows its name. */
  tools: {
    read_presentation_context: '读取演示文稿',
    list_slide_shapes: '查看页面上的形状',
    read_slide_image: '截取页面图片',
    read_slide_text: '读取页面文字',
    read_chart_style: '读取图表样式',
    verify_slides: '检查版面',
    verify_slide_visual: '视觉复核',
    edit_slide_text: '修改页面文字',
    edit_slide_xml: '修改页面',
    edit_slide_chart: '修改图表',
    create_slide_chart: '新建图表',
    edit_built_chart: '修改图表',
    edit_slide_master: '修改母版',
    find_and_replace: '查找替换',
    presentation_apply_patch: '更新页面',
    duplicate_slide: '复制页面',
    import_slides: '导入页面',
    copy_image_between_slides: '复制图片到其他页面',
    get_cell_ranges: '读取单元格',
    get_range_as_csv: '读取表格数据',
    set_cell_range: '写入单元格',
    copy_paste_range: '复制区域',
    execute_office_js: '运行代码',
    read_surface_context: '读取当前选区',
    read_doc_section: '读取文档段落',
    search_doc_text: '查找文字',
    edit_doc_text: '修改文字',
    edit_doc_list: '编辑列表',
    collapse_blank_paragraphs: '合并空行',
    verify_doc: '检查文档结构',
    propose_doc_edits: '提出修改建议',
    withdraw_doc_edits: '撤回修改建议',
    explain_edits: '核对修改依据',
    verify_doc_visual: '检查页面排版',
    list_open_documents: '查看打开的文档',
    publish_office_asset: '准备素材文件',
    session_send_message: '发送任务给另一份文档',
    tool_search: '查找工具',
    skill_tool: '读取技能说明',
    ask_user: '向你提问',
  } as Record<string, string>,
} as const;

export function toolLabel(name: string): string {
  return copy.tools[name] ?? name;
}

/** The text of an error for the user. */
export function reasonOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
