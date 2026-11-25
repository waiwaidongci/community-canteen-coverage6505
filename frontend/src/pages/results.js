import {plot} from '../components/common/chart.js';
import {feedback} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {DISTRIBUTION, REVIEW, STATUS} from '../constants/enums.js';
import {handler, poll} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';
import {date, value} from '../utils/format.js';

function distributionPill(distribution) {
  if (!distribution) return null;
  const tone = DISTRIBUTION[distribution.state]?.tone || distribution.state;
  return pill(distribution.label, tone);
}

function extremesPanel(job) {
  const extremes = job.result?.extremes;
  if (!extremes) return null;
  const rows = [];
  for (const [label, item] of [['最小差值', extremes.min], ['最大差值', extremes.max]]) {
    for (const meal of item.meals) {
      rows.push({end:label, meal_id:meal.meal_id, resident_id:meal.resident_id, community:meal.community,
        date:meal.date, status:meal.status});
    }
  }
  const scope = extremes.min.delta === extremes.max.delta
    ? `所有可行组合的换版差值都是 ${value(extremes.min.delta)} 个百分点。`
    : `最小 ${value(extremes.min.delta)} 个百分点（${extremes.min.combinations} 种组合），最大 ${value(extremes.max.delta)} 个百分点（${extremes.max.combinations} 种组合）。`;
  return panel('达到两端的补报餐单', p(scope, 'subtle'), table([
    {key:'end', label:'差值端'}, {key:'meal_id', label:'结算流水号'}, {key:'resident_id', label:'居民编号'},
    {key:'community', label:'用餐社区'}, {key:'date', label:'用餐日期'}, {key:'status', label:'结算状态'},
  ], rows, {caption:'两端所选补报餐单'}));
}

export async function results(route, refresh) {
  const {spec,runs,datasets} = workspace;
  const id = route.id || runs[0]?.id;
  if (!id) return panel(spec.pages[3],empty('还没有可查看的运行结果。','#run','前往运行'));
  const job = await workspace.getRun(id);
  const source = datasets.find(item => item.id === job.dataset_id);
  const message = feedback();
  const finished = item => ['complete','failed','superseded'].includes(item.status);
  if (!finished(job)) {
    const original = location.hash;
    const status = el('p',{role:'status'},'后台正在计算，完成后会自动展示。');
    poll(()=>workspace.getRun(id),finished,()=>location.hash===original)
      .then(result=>{if (result && status.isConnected && location.hash===original) refresh();
        else if(status.isConnected) status.textContent='仍在执行，请点击刷新检查。';})
      .catch(error=>{if(status.isConnected) status.textContent=error.message;});
    return panel(job.name,pill(STATUS[job.status],job.status),status,button('刷新',{class:'quiet',onClick:refresh}));
  }
  const distribution = job.distribution;
  const download = button('下载结果 CSV', {class:'quiet','data-testid':'export',onClick:event=>
    handler(download,message,()=>workspace.download(`/runs/${job.id}/export`,`${spec.short_name}-${job.id}.csv`))(event)});
  const replay = job.result && distribution?.state === 'delivered' && session.canWrite
    ? button('按存档餐单复算',{class:'quiet','data-testid':'replay',onClick:event=>handler(replay,message,async()=>{
        const item = await workspace.replay(job.id,{dataset_id:job.dataset_id,name:`${job.name} · 按存档复算`,params:job.params});
        location.hash = `#results/${item.id}`;
      })(event)}) : null;
  const history = el('label',{class:'field run-picker',for:'run-picker'},el('span',{},'切换运行记录'),
    el('select',{id:'run-picker',onChange:event=>{location.hash=`#results/${event.target.value}`;}},
      (runs.some(item=>item.id===job.id) ? runs : [job,...runs]).map(item=>el('option',{value:item.id,selected:item.id===job.id},item.name))));
  const result = job.result;
  const analysis = result ? el('section',{class:'analysis','data-testid':'analysis-complete'},
    el('div',{class:'result-head'},p(job.name,'eyeline'),h(2,result.headline),p(result.note,'intro-copy'),
      el('div',{class:'actions'},pill(STATUS[job.status],job.status),distributionPill(distribution),download,replay,message)),
    metrics(result.metrics),
    result.charts.map(plot),
    (result.sections || []).map(item=>panel(item.title,p(item.text))),
    (distribution && !['eligible','delivered','replay'].includes(distribution.state))
      && panel('分发资格', p(distribution.reason, 'feedback')),
    extremesPanel(job),
    result.tables.map(item=>panel(item.title,table(item.columns,item.rows,{caption:item.title}))))
    : panel(job.name,pill(STATUS[job.status],job.status),p(job.error || (job.status === 'superseded' ? '同批已有更晚受理的试算。' : ''),'feedback'),
      link('返回调整参数',`#run/${job.dataset_id}`,{class:'quiet'}));
  const inputs = spec.params.map(param=>el('div',{},el('dt',{},param.label),el('dd',{},value(job.params[param.key]))));
  return el('div',{},el('div',{class:'two-col'},el('div',{class:'wide-panel'},analysis),
    el('aside',{class:'sidebar-panel'},panel('运行来源',history,
      source && link(source.name,`#inputs/${source.id}`),p(date(job.finished_at),'subtle'),
      el('dl',{class:'dictionary'},inputs)),
    distribution && panel('分发资格', p(distribution.label,'eyeline'), p(distribution.reason,'subtle')),
    result && panel(spec.decision, pill(REVIEW[job.review],job.review),
      link('打开复核记录',`#review/${job.id}`,{class:'quiet'})))));
}
