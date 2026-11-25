"""Per-request inputs; unknown measurements never become zeros or bridged deltas."""
from html import escape


def readings(trace):
    result=[];previous=None;tools=[]
    for event in trace:
        if event['kind']=='tools':
            tools.extend(str(d['name']) for d in event.get('details',[]))
            continue
        if event['kind']!='model':continue
        value=event.get('usage',{}).get('input_tokens') if event['status']=='done' else None
        if type(value) is not int or value<0:value=None
        identity=(event.get('model'),event.get('agent_id'))
        delta=None;percent=None
        if previous and previous['identity']==identity and previous['input'] is not None and value is not None:
            delta=value-previous['input']
            if previous['input']>0:percent=delta/previous['input']*100
        row=dict(number=len(result)+1,input=value,delta=delta,percent=percent,identity=identity,
                 tools=tools[:12],event=event)
        result.append(row);previous=row;tools=[]
    return result[-24:]


def trend_html(state):
    rows=readings(state.trace)
    bars=[];details=[];points=[];segments=[]
    maximum=max((r['input'] or 0 for r in rows),default=1) or 1
    delta_max=max((abs(r['delta'] or 0) for r in rows),default=1) or 1
    for i,row in enumerate(rows):
        value=row['input'];delta=row['delta'];event=row['event']
        size='未知' if value is None else f'{value:,}'
        change='—' if delta is None else f'{delta:+,}'
        pct='' if row['percent'] is None else f' ({row["percent"]:+.1f}%)'
        bars.append(f'<div class="request-column"><div class="request-bar {"unknown" if value is None else ""}" style="height:{max(2,(value or 0)/maximum*100):.2f}%"><span>{size}</span></div><label>#{row["number"]}</label></div>')
        if delta is None:
            if points:segments.append(points);points=[]
        else:points.append(f'{(i+.5)/len(rows)*1000:.2f},{70-delta/delta_max*55:.2f}')
        status='等待返回' if event['status']=='running' else {'done':'已返回','error':'异常','cancelled':'已取消'}.get(event['status'],'未知')
        tokens=' · '.join(f'{label} {event.get("usage",{}).get(key,"未知")}' for key,label in [('output_tokens','输出'),('cache_read_tokens','缓存读取')])
        timing='未知' if event.get('duration_ms') is None else f'{event["duration_ms"]/1000:.2f}s'
        tools=' → '.join(row['tools']) or '无已观测工具批次'
        details.append(f'<details class="request-detail"><summary><b>请求 #{row["number"]}</b><span>输入 {size}</span><em>{change}{pct}</em></summary><p>{escape(str(event.get("model", "未知")))} · {status} · {timing} · {escape(tokens)}</p><p>与前一请求之间：{escape(tools)}</p></details>')
    if points:segments.append(points)
    lines=''.join(f'<polyline points="{" ".join(p)}"/>'+''.join(f'<circle cx="{xy.split(",")[0]}" cy="{xy.split(",")[1]}" r="3"/>' for xy in p) for p in segments)
    graph=(f'<div class="request-chart">{"".join(bars)}</div><div class="delta-caption">变化量 Δ · 独立刻度 ±{delta_max:,} Token</div><svg class="delta-chart" viewBox="0 0 1000 140" preserveAspectRatio="none" aria-label="相邻请求输入变化"><line x1="0" y1="70" x2="1000" y2="70"/>{lines}</svg>') if rows else '<p class="empty-note">安装后发送消息，即可采集每次模型请求；没有观测时不生成示例数据。</p>'
    return f'<section class="request-trend glass"><div class="section-head"><h2>Context Trend · 逐请求趋势</h2><span class="chip">{len(rows)} 个请求</span></div>{graph}<div class="request-details">{"".join(details)}</div><p class="footnote">柱：实际输入 Token；线：相邻请求增减量。按开始顺序排列，完成后更新；缺失值、模型或 Agent 切换不计算差值。工具关联仅表示时间顺序，不代表增长全部来自工具。最近 60 个执行事件内最多显示 24 个请求，本次进程内采集，重启清空。</p></section>'
