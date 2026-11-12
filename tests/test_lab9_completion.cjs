const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8').replace(/\nstart\(\);\s*$/,'');
const flag = 'PANOPTIS{durable-lab9-fixture}';

function studentUI({completed=false,exploited=true,attempt=1,revision=3}={}) {
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
  const context=vm.createContext({document:{getElementById:get,querySelector:()=>null,
    createElement:()=>new Element(),createTextNode:text=>({textContent:text})},
    window:{confirm:text=>{confirmations.push(text);return true;}},setTimeout:()=>0,clearTimeout(){},
    completed,exploited,attempt,revision,flag});
  vm.runInContext(source,context);
  const run=code=>vm.runInContext(code,context);
  run(`state.current=9;state.catalog=Array.from({length:9},(_,i)=>({id:i+1,points:i===8?500:100}));
    state.labs['9']={id:9,state_version:[attempt,revision],completed,exploited,flag,
      guardrail_status:exploited?'disclosed':'normal',messages:[],defense_level:0,last_result:null};
    state.summary={score:completed?500:0,completed:completed?[9]:[]};
    drawLab=drawFlag;drawNavigation=updateControls=()=>{};drawFlag();`);
  get('flag-input').value=flag;
  const response=({completed=true,exploited=true,attempt=1,revision=3,level=0,
    award={newly_completed:true,points_awarded:500}}={})=>({
    lab:{id:9,state_version:[attempt,revision],completed,exploited,flag:completed||exploited?flag:null,
      guardrail_status:exploited?'disclosed':'normal',messages:[],defense_level:level,last_result:null},
    summary:{score:completed?500:0,completed:completed?[9]:[]},flag_submission:award});
  const respond=(value,ok=true)=>{context.fetch=async()=>({ok,json:async()=>value});};
  const accept=value=>{context.incoming=value;return run('acceptResponse(9,incoming)');};
  const pending=()=>{
    let release;
    context.fetch=()=>new Promise(resolve=>{release=(value,ok=true)=>resolve({ok,json:async()=>value});});
    return (...args)=>release(...args);
  };
  return {context,run,get,response,respond,accept,pending,confirmations};
}
const submit=ui=>ui.run('submitFlag({preventDefault(){}})');

test('first confirmed completion displays exactly +500 points and learning',async()=>{
  const ui=studentUI();ui.respond(ui.response());await submit(ui);
  assert.equal(ui.get('flag-feedback').textContent,'+500 points. Well played.');
  assert.equal(ui.run('state.summary.score'),500);
  assert.equal(ui.get('lab9-completion-explanation').hidden,false);
});

test('duplicate submissions and concurrent-loser responses show no new award',async()=>{
  for(const completed of [false,true]){
    const ui=studentUI({completed});
    ui.respond(ui.response({award:{newly_completed:false,points_awarded:0}}));await submit(ui);
    assert.equal(ui.get('flag-feedback').textContent,'Lab already completed');
    assert.equal(ui.run('state.summary.score'),500);
  }
});

test('replaying the first winning response cannot show another +500 award',async()=>{
  const ui=studentUI();ui.respond(ui.response());await submit(ui);await submit(ui);
  assert.equal(ui.get('flag-feedback').textContent,'Lab already completed');
});

test('eight authoritative submission outcomes display exactly one +500 award',async()=>{
  const browsers=Array.from({length:8},()=>studentUI());
  await Promise.all(browsers.map(async(ui,index)=>{
    ui.respond(ui.response({award:{newly_completed:index===0,points_awarded:index===0?500:0}}));
    await submit(ui);
  }));
  assert.equal(browsers.filter(ui=>ui.get('flag-feedback').textContent.includes('+500')).length,1);
  assert(browsers.every(ui=>ui.run('state.summary.score')===500));
});

test('one browser suppresses repeated in-flight flag submissions',async()=>{
  const ui=studentUI();let calls=0,release;
  ui.context.fetch=()=>{calls++;return new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>ui.response()});});};
  const pending=submit(ui);await submit(ui);assert.equal(calls,1);release();await pending;
});

test('unconfirmed completion cannot claim points or reveal learning',async()=>{
  const ui=studentUI();ui.respond(ui.response({completed:false}));await submit(ui);
  assert.match(ui.get('flag-feedback').textContent,/did not confirm Lab 9 completion/);
  assert.equal(ui.get('lab9-completion-explanation').hidden,true);
  assert.equal(ui.run('state.summary.score'),0);
});

