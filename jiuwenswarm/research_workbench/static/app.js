'use strict';
let state, toastTimer;
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const time = (value) => new Date(value).toLocaleString('zh-CN', {hour12:false});
function notify(message, error=false){const box=$('#toast');box.textContent=message;box.className=error?'error-toast':'';box.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>box.hidden=true,5500);}
async function api(url, method='GET', body){const response=await fetch(url,{method,headers:{'Content-Type':'application/json','X-Research-Token':$('meta[name="research-token"]').content},body:body===undefined?undefined:JSON.stringify(body)});if(!response.ok){const data=await response.json().catch(()=>({detail:'请求失败'}));throw Error(typeof data.detail==='string'?data.detail:'填写内容不完整或格式不正确，请检查字段与证据位置。');}return response.json();}
function empty(title,detail){return `<div class="empty"><b>${escapeHtml(title)}</b><span>${escapeHtml(detail)}</span></div>`;}
function render(){
 $('#paperCount').textContent=state.papers.length;$('#verifiedCount').textContent=state.papers.filter(p=>p.verified).length;$('#experimentCount').textContent=state.experiments.length+(state.pilots||[]).length+(state.fulltext_runs||[]).length;
 $('#readyCount').textContent=`${state.preflight.filter(c=>c.ok).length} / 4`;
 $('#checks').innerHTML=state.preflight.map(c=>`<div class="check ${c.ok?'done':''}"><i>${c.ok?'✓':'·'}</i>${escapeHtml(c.label)}</div>`).join('');
 $('#recentPapers').innerHTML=state.papers.slice(0,3).map(p=>`<a class="paper-mini" href="#literature"><span class="paper-symbol">▤</span><div><h3>${escapeHtml(p.title)}</h3><small>${p.year} · ${escapeHtml(p.topic)} · ${p.verified?'已读':'待阅读'}</small></div></a>`).join('');
 $('#events').innerHTML=state.events.length?state.events.slice(-4).reverse().map(e=>`<div class="event">${escapeHtml(e.action)}<time>${time(e.time)}</time></div>`).join(''):'<div class="event">工作台已就绪，等待第一条阅读笔记。<time>本地数据会在保存后出现在这里</time></div>';
 for(const [key,value] of Object.entries(state.project))$('#projectForm').elements[key].value=value;
 $('#workspacePath').textContent=state.workspace;
 renderPapers();
 renderExecution();
 renderPilots();
 $('#experimentsList').innerHTML=state.experiments.length?state.experiments.slice().reverse().map(e=>`<article class="panel experiment-row"><div class="section-head"><h3>${escapeHtml(e.name)}</h3><span class="tag">${({planned:'计划中',completed:'已执行 · 人工登记',failed:'失败 / 负结果'})[e.status]}</span></div><span class="tiny">${time(e.created_at)} · ${escapeHtml(e.dataset)}</span><pre>${escapeHtml(e.command)}</pre><p>${escapeHtml(e.result)}</p><p class="tiny">证据位置：${escapeHtml(e.evidence)}<br>此记录由人工填写，程序未执行或验证该命令。</p></article>`).join(''):empty('暂无额外人工实验记录','上方已列出自动对照实验；这里用于补充人工执行的其他实验。');
 $('#runsList').innerHTML=state.runs.length?state.runs.slice().reverse().map(r=>`<article class="panel run-row"><div><h3>${escapeHtml(r.id)} <span class="tag ${r.ready_for_planning?'':'muted'}">${r.ready_for_planning?'预检已通过':'有待补齐项'}</span></h3><p>${time(r.created_at)} · ${r.reviewed_references} 篇已读文献 · ${Object.keys(r.files).length+1} 个文件 · 0 次 API 调用</p></div><a class="text-link" href="/api/runs/${encodeURIComponent(r.id)}/download">下载任务包 ZIP ↓</a></article>`).join(''):empty('你的第一份研究任务包','点击“生成研究任务包”，保存当前证据快照、阶段任务与论文提纲。');
}
function renderPapers(){const q=$('#paperSearch').value.trim().toLowerCase();const papers=state.papers.filter(p=>`${p.title} ${p.topic} ${p.note}`.toLowerCase().includes(q));$('#papers').innerHTML=papers.length?papers.map(p=>`<article class="panel paper-card"><div class="card-top"><span class="tag muted">${escapeHtml(p.topic)}</span><span class="tag ${p.verified?'':'muted'}">${p.verified?'已读 · 人工确认':'待阅读全文'}</span></div><h3>${escapeHtml(p.title)}</h3><div class="card-meta">${escapeHtml(p.authors)} · ${p.year}</div><p>${escapeHtml(p.note||'还没有阅读笔记。阅读原文后，补充方法、贡献、局限和证据位置。')}</p><div class="card-bottom"><a class="text-link" href="${escapeHtml(p.url)}" target="_blank" rel="noopener noreferrer">阅读原文 ↗</a><button data-edit="${escapeHtml(p.id)}">编辑阅读卡</button></div></article>`).join(''):empty('没有匹配的文献','试试其他关键词，或者登记新的来源。');}
async function refresh(){state=await api('/api/state');render();}
function navigate(){const id=location.hash.slice(1)||'overview';const valid=!!document.querySelector(`nav a[data-view="${CSS.escape(id)}"]`);const view=valid?id:'overview';document.querySelectorAll('.view').forEach(e=>e.hidden=e.id!==view);document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.dataset.view===view));$('#crumb').textContent=document.querySelector(`nav a[data-view="${view}"]`).textContent.trim().slice(1).trim();}
function openPaper(paper){const form=$('#paperForm');form.reset();form.elements.id.value='';form.elements.year.value=new Date().getFullYear();if(paper)for(const [k,v]of Object.entries(paper)){if(!form.elements[k])continue;if(k==='verified')form.elements[k].checked=v;else form.elements[k].value=v;}$('#paperDialog').showModal();}
async function submit(form,action){const button=form.querySelector('[type="submit"]');button.disabled=true;try{await action();await refresh();notify('已保存到桌面比赛文件夹');}catch(e){notify(e.message,true);}finally{button.disabled=false;}}
$('#paperSearch').addEventListener('input',renderPapers);
$('#addPaper').onclick=()=>openPaper();
$('#papers').onclick=e=>{const button=e.target.closest('[data-edit]');if(button)openPaper(state.papers.find(p=>p.id===button.dataset.edit));};
$('#paperForm').onsubmit=e=>{e.preventDefault();const form=e.currentTarget;submit(form,async()=>{const data=Object.fromEntries(new FormData(form));const id=data.id;delete data.id;data.year=Number(data.year);data.verified=form.elements.verified.checked;await api(id?'/api/papers/'+id:'/api/papers',id?'PUT':'POST',data);$('#paperDialog').close();});};
$('#projectForm').onsubmit=e=>{e.preventDefault();submit(e.currentTarget,()=>api('/api/project','PUT',Object.fromEntries(new FormData(e.currentTarget))));};
$('#addExperiment').onclick=()=>{$('#experimentForm').reset();$('#experimentDialog').showModal();};
$('#experimentForm').onsubmit=e=>{e.preventDefault();const form=e.currentTarget;submit(form,async()=>{await api('/api/experiments','POST',Object.fromEntries(new FormData(form)));$('#experimentDialog').close();});};
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>document.getElementById(b.dataset.close).close());
document.querySelectorAll('[data-action="prepare"]').forEach(button=>button.onclick=async()=>{button.disabled=true;try{await api('/api/prepare','POST',{});await refresh();location.hash='runs';notify('研究任务包已保存；本次没有调用 API');}catch(e){notify(e.message,true);}finally{button.disabled=false;}});
window.addEventListener('hashchange',navigate);navigate();refresh().then(loadPilot).catch(e=>notify('无法读取本地工作区：'+e.message,true));

