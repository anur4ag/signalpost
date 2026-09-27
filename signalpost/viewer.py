"""Self-contained HTML viewer: search, filter, compare and verify every claim against its saved source."""
from __future__ import annotations

import json
from pathlib import Path


# Evidence copies that may be republished: Brønnøysund open data (NLOD 2.0), with birth dates already removed.
PUBLIC_EVIDENCE = {"official_registry", "official_annual_accounts"}


def compact(env: dict, public: bool = False) -> dict:
    keep = lambda e: not public or e.get("source_class") in PUBLIC_EVIDENCE
    return {
        "org": env["organisation_number"], "name": env.get("name") or env["organisation_number"], "state": env["availability"],
        "run": {k: env["run"].get(k) for k in ("run_id", "completed_at", "previous_run_id")},
        "summary": env.get("summary") or {}, "sections": env["sections"],
        "claims": [{k: c.get(k) for k in ("id", "section", "field", "value", "evidence_ids", "reporting_period", "effective_date", "basis",
                                           "stale", "first_observed_at", "identity_proof", "relationship")} for c in env["claims"]],
        "evidence": {e["id"]: {**{k: e.get(k) for k in ("source_url", "retrieved_at", "content_sha256", "source_class", "superseded")},
                               "snapshot_path": e.get("snapshot_path") if keep(e) else None}
                     for e in env["evidence"]},
        "changes": env.get("change_log", [])[-50:],
    }


def write(path: Path, envelopes: list[dict], report: dict, public: bool = False) -> None:
    """public=True links saved copies only for openly licensed sources; other evidence shows its source URL and hash."""
    data = json.dumps({"report": {k: report.get(k) for k in ("run_id", "completed_at", "availability", "changes")},
                       "companies": [compact(e, public) for e in envelopes]}, ensure_ascii=False, separators=(",", ":"))
    path.write_text(PAGE.replace("__DATA__", data.replace("</", "<\\/")), encoding="utf-8")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signalpost profiles</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--muted:#5d6470;--line:#e3e6eb;--accent:#1f5eff;--ok:#11824a;--warn:#a15c00;--bad:#b42318;--chip:#eef2ff}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--ink:#e8eaee;--muted:#9aa3b2;--line:#2a2f3a;--accent:#7aa2ff;--ok:#4cc38a;--warn:#f0b353;--bad:#ff7a70;--chip:#1e2640}}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:12px;flex-wrap:wrap;align-items:center}
