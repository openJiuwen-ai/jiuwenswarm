import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, Minus, Plus, Loader2 } from 'lucide-react';
import { useConnectorStore } from '../../stores/connectorStore';

type McpConfigType = 'stdio' | 'streamable-http';

interface KeyValueRow {
  id: number;
  key: string;
  value: string;
}

let rowSeq = 0;
function newRow(): KeyValueRow {
  rowSeq += 1;
  return { id: rowSeq, key: '', value: '' };
}

function rowsFromArray(values: string[]): KeyValueRow[] {
  if (values.length === 0) return [newRow()];
  return values.map((v) => ({ ...newRow(), key: v }));
}

function rowsFromRecord(record: Record<string, string>): KeyValueRow[] {
  const entries = Object.entries(record);
  if (entries.length === 0) return [newRow()];
  return entries.map(([k, v]) => ({ ...newRow(), key: k, value: v }));
}

interface ParsedMcpConfig {
  name?: string;
  transport: McpConfigType;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  headers?: Record<string, string>;
}

// 2026-08-11：这个函数是这次新加的——之前"添加JSON代码"这个输入框只把粘贴的文本存进
// jsonPaste 这个 state，提交时压根没读它，粘贴了也是白粘（真实 bug：用户粘贴标准 mcpServers
// JSON 配置，最后发给后端的 command/args/env 全是空的，因为走的是手填字段的默认值）。
//
// 兼容业界通用的"mcpServers 包裹"格式（Claude Desktop/Cursor 等配置文件同款写法，也是这个
// 表单自己 placeholder 给的示例格式），也兼容不带 mcpServers 包裹、直接 {name: {...}} 的写法。
// 取第一个 server key 当名称——这个表单一次只能填一个 MCP，粘贴的 JSON 里如果有多个 server
// 只取第一个，不做批量导入。
function parseJsonPasteConfig(raw: string): ParsedMcpConfig | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return null;
  }
  if (typeof parsed !== 'object' || parsed === null) return null;
  const root = parsed as Record<string, unknown>;
  const serversObj =
    typeof root.mcpServers === 'object' && root.mcpServers !== null
      ? (root.mcpServers as Record<string, unknown>)
      : root;
  const firstKey = Object.keys(serversObj)[0];
  if (!firstKey) return null;
  const cfgRaw = serversObj[firstKey];
  if (typeof cfgRaw !== 'object' || cfgRaw === null) return null;
  const cfg = cfgRaw as Record<string, unknown>;

  const rawType = typeof cfg.type === 'string' ? cfg.type.toLowerCase() : '';
  const hasUrl = typeof cfg.url === 'string' && cfg.url.trim().length > 0;
  const transport: McpConfigType =
    rawType === 'stdio'
      ? 'stdio'
      : rawType === 'sse' || rawType === 'http' || rawType === 'streamable-http'
        ? 'streamable-http'
        : hasUrl
          ? 'streamable-http'
          : 'stdio';

  const result: ParsedMcpConfig = { name: firstKey, transport };
  if (typeof cfg.command === 'string') result.command = cfg.command;
  if (Array.isArray(cfg.args)) result.args = cfg.args.map((a) => String(a));
  if (typeof cfg.env === 'object' && cfg.env !== null) {
    result.env = Object.fromEntries(Object.entries(cfg.env as Record<string, unknown>).map(([k, v]) => [k, String(v)]));
  }
  if (typeof cfg.url === 'string') result.url = cfg.url;
  if (typeof cfg.headers === 'object' && cfg.headers !== null) {
    result.headers = Object.fromEntries(Object.entries(cfg.headers as Record<string, unknown>).map(([k, v]) => [k, String(v)]));
  }
  return result;
}

interface RegisterMcpPageProps {
  onBack: () => void;
  onRegistered: () => void;
  /**
   * 编辑已有自定义 MCP 时传入原名字——按 MCP 接口文档 §9"自定义 MCP 编辑流程"：name 只读，
   * 表单从 mcp.show(name) 回填，提交时仍调 mcp.register_custom(同名, 新配置)，后端按同名识别成
   * "编辑"（先 remove 旧 live 实例再用新配置注册），不是走一个独立的 update 接口。不传就是原有
   * 的"新建"流程。
   */
  editName?: string;
}

const JSON_EXAMPLE = `// 示例:
// {
//   "mcpServers": {
//     "example-server": {
//       "command": "npx",
//       "args": [
//         "-y",
//         "mcp-server-example"
//       ]
//     }
//   }
// }`;

