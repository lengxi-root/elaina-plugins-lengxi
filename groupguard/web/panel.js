const BASE = '/api/ext/groupguard';
const $ = id => document.getElementById(id);
const THEME_MAP = {'--bg':'--host-bg','--bg2':'--host-bg2','--bg3':'--host-bg3','--bg-float':'--host-float','--text':'--host-text','--text2':'--host-text2','--text3':'--host-text3','--border':'--host-border','--accent':'--host-accent','--accent-hover':'--host-accent-hover','--accent-light':'--host-accent-light','--accent-soft':'--host-accent-soft','--success':'--host-success','--danger':'--host-danger','--warning':'--host-warning','--info':'--host-info'};
const PAGE_TITLES = {overview:'概览',config:'功能设置',forbidden:'违禁词',targets:'发言撤回',templates:'消息模板',audit:'审计日志'};
const ACTION_LABELS = {mute:'禁言',unmute:'解禁',recall:'撤回消息',speak_recall:'发言撤回',cancel_recall:'取消撤回',approve_join:'通过入群',decline_join:'拒绝入群',blacklist_join:'拒绝并拉黑',verify_pass:'通过验证',verify_failure_mute:'验证失败禁言',spam_punish:'刷屏处罚',config_change:'配置变更',forbidden_add:'添加违禁词',forbidden_delete:'删除违禁词',forbidden_clear:'清空违禁词',cache_clear:'清除缓存'};
const SOURCE_LABELS = {command:'群命令',automatic:'自动监管',verification:'入群验证',web:'Web 面板'};
const POLICY_FIELDS = [
  ['block_links','cfg-block-links'],
  ['block_cards','cfg-block-cards'],
  ['block_forward','cfg-block-forward'],
  ['forbidden_words','cfg-forbidden'],
];
let groups = [];
let dashboard = null;
let templates = {};
let selectedTemplateKey = '';
let activePage = 'overview';
const sidebarMedia = window.matchMedia('(max-width:700px)');

function syncHostTheme(){
  try{
    if(window.parent===window)return;
    const style=window.parent.getComputedStyle(window.parent.document.documentElement);
    Object.entries(THEME_MAP).forEach(([source,target])=>{const value=style.getPropertyValue(source).trim();if(value)document.documentElement.style.setProperty(target,value)});
    document.documentElement.style.colorScheme=style.colorScheme||'normal';
  }catch(_){}
}
syncHostTheme();
try{if(window.parent!==window)new MutationObserver(syncHostTheme).observe(window.parent.document.documentElement,{attributes:true,attributeFilter:['style','class']})}catch(_){}

function esc(value){return String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))}
function toast(message,error=false){const node=$('toast');node.textContent=message;node.className='toast show'+(error?' error':'');clearTimeout(toast.timer);toast.timer=setTimeout(()=>node.className='toast',2600)}
async function api(path,options={}){
  const headers=new Headers(options.headers||{});
  if(options.body)headers.set('Content-Type','application/json');
  const response=await fetch(BASE+path,{...options,headers,credentials:'same-origin'});
  const raw=await response.text();let payload={};
  try{payload=raw?JSON.parse(raw):{}}catch(_){payload={error:raw}}
  if(!response.ok||payload.success===false)throw new Error(payload.error||('HTTP '+response.status));
  return payload.data??payload;
}
function currentGroup(){return $('group-select').value}
function formatGroup(item){const name=item.group_name||('群 '+item.group_id);const state=item.in_group?'':' · 已离群';return `${name} (${item.group_id})${state}`}
function formatTime(timestamp){return new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(Number(timestamp)*1000))}
function formatExpire(expire){if(!expire)return '永久';const remain=Number(expire)*1000-Date.now();if(remain<=0)return '已到期';return new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(Number(expire)*1000))}
function setSidebar(collapsed){$('app').classList.toggle('sidebar-collapsed',collapsed);document.body.classList.toggle('sidebar-open',sidebarMedia.matches&&!collapsed);const button=$('sidebar-toggle');button.textContent=sidebarMedia.matches?(collapsed?'☰':'×'):(collapsed?'›':'‹');button.title=collapsed?'展开侧边栏':'收起侧边栏';button.setAttribute('aria-label',button.title)}
function openPage(page){
  activePage=PAGE_TITLES[page]?page:'overview';
  document.querySelectorAll('.nav button').forEach(button=>button.classList.toggle('active',button.dataset.page===activePage));
  document.querySelectorAll('.page').forEach(node=>node.classList.toggle('active',node.id===`page-${activePage}`));
  $('page-title').textContent=PAGE_TITLES[activePage];
  $('empty-state').hidden=activePage==='templates'||groups.length>0;
  if(activePage==='templates'&&!Object.keys(templates).length)loadTemplates();
  if(sidebarMedia.matches)setSidebar(true);
}
function showReady(hasGroups=true){
  $('loading').hidden=true;$('empty-state').hidden=hasGroups;
  document.querySelectorAll('.page').forEach(node=>{node.hidden=!hasGroups&&node.id!=='page-templates'});
}

