import {heading} from '../components/common/header.js';
import {table} from '../components/common/table.js';
import {status} from '../components/common/status.js';
import {esc} from '../utils/format.js';
import {loadSegments,loadApportion,loadTrace,downloadOccupancy,downloadTrace} from '../api/business.js';

const ui={runId:null,tab:'apportion',day:'',segOffset:0,segTotal:0,segDates:[],traceOffset:0,traceTotal:0,filter:{natural_key:'',group:''}};

async function fetchData(){
 const id=ui.runId;if(!id)return null;
 const run=window.__occData.runs.find(r=>r.id===id);
 if(!run||run.status!=='completed'||!run.result?.occupancy)return {run,empty:true};
 const [app,seg,tr]=await Promise.all([
  loadApportion(id),
  loadSegments(id,ui.day||null,ui.segOffset),
  loadTrace(id,ui.filter,ui.traceOffset),
 ]);
 ui.segTotal=seg.total;ui.segDates=seg.dates;ui.traceTotal=tr.total;
 return {run,apportion:app.rows,segments:seg.rows,trace:tr.rows};
}
export async function occupancy(c,d){
 window.__occData=d;
 if(!ui.runId)ui.runId=d.runs[0]?.id??null;
 let view=null,error='';
 try{view=await fetchData()}catch(e){error=e.message}
 const completed=d.runs.filter(r=>r.status==='completed'&&r.result?.occupancy);
 const tabs=[['apportion','分摊明细'],['segments','重叠段（去重分钟）'],['trace','逐笔追溯'],['dates','日期合计']];
 return heading('OCCUPANCY LEDGER / 07','服务占用去重分摊报告','冻结底稿扫描线分段；重叠段按在途单数占比分摊，全局四舍五入与两级最大余数取整，发布闸门守恒后可查可导。')
 +`<div class="inline-form"><label>报告<select id="occ-run">${d.runs.map(r=>`<option value="${r.id}" ${r.id===ui.runId?'selected':''}>${esc(r.name)} ${status(r.status)}</option>`).join('')}</select></label><button id="occ-export" ${!completed.some(r=>r.id===ui.runId)?'disabled':''}>下载分摊 CSV</button></div>`
 +(error?`<p class="error">${esc(error)}</p>`:'')
 +(view?.empty?`<p class="empty">该报告尚未发布占用分摊（未完成或未通过发布闸门）。原SLA结果在「核算与复核」页照常查看。</p>`:view?`<nav class="tabs">${tabs.map(([k,t])=>`<button class="tab ${ui.tab===k?'active':''}" data-tab="${k}">${t}</button>`).join('')}</nav>`+renderTab(view):'<p class="empty">暂无报告，请先在核算页排队任务。</p>')
 +`<details><summary>取整与发布口径</summary><p class="method">${esc(view?.run.result.occupancy?.rounding.rule||'全局四舍五入；最大余数法先分日期再分小组；同余按日期或组名升序。')}</p><p class="warning">${esc(view?.run.result.occupancy?.warnings?.join('；')||'')}</p></details>`;
}
function renderTab(view){
 const o=view.run.result.occupancy;
 if(ui.tab==='apportion')return `<h4>日期 × 小组整数分摊（合计 ${view.apportion.reduce((a,r)=>a+r['占用分钟'],0)} / 全局 ${o.global_minutes} 分钟）</h4>${table(view.apportion)}`;
 if(ui.tab==='dates')return `<h4>业务日期合计</h4>${table(o.date_totals)}`;
 if(ui.tab==='segments'){
  const pages=Math.ceil(ui.segTotal/100);const cur=Math.floor(ui.segOffset/100)+1;
  return `<h4>重叠段明细（共 ${ui.segTotal} 段，第 ${cur}/${Math.max(pages,1)} 页）</h4>
  <form id="seg-filter" class="inline-form"><label>日期筛选<select name="day"><option value="">全部日期</option>${ui.segDates.map(d=>`<option value="${esc(d)}" ${d===ui.day?'selected':''}>${esc(d)}</option>`).join('')}</select></label><button>应用</button></form>
  ${table(view.segments)}
  <div class="pager"><button id="seg-prev" ${ui.segOffset===0?'disabled':''}>上一页</button><span>${cur} / ${Math.max(pages,1)}</span><button id="seg-next" ${cur>=pages?'disabled':''}>下一页</button></div>`;
 }
 const pages=Math.ceil(ui.traceTotal/50);const cur=Math.floor(ui.traceOffset/50)+1;
 return `<h4>逐笔在途单追溯（共 ${ui.traceTotal} 笔，第 ${cur}/${Math.max(pages,1)} 页）</h4>
 <form id="trace-filter" class="inline-form"><label>底稿键<input name="natural_key" value="${esc(ui.filter.natural_key)}" placeholder="如 demo-12"></label><label>小组<input name="group" value="${esc(ui.filter.group)}" placeholder="小组名称"></label><button>查询</button><button type="button" id="trace-csv">导出当前筛选 CSV</button></form>
 ${table(view.trace)}
 <div class="pager"><button id="trace-prev" ${ui.traceOffset===0?'disabled':''}>上一页</button><span>${cur} / ${Math.max(pages,1)}</span><button id="trace-next" ${cur>=pages?'disabled':''}>下一页</button></div>`;
}
export function bindOccupancy(rerender){
 const sel=document.querySelector('#occ-run');if(sel)sel.onchange=()=>{ui.runId=Number(sel.value);ui.segOffset=0;ui.traceOffset=0;ui.day='';rerender()};
 const ex=document.querySelector('#occ-export');if(ex)ex.onclick=()=>downloadOccupancy(ui.runId);
 document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{ui.tab=b.dataset.tab;rerender()});
 const sf=document.querySelector('#seg-filter');if(sf)sf.onsubmit=e=>{e.preventDefault();ui.day=new FormData(sf).get('day');ui.segOffset=0;rerender()};
 const tf=document.querySelector('#trace-filter');if(tf)tf.onsubmit=e=>{e.preventDefault();const f=new FormData(tf);ui.filter={natural_key:f.get('natural_key')||'',group:f.get('group')||''};ui.traceOffset=0;rerender()};
 const tcsv=document.querySelector('#trace-csv');if(tcsv)tcsv.onclick=()=>downloadTrace(ui.runId,ui.filter);
 const sp=document.querySelector('#seg-prev');if(sp)sp.onclick=()=>{ui.segOffset=Math.max(0,ui.segOffset-100);rerender()};
 const sn=document.querySelector('#seg-next');if(sn)sn.onclick=()=>{ui.segOffset+=100;rerender()};
 const tp=document.querySelector('#trace-prev');if(tp)tp.onclick=()=>{ui.traceOffset=Math.max(0,ui.traceOffset-50);rerender()};
 const tn=document.querySelector('#trace-next');if(tn)tn.onclick=()=>{ui.traceOffset+=50;rerender()};
}
