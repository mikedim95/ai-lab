const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8').replace(/\nstart\(\);\s*$/,'');
const flag='PANOPTIS{fixture-earned-flag}';

function ui({completed=false,exploited=true,attempt=1,revision=1}={}) {
  const elements=new Map(),confirmations=[];
  class Element {
    constructor(){this.value='';this.textContent='';this.children=[];this.hidden=true;}
    append(...children){this.children.push(...children);}
    replaceChildren(...children){this.children=children;}
    setAttribute(){}
    focus(){}
    showModal(){}
  }
  const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const context=vm.createContext({document:{getElementById:get,querySelector:()=>null,createElement:()=>new Element(),
    createTextNode:text=>({textContent:text})},window:{confirm:text=>{confirmations.push(text);return true;}},
    setTimeout:()=>0,clearTimeout(){},completed,exploited,attempt,revision,flag});
  vm.runInContext(source,context);
  const run=code=>vm.runInContext(code,context);
  run(`state.current=8;state.catalog=Array.from({length:9},(_,i)=>({id:i+1,points:i===7?400:100}));
    state.labs['8']={id:8,attempt,state_version:[attempt,revision],completed,exploited,flag,
      messages:[],defense_level:0,last_result:null,documents:[]};
    state.summary={score:completed?400:0,completed:completed?[8]:[]};
    drawLab=drawFlag;drawNavigation=updateControls=()=>{};drawFlag();`);
  get('flag-input').value=flag;
  const response=({complete=true,verified=true,attempt=1,revision=2,level=0,
    award={newly_completed:true,points_awarded:400}}={})=>({
    lab:{id:8,attempt,state_version:[attempt,revision],completed:complete,exploited:verified,flag,
      messages:[],defense_level:level,last_result:null,documents:[]},
    summary:{score:complete?400:0,completed:complete?[8]:[]},flag_submission:award});
  const respond=value=>{context.fetch=async()=>({ok:true,json:async()=>value});};
  const accept=value=>{context.incoming=value;return run('acceptResponse(8,incoming)');};
  const pending=()=>{let release;context.fetch=()=>new Promise(resolve=>{release=(value,ok=true)=>resolve({ok,json:async()=>value});});return (...args)=>release(...args);};
  return {context,get,run,response,respond,accept,pending,confirmations};
}
const submit=view=>view.run('submitFlag({preventDefault(){}})');

test('first authoritative backend award displays exactly +400 points',async()=>{
  const view=ui();view.respond(view.response());await submit(view);
  assert.equal(view.get('flag-feedback').textContent,'+400 points. Well played.');
  assert.equal(view.run('state.summary.score'),400);
  assert.equal(view.get('lab8-completion-explanation').hidden,true);
});

test('duplicate and concurrent-loser responses display Lab already completed with no award',async()=>{
  for(const completed of [false,true]) {
    const view=ui({completed});
    view.respond(view.response({award:{newly_completed:false,points_awarded:0}}));await submit(view);
    assert.equal(view.get('flag-feedback').textContent,'Lab already completed');
    assert.equal(view.run('state.summary.score'),400);
  }
});

test('HTTP success without valid award metadata never claims +400 points',async()=>{
  for(const award of [null,{}, {newly_completed:true,points_awarded:0},
    {newly_completed:'true',points_awarded:400},{newly_completed:true,points_awarded:'400'},
    {newly_completed:true,points_awarded:800}]) {
    const view=ui();view.respond(view.response({award}));await submit(view);
    assert.doesNotMatch(view.get('flag-feedback').textContent,/\+400/);
    assert.equal(view.get('flag-feedback').textContent,'Lab complete. Award details unavailable.');
  }
});

test('replaying a winning response cannot display the award twice',async()=>{
  const view=ui();view.respond(view.response());await submit(view);await submit(view);
  assert.equal(view.get('flag-feedback').textContent,'Lab already completed');
});

test('unconfirmed completion cannot reveal learning or claim an award',async()=>{
  const view=ui();view.respond(view.response({complete:false}));await submit(view);
  assert.equal(view.get('lab8-completion-explanation').hidden,true);
  assert.match(view.get('flag-feedback').textContent,/did not confirm Lab 8 completion/);
  assert.equal(view.run('state.summary.score'),0);
});

