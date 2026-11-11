'use strict';
const $ = (id) => document.getElementById(id);
const state = {date: '', items: [], topics: [], inbox: [], settings: null, selected: null, detail: null, images: [], image: 0, input: null, loading: false, sequence: 0};
const statusNames = {queued:['◷','待制作'],running:['◉','处理中'],ready:['✓','证据就绪'],approved:['✓','已验收'],completed:['✓','制作完成'],failed:['!','失败'],blocked:['!','待处理'],unknown:['?','结果待确认'],cancelled:['−','已取消'],archived:['−','已归档'],researching:['◉','调研中'],needs_input:['!','需补充素材'],interrupted:['!','技术中断']};
let toastTimer;
const dirtyForms=new Set();
document.addEventListener('input',event=>{const owner=event.target.closest('form, #plan-table');if(owner)dirtyForms.add(owner);});
function node(tag, text, className) { const el=document.createElement(tag); if(text!==undefined&&text!==null) el.textContent=String(text); if(className) el.className=className; return el; }
function badge(status, override) { const known=statusNames[status] || ['·',status || '待处理']; const el=node('span',`${known[0]} ${override || known[1]}`,'badge'); if(statusNames[status]) el.classList.add(status); return el; }
function button(text, action, className='button quiet') { const el=node('button',text,className); el.type='button'; el.addEventListener('click',()=>runAction(el,action)); return el; }
function platformLabel(value) { return value==='mp'?'公众号':'小红书'; }
function empty(el, text) { el.replaceChildren(node('p',text,'empty')); }
function toast(text, error=false) { clearTimeout(toastTimer); $('toast').textContent=text; $('toast').classList.toggle('error',error); $('toast').hidden=false; toastTimer=setTimeout(()=>$('toast').hidden=true,6000); }
async function api(path, method='GET', value) {
  const options={method,headers:{'Accept':'application/json'}};
  if(method!=='GET') { options.headers['Content-Type']='application/json'; options.headers['X-Workbench-Token']=document.querySelector('meta[name=workbench-token]').content; options.body=JSON.stringify(value ?? {}); }
  let response; try { response=await fetch(path,options); } catch (_) { throw new Error('无法连接本机工作台，请检查服务是否仍在运行。'); }
  let data; try { data=await response.json(); } catch (_) { throw new Error('服务未返回有效 JSON，请稍后重试。'); }
  if(!response.ok || data.ok!==true) { const error=new Error(data.message || '操作未完成。'); error.code=data.reason_code; throw error; }
  return data;
}
async function runAction(el, action) { if(el.disabled)return; el.disabled=true; try { await action(); } catch(error) { toast(error.message,true); if(error.code==='revision_conflict')await refresh(); } finally { el.disabled=false; } }
function contentPath(item) { return `/api/content/${state.date}/${item.id}`; }
function tab(name, focus=false) {
  document.querySelectorAll('[role=tab]').forEach(el=>{const active=el.dataset.tab===name;el.setAttribute('aria-selected',String(active));el.tabIndex=active?0:-1;if(active&&focus)el.focus();});
  document.querySelectorAll('[role=tabpanel]').forEach(el=>el.hidden=el.id!==`panel-${name}`);
}
function localDate(timezone) { try {const parts=new Intl.DateTimeFormat('en-CA',{timeZone:timezone || 'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());return ['year','month','day'].map(type=>parts.find(part=>part.type===type).value).join('-');} catch (_) {return new Date().toISOString().slice(0,10);} }
async function refresh(silent=false) {
  for(const form of dirtyForms)if(!form.isConnected)dirtyForms.delete(form);
  if(state.loading || (silent && dirtyForms.size))return; state.loading=true; if(!silent)$('refresh').disabled=true;
  const date=state.date;
  try {
    const results=await Promise.allSettled([api(`/api/day/${date}`),api('/api/inbox'),api('/api/topics'),api('/api/settings'),api('/api/archive')]);
    if(date!==state.date)return;
    if(results[0].status==='fulfilled') {state.items=results[0].value.items;renderToday(results[0].value.plan);} else {empty($('content-list'),results[0].reason.message);if(!silent)toast(results[0].reason.message,true);}
    if(results[1].status==='fulfilled')state.inbox=results[1].value.items;
    if(results[2].status==='fulfilled')state.topics=results[2].value.items;
    if(results[3].status==='fulfilled'){state.settings=results[3].value.settings; if(!dirtyForms.has($('settings-form')) && !$('settings-form').contains(document.activeElement))renderSettings();} else {$('settings-state').textContent=results[3].reason.message;state.settings=null;}
    if(results[4].status==='fulfilled')renderArchive(results[4].value.archive); else $('archive-state').textContent=results[4].reason.message;
    for(const result of results.slice(1,3))if(result.status==='rejected'&&!silent)toast(result.reason.message,true);
    renderLibrary();if(!dirtyForms.has($('plan-table')))renderPlan();
    if(state.selected && state.items.some(item=>item.id===state.selected))await selectContent(state.selected,silent);
    else if(state.selected){state.selected=null;resetDetail();}
  } finally {state.loading=false;$('refresh').disabled=false; if(date!==state.date)void refresh();}
}
function renderToday(plan) {
  $('today-heading').textContent=`${state.date} · 内容`;
  $('content-count').textContent=`${state.items.length} 篇`;
  const gaps=Object.entries(plan.shortages || {}).filter(([,count])=>count>0).map(([platform,count])=>`${platformLabel(platform)}缺 ${count} 篇`);
  $('shortages').hidden=!gaps.length; $('shortages').textContent=`${gaps.join('，')}。先补充素材或等待调研，不使用空白选题凑数。`;
  $('content-list').replaceChildren();
  if(!state.items.length){empty($('content-list'),'当日尚无内容。从「素材与选题」开始，或生成排期。');return;}
  state.items.forEach(item=>{
    const card=button('',async()=>selectContent(item.id),'content-card');card.classList.toggle('selected',state.selected===item.id);card.setAttribute('aria-pressed',String(state.selected===item.id));
    const top=node('div',null,'card-top');top.append(node('span',platformLabel(item.platform),'platform'),badge(item.status));card.append(top,node('span',item.topic_title,'card-title'));
    const meta=node('div',null,'card-meta');meta.append(node('span',`r${String(item.revision).padStart(4,'0')}`),node('span',item.production_time || '待安排'),node('span',item.manual?'人工安排':'自动排期'));if(item.approval_current)meta.append(node('span','✓ 当前验收'));card.append(meta);$('content-list').append(card);
  });
}
function resetDetail() {state.sequence++;state.detail=null;state.images=[];empty($('content-detail'),'从左侧选择一篇内容。');$('preview-empty').hidden=false;$('xhs-preview').hidden=true;$('mp-preview').hidden=true;$('mp-preview').removeAttribute('src');}
async function selectContent(id, silent=false) {
  state.selected=id;const sequence=++state.sequence;const date=state.date;
  document.querySelectorAll('.content-card').forEach((el,index)=>{el.classList.toggle('selected',state.items[index]?.id===id);el.setAttribute('aria-pressed',String(state.items[index]?.id===id));});
  if(!silent)empty($('content-detail'),'正在读取当前版本…');
  try {const data=await api(`/api/content/${date}/${id}`);if(sequence!==state.sequence||date!==state.date)return;state.detail=data.detail;renderDetail();renderPreview();}
  catch(error){if(sequence!==state.sequence)return;empty($('content-detail'),error.message);state.detail=null;renderPreview();if(!silent)toast(error.message,true);}
}
function block(title) { const section=node('section',null,'detail-block');section.append(node('h4',title));return section; }
function renderDetail() {
  const detail=state.detail,item=detail.item,draft=detail.artifacts.draft,topic=detail.topic || {};const root=$('content-detail');root.replaceChildren();
  root.append(node('p',platformLabel(item.platform),'eyebrow'),node('h3',draft?.title || topic.title || '未命名内容','detail-title'));
  const meta=node('div',null,'detail-meta');meta.append(badge(item.status),node('span',`版本 r${String(item.revision).padStart(4,'0')}`,'badge'),badge(detail.approval_current?'approved':'queued',detail.approval_current?'当前版本已验收':'当前版本未验收'));root.append(meta);
  const actions=node('div',null,'detail-actions');
  if(item.status==='queued')actions.append(button('加入宿主领取队列',async()=>{await api(`${contentPath(item)}/produce`,'POST',{});toast('已加入生产队列。');await refresh();},'button primary'));
  if(['failed','interrupted','needs_input'].includes(item.status))actions.append(button('重试当前版本',async()=>{await api(`${contentPath(item)}/retry`,'POST',{revision:item.revision});toast('已请求重试。');await refresh();}));
  if(item.status==='unknown')root.append(node('p','结果待确认。请先核查记录，不盲目重试。','notice'));
  root.append(actions);
  if(item.reason_code || item.reason)root.append(node('p',item.reason_code || item.reason,'notice'));
  if(draft){const copy=block('正文');copy.append(node('div',draft.body || draft.markdown || '暂无正文','copy'));root.append(copy);}else root.append(node('p','尚未生成正文。提交生产后，后台会逐步产出草稿、配图和验收记录。','muted small'));
  if(item.started_at || draft) {
    const section=block('修改意见 · 创建新版本'),form=node('form'),label=node('label','指出需要修改的事实、表达或配图'),feedback=node('textarea');feedback.name='feedback';feedback.rows=4;feedback.maxLength=10000;feedback.required=true;label.append(feedback);form.append(label);const submit=node('button','保存意见并建新版本','button');submit.type='submit';submit.disabled=item.status==='running';form.append(submit);form.addEventListener('submit',event=>{event.preventDefault();void runAction(submit,async()=>{const result=await api(`${contentPath(item)}/revise`,'POST',{revision:item.revision,feedback:feedback.value});dirtyForms.delete(form);toast(`已创建 r${String(result.item.revision).padStart(4,'0')}，请请求生产。`);await refresh();});});section.append(form);root.append(section);
  }
  const sources=block('选题依据');const evidence=topic.research?.evidence || topic.evidence || [];if(evidence.length){const list=node('ul',null,'evidence');evidence.slice(0,6).forEach(value=>list.append(node('li',value.claim || value.text || JSON.stringify(value))));sources.append(list);}else sources.append(node('p',topic.research?.summary || topic.summary || '请查看调研产物确认来源。','muted small'));root.append(sources);
  if(detail.artifacts.review){const review=block('验收记录');review.append(node('pre',JSON.stringify(detail.artifacts.review,null,2)));root.append(review);}
  if(detail.receipts?.length){const records=block('运行回执');detail.receipts.slice(-8).forEach(receipt=>{const entry=node('details'),summary=node('summary',`${receipt.stage || receipt.role || '任务'} · ${receipt.outcome==='succeeded'?'已完成':receipt.ok===false?'失败':'回执'}`);entry.append(summary,node('pre',JSON.stringify(receipt,null,2)));records.append(entry);});root.append(records);}
}
function renderPreview() {
  const detail=state.detail,draft=detail?.artifacts.draft;state.images=detail?.asset_urls || [];state.image=Math.min(state.image,Math.max(0,state.images.length-1));
  $('preview-empty').hidden=!!draft;$('xhs-preview').hidden=true;$('mp-preview').hidden=true;
  if(!draft){$('mp-preview').removeAttribute('src');return;}
  const item=detail.item;$('preview-version').textContent=`${platformLabel(item.platform)} · r${String(item.revision).padStart(4,'0')} · 未发布`;
  if(item.platform==='mp') { const src=`/preview/${state.date}/${item.id}/${item.revision}/mp`;$('mp-preview').hidden=false;if($('mp-preview').getAttribute('src')!==src)$('mp-preview').src=src; }
  else {$('mp-preview').removeAttribute('src');$('xhs-preview').hidden=false;$('xhs-title').textContent=draft.title || '';$('xhs-body').textContent=draft.body || draft.markdown || '';renderImage();}
}
function renderImage() { const image=state.images[state.image];$('xhs-image').hidden=!image;$('image-empty').hidden=!!image;$('image-counter').hidden=!image;if(image){$('xhs-image').src=image.url;$('xhs-image').alt=`当前版本配图 ${state.image+1}，共 ${state.images.length} 张`;$('image-counter').textContent=`${state.image+1} / ${state.images.length}`;}else $('xhs-image').removeAttribute('src');$('image-prev').disabled=state.images.length<2;$('image-next').disabled=state.images.length<2; }
function moveImage(direction) {if(!state.images.length)return;state.image=(state.image+direction+state.images.length)%state.images.length;renderImage();}
function platformSelect(value='xhs') {const select=node('select');select.setAttribute('aria-label','生产平台');['xhs','mp'].forEach(platform=>{const option=node('option',platformLabel(platform));option.value=platform;select.append(option);});select.value=value;return select;}
function renderLibrary() {
  $('inbox-list').replaceChildren();if(!state.inbox.length)empty($('inbox-list'),'收件箱为空。存下一条值得研究的素材。');
  state.inbox.forEach(input=>{const card=node('article',null,'library-card');card.append(badge(input.status,input.status==='queued'?'排队待调研':undefined),node('h4',input.theme || input.url || '参考原文素材'),node('p',(input.reference || input.notes || input.url || '').slice(0,180)));card.append(button('编辑素材',async()=>{state.input=input;$('input-form-heading').textContent='编辑素材';$('input-cancel').hidden=false;$('input-submit').textContent='保存并重新排队';['theme','url','reference','notes'].forEach(key=>$('input-form').elements[key].value=input[key] || '');$('input-form').elements.theme.focus();}));if(input.status==='running')card.lastChild.disabled=true;$('inbox-list').append(card);});
  $('topics-list').replaceChildren();if(!state.topics.length)empty($('topics-list'),'暂无选题。素材调研完成后，证据就绪的内容会出现在这里。');
  state.topics.filter(topic=>!$('topic-search').value || (topic.title + ' ' + (topic.notes || '')).toLowerCase().includes($('topic-search').value.toLowerCase())).forEach(topic=>{const card=node('article',null,'library-card');card.append(badge(topic.status),node('h4',topic.title || '未命名选题'),node('p',topic.research?.summary || topic.summary || '来源与证据已随选题保存。'));const evidence=topic.research?.evidence || topic.evidence || [];if(evidence.length){const details=node('details');details.append(node('summary','查看事实依据'),node('p',evidence.slice(0,4).map(value=>value.claim || value.text || JSON.stringify(value)).join('\n')));card.append(details);}const actions=node('div',null,'row-actions'),select=platformSelect();const produce=button('手动生产',async()=>{const result=await api('/api/produce','POST',{date:state.date,topic_id:topic.id,platform:select.value});state.selected=result.item.id;toast('内容已创建，生产已排队。');tab('today');await refresh();},'button primary');produce.disabled=topic.status!=='ready';actions.append(select,produce);const archived=topic.status==='archived';actions.append(button(archived?'恢复选题':'归档选题',async()=>{await api(`/api/topics/${topic.id}`,'PATCH',{revision:topic.revision || 1,fields:{status:archived?'ready':'archived'}});toast('选题状态已更新；已冻结内容不受影响。');await refresh();}));card.append(actions);$('topics-list').append(card);});
}
function topicSelect(selected) {const select=node('select');select.setAttribute('aria-label','排期选题');state.topics.filter(topic=>topic.status==='ready'||topic.id===selected).forEach(topic=>{const option=node('option',topic.title || '未命名选题');option.value=topic.id;select.append(option);});select.value=selected;return select;}
function renderPlan() {
  $('plan-table').replaceChildren();$('plan-empty').hidden=state.items.length>0;
  state.items.forEach(item=>{
    const row=node('tr'),title=node('td'),platform=node('td'),time=node('td'),status=node('td'),actions=node('td');const editable=!item.started_at && !['running','completed','approved','ready'].includes(item.status);status.append(badge(item.status));
    if(editable){const topic=topicSelect(item.topic_id),channel=platformSelect(item.platform),clock=node('input');clock.type='time';clock.value=item.production_time;clock.setAttribute('aria-label','生产时间');title.append(topic);platform.append(channel);time.append(clock);actions.append(button('保存调整',async()=>{await api(contentPath(item),'PATCH',{revision:item.revision,fields:{topic_id:topic.value,platform:channel.value,production_time:clock.value}});dirtyForms.delete($('plan-table'));toast('已保存排期。');await refresh();}));}
    else {title.append(node('span',item.topic_title,'plan-title'));platform.textContent=platformLabel(item.platform);time.textContent=item.production_time || '—';actions.append(button('查看 / 提修改',async()=>{tab('today');await selectContent(item.id);$('content-detail').scrollIntoView({block:'start',behavior:'auto'});}));}
    title.append(node('span',item.manual?'人工安排，自动排期会保留':item.reason || '自动排期','plan-reason'));row.append(title,platform,time,status,actions);$('plan-table').append(row);
  });
}
function renderSettings() {const settings=state.settings,form=$('settings-form');if(!settings)return;['timezone','planning_time','production_time'].forEach(key=>form.elements[key].value=settings[key]);['auto_plan','auto_produce'].forEach(key=>form.elements[key].checked=settings[key]);form.elements.xhs_count.value=settings.daily_counts.xhs;form.elements.mp_count.value=settings.daily_counts.mp;$('settings-state').textContent=`配置版本 ${settings.revision} · 发布始终关闭 · 由原生Agent定时任务领取 · 页面不启动模型`;}
function renderArchive(archive) {const el=$('archive-state');if(!archive || !archive.available){el.textContent='归档尚未生成：首次同步后，这里显示按日存档与远端状态。';return;}const manifest=archive.manifest || {},dates=archive.dates || [];el.replaceChildren();const lines=[`归档 ${dates.length} 个日期 · ${manifest.workspace_files || 0} 个内容文件`,`最近生成 ${manifest.generated_at || '未知'}`,`源提交 ${(manifest.source_commit || '未知').slice(0,12)} · ${manifest.pipeline_files || 0} 个脱敏管线记录`];lines.forEach(text=>el.append(node('p',text)));if(dates.length){el.append(node('p',dates.slice(-3).map(value=>`${value.date}：${value.items} 篇 / ${value.contents} 个内容 / ${value.revisions} 个版本`).join('；')));}el.append(node('p','打开归档仓库运行 open-workbench.ps1 即可查看同一工作台；发布始终关闭。','muted small'));}
function resetInput() {dirtyForms.delete($('input-form'));state.input=null;$('input-form').reset();$('input-form-heading').textContent='添加素材';$('input-cancel').hidden=true;$('input-submit').textContent='保存并排队调研';}
$('input-form').addEventListener('submit',event=>{event.preventDefault();void runAction($('input-submit'),async()=>{const fields=Object.fromEntries(new FormData(event.currentTarget));if(state.input)await api(`/api/inbox/${state.input.id}`,'PATCH',{revision:state.input.revision,fields});else await api('/api/inbox','POST',fields);resetInput();toast('已保存，排队待调研。库存不足时不会空白生产。');await refresh();});});
$('input-cancel').addEventListener('click',resetInput);
$('topic-search').addEventListener('input',renderLibrary);
$('show-automations').addEventListener('click',()=>runAction($('show-automations'),async()=>{const data=await api('/api/automations');$('automation-templates').replaceChildren();data.tasks.forEach(task=>{const details=node('details');details.append(node('summary',task.name),node('pre',task.prompt));$('automation-templates').append(details);});$('automation-templates').hidden=false;}));
$('show-runtime').addEventListener('click',()=>runAction($('show-runtime'),async()=>{const data=await api('/api/runtime');$('runtime-attempts').textContent=JSON.stringify(data.attempts,null,2);$('runtime-attempts').hidden=false;}));
$('settings-form').addEventListener('submit',event=>{event.preventDefault();const form=event.currentTarget;void runAction(form.querySelector('button[type=submit]'),async()=>{if(!state.settings)throw new Error('设置尚未读取，不能保存。');const value={revision:state.settings.revision,timezone:form.elements.timezone.value,planning_time:form.elements.planning_time.value,production_time:form.elements.production_time.value,daily_counts:{xhs:Number(form.elements.xhs_count.value),mp:Number(form.elements.mp_count.value)},auto_plan:form.elements.auto_plan.checked,auto_produce:form.elements.auto_produce.checked};const result=await api('/api/settings','PUT',value);state.settings=result.settings;dirtyForms.delete(form);renderSettings();toast('工作节奏已保存。');});});
$('generate-plan').addEventListener('click',()=>runAction($('generate-plan'),async()=>{await api(`/api/plan/${state.date}`,'POST',{});toast('排期已生成，保留人工内容；缺口按实际库存显示。');await refresh();}));
$('refresh').addEventListener('click',()=>void refresh());
$('day-picker').addEventListener('change',()=>{if(!$('day-picker').value)return;state.date=$('day-picker').value;dirtyForms.delete($('plan-table'));state.selected=null;resetDetail();void refresh();});
function fitPhonePreview() {
  const frame=$('phone-frame'),stage=frame.parentElement;
  if(!stage.clientWidth)return;
  const scale=Math.min(1,stage.clientWidth/frame.offsetWidth);
  frame.style.setProperty('--phone-scale',String(scale));
  stage.style.height=`${frame.offsetHeight*scale+18}px`;
}
$('phone-width').addEventListener('change',()=>{const value=$('phone-width').value;if(['360','390','414'].includes(value)){$('phone-frame').style.setProperty('--phone-width',`${value}px`);requestAnimationFrame(fitPhonePreview);}});
const phoneObserver=new ResizeObserver(fitPhonePreview);
phoneObserver.observe($('phone-frame').parentElement);
phoneObserver.observe($('phone-frame'));
window.addEventListener('resize',fitPhonePreview);
$('image-prev').addEventListener('click',()=>moveImage(-1));$('image-next').addEventListener('click',()=>moveImage(1));
$('xhs-preview').tabIndex=0;$('xhs-preview').addEventListener('keydown',event=>{if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();moveImage(event.key==='ArrowRight'?1:-1);}});
$('xhs-image').addEventListener('error',()=>{$('xhs-image').hidden=true;$('image-empty').hidden=false;$('image-empty').textContent='图片已变更或不可用，请刷新重新校验。';});
document.querySelectorAll('[role=tab]').forEach((el,index,list)=>{el.addEventListener('click',()=>tab(el.dataset.tab));el.addEventListener('keydown',event=>{let next;if(event.key==='ArrowRight')next=(index+1)%list.length;else if(event.key==='ArrowLeft')next=(index+list.length-1)%list.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=list.length-1;else return;event.preventDefault();tab(list[next].dataset.tab,true);});});
async function initialize() {try {const result=await api('/api/settings');state.settings=result.settings;}catch(error){toast(error.message,true);}state.date=localDate(state.settings?.timezone);$('day-picker').value=state.date;await refresh();}
void initialize();
setInterval(()=>{if(document.visibilityState==='visible' && !document.querySelector('form').contains(document.activeElement) && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName))void refresh(true);},15000);
