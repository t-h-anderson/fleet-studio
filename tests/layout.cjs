const assert=require('node:assert/strict');
const fs=require('node:fs');
const L=require('../web/layout.js');
const sample=JSON.parse(fs.readFileSync('workflows/sample.workflow.json','utf8'));
const fleet={roles:sample.roles.map(r=>({...r,key:r.id,position:{x:0,y:0}})),roleLinks:sample.handoffs.map(h=>({...h,label:h.action}))};
function check(nodes,links,name){
 const positions=L.arrange(nodes,links),arranged=nodes.map(n=>({...n,position:positions[n.id]}));
 assert.deepEqual(L.arrange(arranged,links),positions,name+': stable arrangement');
 for(let i=0;i<arranged.length;i++)for(let j=i+1;j<arranged.length;j++)assert.ok(!L.intersects(L.rect(arranged[i]),L.rect(arranged[j])),name+': cards overlap');
 const geometry=L.route(arranged,links);
 const valid=links.filter(l=>positions[l.source]&&positions[l.target]);
 assert.equal(geometry.routes.length,valid.length);
 for(const route of geometry.routes){
  assert.ok(!route.unrouted,name+': missing route '+route.link.label);
  assert.ok(route.points.length>=2);
  for(const [a,b] of L.segments(route.points)){
   assert.ok(a.x===b.x||a.y===b.y,name+': non orthogonal route');
   for(const n of arranged)assert.ok(!L.segmentHits(a,b,L.rect(n)),name+': '+route.link.label+' crosses '+n.name);
  }
  if(route.label)for(const n of arranged)assert.ok(!L.intersects(route.label.box,L.rect(n)),name+': label overlaps '+n.name);
 }
 console.log(name+': '+nodes.length+' cards, '+geometry.routes.length+' routes, '+geometry.width+' x '+geometry.height);
 return {positions,geometry,arranged};
}
function crossings(geometry){
 let count=0;
 for(let i=0;i<geometry.routes.length;i++)for(let j=i+1;j<geometry.routes.length;j++){
  for(const [a,b] of L.segments(geometry.routes[i].points))for(const [c,d] of L.segments(geometry.routes[j].points)){
   const ah=a.y===b.y,ch=c.y===d.y;if(ah===ch)continue;
   const h=ah?[a,b]:[c,d],v=ah?[c,d]:[a,b];
   if(v[0].x>Math.min(h[0].x,h[1].x)&&v[0].x<Math.max(h[0].x,h[1].x)&&h[0].y>Math.min(v[0].y,v[1].y)&&h[0].y<Math.max(v[0].y,v[1].y))count++;
  }
 }
 return count;
}
const initialActions=new Set(['Set scope and priorities','Assign scoped packet','Request static review','Return findings','Submit reviewed packet','Assign bounded group','Transfer candidate','Review integrated diff','Return bundle findings','Escalate scope exception','Propose process improvement']);
const initialLinks=fleet.roleLinks.filter(l=>initialActions.has(l.label));
const primary=L.visibleRoles(fleet.roles),main=check(primary,initialLinks,'sample core workflow');
assert.equal(crossings(main.geometry),0,'Main workflow has unnecessary crossings');
const find=key=>primary.find(n=>n.key===key).id;
assert.ok(main.positions[find('coordinator')].x<main.positions[find('writer')].x);
assert.ok(main.positions[find('writer')].x<main.positions[find('integrator')].x);
assert.equal(primary.length,fleet.roles.length-1);
assert.ok(!main.geometry.routes.some(r=>r.link.source===fleet.roles.find(L.internalRole).id||r.link.target===fleet.roles.find(L.internalRole).id));
check(fleet.roles,initialLinks,'sample core including internal reviewers');
const spec=JSON.parse(fs.readFileSync('workflows/sample.workflow.json','utf8'));
const specRoles=spec.roles.map(r=>({...r,key:r.id,position:{x:0,y:0}})),specLinks=spec.handoffs.map(h=>({...h,label:h.action}));
check(L.visibleRoles(specRoles),specLinks,'Managed sample workflow');
check(specRoles,specLinks,'Managed sample including parent review');