// 对应高保真 3.7/3.8 配置MCP。上一轮把"环境变量传递"/"工作目录"/"来自环境变量的标头"/
// "添加JSON代码"这几个字段直接漏掉了——不是有意精简，是照着 connector.register_custom
// 接口文档字段表一个个对应时想当然漏了没有直接对应字段的几项。这一轮全部按 demo 补回来，
// 其中"环境变量传递"和"来自环境变量的标头"其实不需要新接口：接口文档里 credentials/mcp_spec
// 都用 `${VAR}` 占位符表示"取值来自环境变量"（见 §3.3/§5.2），所以这两组字段提交时按
// `${行内填的名字}` 拼进 env/headers 里即可，复用同一套占位符机制，不是新概念。
// 2026-08-07：用户明确要求 stdio 型 MCP 配置去掉"工作目录"（cwd）这个字段——它本来就是
// register_custom 参数表里没有对应字段的（需求16，backend-requests.md），乐观发送的意义
// 不大，直接从表单里去掉，不再提交这个参数。
// 2026-08-11 修复：下面"添加JSON代码"那个文本框之前是纯摆设——粘贴内容只存进 jsonPaste 这个
// state，handleSubmit 从来没读过它，实际提交的还是手填字段的默认值（真实 bug：粘贴标准
// mcpServers JSON 配置后，发给后端的 command/args/env 全是空的）。现在改成粘贴/编辑时就地解析
// （parseJsonPasteConfig）并回填 name/type/command/args/env/url/headers，用户能立刻看到解析
// 结果，也能在此基础上继续手动微调。
export function RegisterMcpPage({ onBack, onRegistered, editName }: RegisterMcpPageProps) {
  const { t } = useTranslation();
  const registerCustom = useConnectorStore((s) => s.registerCustom);
  const detail = useConnectorStore((s) => (editName ? s.detailCache[editName] : undefined));
  const loadDetail = useConnectorStore((s) => s.loadDetail);
  const [type, setType] = useState<McpConfigType>('stdio');
  const [name, setName] = useState('');
  const [command, setCommand] = useState('');
  const [url, setUrl] = useState('');
  const [bearerEnvKey, setBearerEnvKey] = useState('');
  const [args, setArgs] = useState<KeyValueRow[]>([newRow()]);
  const [env, setEnv] = useState<KeyValueRow[]>([newRow()]);
  const [envPassthrough, setEnvPassthrough] = useState<KeyValueRow[]>([newRow()]);
  const [httpHeaders, setHttpHeaders] = useState<KeyValueRow[]>([newRow()]);
  const [httpHeadersFromEnv, setHttpHeadersFromEnv] = useState<KeyValueRow[]>([newRow()]);
  const [jsonPaste, setJsonPaste] = useState('');
  const [creating, setCreating] = useState(false);
  const [backfilled, setBackfilled] = useState(false);
  // 必填项前端拦截：后端 register_custom_mcp 校验 name 必填；transport=stdio 时 command
  // 必填、transport=streamable-http 时 url 必填（server/runtime/mcp/registry.py）。留空提交
  // 会被后端拒，这里在点"确定"时先本地逐项校验，命中的项红框 + 下方提示。
  const [fieldErrors, setFieldErrors] = useState<{ name?: boolean; command?: boolean; url?: boolean }>({});

  const clearFieldError = (key: 'name' | 'command' | 'url') =>
    setFieldErrors((prev) => (prev[key] ? { ...prev, [key]: false } : prev));

  // 编辑模式：进入即按文档 §9 重新拉一次 mcp.show(editName)（refresh:true，不吃详情页可能已有
  // 的旧缓存），拿到 transport/command/args/env/url/headers 后一次性回填表单。只在第一次拿到
  // 数据时回填（backfilled 门控），避免后台轮询刷新 detailCache 时把用户正在编辑的内容覆盖掉。
  useEffect(() => {
    if (!editName) return;
    void loadDetail(editName, { refresh: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editName]);

  useEffect(() => {
    if (!editName || !detail || backfilled) return;
    setName(detail.name);
    setType(detail.transport === 'streamable-http' || detail.transport === 'sse' ? 'streamable-http' : 'stdio');
    if (detail.command) setCommand(detail.command);
    if (detail.args) setArgs(rowsFromArray(detail.args));
    // env/headers 原样明文回填（文档 §9 明确允许），"来自环境变量的XXX"这组 UI 专属拆分字段
    // 不参与反向拆解——`${VAR}` 占位符本来就会随原始 key/value 一起出现在这里，直接展示即可，
    // 用户要新增才用那组辅助字段。
    if (detail.env) setEnv(rowsFromRecord(detail.env));
    if (detail.url) setUrl(detail.url);
    if (detail.headers) setHttpHeaders(rowsFromRecord(detail.headers));
    setBackfilled(true);
  }, [editName, detail, backfilled]);

  // 粘贴/编辑 JSON 时实时解析并回填其他字段（一旦能解析出合法 JSON 就立刻生效，通常是粘贴完
  // 那一刻）——不是等提交时才处理，让用户能马上看到解析出来的结果，也方便粘贴完继续手动微调。
  // 解析不出来（比如还没粘完、格式不对）就什么都不做，不清空已经填好的字段。
  function handleJsonPasteChange(value: string) {
    setJsonPaste(value);
    const parsed = parseJsonPasteConfig(value);
    if (!parsed) return;
    if (parsed.name) setName(parsed.name);
    setType(parsed.transport);
    if (parsed.command !== undefined) setCommand(parsed.command);
    if (parsed.args !== undefined) setArgs(rowsFromArray(parsed.args));
    if (parsed.env !== undefined) setEnv(rowsFromRecord(parsed.env));
    if (parsed.url !== undefined) setUrl(parsed.url);
    if (parsed.headers !== undefined) setHttpHeaders(rowsFromRecord(parsed.headers));
  }

  // 2026-08-11 改成 await 真正结果再跳转：之前"调完马上 onRegistered 跳我的MCP"的设计，实测
  // 会撞上 ConnectorMarket/index.tsx 切回 market 视图时触发的非静默 loadList——这次 loadList
  // 跑在 register_custom 的长 RPC（最长 10min）真正落地之前，会用后端"还没这条记录"的真实态
  // 把刚插的占位卡整份覆盖掉，用户跳过去那一刻列表里看不到刚创建的 MCP（见 connectorStore.ts
  // registerCustom 头注释）。现在提交按钮本身变成"创建中"态、留在表单页等真正结果，返回/取消
  // 按钮全程可点（不因 creating 禁用）——用户不想等可以随时手动离开，store 里的 RPC 该怎么跑
  // 还怎么跑，不受组件是否还挂载影响。
  async function handleSubmit() {
    if (creating) return;
    const nextErrors = {
      name: !name.trim(),
      command: type === 'stdio' && !command.trim(),
      url: type === 'streamable-http' && !url.trim(),
    };
    if (nextErrors.name || nextErrors.command || nextErrors.url) {
      setFieldErrors(nextErrors);
      return;
    }
    setFieldErrors({});

    const stdioEnv: Record<string, string> = Object.fromEntries(env.filter((r) => r.key).map((r) => [r.key, r.value]));
    for (const row of envPassthrough) {
      if (row.key) stdioEnv[row.key] = `\${${row.key}}`;
    }

    const httpHeaderMap: Record<string, string> = Object.fromEntries(
      httpHeaders.filter((r) => r.key).map((r) => [r.key, r.value]),
    );
    for (const row of httpHeadersFromEnv) {
      if (row.key && row.value) httpHeaderMap[row.key] = row.value;
    }
    if (bearerEnvKey) httpHeaderMap.Authorization = `Bearer ${bearerEnvKey}`;

    setCreating(true);
    await registerCustom({
      name,
      transport: type === 'stdio' ? 'stdio' : 'streamable-http',
      command: type === 'stdio' ? command : undefined,
      args: type === 'stdio' ? args.map((r) => r.key).filter(Boolean) : undefined,
      env: type === 'stdio' ? stdioEnv : undefined,
      url: type === 'streamable-http' ? url : undefined,
      headers: type === 'streamable-http' ? httpHeaderMap : undefined,
    });
    // 不管后端成功失败，等真正结果出来后再跳"我的MCP"：成功→已连接+绿色Toast；失败→真实态
    // （后端已回滚，卡片显示未连接或消失）+红色Toast带真实错误。这时候列表里已经能看到正确结果，
    // 不会再有"跳过去空空如也"的问题。组件可能已经因为用户手动点了返回而卸载，setCreating
    // 这行在那种情况下是 no-op（React 卸载后 setState 静默丢弃），onRegistered 重复调用一次
    // 也无害（对应视图早已经不是这个表单了）。
    setCreating(false);
    onRegistered();
  }

  return (
    <div className="relative h-full overflow-y-auto bg-card px-8 py-6">
      {/* 返回样式跟详情页（McpDetailPage.tsx/PluginDetailPage.tsx）保持一致：ChevronLeft
          纯尖角图标 + 黑色文字，用户明确要求这个页面也照这个样式改。 */}
      <button type="button" onClick={onBack} className="mb-4 flex items-center gap-1 text-[14px] leading-[22px] text-text hover:opacity-70">
        <ChevronLeft size={16} />
        {t('connectorMarket.common.back')}
      </button>

      {/* 2026-08-07：宽度几轮调整——固定的 max-w-xl/max-w-3xl 太窄，且浏览器再宽也不会跟着变宽；
          完全去掉 max-w（w-full）又在超宽屏上被拉得太开、不好看。定在 mx-auto + w-full + max-w-5xl：
          小于 1024px 可用宽度时跟手拉伸（随浏览器变宽），超过后封顶在 1024px 且居中，不会贴着左边
          也不会无限撑开——mx-auto 这次别再漏掉了。 */}
      <div className="mx-auto w-full max-w-5xl">
        <h1 className="mb-6 text-[18px] font-semibold leading-7 text-text">
          {t(editName ? 'connectorMarket.registerMcp.editTitle' : 'connectorMarket.registerMcp.title')}
        </h1>

        {/* 编辑模式 name 只读（文档 §9："编辑：name 只读"）——name 是后端识别"编辑同一个 MCP"
            还是"新建一个"的依据，允许改名会变成误建一个新条目而不是编辑原条目。 */}
        <Field
          label={t('connectorMarket.registerMcp.name')}
          required
          error={fieldErrors.name ? t('connectorMarket.create.fieldRequired') : undefined}
        >
          <input
            value={name}
            onChange={(event) => {
              setName(event.target.value);
              clearFieldError('name');
            }}
            readOnly={!!editName}
            disabled={!!editName}
            className={`h-9 w-full rounded-lg border bg-card px-3 text-[13px] text-text outline-none placeholder:text-[color:var(--color-text-placeholder)] focus:border-border-hover disabled:cursor-not-allowed disabled:bg-bg-muted disabled:text-text-muted ${
              fieldErrors.name ? 'border-danger' : 'border-border'
            }`}
          />
        </Field>

        <Field label={t('connectorMarket.registerMcp.type')}>
          <div className="flex gap-2">
            {([
              { key: 'stdio', label: t('connectorMarket.registerMcp.typeStdio') },
              { key: 'streamable-http', label: t('connectorMarket.registerMcp.typeHttp') },
            ] as const).map((opt) => (
              <button
                key={opt.key}
                type="button"
                onClick={() => setType(opt.key)}
                className={`rounded-lg px-4 py-1.5 text-[13px] ${type === opt.key ? 'bg-[color:var(--color-chat-accent)] text-white' : 'bg-bg-muted text-text-muted'}`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </Field>

        {type === 'stdio' ? (
          <>
            <Field
              label={t('connectorMarket.registerMcp.command')}
              required
              error={fieldErrors.command ? t('connectorMarket.create.fieldRequired') : undefined}
            >
              <input
                value={command}
                onChange={(event) => {
                  setCommand(event.target.value);
                  clearFieldError('command');
                }}
                placeholder="dev-mcp serve-sqlite"
                className={`h-9 w-full rounded-lg border bg-card px-3 text-[13px] text-text outline-none placeholder:text-[color:var(--color-text-placeholder)] focus:border-border-hover ${
                  fieldErrors.command ? 'border-danger' : 'border-border'
                }`}
              />
            </Field>
            <KeyValueField label={t('connectorMarket.registerMcp.args')} single rows={args} onChange={setArgs} placeholderKey={t('connectorMarket.registerMcp.pleaseInput')} />
            <KeyValueField label={t('connectorMarket.registerMcp.env')} rows={env} onChange={setEnv} placeholderKey={t('connectorMarket.registerMcp.key')} placeholderValue={t('connectorMarket.registerMcp.value')} />
            <KeyValueField
              label={t('connectorMarket.registerMcp.envPassthrough')}
              single
              rows={envPassthrough}
              onChange={setEnvPassthrough}
              placeholderKey={t('connectorMarket.registerMcp.pleaseInput')}
            />
          </>
        ) : (
          <>
            <Field
              label="URL"
              required
              error={fieldErrors.url ? t('connectorMarket.create.fieldRequired') : undefined}
            >
              <input
                value={url}
                onChange={(event) => {
                  setUrl(event.target.value);
                  clearFieldError('url');
                }}
                placeholder="https://mcp.example.com/mcp"
                className={`h-9 w-full rounded-lg border bg-card px-3 text-[13px] text-text outline-none placeholder:text-[color:var(--color-text-placeholder)] focus:border-border-hover ${
                  fieldErrors.url ? 'border-danger' : 'border-border'
                }`}
              />
            </Field>
            <Field label={t('connectorMarket.registerMcp.bearerTokenEnvKey')}>
              <input
                value={bearerEnvKey}
                onChange={(event) => setBearerEnvKey(event.target.value)}
                placeholder="MCP_BEARER_TOKEN"
                className="h-9 w-full rounded-lg border border-border bg-card px-3 text-[13px] text-text outline-none placeholder:text-[color:var(--color-text-placeholder)] focus:border-border-hover"
              />
            </Field>
            <KeyValueField label={t('connectorMarket.registerMcp.headers')} rows={httpHeaders} onChange={setHttpHeaders} placeholderKey={t('connectorMarket.registerMcp.key')} placeholderValue={t('connectorMarket.registerMcp.value')} />
            <KeyValueField
              label={t('connectorMarket.registerMcp.headersFromEnv')}
              rows={httpHeadersFromEnv}
              onChange={setHttpHeadersFromEnv}
              placeholderKey={t('connectorMarket.registerMcp.key')}
              placeholderValue={t('connectorMarket.registerMcp.envVarName')}
            />
          </>
        )}

        <div className="mb-6">
          <label className="mb-1 block text-[13px] font-medium text-text">{t('connectorMarket.registerMcp.addJson')}</label>
          <p className="mb-2 text-[12px] leading-[18px] text-text-muted">{t('connectorMarket.registerMcp.addJsonHint')}</p>
          <textarea
            value={jsonPaste}
            onChange={(event) => handleJsonPasteChange(event.target.value)}
            placeholder={JSON_EXAMPLE}
            rows={10}
            className="w-full resize-none rounded-lg border border-border bg-bg-muted px-3 py-2 font-mono text-[12px] leading-5 text-text-muted outline-none focus:border-border-hover"
          />
        </div>

        <div className="flex justify-end gap-2 border-t border-border pt-4">
          <button type="button" onClick={onBack} className="rounded-lg border border-border px-4 py-1.5 text-[13px] text-text hover:border-border-hover">
            {t('connectorMarket.common.cancel')}
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={creating}
            className="flex items-center gap-1.5 rounded-lg bg-text px-4 py-1.5 text-[13px] text-text-inverse disabled:opacity-60"
          >
            {creating && <Loader2 size={13} className="animate-spin" />}
            {creating
              ? t(editName ? 'connectorMarket.registerMcp.saving' : 'connectorMarket.registerMcp.creating')
              : t('connectorMarket.common.confirm')}
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  required,
  error,
  children,
}: {
  label: string;
  required?: boolean;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-4">
      <label className="mb-1.5 block text-[13px] font-medium text-text">
        {label}
        {required && <span className="text-danger"> *</span>}
      </label>
      {children}
      {error && <p className="mt-1 text-[11px] leading-4 text-danger">{error}</p>}
    </div>
  );
}

function KeyValueField({
  label,
  rows,
  onChange,
  single,
  placeholderKey,
  placeholderValue,
}: {
  label: string;
  rows: KeyValueRow[];
  onChange: (rows: KeyValueRow[]) => void;
  single?: boolean;
  placeholderKey: string;
  placeholderValue?: string;
}) {
  return (
    <div className="mb-4">
      <label className="mb-1.5 block text-[13px] font-medium text-text">{label}</label>
      <div className="flex flex-col gap-2">
        {rows.map((row) => (
          <div key={row.id} className="flex items-center gap-2">
            <input
              value={row.key}
              onChange={(event) => onChange(rows.map((r) => (r.id === row.id ? { ...r, key: event.target.value } : r)))}
              placeholder={placeholderKey}
              className="h-9 flex-1 rounded-lg border border-border bg-card px-3 text-[13px] text-text outline-none placeholder:text-[color:var(--color-text-placeholder)] focus:border-border-hover"
            />
            {!single && (
              <input
                value={row.value}
                onChange={(event) => onChange(rows.map((r) => (r.id === row.id ? { ...r, value: event.target.value } : r)))}
                placeholder={placeholderValue}
                className="h-9 flex-1 rounded-lg border border-border bg-card px-3 text-[13px] text-text outline-none placeholder:text-[color:var(--color-text-placeholder)] focus:border-border-hover"
              />
            )}
            <button type="button" onClick={() => onChange(rows.filter((r) => r.id !== row.id))} className="flex h-7 w-7 shrink-0 items-center justify-center rounded border border-border text-text-muted hover:border-border-hover">
              <Minus size={13} />
            </button>
            <button type="button" onClick={() => onChange([...rows, newRow()])} className="flex h-7 w-7 shrink-0 items-center justify-center rounded border border-border text-text-muted hover:border-border-hover">
              <Plus size={13} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