function renderExecution(){
 const b=state.budget_status;
 for(const [key,value] of Object.entries(b.settings)){const el=$('#budgetForm').elements[key];if(!el)continue;if(el.type==='checkbox')el.checked=value;else el.value=value;}
 $('#budgetCalls').textContent=b.calls;$('#apiCallCount').textContent=b.calls;$('#budgetHeld').textContent=b.held_cny.toFixed(4);$('#budgetEstimated').textContent=b.estimated_cny.toFixed(4);$('#keyStatus').textContent=state.key_configured?'已存入本次会话':'未填写';
 const tasks=state.executions||[];const select=$('#executionTask');const previous=select.value;
 select.innerHTML='<option value="">新建任务（从文献阶段开始）</option>'+tasks.map(t=>`<option value="${escapeHtml(t.id)}">${escapeHtml(t.id.slice(0,12))} · 已完成 ${t.stages.filter(s=>s.status==='completed').length}/4 阶段</option>`).join('');if(tasks.some(t=>t.id===previous))select.value=previous;
 $('#executionList').innerHTML=tasks.slice().reverse().map(t=>`<div class="event"><b>任务 ${escapeHtml(t.id)}</b><p>${t.stages.map(s=>escapeHtml(s.stage+' · '+s.status)).join(' → ')||'待执行'}</p><small>产物目录：executions/${escapeHtml(t.id)}</small></div>`).join('');
}
$('#budgetForm').onsubmit=e=>{e.preventDefault();const form=e.currentTarget;submit(form,()=>{const data=Object.fromEntries(new FormData(form));for(const key of ['total_cny','task_cny','calls_per_task','max_output_tokens','input_cny_per_million','output_cny_per_million'])data[key]=Number(data[key]);data.enabled=form.elements.enabled.checked;data.pricing_checked=form.elements.pricing_checked.checked;return api('/api/budget','PUT',data);});};
$('#keyForm').onsubmit=async e=>{e.preventDefault();const form=e.currentTarget;try{await api('/api/session-key','PUT',{api_key:form.elements.api_key.value});form.reset();await refresh();notify('密钥已存入本次会话；没有测试或调用模型');}catch(error){notify(error.message,true);}};
$('#clearKey').onclick=async()=>{try{await api('/api/session-key','PUT',{api_key:''});$('#keyForm').reset();await refresh();notify('会话密钥已清除');}catch(e){notify(e.message,true);}};
$('#stageForm').onsubmit=async e=>{e.preventDefault();const form=e.currentTarget;const button=form.querySelector('[type="submit"]');button.disabled=true;button.textContent='阶段执行中…';try{const data=Object.fromEntries(new FormData(form));if(!data.task_id)delete data.task_id;const result=await api('/api/stages','POST',data);await refresh();$('#executionTask').value=result.task_id;notify(result.status==='completed'?'阶段已保存，请审核草稿内容':(result.error||'用量状态异常，已停用后续请求'),result.status!=='completed');}catch(error){notify(error.message,true);}finally{button.disabled=false;button.textContent='运行所选阶段（消耗额度）';}};