const groupedBefore=structuredClone({roles:specRoles,links:specLinks});
const groupedNodes=L.visibleRoles(specRoles),groupedLinks=L.bundles(groupedNodes,specLinks);
const grouped=check(groupedNodes,groupedLinks,'Grouped sample handoffs');
assert.equal(groupedLinks.length,3);
assert.equal(groupedLinks.reduce((n,g)=>n+g.members.length,0),3);
assert.deepEqual(groupedLinks.flatMap(g=>g.members.map(m=>m.id)).sort(),specLinks.filter(l=>groupedNodes.some(n=>n.id===l.source)&&groupedNodes.some(n=>n.id===l.target)).map(l=>l.id).sort());
assert.ok(groupedLinks.filter(g=>g.reverse).every(g=>g.forward&&g.members.some(m=>m.source===g.target)));
assert.deepEqual({roles:specRoles,links:specLinks},groupedBefore,'Bundling/layout must not mutate policy definitions');
const positions=L.arrange(groupedNodes,specLinks);
assert.ok(positions.coordinator.x<positions.writer.x&&positions.writer.x<positions.integrator.x,'Return actions must not reverse delivery order');
assert.ok(grouped.geometry.width<2100&&grouped.geometry.height<900,'sample layout stays compact');
const bends=g=>g.routes.reduce((n,r)=>n+Math.max(0,r.points.length-2),0);
const individualGeometry=L.route(groupedNodes.map(n=>({...n,position:positions[n.id]})),specLinks);
const groupedGeometry=L.route(groupedNodes.map(n=>({...n,position:positions[n.id]})),L.bundles(groupedNodes,specLinks));
const repeatedLinks=specLinks.flatMap(l=>Array.from({length:8},(_,i)=>({...l,id:l.id+'-'+i,label:l.label+' '+i})));
const repeatedNodes=groupedNodes.map(n=>({...n,position:positions[n.id]}));
assert.ok(bends(L.route(repeatedNodes,L.bundles(groupedNodes,repeatedLinks)))<bends(L.route(repeatedNodes,repeatedLinks)),'Grouping repeated handoffs reduces bends');

const node=(id,x=0,y=0)=>({id,name:id,position:{x,y}});
const edge=(source,target)=>({id:source+target,source,target,label:source+' sends work to '+target});
const starNodes=['hub','a','b','c'].map(id=>node(id)),starLinks=['a','b','c'].flatMap(id=>[edge('hub',id),edge(id,'hub')]);
check(starNodes,starLinks,'Reciprocal star');
check([...starNodes,node('x'),node('y')],[...starLinks,edge('x','y')],'Reciprocal star and disconnected chain');
assert.equal(crossings(check([node('a'),node('b'),node('c'),node('d')],[edge('a','d'),edge('b','c')],'Crossed input ordering').geometry),0);
check([node('a'),node('b'),node('c')],[edge('a','b'),edge('b','c'),edge('c','a')],'Cycle');
check([node('a'),node('b')],[edge('a','b'),edge('b','a')],'Reciprocal links');
check(Array.from({length:8},(_,i)=>node('n'+i)),[],'Disconnected nodes');
check([],[],'Empty graph');
check(Array.from({length:50},(_,i)=>node('n'+i)),[],'Maximum disconnected fleet');
check(Array.from({length:9},(_,i)=>node('n'+i)),Array.from({length:8},(_,i)=>edge('n'+i,'n'+(i+1))),'Nine agent chain');
const blocked=[node('a',50,100),node('obstacle',420,100),node('b',800,100)];
const detour=L.route(blocked,[edge('a','b')]);
assert.ok(!detour.routes[0].unrouted);
for(const [a,b] of L.segments(detour.routes[0].points))for(const n of blocked)assert.ok(!L.segmentHits(a,b,L.rect(n)),'Manual obstacle avoidance');
assert.ok(detour.routes[0].points.length>2);
fs.writeFileSync('tests/layout-results.json',JSON.stringify({passed:true,main:main.geometry},null,2));
console.log('Manual obstacle avoidance passed');

for(const gap of [10,25,31,40,43,44]){
 const close=[node('a',80,100),node('b',80+245+gap,100)],g=L.route(close,[edge('a','b')]);
 assert.ok(!g.routes[0].unrouted,'Clear narrow gap '+gap+' must retain its connection');
 for(const [a,b] of L.segments(g.routes[0].points))for(const n of close)assert.ok(!L.segmentHits(a,b,L.rect(n)));
}
console.log('Tightly spaced manual cards retain clear routes');