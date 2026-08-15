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
const TEMPLATE_VARIABLE_GROUPS = [
  {label:'框架变量', items:[
    ['userid','触发者 ID（兼容别名）','所有群管回复'], ['user_id','触发者 ID','所有群管回复'],
    ['rawuserid','原始触发者 ID（兼容别名）','所有群管回复'], ['raw_user_id','原始触发者 ID','所有群管回复'],
    ['groupid','群 ID（兼容别名）','群聊回复'], ['group_id','群 ID','群聊回复'],
    ['username','触发者昵称','消息事件'], ['nickname','触发者昵称（兼容别名）','消息事件'],
    ['appid','机器人 AppID','所有群管回复'], ['botid','机器人 AppID（兼容别名）','所有群管回复'],
    ['bot_name','机器人名称','所有群管回复'], ['botname','机器人名称（兼容别名）','所有群管回复'],
    ['bot_qq','机器人 QQ','所有群管回复'], ['botqq','机器人 QQ（兼容别名）','所有群管回复'], ['selfid','机器人 QQ（兼容别名）','所有群管回复'],
    ['content','处理后的消息内容','消息事件'], ['raw_content','原始消息内容','消息事件'],
    ['message_id','消息 ID','消息事件'], ['messageid','消息 ID（兼容别名）','消息事件'], ['message_type','消息类型','消息事件'],
    ['event_id','事件 ID','所有事件'], ['eventid','事件 ID（兼容别名）','所有事件'], ['event_type','事件类型','所有事件'],
    ['timestamp','事件时间','所有事件'],
    ['channel_id','子频道 ID','频道事件'], ['channelid','子频道 ID（兼容别名）','频道事件'],
    ['guild_id','频道 ID','频道事件'], ['guildid','频道 ID（兼容别名）','频道事件'], ['image_url','首张图片地址','含图片的消息事件'],
  ]},
  {label:'通用变量', items:[
    ['target_id','目标成员 ID','自动处理、验证、禁言'], ['group_id','群 ID','按钮或验证'],
    ['member_id','成员 ID','列表项或验证'], ['username','成员昵称','列表项'],
    ['count','数量','操作结果'], ['failed','失败数量','撤回结果'],
    ['minutes','分钟数','禁言或验证'], ['seconds','秒数','刷屏窗口'],
    ['limit','消息条数上限','刷屏设置'], ['word','违禁词','违禁词操作'],
    ['action_text','处理结果','自动监管通知'], ['remaining','剩余时间','发言撤回'],
    ['error','错误信息','失败提示'], ['days','统计天数','统计模板'],
    ['url','图片地址','违禁词图片'], ['px','图片宽度','违禁词图片'], ['names','成员提及文本','禁言结果'], ['punish','处罚说明','刷屏设置'],
  ]},
  {label:'群管开关', items:[
    ['group_mark','群管状态图标','main_panel'], ['group_command','群管切换命令','main_panel'],
    ['notify_mark','提醒状态图标','main_panel'], ['notify_command','提醒切换命令','main_panel'],
    ['join_verify_mark','入群验证图标','main_panel'], ['join_verify_command','入群验证命令','main_panel'],
    ['block_links_mark','链接拦截图标','main_panel'], ['block_links_command','链接拦截命令','main_panel'],
    ['block_cards_mark','卡片拦截图标','main_panel'], ['block_cards_command','卡片拦截命令','main_panel'],
    ['block_forward_mark','转发拦截图标','main_panel'], ['block_forward_command','转发拦截命令','main_panel'],
    ['forbidden_words_mark','违禁词图标','main_panel'], ['forbidden_words_command','违禁词命令','main_panel'],
    ['spam_mark','刷屏检测图标','main_panel'], ['spam_command','刷屏检测命令','main_panel'],
    ['forbidden_switch_state','违禁词开关文字','category_forbidden'],
    ['join_verify_switch_state','验证开关文字','category_filter'], ['join_verify_switch_short','验证开关短文字','category_filter'],
    ['block_links_switch_state','链接开关文字','category_filter'], ['block_links_switch_short','链接开关短文字','category_filter'],
    ['block_cards_switch_state','卡片开关文字','category_filter'], ['block_cards_switch_short','卡片开关短文字','category_filter'],
    ['block_forward_switch_state','转发开关文字','category_filter'], ['block_forward_switch_short','转发开关短文字','category_filter'],
  ]},
  {label:'列表与统计', items:[
    ['request_count','入群申请数量','join_requests'], ['request_rows','入群申请行','join_requests'], ['next_page','下一页内容','join_requests'], ['next_cursor','下一页游标','入群申请分页'],
    ['index','当前序号','列表项'], ['avatar','30px 用户头像 Markdown','入群申请项'], ['request_id','入群申请 ID','入群申请按钮'], ['verify_message','验证信息（含审核问答）','入群申请项'],
    ['audit_count','审计记录数量','audit_list'], ['audit_rows','审计记录行','audit_list'], ['time','时间','审计行'], ['action_label','操作名称','审计行'], ['state','成功或失败','审计行'], ['affected_count','影响数量','审计行'], ['trace_short','Trace 短 ID','审计行'],
    ['global_mode','全局禁言模式','mute_list'], ['member_count','成员数量','mute_list'], ['member_rows','禁言成员行','mute_list'], ['overflow','超出提示','mute_list'], ['overflow_count','超出数量','禁言列表'],
    ['word_count','违禁词数量','forbidden_list_text'], ['word_rows','违禁词行','forbidden_list_text'], ['entry_count','处罚成员数量','punish_list'], ['entry_rows','处罚成员行','punish_list'], ['display','显示文本','处罚列表项'], ['expire_at','到期时间','禁言列表项'],
    ['management_count','管理操作数','management_stats'], ['manual_count','手动操作数','management_stats'], ['automatic_count','自动操作数','management_stats'], ['mute_count','禁言次数','management_stats'], ['unmute_count','解禁次数','management_stats'], ['recall_count','撤回次数','management_stats'], ['punish_count','处罚次数','management_stats'], ['approve_count','通过次数','management_stats'], ['decline_count','拒绝次数','management_stats'], ['config_count','配置变更次数','management_stats'], ['failed_count','失败次数','management_stats'],
  ]},
  {label:'验证与状态', items:[
    ['a','算术左值','verify_question'], ['op','运算符','verify_question'], ['b','算术右值','verify_question'], ['verify_id','验证 ID','verify_question 按钮'], ['option','验证选项','verify_question 按钮'], ['option_index','选项序号','verify_question 按钮'],
    ['is_admin','管理员状态','group_state'], ['is_full_access','完整权限状态','group_state'], ['allow_proactive_msg','主动消息权限','group_state'], ['retry_count','重试次数','verify_wrong_muted'], ['retry_text','重试提示','verify_wrong_muted'], ['decision_text','审批结果','join_declined'], ['scope_text','处理范围','recall_done'], ['failed_text','失败提示','recall_done'],
  ]},
];
let templateInsertTarget = 'tpl-content';
let groups = [];
let dashboard = null;
let templates = {};
let selectedTemplateKey = '';
let activePage = 'overview';
let templateVariablesOpen = false;
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
function setTemplateVariablesOpen(open){
  const modal=$('template-variable-modal'),toggle=$('template-variable-toggle');
  if(!modal||!toggle)return;
  templateVariablesOpen=!!open;modal.hidden=!templateVariablesOpen;toggle.setAttribute('aria-expanded',String(templateVariablesOpen));
}
function closeTemplateVariables(){setTemplateVariablesOpen(false)}
function updateTemplateVariableContext(){
  const context=$('template-variable-context');
  if(context)context.textContent=`当前插入位置：${templateInsertTargetLabel()}`;
}
function templateInsertTargetLabel(){return ({'tpl-content':'发送正文','tpl-buttons':'按钮 JSON','tpl-raw':'完整模板 JSON'})[templateInsertTarget]||'发送正文'}
function renderTemplateVariables(){
  const item=selectedTemplate();
  if(!item){$('template-variable-context').textContent='';$('template-variable-list').innerHTML='';closeTemplateVariables();return}
  updateTemplateVariableContext();
  $('template-variable-list').innerHTML='<div class="template-variable-groups">'+TEMPLATE_VARIABLE_GROUPS.map(group=>'<section class="template-variable-group"><div class="template-variable-group-title">'+esc(group.label)+'</div><div class="template-variable-items">'+group.items.map(([name,description,scope])=>'<button type="button" class="template-variable" data-template-variable="'+esc(name)+'" title="适用：'+esc(scope)+'"><code>{'+esc(name)+'}</code><small>'+esc(description)+'</small></button>').join('')+'</div></section>').join('')+'</div>';
  closeTemplateVariables();
}
function insertTemplateVariable(name){
  const field=$(templateInsertTarget)||$('tpl-content');
  if(!field||field.disabled)return;
  const token='{'+name+'}';const start=field.selectionStart??field.value.length;const end=field.selectionEnd??start;
  field.value=field.value.slice(0,start)+token+field.value.slice(end);field.focus();const cursor=start+token.length;field.setSelectionRange(cursor,cursor);closeTemplateVariables();
}
function renderTemplateForm(){
  const item=selectedTemplate(),has=!!item;$('template-form').hidden=!has;$('template-empty').hidden=has;$('save-template').disabled=!has;
  if(!has){$('template-title').textContent='选择模板';$('template-key').textContent='模板保存在 data/reply_templates.json';return}
  $('template-title').textContent=templateLabel(selectedTemplateKey,item);$('template-key').textContent=selectedTemplateKey;
  $('tpl-label').value=item.label||'';$('tpl-category').value=item.category||'';$('tpl-small-buttons').checked=!!item.small_buttons;$('tpl-at-user').checked=item.at_user!==false;
  $('tpl-msg-type').value=item.msg_type===0||item.msg_type===2?String(item.msg_type):'';
  $('tpl-content').value=item.content||'';$('tpl-buttons').value=pretty(item.buttons);$('tpl-raw').value=pretty(item);renderTemplateVariables();
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
  const joinPolicy=config.join_policy||{mode:'manual',reject_reason:'不符合入群要求'};$('cfg-join-policy').value=joinPolicy.mode;$('cfg-join-reason').value=joinPolicy.reject_reason||'';
  POLICY_FIELDS.forEach(([key,id])=>{const policy=policies[key]||{action:'recall',mute_minutes:10};$(id).checked=!!features[key];$(id+'-action').value=policy.action;$(id+'-mute').value=policy.mute_minutes});
  $('cfg-spam-enabled').checked=!!spam.enabled;$('cfg-spam-window').value=spam.window_seconds;$('cfg-spam-limit').value=spam.limit_count;$('cfg-spam-action').value=spam.action;$('cfg-spam-mute').value=spam.mute_minutes;
  syncPolicyFields();syncJoinPolicyFields();
}
function syncPolicyFields(){document.querySelectorAll('.policy-action').forEach(select=>{const row=select.closest('.rule-controls');const input=row?.querySelector('.mute-duration input');if(input){const enabled=select.value!=='recall';input.disabled=!enabled;input.closest('.mute-duration').classList.toggle('disabled',!enabled)}})}
function syncJoinPolicyFields(){const mode=$('cfg-join-policy').value;const enabled=mode==='auto_decline'||mode==='auto_blacklist';$('cfg-join-reason').disabled=!enabled;$('cfg-join-reason-field').classList.toggle('disabled',!enabled)}
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
  const payload={group_id:currentGroup(),enabled:$('cfg-enabled').checked,notify:$('cfg-notify').checked,features,policies,join_policy:{mode:$('cfg-join-policy').value,reject_reason:$('cfg-join-reason').value.trim()},spam:{enabled:$('cfg-spam-enabled').checked,window_seconds:Number($('cfg-spam-window').value),limit_count:Number($('cfg-spam-limit').value),action:$('cfg-spam-action').value,mute_minutes:Number($('cfg-spam-mute').value)}};
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
$('cfg-join-policy').addEventListener('change',syncJoinPolicyFields);
$('template-search').addEventListener('input',renderTemplates);$('template-list').addEventListener('click',event=>{const button=event.target.closest('[data-template-key]');if(button){selectedTemplateKey=button.dataset.templateKey;renderTemplates()}});$('save-template').addEventListener('click',saveTemplate);$('apply-template-json').addEventListener('click',applyRawTemplate);$('sync-template-json').addEventListener('click',syncRawTemplate);
document.querySelectorAll('#tpl-content,#tpl-buttons,#tpl-raw').forEach(field=>field.addEventListener('focus',()=>{templateInsertTarget=field.id;updateTemplateVariableContext()}));$('template-variable-toggle').addEventListener('click',()=>setTemplateVariablesOpen(!templateVariablesOpen));$('template-variable-close').addEventListener('click',closeTemplateVariables);$('template-variable-modal').addEventListener('click',event=>{if(event.target.matches('[data-template-variable-close]'))closeTemplateVariables()});$('template-variable-list').addEventListener('click',event=>{const button=event.target.closest('[data-template-variable]');if(button)insertTemplateVariable(button.dataset.templateVariable)});document.addEventListener('keydown',event=>{if(event.key==='Escape'&&templateVariablesOpen)closeTemplateVariables()});
$('forbidden-list').addEventListener('click',event=>{const button=event.target.closest('[data-delete-word]');if(button)deleteForbidden(button.dataset.deleteWord)});$('target-list').addEventListener('click',event=>{const button=event.target.closest('[data-delete-target]');if(button)deleteTarget(button.dataset.deleteTarget)});['filter-source','filter-status','filter-action'].forEach(id=>$(id).addEventListener('change',renderAudit));
sidebarMedia.addEventListener('change',()=>setSidebar(true));setSidebar(true);Promise.all([loadTemplates(),loadGroups()]).catch(error=>{showReady(false);toast(error.message,true)});