test('invalid flags display backend error and retain earned completion',async()=>{
  for(const completed of [false,true]) {
    const view=ui({completed});
    view.context.fetch=async()=>({ok:false,json:async()=>({error:'That flag does not match this lab and attempt.'})});
    await submit(view);
    assert.match(view.get('flag-feedback').textContent,/does not match/);
    assert.equal(view.run("state.labs['8'].completed"),completed);
    assert.equal(view.run('state.summary.score'),completed?400:0);
  }
});

test('restart clears earned flag, completion and points',async()=>{
  const view=ui({completed:true});
  const reset=view.response({complete:false,attempt:2,revision:0,verified:false});
  reset.lab.flag=null;view.respond(reset);
  await view.run('restartLab()');
  assert.equal(view.run("state.labs['8'].exploited"),false);
  assert.equal(view.get('captured-panel').hidden,true);
  assert.equal(view.get('captured-flag').textContent,'');
  assert.equal(view.get('lab8-completion-explanation').hidden,true);
  assert.equal(view.run('state.summary.score'),0);
});

test('difficulty confirmation describes durable completion and preserves original flag',async()=>{
  const view=ui({completed:true});view.get('defense-select').value='2';
  view.respond(view.response({attempt:2,revision:3,verified:false,level:2}));
  await view.run('changeDefense()');
  assert.match(view.confirmations[0],/Earned completion and points stay saved/);
  assert.match(view.confirmations[0],/knowledge base stays saved/);
  assert.equal(view.run("state.labs['8'].defense_level"),2);
  assert.equal(view.get('captured-flag').textContent,'');
  assert.equal(view.get('lab8-completion-explanation').hidden,true);
});

test('refresh restores earned flag even when current verification is false',()=>{
  const view=ui({completed:true,exploited:false,attempt:4});
  assert.equal(view.get('captured-panel').hidden,true);
  assert.equal(view.get('captured-flag').textContent,'');
  assert.equal(view.get('flag-form').hidden,false);
  assert.equal(view.get('lab8-completion-explanation').hidden,true);
});

test('same-attempt and older uncompleted state cannot erase an earned award',()=>{
  const view=ui({completed:true,attempt:2,revision:3});
  for(const response of [view.response({complete:false,attempt:1}),view.response({complete:false,attempt:2,revision:99})]) {
    assert.equal(view.accept(response),false);
    assert.equal(view.run('state.summary.score'),400);
    assert.equal(view.get('lab8-completion-explanation').hidden,true);
  }
});

test('late winning response cannot restore a previous attempt after completed reset',async()=>{
  const view=ui();const release=view.pending();const active=submit(view);
  view.accept(view.response({attempt:2,revision:3,verified:false}));
  view.run("state.flagFeedback.set(8,{message:'Current completion'})");
  release(view.response());await active;
  assert.equal(view.run("state.labs['8'].attempt"),2);
  assert.equal(view.run('state.flagFeedback.get(8).message'),'Current completion');
});

test('late invalid-flag error cannot replace newer successful completion feedback',async()=>{
  const view=ui();const release=view.pending();const active=submit(view);
  view.accept(view.response());view.run("state.flagFeedback.set(8,{message:'Current completion'})");
  release({error:'Old rejection'},false);await active;
  assert.equal(view.run('state.flagFeedback.get(8).message'),'Current completion');
});

test('one browser suppresses duplicate in-flight flag submissions',async()=>{
  const view=ui();let calls=0,release;
  view.context.fetch=()=>{calls++;return new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>view.response()});});};
  const active=submit(view);await submit(view);assert.equal(calls,1);release();await active;
  assert.equal(view.run('state.summary.score'),400);
});

test('earned learning remains hidden in other labs',()=>{
  const view=ui({completed:true,exploited:false});
  for(const id of [1,2,3,4,5,6,7,9]) {
    view.run(`state.current=${id};state.labs['${id}']={id:${id},messages:[],completed:true,flag:'other'};drawFlag()`);
    assert.equal(view.get('lab8-completion-explanation').hidden,true);
  }
});