test('missing or inconsistent award metadata never claims +500',async()=>{
  for(const award of [null,{}, {newly_completed:true,points_awarded:0},
    {newly_completed:'true',points_awarded:500},{newly_completed:true,points_awarded:'500'}]){
    const ui=studentUI();ui.respond(ui.response({award}));await submit(ui);
    assert.doesNotMatch(ui.get('flag-feedback').textContent,/\+500/);
  }
});

test('invalid flags never alter existing earned completion or score',async()=>{
  for(const completed of [false,true]){
    const ui=studentUI({completed});ui.respond({error:'That flag does not match this lab and attempt.'},false);
    await submit(ui);
    assert.equal(ui.run("state.labs['9'].completed"),completed);
    assert.equal(ui.run('state.summary.score'),completed?500:0);
    assert.match(ui.get('flag-feedback').textContent,/does not match/);
  }
});

test('earned original flag and learning remain visible without current verification',()=>{
  const ui=studentUI({completed:true,exploited:false,attempt:4,revision:0});
  assert.equal(ui.get('captured-panel').hidden,false);
  assert.equal(ui.get('captured-flag').textContent,flag);
  assert.equal(ui.get('flag-form').hidden,true);
  assert.equal(ui.get('lab9-completion-explanation').hidden,false);
  assert.equal(ui.run("lab9Progress(state.labs['9']).status"),'normal');
});

test('new chat keeps the award, original flag, and learning while clearing progress',async()=>{
  const ui=studentUI({completed:true});
  ui.respond(ui.response({attempt:2,revision:0,exploited:false,award:{newly_completed:false,points_awarded:0}}));
  await ui.run('restartLab()');
  assert.equal(ui.get('captured-flag').textContent,flag);
  assert.equal(ui.get('captured-panel').hidden,false);
  assert.equal(ui.get('lab9-completion-explanation').hidden,false);
  assert.equal(ui.run("lab9Progress(state.labs['9']).status"),'normal');
  assert.equal(ui.run('state.summary.score'),500);
});

test('difficulty changes explain durable completion and preserve the original flag',async()=>{
  const ui=studentUI({completed:true});ui.get('defense-select').value='1';
  ui.respond(ui.response({attempt:2,revision:0,level:1,exploited:false}));await ui.run('changeDefense()');
  assert.match(ui.confirmations[0],/Earned completion and points stay saved/);
  assert.equal(ui.get('captured-flag').textContent,flag);
  assert.equal(ui.run("lab9Progress(state.labs['9']).status"),'normal');
});

test('equal-version stale responses cannot remove earned completion',()=>{
  const ui=studentUI({completed:true});
  assert.equal(ui.accept(ui.response({completed:false})),false);
  assert.equal(ui.run('state.summary.score'),500);
  assert.equal(ui.get('lab9-completion-explanation').hidden,false);
});

test('a delayed winning response cannot restore an older attempt or repeat its award',async()=>{
  const ui=studentUI();const release=ui.pending();const pending=submit(ui);
  ui.accept(ui.response({attempt:2,revision:0,exploited:false}));
  ui.run("state.flagFeedback.set(9,{message:'Current completion'})");
  release(ui.response());await pending;
  assert.equal(ui.run("state.labs['9'].state_version[0]"),2);
  assert.equal(ui.run('state.flagFeedback.get(9).message'),'Current completion');
});

test('a delayed invalid-flag error cannot overwrite newer completion feedback',async()=>{
  const ui=studentUI();const release=ui.pending();const pending=submit(ui);
  ui.accept(ui.response());ui.run("state.flagFeedback.set(9,{message:'Current completion'})");
  release({error:'Old rejection'},false);await pending;
  assert.equal(ui.run('state.flagFeedback.get(9).message'),'Current completion');
  assert.equal(ui.run('state.summary.score'),500);
});

test('a stale initial refresh cannot hide an earned completion',()=>{
  const ui=studentUI({completed:true});
  ui.context.old={catalog:ui.run('state.catalog'),labs:{'9':ui.response({completed:false}).lab},summary:{score:0,completed:[]}};
  ui.run('acceptInitialState(old);drawFlag()');
  assert.equal(ui.get('captured-panel').hidden,false);
  assert.equal(ui.get('lab9-completion-explanation').hidden,false);
  assert.equal(ui.run('state.summary.score'),500);
});
