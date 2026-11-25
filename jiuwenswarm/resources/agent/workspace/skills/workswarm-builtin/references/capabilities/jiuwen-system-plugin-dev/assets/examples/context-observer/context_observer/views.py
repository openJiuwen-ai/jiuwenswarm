"""Native WebUI views; use estimates only where the host reports estimates."""
from jiuwenswarm.extensions_v2.webui.models import RenderNode as N

PACKAGE = 'webui.context-observer'
BAR = 'conversation.input.above'
PANEL = 'conversation.panel.right'
NAMES = {'system_prompt': '系统提示词', 'tools': '工具定义', 'skills': '技能', 'messages': '消息'}


def text(value, **props):
    return N('Text', {'value': value, **props})


def column(*children):
    return N('Box', {'direction': 'column', 'gap': 'sm'}, children=tuple(children))


def button(label, action, **props):
    return N('Button', {'value': label, 'size': 'sm', **props}, events={'ui.click': {'id': action, 'args': {}}})


def number(value):
    return '未知' if value is None else f'{value:,}'


def occupancy(context):
    tokens, window = context.get('tokens'), context.get('window')
    if tokens is not None and window:
        return tokens / window * 100
    return context.get('percent')


def summary_view(state):
    context = state.usage.get('context', {})
    return N('Box', {'direction': 'row', 'align': 'center', 'justify': 'between', 'wrap': True, 'gap': 'sm'}, children=(
        text(f"上下文  {number(context.get('tokens'))} / {number(context.get('window'))}", size='sm'),
        button('上下文观察', 'open', openPanel='context', type='text'),
        button('刷新面板', 'refresh', type='text'),
    ))


def panel_view(state):
    context = state.usage.get('context', {})
    percent = occupancy(context)
    title = '未知' if percent is None else f'{percent:.1f}%'
    pressure = '等待观测' if percent is None else ('较高占用' if percent >= 75 else '已观测')
    categories = context.get('breakdown', {}).get('categories', [])
    total = sum(item['tokens'] for item in categories)
    composition = []
    for item in categories:
        share = item['tokens'] / total * 100 if total else 0
        blocks = min(16, round(share / 100 * 16))
        composition.append(column(
            text(f"{NAMES.get(item['name'], item['name'])}   {number(item['tokens'])} · {share:.1f}%", size='sm'),
            text('━' * blocks + '─' * (16 - blocks), type='secondary', size='sm'),
        ))
    if not composition:
        composition.append(text('尚无分类观测；完成一轮后刷新。', type='secondary', size='sm'))
    trend = []
    if state.history:
        known = [row['context'] for row in state.history if row['context'] is not None]
        peak = max(known, default=1) or 1
        spark = ''.join('·' if row['context'] is None else '▁▂▃▄▅▆▇█'[min(7, int(row['context'] / peak * 7))] for row in state.history)
        trend.append(text(f'窗口变化  {spark}', strong=True))
        for row in reversed(state.history):
            trend.append(text(f"#{row['number']}  输入 {number(row['input'])}  /  输出 {number(row['output'])}\n缓存 {number(row['cache'])}  ·  上下文 {number(row['context'])}", size='sm'))
    else:
        trend.append(text('尚未采集完成轮次。打开面板、刷新不会增加轮数。', type='secondary', size='sm'))
    messages = [button('收起消息' if state.expanded else '浏览最近消息', 'messages', type='default')]
    if state.expanded:
        if not state.messages:
            messages.append(text('当前没有可读取的历史消息。', type='secondary', size='sm'))
        for item in state.messages:
            messages.append(column(text(item['role'], strong=True, size='sm'), text(item['text'], size='sm')))
        messages.append(text('仅最近 12 条，每条最多 600 字；这是会话记录，不是最终模型 Prompt。', size='sm', type='secondary'))
    children = [column(
        N('Box', {'direction': 'row', 'justify': 'between', 'align': 'center'}, children=(
            text('CONTEXT OBSERVER', strong=True), button('刷新', 'refresh', type='text'))),
        text('看清当前窗口与每轮用量', size='sm', type='secondary'),
        N('Card', {'title': '当前上下文', 'extra': pressure, 'size': 'small'}, children=(column(
            text(title, size='lg', strong=True, type='secondary' if percent is None else ('warning' if percent >= 75 else 'success')),
            text(f"{number(context.get('tokens'))} / {number(context.get('window'))} Token", strong=True),
            text(f"模型  {state.model or '未知'}\n用户轮次  {number(state.turns)}", size='sm', type='secondary'),
        ),)),
        N('Card', {'title': '上下文组成', 'extra': '分类估算', 'size': 'small'}, children=(column(*composition,
            text('占比按已知分类合计计算；可能与实际输入 Token 不同。', size='sm', type='secondary')),)),
        N('Card', {'title': '最近完成轮次', 'extra': f'已采集 {state.count} 轮', 'size': 'small'}, children=(column(*trend),)),
        N('Card', {'title': '消息浏览', 'size': 'small'}, children=(column(*messages),)),
        text(f'读取时间 {state.updated} · 用量通知 {state.observed}', type='secondary', size='sm'),
        text('本轮运行中不会实时计数。历史仅保留本次插件运行期间最近 12 轮，不代表账单。', type='secondary', size='sm'),
    )]
    if state.error:
        children.insert(0, text('部分数据读取失败，保留上次观测。可稍后刷新。', type='warning', size='sm'))
    return N('Panel', {'title': '上下文观察'}, children=tuple(children))