function renderPilots(){const jobs=state.pilots||[];$('#pilotsList').innerHTML=jobs.slice().reverse().map(p=>`<div class="event"><b>${escapeHtml(p.case_id)} · ${escapeHtml(p.status)}</b><p>${p.api_calls} 次请求 · 占用 ${(p.held_cny||0).toFixed(4)} 元 · ${escapeHtml(p.duration_seconds??'执行中')} 秒</p><p>${p.results.map(r=>escapeHtml(r.method+' '+r.status)).join(' / ')}</p><small>结果：pilots/${escapeHtml(p.id)}/report.md；队友单人评分 ${(state.independent_reviews||[]).filter(r=>r.run_id===p.id).length}/${p.results.length}；协议 ${escapeHtml(p.protocol?.version||'未知')}</small><p>${(state.independent_reviews||[]).filter(r=>r.run_id===p.id).map(r=>escapeHtml(r.method+'：'+(r.answer_correct?'答案正确':'答案不正确')+' / 事实支持 '+r.factual_support)).join('；')}</p></div>`).join('');}
async function loadPilot(){try{const p=await api('/api/pilot/preflight');$('#pilotCase').innerHTML=p.cases.map(c=>`<option value="${escapeHtml(c.case_id)}">${escapeHtml(c.case_id+' · '+c.question)}</option>`).join('');$('#pilotState').textContent=`语料：${p.evidence_count} 条证据 / ${p.cases.length} 个开发问题 · 冻结数据原状态 ${p.review_status}；最新队友评分 ${(state.independent_reviews||[]).length} 份 · 数据hash ${p.dataset_hash.slice(0,12)}`;}catch(e){$('#pilotState').textContent=e.message;}}
$('#pilotForm').onsubmit=async e=>{e.preventDefault();const form=e.currentTarget;const button=form.querySelector('[type="submit"]');button.disabled=true;button.textContent='对照实验执行中…';try{const result=await api('/api/pilot','POST',Object.fromEntries(new FormData(form)));await refresh();notify(result.status==='completed'?'实验输出与报告已保存；请复核答案和引用':result.error,result.status!=='completed');}catch(e){notify(e.message,true);}finally{button.disabled=false;button.textContent='运行所选实验（消耗额度）';}};