async function loadGroups(){
  const previous=currentGroup()||localStorage.getItem('groupguard-group')||'';
  groups=(await api('/groups')).groups||[];
  $('group-select').innerHTML=groups.length?groups.map(item=>`<option value="${esc(item.group_id)}">${esc(formatGroup(item))}</option>`).join(''):'<option value="">暂无群聊</option>';
  if(groups.some(item=>item.group_id===previous))$('group-select').value=previous;
  if(!groups.length){dashboard=null;showReady(false);openPage('templates');return}
  localStorage.setItem('groupguard-group',currentGroup());
  await loadDashboard();
}
async function loadTemplates(){
  try{
    const data=await api('/templates');templates=data.templates||{};
    const keys=Object.keys(templates);
    if(!selectedTemplateKey||!templates[selectedTemplateKey])selectedTemplateKey=keys[0]||'';
    renderTemplates();
  }catch(error){toast(error.message,true)}
}
async function loadDashboard(){
  const groupId=currentGroup();if(!groupId)return;
  $('loading').hidden=false;
  try{dashboard=await api(`/dashboard?group_id=${encodeURIComponent(groupId)}&days=${encodeURIComponent($('days-select').value)}`);renderAll();showReady(true)}catch(error){$('loading').hidden=true;toast(error.message,true)}
}
function renderAll(){
  if(!dashboard)return;
  const group=dashboard.group||{};
  $('group-caption').textContent=`${group.group_name||('群 '+group.group_id)} · ${group.member_count||0} 名成员`;
  renderOverview();renderConfig();renderForbidden();renderTargets();renderAudit();
}
function templateLabel(key,item){return item.label||key}
function renderTemplates(){
  const query=$('template-search').value.trim().toLowerCase();
  const entries=Object.entries(templates).filter(([key,item])=>`${key} ${item.label||''} ${item.category||''}`.toLowerCase().includes(query)).sort((a,b)=>`${a[1].category||''}-${templateLabel(a[0],a[1])}`.localeCompare(`${b[1].category||''}-${templateLabel(b[0],b[1])}`,'zh-CN'));
  $('template-count').textContent=`共 ${Object.keys(templates).length} 个模板`;
  $('template-list').innerHTML=entries.length?entries.map(([key,item])=>`<button type="button" class="template-item ${key===selectedTemplateKey?'active':''}" data-template-key="${esc(key)}"><span>${esc(templateLabel(key,item))}</span><small>${esc(item.category||'未分类')} · ${esc(key)}</small></button>`).join(''):'<div class="empty">没有匹配的模板</div>';
  renderTemplateForm();
}
function pretty(value){return JSON.stringify(value??null,null,2)}
function parseJsonField(id,label){const value=$(id).value.trim();try{return value?JSON.parse(value):null}catch(_){throw new Error(`${label} JSON 格式无效`)}}
function selectedTemplate(){return templates[selectedTemplateKey]||null}
function renderTemplateForm(){
  const item=selectedTemplate(),has=!!item;$('template-form').hidden=!has;$('template-empty').hidden=has;$('save-template').disabled=!has;
  if(!has){$('template-title').textContent='选择模板';$('template-key').textContent='全部模板保存在 reply_templates.json';return}
  $('template-title').textContent=templateLabel(selectedTemplateKey,item);$('template-key').textContent=selectedTemplateKey;
  $('tpl-label').value=item.label||'';$('tpl-category').value=item.category||'';$('tpl-small-buttons').checked=!!item.small_buttons;$('tpl-at-user').checked=item.at_user!==false;
  $('tpl-msg-type').value=item.msg_type===0||item.msg_type===2?String(item.msg_type):'';
  $('tpl-content').value=item.content||'';$('tpl-buttons').value=pretty(item.buttons);$('tpl-raw').value=pretty(item);
}
function formTemplate(){
  const current=selectedTemplate();if(!current)throw new Error('请先选择模板');
  const item=structuredClone(current);
  item.label=$('tpl-label').value.trim()||selectedTemplateKey;item.category=$('tpl-category').value.trim()||'未分类';item.content=$('tpl-content').value;item.buttons=parseJsonField('tpl-buttons','按钮');item.small_buttons=$('tpl-small-buttons').checked;item.at_user=$('tpl-at-user').checked;
  const msgType=$('tpl-msg-type').value;if(msgType==='')delete item.msg_type;else item.msg_type=Number(msgType);return item;
}
function applyRawTemplate(){try{const item=JSON.parse($('tpl-raw').value);if(!item||Array.isArray(item)||typeof item!=='object')throw new Error();templates[selectedTemplateKey]=item;renderTemplateForm();toast('完整 JSON 已应用到表单')}catch(_){toast('完整模板 JSON 格式无效',true)}}
function syncRawTemplate(){try{$('tpl-raw').value=pretty(formTemplate());toast('表单内容已同步到完整 JSON')}catch(error){toast(error.message,true)}}
async function saveTemplate(){
  const button=$('save-template');button.disabled=true;
  try{const template=formTemplate();const data=await api('/template',{method:'PUT',body:JSON.stringify({key:selectedTemplateKey,template,group_id:currentGroup()})});templates[selectedTemplateKey]=data.template;renderTemplates();toast('消息模板已保存并立即生效')}catch(error){toast(error.message,true)}finally{button.disabled=false}
}
function renderOverview(){
  const stats=dashboard.stats||{};
  $('m-management').textContent=stats.management_count||0;$('m-source').textContent=`手动 ${stats.manual_count||0} / 自动 ${stats.automatic_count||0}`;
  $('m-mute').textContent=stats.mute_count||0;$('m-unmute').textContent=`解禁 ${stats.unmute_count||0}`;
  $('m-recall').textContent=stats.recall_count||0;$('m-punish').textContent=`处罚 ${stats.punish_count||0}`;
  $('m-failed').textContent=stats.failed_count||0;$('m-approval').textContent=`审批 ${(stats.approve_count||0)+(stats.decline_count||0)}`;
  const enabled=!!dashboard.config.enabled;$('guard-state').textContent=enabled?'已启用':'未启用';$('guard-state').classList.toggle('on',enabled);
  $('s-enabled').textContent=enabled?'开启':'关闭';$('s-spam').textContent=dashboard.spam.enabled?'开启':'关闭';$('s-forbidden').textContent=`${dashboard.forbidden_words.length} 个`;$('s-targets').textContent=`${dashboard.targets.length} 人`;
  const rows=dashboard.audit.slice(0,5);$('recent-list').innerHTML=rows.length?rows.map(item=>`<div class="activity-row"><time>${esc(formatTime(item.time))}</time><b>${esc(ACTION_LABELS[item.action]||item.action)}</b><span class="result ${item.success?'ok':'fail'}">${item.success?'成功':'失败'}</span></div>`).join(''):'<div class="empty">暂无管理记录</div>';
}
function renderConfig(){
  const config=dashboard.config,features=config.features||{},policies=config.policies||{},spam=dashboard.spam;
  $('cfg-enabled').checked=!!config.enabled;$('cfg-notify').checked=!!config.notify;$('cfg-join-verify').checked=!!features.join_verify;
  POLICY_FIELDS.forEach(([key,id])=>{const policy=policies[key]||{action:'recall',mute_minutes:10};$(id).checked=!!features[key];$(id+'-action').value=policy.action;$(id+'-mute').value=policy.mute_minutes});
  $('cfg-spam-enabled').checked=!!spam.enabled;$('cfg-spam-window').value=spam.window_seconds;$('cfg-spam-limit').value=spam.limit_count;$('cfg-spam-action').value=spam.action;$('cfg-spam-mute').value=spam.mute_minutes;
  syncPolicyFields();
}
function syncPolicyFields(){document.querySelectorAll('.policy-action').forEach(select=>{const row=select.closest('.rule-controls');const input=row?.querySelector('.mute-duration input');if(input){const enabled=select.value!=='recall';input.disabled=!enabled;input.closest('.mute-duration').classList.toggle('disabled',!enabled)}})}
function renderForbidden(){
  const words=dashboard.forbidden_words||[];$('forbidden-count').textContent=`共 ${words.length} 个`;$('forbidden-list').innerHTML=words.length?words.map(word=>`<span class="tag"><span>${esc(word)}</span><button type="button" data-delete-word="${esc(word)}" title="删除违禁词" aria-label="删除违禁词">×</button></span>`).join(''):'<div class="empty">暂无违禁词</div>';
}
function renderTargets(){
  const targets=dashboard.targets||[];$('target-count').textContent=`共 ${targets.length} 人`;$('target-list').innerHTML=targets.length?targets.map(item=>`<tr><td>${esc(item.user_id)}</td><td>${esc(formatExpire(item.expire))}</td><td><span class="result ok">生效中</span></td><td class="action-col"><button class="table-action" type="button" data-delete-target="${esc(item.user_id)}" title="取消发言撤回" aria-label="取消发言撤回">×</button></td></tr>`).join(''):'<tr><td colspan="4" class="empty">暂无发言撤回成员</td></tr>';
}
function renderAudit(){
  const rows=dashboard.audit||[];const actions=[...new Set(rows.map(item=>item.action))].sort();const actionSelect=$('filter-action'),previous=actionSelect.value;actionSelect.innerHTML='<option value="">全部操作</option>'+actions.map(action=>`<option value="${esc(action)}">${esc(ACTION_LABELS[action]||action)}</option>`).join('');if(actions.includes(previous))actionSelect.value=previous;
  const source=$('filter-source').value,status=$('filter-status').value,action=actionSelect.value;const filtered=rows.filter(item=>(!source||item.source===source)&&(!status||String(item.success)===status)&&(!action||item.action===action));
  $('audit-list').innerHTML=filtered.length?filtered.map(item=>`<tr><td>${esc(formatTime(item.time))}</td><td>${esc(ACTION_LABELS[item.action]||item.action)}</td><td>${esc(SOURCE_LABELS[item.source]||item.source)}</td><td>${esc(item.operator_id||'-')}</td><td>${esc(item.target_id||'-')}</td><td>${Number(item.affected_count)||0}</td><td><span class="result ${item.success?'ok':'fail'}">${item.success?'成功':'失败'}</span></td><td class="trace">#${esc(String(item.trace_id||'').slice(0,8))}</td></tr>`).join(''):'<tr><td colspan="8" class="empty">没有符合条件的审计记录</td></tr>';
}
async function saveConfig(){
  const button=$('save-config');button.disabled=true;
  const features={join_verify:$('cfg-join-verify').checked};const policies={};POLICY_FIELDS.forEach(([key,id])=>{features[key]=$(id).checked;policies[key]={action:$(id+'-action').value,mute_minutes:Number($(id+'-mute').value)}});
  const payload={group_id:currentGroup(),enabled:$('cfg-enabled').checked,notify:$('cfg-notify').checked,features,policies,spam:{enabled:$('cfg-spam-enabled').checked,window_seconds:Number($('cfg-spam-window').value),limit_count:Number($('cfg-spam-limit').value),action:$('cfg-spam-action').value,mute_minutes:Number($('cfg-spam-mute').value)}};
  try{dashboard=await api(`/config?days=${encodeURIComponent($('days-select').value)}`,{method:'PUT',body:JSON.stringify(payload)});renderAll();toast('群管配置已保存')}catch(error){toast(error.message,true)}finally{button.disabled=false}
}
async function addForbidden(event){event.preventDefault();const word=$('forbidden-input').value.trim();if(!word)return;try{const data=await api('/forbidden',{method:'POST',body:JSON.stringify({group_id:currentGroup(),word})});dashboard.forbidden_words=data.forbidden_words;renderForbidden();renderOverview();$('forbidden-input').value='';toast('违禁词已添加');await loadDashboard()}catch(error){toast(error.message,true)}}
async function deleteForbidden(word){try{const data=await api('/forbidden',{method:'DELETE',body:JSON.stringify({group_id:currentGroup(),word})});dashboard.forbidden_words=data.forbidden_words;renderForbidden();renderOverview();toast('违禁词已删除');await loadDashboard()}catch(error){toast(error.message,true)}}
async function deleteTarget(userId){try{const data=await api('/target',{method:'DELETE',body:JSON.stringify({group_id:currentGroup(),user_id:userId})});dashboard.targets=data.targets;renderTargets();renderOverview();toast('已取消发言撤回');await loadDashboard()}catch(error){toast(error.message,true)}}

