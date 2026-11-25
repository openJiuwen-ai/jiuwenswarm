"""Self-contained visual dashboard backed exclusively by observed session data."""
from html import escape
from pathlib import Path
import shutil
from .loop import trace_html
from .trend import trend_html
from .views import number, occupancy, NAMES

ASSETS = Path(__file__).parent.parent / 'assets'
COLORS = {'system_prompt': '#a78bfa', 'tools': '#45ddfc', 'skills': '#f9bf63', 'messages': '#63edb5'}


def document(state):
    context = state.usage.get('context', {})
    percent = occupancy(context)
    known = percent is not None
    ratio = max(0, min(100, percent or 0))
    value = f'{percent:.1f}<small>%</small>' if known else '—'
    categories = context.get('breakdown', {}).get('categories', [])
    total = sum(item['tokens'] for item in categories)
    segments, rows = [], []
    for index, item in enumerate(categories):
        name = str(item['name'])
        share = item['tokens'] / total * 100 if total else 0
        color = COLORS.get(name, '#a9b7ca')
        label = escape(NAMES.get(name, name))
        segments.append(f'<span style="--color:{color};flex:{item["tokens"]}" title="{label}: {number(item["tokens"])}"></span>')
        rows.append(f'<div class="category" style="--color:{color};--delay:{index * 90}ms"><div class="category-top"><span><i></i>{label}</span><b>{number(item["tokens"])}</b><em>{share:.1f}%</em></div><div class="track"><span style="width:{share:.3f}%"></span></div></div>')
    if not rows:
        rows.append('<p class="empty-note">尚无分类观测，完成一轮后刷新。</p>')
    history = []
    max_tokens = max((row['context'] or 0 for row in state.history), default=1) or 1
    for row in state.history:
        height = max(3, (row['context'] or 0) / max_tokens * 100)
        history.append(f'<div class="history-step" tabindex="0"><div class="step-bar" style="height:{height:.2f}%" data-known="{str(row["context"] is not None).lower()}"><span>{number(row["context"])}</span></div><label>#{row["number"]}</label><div class="step-tip">输入 {number(row["input"])}<br>输出 {number(row["output"])}<br>缓存 {number(row["cache"])}</div></div>')
    timeline = '<div class="history-chart">' + ''.join(history) + '</div>' if history else '<div class="waiting"><div class="waiting-grid"></div><div class="waiting-icon">⌁</div><strong>尚未采集完成轮次</strong><p>下一次用量通知，将在这里留下轨迹</p><span>WAITING FOR OBSERVATIONS</span></div>'
    messages = []
    for index, item in enumerate(state.messages):
        role = escape(item['role'])
        body = escape(item['text'])
        messages.append(f'<details class="message"><summary><span class="message-index">{index+1:02d}</span><span class="role">{role}</span><span class="preview">{escape(item["text"][:55])}</span><span class="chevron">＋</span></summary><div class="message-body">{body}</div></details>')
    message_html = ''.join(messages) or '<p class="empty-note">当前没有可读取的历史消息</p>'
    alert = '<aside class="notice">部分查询失败，当前保留上次观测。请通过输入框上方的刷新按钮重试。</aside>' if state.error else ''
    if state.history_error:
        alert += '<aside class="notice">本地历史读取或保存失败；当前仅保证本次运行内的观测。</aside>'
    status = '当前窗口占用' if known else '等待观测'
    updated = escape(state.updated)
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline' https: http:; script-src https: http:; img-src data:; connect-src 'none'; font-src 'none'; base-uri 'none'; form-action 'none'"><title>上下文驾驶舱</title><link rel="stylesheet" href="dashboard.css"></head><body>
<div class="ambient" aria-hidden="true"><div class="aurora a1"></div><div class="aurora a2"></div><div class="stars"></div></div>
<main><header><a class="brand" href="#"><span class="logo">◈</span><span>CONTEXT<span class="brand-light"> / OBSERVER</span><small>感知每一轮思考的边界</small></span></a><button id="motion" aria-pressed="true">动效 <span>ON</span></button></header>
{alert}{trace_html(state)}<section class="hero glass"><div class="hero-copy"><div class="eyebrow"><span class="signal"></span>CONTEXT INTELLIGENCE</div><h1>看见上下文<br><span>流动的形状。</span></h1><p>每一段输入，都有迹可循。<br>让复杂的 Agent 上下文变得清晰。</p><div class="model-pill"><span>◆</span><span>{escape(state.model or '模型未知')}</span></div></div>
<div class="gauge" data-observed="{str(known).lower()}"><div class="orbit o1"></div><div class="orbit o2"></div><svg viewBox="0 0 220 220" aria-label="{status}"><defs><linearGradient id="ring"><stop stop-color="#49e4fa"/><stop offset="1" stop-color="#af88ff"/></linearGradient></defs><circle class="ring-base" cx="110" cy="110" r="83"/><circle class="ring-ticks" cx="110" cy="110" r="99"/><circle class="ring-value" cx="110" cy="110" r="83" pathLength="100" stroke-dasharray="{ratio} 100"/></svg><div class="gauge-value"><strong>{value}</strong><span>{status}</span><small>{'OBSERVED' if known else 'WAITING FOR SIGNAL'}</small></div></div>
<div class="hero-bottom"><span><i></i>仅展示宿主已观测数据</span><span>最后读取 {updated}</span></div></section>
<div class="metrics"><article class="metric glass"><span class="metric-label">当前上下文 <i>↗</i></span><b>{number(context.get('tokens'))}</b><small>TOKENS · 权威输入</small></article><article class="metric glass"><span class="metric-label">上下文窗口 <i>◫</i></span><b>{number(context.get('window'))}</b><small>WINDOW CAPACITY</small></article><article class="metric glass"><span class="metric-label">用户轮次 <i>⌁</i></span><b>{number(state.turns)}</b><small>CONVERSATION TURNS</small></article></div>
<section class="composition glass"><div class="section-head"><div><div class="eyebrow">01 / COMPOSITION</div><h2>上下文光谱</h2></div><span class="chip">分类估算</span></div><div class="composition-total"><strong>{number(total) if categories else '—'}</strong><span>已知分类合计 · TOKEN ESTIMATE</span></div><div class="spectrum">{''.join(segments)}</div><div class="categories">{''.join(rows)}</div><p class="footnote">占比按已知分类合计计算，可能与实际输入 Token 不同。</p></section>
{trend_html(state)}
<section class="timeline glass"><div class="section-head"><div><div class="eyebrow">02 / SIGNAL HISTORY</div><h2>轮次上下文趋势</h2></div><span class="chip">已采集 {state.count} 轮</span></div>{timeline}<div class="chart-footer"><span>最近 12 个完成轮次</span><span>悬停查看输入 / 输出 / 缓存</span></div></section>
<section class="messages glass"><div class="section-head"><div><div class="eyebrow">03 / MESSAGE EXPLORER</div><h2>最近会话消息</h2></div><span class="chip">{len(state.messages):02d} 条记录</span></div>{message_html}<p class="footnote">仅最近 12 条，每条最多 600 字。会话记录 ≠ 最终模型 Prompt。</p></section>
<footer><span class="footer-mark">◈ CONTEXT OBSERVER <b>1.0</b></span><p>执行脉络按事件更新 · 上下文用量在轮次结束后更新 · 轮次历史已本地保存</p><span class="decorative-note">背景与轨道动效仅为装饰，不表示模型正在运行</span></footer></main><script src="dashboard.js"></script></body></html>'''


def write_dashboard(directory, state):
    (directory / 'index.html').write_text(document(state), encoding='utf-8')
    for name in ('dashboard.css', 'dashboard.js'):
        shutil.copyfile(ASSETS / name, directory / name)
