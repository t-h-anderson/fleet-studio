'use strict';
// Pure geometry shared by both board views and the layout regression tests.
const FleetLayout = (() => {
 const W=245,H=128,PAD=18;
 const internalRole=r=>r.kind==='subagent'||r.key==='subagent-reviewers'||/^subagent reviewers\b/i.test(r.baseline?.name||r.name);
 const visibleRoles=(roles,showInternal=false)=>roles.filter(r=>showInternal||!internalRole(r));
 const intersects=(a,b)=>a.x<b.x+b.w&&a.x+a.w>b.x&&a.y<b.y+b.h&&a.y+a.h>b.y;
 const rect=(n,p=0)=>({x:n.position.x-p,y:n.position.y-p,w:W+2*p,h:H+2*p});
 const segmentHits=(a,b,r)=>a.x===b.x ? a.x>r.x&&a.x<r.x+r.w&&Math.max(a.y,b.y)>r.y&&Math.min(a.y,b.y)<r.y+r.h : a.y>r.y&&a.y<r.y+r.h&&Math.max(a.x,b.x)>r.x&&Math.min(a.x,b.x)<r.x+r.w;
 const segments=points=>points.slice(1).map((p,i)=>[points[i],p]);
 function bundles(nodes,links){
  const ids=new Set(nodes.map(n=>n.id)),groups=new Map();
  for(const link of links.flatMap(l=>l.members||[l])){
   if(!ids.has(link.source)||!ids.has(link.target)||link.source===link.target)continue;
   const key=[link.source,link.target].sort().join(':');
   if(!groups.has(key))groups.set(key,{id:'pair:'+key,source:link.source,target:link.target,members:[]});
   groups.get(key).members.push(link);
  }
  return [...groups.values()].map(group=>{
   const forward=group.members.filter(l=>l.source===group.source).length,reverse=group.members.length-forward;
   return {...group,forward,reverse,label:group.members.length+' handoff'+(group.members.length===1?'':'s')};
  });
 }
 function arrange(nodes,links){
  // Rank one connection per role pair. The first declared direction is only a
  // layout bias; all actual directions/actions remain in the original model.
  const pairs=bundles(nodes,links),degree=new Map(nodes.map(n=>[n.id,pairs.filter(e=>e.source===n.id||e.target===n.id).length]));
  const satellites=new Map();
  for(const n of nodes){
   if(degree.get(n.id)!==1)continue;
   const pair=pairs.find(e=>e.source===n.id||e.target===n.id),hub=pair.source===n.id?pair.target:pair.source;
   if(pair.forward&&pair.reverse&&degree.get(hub)>=3)satellites.set(n.id,hub);
  }
  const core=nodes.filter(n=>!satellites.has(n.id)),ids=new Set(core.map(n=>n.id));
  const edges=pairs.filter(l=>ids.has(l.source)&&ids.has(l.target));
  const hubs=new Set(satellites.values());
  const connected=core.filter(n=>hubs.has(n.id)||edges.some(e=>e.source===n.id||e.target===n.id));
  const remaining=new Set(connected.map(n=>n.id)),left=[],right=[];
  while(remaining.size){
   const degree=id=>[edges.filter(e=>e.target===id&&remaining.has(e.source)).length,edges.filter(e=>e.source===id&&remaining.has(e.target)).length];
   const sink=[...remaining].find(id=>degree(id)[1]===0);
   if(sink!==undefined){right.unshift(sink);remaining.delete(sink);continue;}
   const source=[...remaining].find(id=>degree(id)[0]===0);
   const chosen=source??[...remaining].sort((a,b)=>{const [ai,ao]=degree(a),[bi,bo]=degree(b);return (bo-bi)-(ao-ai);})[0];
   left.push(chosen);remaining.delete(chosen);
  }
  const order=[...left,...right],index=new Map(order.map((id,i)=>[id,i])),rank=new Map(order.map(id=>[id,0]));
  const forward=edges.filter(e=>index.get(e.source)<index.get(e.target));
  for(const id of order)for(const e of forward.filter(e=>e.source===id))rank.set(e.target,Math.max(rank.get(e.target),rank.get(id)+1));
  const layers=[];
  order.forEach(id=>(layers[rank.get(id)]??=[]).push(id));
  const adjacent=[];
  forward.forEach((e,i)=>{let prev=e.source;for(let r=rank.get(e.source)+1;r<rank.get(e.target);r++){const dummy='dummy:'+i+':'+r;layers[r].push(dummy);adjacent.push([prev,dummy]);prev=dummy;}adjacent.push([prev,e.target]);});
  for(let sweep=0;sweep<8;sweep++){
   const down=sweep%2===0;
   const scan=Array.from({length:Math.max(0,layers.length-1)},(_,i)=>down?i+1:layers.length-i-2);
   for(const r of scan){const other=layers[r+(down?-1:1)];const score=id=>{const ns=adjacent.filter(e=>e[down?1:0]===id).map(e=>other.indexOf(e[down?0:1])).filter(i=>i>=0);return ns.length?ns.reduce((a,b)=>a+b,0)/ns.length:layers[r].indexOf(id);};const scores=new Map(layers[r].map(id=>[id,score(id)]));layers[r].sort((a,b)=>scores.get(a)-scores.get(b));}
  }
  const step=Math.min(380,Math.floor(3920/Math.max(1,layers.length-1))),rowStep=220;
  if(step<W+80)throw new Error('This workflow exceeds the automatic layout area. Use a smaller workflow or position the cards manually.');
  const positions={},maxRows=Math.max(1,...layers.map(l=>l.filter(id=>ids.has(id)).length));
  layers.forEach((layer,r)=>{const real=layer.filter(id=>ids.has(id));real.forEach((id,i)=>positions[id]={x:80+r*step,y:100+(maxRows-real.length)*rowStep/2+i*rowStep});});
  // Reciprocal leaf roles form a nearby support row rather than stretching
  // every leaf into the main delivery rank. Reserve each cell exactly once.
  const extra=nodes.filter(n=>!positions[n.id]),columns=Math.max(layers.length,Math.min(5,Math.ceil(Math.sqrt(extra.length))||1));
  const baseY=connected.length?100+(maxRows-1)*rowStep+H+100:100;
  const cells=Array.from({length:Math.max(extra.length,1)*columns},(_,i)=>({x:80+i%columns*step,y:baseY+Math.floor(i/columns)*rowStep}));
  for(const n of extra.filter(n=>satellites.has(n.id))){
   const hub=positions[satellites.get(n.id)];
   cells.sort((a,b)=>(Math.abs(a.x-hub.x)+2*(a.y-baseY))-(Math.abs(b.x-hub.x)+2*(b.y-baseY))||a.y-b.y||a.x-b.x);
   positions[n.id]=cells.shift();
  }
  cells.sort((a,b)=>a.y-b.y||a.x-b.x);
  for(const n of extra.filter(n=>!positions[n.id]))positions[n.id]=cells.shift();
  if(Object.values(positions).some(p=>p.x>4000||p.y>4000))throw new Error('This workflow exceeds the automatic layout area. Use a smaller workflow or position the cards manually.');
  return positions;
 }
 class Heap{
  constructor(){this.items=[];}
  push(item){const a=this.items;let i=a.length;a.push(item);while(i){const p=(i-1)>>1;if(a[p].cost<=item.cost)break;a[i]=a[p];i=p;}a[i]=item;}
  pop(){const a=this.items,top=a[0],last=a.pop();if(a.length){let i=0;while(i*2+1<a.length){let c=i*2+1;if(c+1<a.length&&a[c+1].cost<a[c].cost)c++;if(a[c].cost>=last.cost)break;a[i]=a[c];i=c;}a[i]=last;}return top;}
 }
 function linePenalty(a,b,used){
  let cost=0;
  for(const [c,d] of used){
   const ah=a.y===b.y,ch=c.y===d.y;
   if(ah===ch){if((ah?a.y===c.y:a.x===c.x)){const overlap=Math.min(ah?Math.max(a.x,b.x):Math.max(a.y,b.y),ah?Math.max(c.x,d.x):Math.max(c.y,d.y))-Math.max(ah?Math.min(a.x,b.x):Math.min(a.y,b.y),ah?Math.min(c.x,d.x):Math.min(c.y,d.y));if(overlap>0)cost+=overlap*4+30;}}
   else {const h=ah?[a,b]:[c,d],v=ah?[c,d]:[a,b];if(v[0].x>=Math.min(h[0].x,h[1].x)&&v[0].x<=Math.max(h[0].x,h[1].x)&&h[0].y>=Math.min(v[0].y,v[1].y)&&h[0].y<=Math.max(v[0].y,v[1].y))cost+=160;}
  }
  return cost;
 }
 function simplify(points){return points.filter((p,i)=>i===0||i===points.length-1||!((points[i-1].x===p.x&&p.x===points[i+1].x)||(points[i-1].y===p.y&&p.y===points[i+1].y)));}
 function findPath(start,end,obstacles,used,lanes){
  const xs=new Set([12,start.x,end.x]),ys=new Set([12,start.y,end.y]);
  obstacles.forEach(r=>{xs.add(Math.max(12,r.x));xs.add(r.x+r.w);ys.add(Math.max(12,r.y));ys.add(r.y+r.h);});
  lanes.forEach(p=>{xs.add(p.x);ys.add(p.y);});
  const xx=[...xs].sort((a,b)=>a-b),yy=[...ys].sort((a,b)=>a-b),nx=xx.length;
  const point=i=>({x:xx[i%nx],y:yy[Math.floor(i/nx)]}),si=yy.indexOf(start.y)*nx+xx.indexOf(start.x),ti=yy.indexOf(end.y)*nx+xx.indexOf(end.x);
  const q=new Heap(),dist=new Map(),prev=new Map(),valid=new Map();
  const initial=si*3;q.push({key:initial,cost:0});dist.set(initial,0);
  while(q.items.length){
   const {key}=q.pop(),i=Math.floor(key/3),dir=key%3,a=point(i);
   if(i===ti){const path=[];let k=key;while(k!==undefined){path.push(point(Math.floor(k/3)));k=prev.get(k);}return simplify(path.reverse());}
   const x=i%nx,y=Math.floor(i/nx);
   for(const [j,nd] of [[x>0?i-1:-1,1],[x<nx-1?i+1:-1,1],[y>0?i-nx:-1,2],[y<yy.length-1?i+nx:-1,2]]){
    if(j<0)continue;const b=point(j),edge=Math.min(i,j)+':'+Math.max(i,j);
    if(!valid.has(edge))valid.set(edge,!obstacles.some(r=>segmentHits(a,b,r)));
    if(!valid.get(edge))continue;
    const next=j*3+nd,cost=dist.get(key)+Math.abs(a.x-b.x)+Math.abs(a.y-b.y)+(dir&&dir!==nd?35:0)+linePenalty(a,b,used);
    if(cost>=(dist.get(next)??Infinity))continue;
    dist.set(next,cost);prev.set(next,key);q.push({key:next,cost:cost+Math.abs(b.x-end.x)+Math.abs(b.y-end.y)});
   }
  }
  return null;
 }
 function wrapLabel(text){
  const lines=[];let line='';
  for(const word of String(text||'Handoff').split(/\s+/)){if(line.length+word.length>29){if(line)lines.push(line);line='';}for(let i=0;i<word.length;i+=29){const chunk=word.slice(i,i+29);if(i){lines.push(line);line=chunk;}else line+=(line?' ':'')+chunk;}}
  if(line)lines.push(line);
  return lines;
 }
 function placeLabel(link,points,boxes,used,number){
  const lines=wrapLabel(link.label),w=Math.max(...lines.map(s=>s.length))*6.5+20,h=lines.length*15+12;
  const choices=[];
  for(const [a,b] of segments(points)){
   const horizontal=a.y===b.y,len=Math.abs(a.x-b.x)+Math.abs(a.y-b.y);
   for(const f of [.5,.3,.7]){
    const x=a.x+(b.x-a.x)*f,y=a.y+(b.y-a.y)*f;
    for(const side of (horizontal?[0,-1,1]:[-1,1])){
     const box={x:x-w/2+(horizontal?0:side*(w/2+12)),y:y-h/2+(horizontal?side*(h/2+10):0),w,h};
     if(box.x<5||box.y<5||boxes.some(b=>intersects(box,b))||used.some(([c,d])=>segmentHits(c,d,box)))continue;
     choices.push({box,lines,anchor:{x,y},score:(horizontal?0:90)+Math.abs(side)*25+Math.abs(f-.5)*20+(len<w?100:0)});
    }
   }
  }
  choices.sort((a,b)=>a.score-b.score);
  if(choices.length)return choices[0];
  // Congested manually placed nodes: keep a numbered action on its line;
  // the complete, editable contract is also in the handoff list.
  for(const [a,b] of segments(points)){const box={x:(a.x+b.x)/2-12,y:(a.y+b.y)/2-12,w:24,h:24};if(!boxes.some(r=>intersects(box,r)))return {box,lines:[String(number)],compact:true,anchor:{x:box.x+12,y:box.y+12}};}
  return null;
 }
 function route(nodes,links){
  const byId=new Map(nodes.map(n=>[n.id,n])),edges=links.filter(l=>byId.has(l.source)&&byId.has(l.target));
  const ports=new Map(),specs=edges.map(link=>{const s=byId.get(link.source),t=byId.get(link.target),dx=t.position.x-s.position.x,dy=t.position.y-s.position.y;let from,to;
   if(link.members){const vertical=Math.abs(dy)/(H+80)>Math.abs(dx)/(W+80);from=vertical?(dy>=0?'bottom':'top'):(dx>=0?'right':'left');to=vertical?(dy>=0?'top':'bottom'):(dx>=0?'left':'right');}
   else if(dx>W+30){from='right';to='left';}else if(dx<-W-30){from='bottom';to='bottom';}else{from=dy>=0?'bottom':'top';to=dy>=0?'top':'bottom';}
   const spec={link,s,t,from,to};for(const [n,side,end] of [[s,from,'start'],[t,to,'end']]){const k=n.id+':'+side;if(!ports.has(k))ports.set(k,[]);ports.get(k).push({spec,n,side,end});}return spec;});
  for(const list of ports.values()){
   list.sort((a,b)=>{const ap=(a.end==='start'?a.spec.t:a.spec.s).position,bp=(b.end==='start'?b.spec.t:b.spec.s).position;return ['left','right'].includes(a.side)?ap.y-bp.y:ap.x-bp.x;});
   list.forEach((v,i)=>{const p=v.n.position,f=(i+1)/(list.length+1),horizontal=['left','right'].includes(v.side),port={x:p.x+(horizontal?(v.side==='right'?W:0):Math.round(W*f)),y:p.y+(horizontal?Math.round(H*f):(v.side==='bottom'?H:0))};const outside={x:port.x+(v.side==='right'?PAD+8:v.side==='left'?-PAD-8:0),y:port.y+(v.side==='bottom'?PAD+8:v.side==='top'?-PAD-8:0)};v.spec[v.end]={port,outside};});
  }
  const boxes=nodes.map(n=>rect(n,PAD)),used=[],labels=[],routes=[];
  const maxX=Math.max(0,...nodes.map(n=>n.position.x+W)),maxY=Math.max(0,...nodes.map(n=>n.position.y+H));
  const lanes=Array.from({length:Math.min(12,edges.length)+1},(_,i)=>({x:maxX+45+i*22,y:maxY+60+i*28}));
  // Short forward handoffs first; return paths take free outer channels.
  specs.sort((a,b)=>Number(a.t.position.x<a.s.position.x)-Number(b.t.position.x<b.s.position.x)||Math.abs(a.t.position.x-a.s.position.x)-Math.abs(b.t.position.x-b.s.position.x));
  for(const spec of specs){
   const {link,s,t,start,end}=spec;
   const obstructed=[[start.port,start.outside,s.id],[end.port,end.outside,t.id]].some(([a,b,id])=>nodes.some(n=>n.id!==id&&segmentHits(a,b,rect(n,4))));
   let path=obstructed?null:findPath(start.outside,end.outside,[...boxes,...labels],used,lanes);
   if(!path&&!obstructed)path=findPath(start.outside,end.outside,boxes,used,lanes);
   let points=path?simplify([start.port,...path,end.port]):null;
   if(!points){
    // Tight manual spacing may have a clear path but no room for the normal
    // padded port leads. Try simple card-safe paths without the padding.
    const a=start.port,b=end.port,mx=(a.x+b.x)/2,my=(a.y+b.y)/2;
    const candidates=[
     [a,{x:mx,y:a.y},{x:mx,y:b.y},b],
     [a,{x:a.x,y:my},{x:b.x,y:my},b]
    ].map(p=>simplify(p.filter((v,i)=>i===0||v.x!==p[i-1].x||v.y!==p[i-1].y)));
    points=candidates.find(p=>segments(p).every(([a,b])=>!nodes.some(n=>segmentHits(a,b,rect(n)))));
   }
   if(!points){routes.push({link,points:[],label:null,unrouted:true});continue;}
   const label=placeLabel(link,points,[...boxes,...labels],used,edges.indexOf(link)+1);
   if(label)labels.push(label.box);
   used.push(...segments(points));routes.push({link,points,label});
  }
  return {routes,width:Math.max(1000,maxX+80,...routes.flatMap(r=>r.points.map(p=>p.x+40)),...labels.map(b=>b.x+b.w+30)),height:Math.max(700,maxY+80,...routes.flatMap(r=>r.points.map(p=>p.y+40)),...labels.map(b=>b.y+b.h+30))};
 }
 return {arrange,route,bundles,internalRole,visibleRoles,intersects,segmentHits,rect,segments};
})();
if(typeof module!=='undefined')module.exports=FleetLayout;