document.querySelectorAll('.nav button').forEach(button=>button.addEventListener('click',()=>openPage(button.dataset.page)));
document.querySelectorAll('[data-open-page]').forEach(button=>button.addEventListener('click',()=>openPage(button.dataset.openPage)));
$('sidebar-toggle').addEventListener('click',()=>setSidebar(!$('app').classList.contains('sidebar-collapsed')));$('sidebar-scrim').addEventListener('click',()=>setSidebar(true));
$('reload').addEventListener('click',loadGroups);$('group-select').addEventListener('change',()=>{localStorage.setItem('groupguard-group',currentGroup());loadDashboard()});$('days-select').addEventListener('change',loadDashboard);$('save-config').addEventListener('click',saveConfig);$('forbidden-form').addEventListener('submit',addForbidden);
document.querySelectorAll('.policy-action').forEach(select=>select.addEventListener('change',syncPolicyFields));
$('template-search').addEventListener('input',renderTemplates);$('template-list').addEventListener('click',event=>{const button=event.target.closest('[data-template-key]');if(button){selectedTemplateKey=button.dataset.templateKey;renderTemplates()}});$('save-template').addEventListener('click',saveTemplate);$('apply-template-json').addEventListener('click',applyRawTemplate);$('sync-template-json').addEventListener('click',syncRawTemplate);
$('forbidden-list').addEventListener('click',event=>{const button=event.target.closest('[data-delete-word]');if(button)deleteForbidden(button.dataset.deleteWord)});$('target-list').addEventListener('click',event=>{const button=event.target.closest('[data-delete-target]');if(button)deleteTarget(button.dataset.deleteTarget)});['filter-source','filter-status','filter-action'].forEach(id=>$(id).addEventListener('change',renderAudit));
sidebarMedia.addEventListener('change',()=>setSidebar(true));setSidebar(true);Promise.all([loadTemplates(),loadGroups()]).catch(error=>{showReady(false);toast(error.message,true)});