async function loadFulltext(){
 const button=$('#refreshFulltext');button.disabled=true;
 try{
  const p=await api('/api/fulltext/preflight');
  $('#fulltextBadge').textContent=p.ready?'准入通过':'待复核 · 未冻结';
  $('#fulltextStatus').textContent=`${p.source_count} 篇原文 / ${p.candidate_cases} 道固定题 / ${p.formal_runs} 次正式运行。当前已预留 ${p.current_held_cny.toFixed(4)} 元。`;
  const labels=[];
  if(p.blockers.some(x=>x.includes('two_confirmed')))labels.push('候选题尚待两人独立核对');
  if(p.blockers.some(x=>x.includes('unresolved_label')))labels.push('答案分歧尚未确认或裁定');
  if(p.blockers.some(x=>x.includes('absence_review')))labels.push('不可回答题需要全文和附录核验');
  if(p.blockers.some(x=>x.includes('price')))labels.push('运行前核对模型单价');
  if(p.blockers.some(x=>x.includes('frozen')||x.includes('freeze')))labels.push('冻结数据、协议及代码版本');
  if(p.blockers.some(x=>x.includes('budget')))labels.push('预算不足，保持停止');
  $('#fulltextBlockers').textContent=labels.length?'还需完成：'+labels.join('；')+'。':p.ready?(p.formal_runs>=p.candidate_cases*2?'已完成计划运行；保持关闭付费执行，等待人工评分。':'冻结核验通过；不自动启动模型调用。'):'有待核实项，请查看桌面准入报告。';
 }catch(e){$('#fulltextStatus').textContent=e.message;$('#fulltextBadge').textContent='未就绪';}
 finally{button.disabled=false;}
}
$('#refreshFulltext').onclick=loadFulltext;
loadFulltext();

function renderPipeline(report){
 const labels={verified:'已核验',frozen:'已冻结',waiting_for_human_reviews:'等待评分',scored:'已统计',draft_ready:'草稿就绪',blocked:'待完成',incomplete:'未完成'};
 $('#pipelineStages').innerHTML=(report.stages||[]).map(s=>`<div class="check"><div><b>${escapeHtml(s.stage)} · ${escapeHtml(labels[s.status]||s.status)}</b><p class="tiny">${escapeHtml(s.detail)}</p></div></div>`).join('');
 const r=report.resources;
 $('#pipelineSummary').textContent=r?`已核验 ${r.verified_artifacts} 个实验产物 · ${r.requests} 次历史请求 · 本次新增 0 次 · ${report.review.basis==='user_accepted_scoring'?'用户确认评分（独立性未核实）':'双人评分'} ${report.review.resolved}/${r.planned_outputs}`:'尚未生成离线流程报告';
}
$('#refreshPipeline').onclick=async()=>{const button=$('#refreshPipeline');button.disabled=true;try{renderPipeline(await api('/api/pipeline/refresh','POST',{}));notify('流程材料已更新，没有调用模型');}catch(e){notify(e.message,true);}finally{button.disabled=false;}};
api('/api/pipeline/status').then(renderPipeline).catch(e=>{$('#pipelineSummary').textContent=e.message;});