header h1{font-size:17px;margin:0 8px 0 0}input[type=search]{flex:1;min-width:200px;padding:9px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font-size:15px}
select,button{padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);font-size:14px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
main{display:grid;grid-template-columns:minmax(260px,340px) minmax(0,1fr);gap:16px;padding:16px;max-width:1400px;margin:0 auto}
#list{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:auto;max-height:calc(100vh - 100px)}
.row{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border-bottom:1px solid var(--line);cursor:pointer}
.row:hover,.row.active{background:var(--chip)}.row .n{font-weight:600}.row .m{color:var(--muted);font-size:13px}
.row input{margin-top:4px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
h2{margin:0 0 4px;font-size:22px}h3{margin:0 0 8px;font-size:16px;display:flex;gap:8px;align-items:center}
.muted{color:var(--muted)}.badge{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);white-space:nowrap}
.available{color:var(--ok);border-color:var(--ok)}.not_available,.not_applicable{color:var(--muted)}.ambiguous,.blocked{color:var(--warn);border-color:var(--warn)}.failed{color:var(--bad);border-color:var(--bad)}
table{width:100%;border-collapse:collapse}td,th{padding:6px 8px;border-top:1px solid var(--line);vertical-align:top;text-align:left}th{font-size:13px;color:var(--muted);font-weight:600}
.src{font-size:12px;color:var(--muted)}.src a{color:var(--accent)}a.cite{white-space:nowrap;font-size:11px;background:var(--chip);color:var(--accent);border-radius:6px;padding:0 5px;margin-left:2px;text-decoration:none}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}.kpi{border:1px solid var(--line);border-radius:10px;padding:8px 10px}.kpi b{display:block;font-size:17px}
.stale{opacity:.65}.chg{border-left:3px solid var(--accent);padding-left:10px;margin:6px 0}
#compare{overflow:auto}.hide{display:none}.tabs{display:flex;gap:6px;margin-bottom:12px}
#detail{min-width:0}td,.src,p{overflow-wrap:anywhere}
footer{max-width:1400px;margin:auto;padding:16px}
@media (max-width:820px){main{grid-template-columns:minmax(0,1fr);padding:12px}#list{max-height:40vh}header{position:static}}
</style></head><body>
<header><h1>Signalpost</h1><input id="q" type="search" placeholder="Search name, org. number, municipality, industry" aria-label="Search companies">
<select id="f" aria-label="Filter"><option value="">All companies</option><option value="website">Verified website</option><option value="jobs">Job postings</option><option value="changes">Changed since last run</option><option value="failed">Failed / blocked</option></select>
<button id="cmp" class="primary" disabled>Compare (0)</button><span id="meta" class="muted"></span><a href="#credits">Credits</a></header>
<main><nav id="list" aria-label="Companies"></nav><section id="detail"></section></main>
<footer id="credits" class="src">Registry data © <a href="https://data.brreg.no">Brønnøysundregistrene</a>, licensed under <a href="https://data.norge.no/nlod/en/2.0">NLOD 2.0</a>. Role holders’ birth dates were removed from saved registry responses. Profiles are assembled from the cited sources.</footer>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent),C=D.companies,sel=new Set();let cur=null,view='profile';
const $=s=>document.querySelector(s),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const LABEL={identity:'Legal identity & brand',financials:'Latest annual accounts',financial_history:'Account history',leadership:'Leadership & roles',locations:'Workplaces & addresses',group:'Group',website:'Official website',social_profiles:'Company-owned profiles',contact:'Contact',jobs:'Hiring',activity:'Dated activity'};
const money=v=>v&&v.amount!=null?new Intl.NumberFormat('en',{maximumFractionDigits:0}).format(v.amount)+' '+(v.currency||''):'';
function val(c){const v=c.value;if(v==null)return'';if(c.section==='financials'&&v.amount!=null)return money(v);
 if(typeof v!=='object')return esc(v);if(Array.isArray(v))return v.map(x=>typeof x==='object'?esc(x.description?x.code+' '+x.description:x.name?x.name+(x.to?' (until '+x.to+')':''):JSON.stringify(x)):esc(x)).join('<br>');
 if(v.url)return `<a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.title||v.platform||v.domain||v.url)}</a>${v.date?' · '+esc(v.date):''}${c.identity_proof?'<div class=src>identity proof: '+esc(c.identity_proof.join(', ').replace(/_/g,' '))+'</div>':''}${c.relationship?'<div class=src>'+esc(c.relationship)+'</div>':''}`;
 if(v.name)return esc(v.name)+(v.role?' — '+esc(v.role):'')+(v.organisation_number?' ('+esc(v.organisation_number)+')':'')+(v.address?'<div class=src>'+esc(v.address)+(v.registered_employees!=null?' · '+v.registered_employees+' employees':'')+'</div>':'');
 if(v.event)return esc(v.event)+' · '+esc(v.date);if(v.address)return esc(v.address);if(v.code&&v.description!==undefined)return esc(v.code)+' — '+esc(v.description);
 return Object.entries(v).filter(([k,x])=>x!=null&&typeof x!=='object').map(([k,x])=>esc(k.replace(/_/g,' '))+': '+esc(x)).join(' · ')}
function src(co,c){return (c.evidence_ids||[]).map(id=>{const e=co.evidence[id];if(!e)return'';return `<div class=src>${esc(e.source_class)} · <a href="${esc(e.source_url)}" target="_blank" rel="noopener">source</a> · retrieved ${esc(e.retrieved_at)}${e.snapshot_path?` · <a href="${esc(e.snapshot_path)}" target="_blank">saved copy</a>`:''}${e.content_sha256?` · sha256 ${esc(e.content_sha256.slice(0,12))}…`:''}${e.superseded?' · earlier evidence':''}</div>`}).join('')}
const has=(co,f)=>co.claims.some(c=>c.field===f&&!c.stale);
function match(co){const q=$('#q').value.toLowerCase().trim(),f=$('#f').value;
 if(q){const hay=[co.name,co.org,...co.claims.filter(c=>['municipality','industry','public_brand'].includes(c.field)).map(c=>JSON.stringify(c.value))].join(' ').toLowerCase();if(!hay.includes(q))return false}
 if(f==='website')return has(co,'official_website');if(f==='jobs')return has(co,'job_posting');if(f==='changes')return co.changes.length>0;if(f==='failed')return co.state!=='available';return true}
function list(){const rows=C.filter(match);$('#meta').textContent=`${rows.length} of ${C.length} · run ${D.report.run_id}`;
 $('#list').innerHTML=rows.map(co=>{const w=co.claims.find(c=>c.field==='official_website');const m=co.claims.find(c=>c.field==='municipality');
 return `<div class="row ${cur===co.org?'active':''}" data-org="${co.org}"><input type=checkbox aria-label="Compare ${esc(co.name)}" ${sel.has(co.org)?'checked':''} data-cmp="${co.org}"><div><div class=n>${esc(co.name)}</div><div class=m>${co.org} · ${esc(m?m.value:'')}${w?' · '+esc(w.value.domain):''}</div></div></div>`}).join('')||'<p class="muted" style="padding:12px">No companies match.</p>'}
