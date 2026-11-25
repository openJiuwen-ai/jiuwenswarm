"""Read-only, bounded Agent Loop observations. Never persist argument/result excerpts."""
from collections.abc import Mapping
from datetime import datetime
from html import escape
import json
import re
from time import perf_counter
from uuid import uuid4

from openjiuwen.core.events.builtins.event_types.turn import TurnStepInput
from openjiuwen.core.events.builtins.event_types.tool_call import ToolCallEventInput

SECRET = re.compile(r'(?i)(api.?key|authorization|password|secret|access.?token|credential)')


def excerpt(value, limit=700):
    def clean(obj, depth=0):
        if depth>4:return '…'
        if isinstance(obj, Mapping):
            return {str(k): '[REDACTED]' if SECRET.search(str(k)) else clean(v,depth+1) for k,v in list(obj.items())[:20]}
        if isinstance(obj,(list,tuple)):return [clean(v,depth+1) for v in obj[:12]]
        if isinstance(obj,str):
            text=re.sub(r'(?i)Bearer\s+\S+|sk-[\w.\-]+','[REDACTED]',obj[:4000])
            return re.sub(r'(?i)((?:api[_-]?key|password|secret|access[_-]?token)\s*[=:]\s*)[^\s,;]+',r'\1[REDACTED]',text)
        return obj if obj is None or type(obj) in (int,float,bool) else type(obj).__name__
    if isinstance(value,str):
        try:value=json.loads(value)
        except (ValueError,TypeError):pass
    value=clean(value)
    text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
    return text[:limit]+('…' if len(text)>limit else '')


def begin(event):
    value=event.event_input
    row={'id':uuid4().hex,'status':'running','at':datetime.now().astimezone().strftime('%H:%M:%S'),
         'duration_ms':None,'usage':{},'details':[],'turn_id':'','model':''}
    if isinstance(value,TurnStepInput):
        row.update(kind='model',title=f'模型步骤 {value.index+1}',turn_id=value.turn_id,model=value.model,
                   agent_id=value.agent_id or '',index=value.index)
    elif isinstance(value,tuple) and value and all(isinstance(v,ToolCallEventInput) for v in value):
        row.update(kind='tools',title=f'工具批次 · {len(value)} 次调用',tool_count=len(value))
        row['details']=[{'name':v.tool_name,'call_id':v.tool_call_id,'arguments':excerpt(v.arguments)} for v in value[:12]]
    else:return None
    return row


def finish(row,output,elapsed,status):
    row['duration_ms']=round(elapsed*1000,1);row['status']=status
    if status!='done':return
    if row['kind']=='model':
        usage=getattr(output,'usage',None)
        if isinstance(usage,Mapping):
            row['usage']={k:v for k,v in usage.items() if k in ('input_tokens','output_tokens','total_tokens','cache_read_tokens','cache_creation_input_tokens','cache_write_tokens','cache_tokens') and type(v) is int and v>=0}
        row['answer']=excerpt(getattr(output,'answer',''))
        row['intent']=[str(t.tool_name) for t in getattr(output,'tool_uses',())][:12]
    elif isinstance(output,tuple):
        results={getattr(v,'tool_call_id',None):v for v in output}
        for detail in row['details']:
            result=results.get(detail['call_id'])
            if result is not None:
                text=getattr(result,'text',None)
                detail['result']=excerpt(text if text is not None else getattr(result,'result',None))


def trace_html(state):
    rows=state.trace
    models=[r for r in rows if r['kind']=='model'];tools=[r for r in rows if r['kind']=='tools']
    running=any(r['status']=='running' for r in rows)
    durations=[r['duration_ms'] for r in rows if r['duration_ms'] is not None]
    label='执行中' if running else ('观测完成' if rows else '等待执行')
    cards=[]
    status_names={'running':'执行中','done':'已返回','error':'异常','cancelled':'已取消'}
    for row in rows[-12:]:
        timing='等待返回' if row['duration_ms'] is None else f'{row["duration_ms"]/1000:.2f}s'
        detail=''
        if row['kind']=='model':
            usage=row['usage']; metrics=' · '.join(f'{name} {usage[key]:,}' if key in usage else f'{name} 未知' for key,name in [('input_tokens','输入'),('output_tokens','输出'),('cache_read_tokens','缓存读取')])
            detail=f'<p class="loop-usage">{metrics}</p><small>{escape(row["model"])}</small>'
            if row.get('intent'):detail+=f'<p>调用意图：{escape(" → ".join(row["intent"]))}</p>'
            if row.get('answer'):detail+=f'<details><summary>回答摘要</summary><pre>{escape(row["answer"])}</pre></details>'
        else:
            for item in row['details']:
                detail+=f'<details class="tool-detail"><summary>{escape(item["name"])} · 参数 / 结果</summary><pre>参数\n{escape(item["arguments"])}\n\n结果\n{escape(item.get("result","等待工具返回"))}</pre></details>'
        cards.append(f'<article class="loop-event {row["kind"]} {row["status"]}"><span class="loop-dot"></span><div class="loop-event-head"><b>{escape(row["title"])}</b><span>{status_names[row["status"]]} · {timing}</span></div><small>{row["at"]}</small>{detail}</article>')
    content=''.join(cards) or '<p class="empty-note">发送一条消息，即可观察模型 → 工具 → 模型的执行过程。</p>'
    return f'''<section class="loop glass" id="agent-loop"><div class="section-head"><div><div class="eyebrow">LIVE / EXECUTION TRACE</div><h2>Agent Loop · 执行脉络</h2></div><span class="chip {'live-chip' if running else ''}">{label}</span></div>
<div class="loop-metrics"><div><b>{len(models)}</b><span>模型步骤</span></div><div><b>{sum(r.get('tool_count',0) for r in tools)}</b><span>工具调用 / {len(tools)} 批</span></div><div><b>{sum(durations)/1000:.2f}s</b><span>已返回事件累计耗时</span></div></div><div class="loop-events">{content}</div><p class="footnote">最近 {len(rows)} 个观测事件（最多 60，展示最近 12）· 工具耗时按批次 · 耗时包含下游事件处理 · 摘要仅本次运行内保存，脱敏不保证覆盖所有敏感内容。模型步骤结束更新用量，非逐 Token 流式计数。</p></section>'''