function show(org){cur=org;view='profile';location.hash='org='+org;const co=C.find(x=>x.org===org);if(!co)return;list();
 const num={};let n=0;Object.keys(LABEL).forEach(k=>co.claims.filter(c=>c.section===k).forEach(c=>num[c.id]=++n));const S=co.summary||{};
 const sum=(S.sentences||[]).map(s=>esc(s.text)+s.claim_ids.map(id=>`<a class=cite href="#c-${org}-${num[id]}" title="${esc(id)}">${num[id]}</a>`).join('')).join(' ');
 const k=f=>co.claims.find(c=>c.field===f&&c.id.includes('.selskap.')||c.field===f&&c.section==='identity');
 const kp=[['Revenue',k('revenue')],['Net result',k('net_result')],['Equity',k('equity')],['Employees',k('registered_employees')]].map(([l,c])=>`<div class=kpi><span class=muted>${l}</span><b>${c?val(c):'—'}</b></div>`).join('');
 let html=`<div class=card><h2>${esc(co.name)}</h2><div class=muted>Org. no. ${co.org} · <span class="badge ${co.state}">${co.state.replace('_',' ')}</span> · checked ${esc(co.run.completed_at)}</div>
 <p>${sum}</p><p class=muted>${esc(S.changes||'')}</p>${S.unknowns&&S.unknowns.length?'<details><summary>Not established ('+S.unknowns.length+')</summary><ul>'+S.unknowns.map(u=>'<li>'+esc(u)+'</li>').join('')+'</ul></details>':''}<div class=grid>${kp}</div></div>`;
 if(co.changes.length)html+=`<div class=card><h3>Changes</h3>${co.changes.slice().reverse().map(ch=>`<div class=chg><b>${esc(ch.event.replace(/_/g,' '))}</b> <span class=muted>${esc(ch.detected_at)} · ${esc(ch.materiality)}</span><div class=src>${esc(JSON.stringify(ch.old_value))} → ${esc(JSON.stringify(ch.new_value))}</div></div>`).join('')}</div>`;
 for(const [key,label] of Object.entries(LABEL)){const s=co.sections[key]||{availability:'not_available'};const cs=co.claims.filter(c=>c.section===key);
  html+=`<div class=card><h3>${label} <span class="badge ${s.availability}">${s.availability.replace('_',' ')}</span></h3>${s.note?`<div class=muted>${esc(s.note)}</div>`:''}${cs.length?'<table><tbody>'+cs.map(c=>`<tr id="c-${org}-${num[c.id]}" class="${c.stale?'stale':''}"><th>${num[c.id]}. ${esc(c.field.replace(/_/g,' '))}${c.reporting_period&&c.reporting_period.to?'<div class=src>period to '+esc(c.reporting_period.to)+'</div>':''}${c.effective_date?'<div class=src>dated '+esc(c.effective_date)+'</div>':''}</th><td>${val(c)}${c.stale?' <span class="badge blocked">stale</span>':''}${src(co,c)}</td></tr>`).join('')+'</tbody></table>':''}</div>`}
 $('#detail').innerHTML=html;window.scrollTo({top:0})}
function compare(){view='compare';const cos=[...sel].map(o=>C.find(x=>x.org===o));const rows=[['Org. no.',c=>c.org],['Legal form',c=>g(c,'legal_form')],['Municipality',c=>g(c,'municipality')],['Industry',c=>g(c,'industry')],['Employees',c=>g(c,'registered_employees')],['Revenue',c=>g(c,'revenue')],['Operating result',c=>g(c,'operating_result')],['Net result',c=>g(c,'net_result')],['Equity',c=>g(c,'equity')],['Website',c=>g(c,'official_website')],['Managing director',c=>{const r=c.claims.find(x=>x.field==='role'&&x.value.role_code==='DAGL');return r?esc(r.value.name):'—'}],['Job postings',c=>c.claims.filter(x=>x.field==='job_posting').length],['Changes',c=>c.changes.length]];
 function g(c,f){const x=c.claims.find(y=>y.field===f&&(y.section!=='financials'||y.id.includes('.selskap.')));return x?val(x):'—'}
 $('#detail').innerHTML=`<div class=card id=compare><h3>Compare ${cos.length} companies <button onclick="show(cur||'${cos[0].org}')">Back</button></h3><table><thead><tr><th></th>${cos.map(c=>`<th><a href="#org=${c.org}">${esc(c.name)}</a></th>`).join('')}</tr></thead><tbody>${rows.map(([l,f])=>`<tr><th>${l}</th>${cos.map(c=>`<td>${f(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`}
$('#q').oninput=list;$('#f').onchange=list;
$('#list').onclick=e=>{const cb=e.target.dataset.cmp;if(cb){e.target.checked?sel.add(cb):sel.delete(cb);$('#cmp').textContent=`Compare (${sel.size})`;$('#cmp').disabled=sel.size<2;return}const r=e.target.closest('.row');if(r)show(r.dataset.org)};
$('#cmp').onclick=compare;window.onhashchange=()=>{const m=location.hash.match(/org=(\d+)/);if(m&&m[1]!==cur)show(m[1])};
const h=location.hash.match(/org=(\d+)/);list();show(h?h[1]:(C.find(c=>has(c,'official_website'))||C[0]).org);
</script></body></html>"""
