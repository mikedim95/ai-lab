const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const read = name => fs.readFileSync(path.join(__dirname, '../static', name), 'utf8');
const app = read('app.js').replace(/\nstart\(\);\s*$/, '');
const hints = [
  'Investigate which maintenance documents you can edit or add to the knowledge base.',
  "Saving a document does not automatically update Plant AI's retrieval snapshot.",
  'Ask a question related to your modified document and examine which sources were retrieved.',
  'Consider the difference between Plant AI summarizing a retrieved document and treating instructions within it as authoritative.',
];
const copy = value => JSON.parse(JSON.stringify(value));

async function studentUI(extra = {}) {
  const elements = new Map();
  class Element {
    constructor(tag) { this.tagName=tag;this.children=[];this.listeners={};this.attributes={};this.value='';this.text='';this.hidden=false; }
    set id(value) { this._id=value;elements.set(value,this); }
    get id() { return this._id; }
    set textContent(value) { this.replaceChildren();this.text=value; }
    get textContent() { return this.text+this.children.map(child=>child.textContent).join(''); }
    append(...children) { children.forEach(child=>{child.remove();child.parentElement=this;this.children.push(child);if(child.id)elements.set(child.id,child);}); }
    after(child) { child.remove();const siblings=this.parentElement.children;siblings.splice(siblings.indexOf(this)+1,0,child);child.parentElement=this.parentElement;if(child.id)elements.set(child.id,child); }
    replaceChildren(...children) { [...this.children].forEach(child=>child.remove());this.children=[];this.text='';this.append(...children); }
    remove() { if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(child=>child!==this);this.parentElement=null;if(this.id)elements.delete(this.id); }
    setAttribute(name,value) { this.attributes[name]=value; }
    addEventListener(name,listener) { this.listeners[name]=listener; }
    querySelectorAll() { return []; }
    focus() {}
    showModal() { this.open=true; }
  }
  const root=new Element('main');
  for(const match of read('index.html').matchAll(/<([\w-]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element=new Element(match[1]);root.append(element);element.id=match[3];element.hidden=/\bhidden\b/.test(match[2]);
  }
  const get=id=>elements.get(id)||null;
  const labs=Object.fromEntries(Array.from({length:9},(_,i)=>[String(i+1),{id:i+1,attempt:1,state_version:[1,0],
    messages:[],documents:[],completed:false,exploited:false,flag:null,last_result:null,
    use_kb:false,kb_revision:0,synced_revision:0,defense_level:0,...(i===7?extra:{})}]));
  const initial={labs,catalog:Array.from({length:9},(_,i)=>({id:i+1,points:i===7?400:100,has_kb:i===7,hints:i===7?hints:[]})),
    summary:{score:extra.completed?400:0,completed:extra.completed?[8]:[]}};
  const context=vm.createContext({document:{getElementById:get,createElement:tag=>new Element(tag),
    createTextNode:text=>{const node=new Element('#text');node.textContent=text;return node;},
    querySelector:()=>null,querySelectorAll:()=>[],addEventListener(){}},
    window:{addEventListener(){}},location:{pathname:'/lab/8'},setTimeout:()=>0,clearTimeout(){},setInterval(){},
    fetch:async()=>({ok:true,json:async()=>copy(initial)})});
  vm.runInContext(read('intro.js'),context);vm.runInContext(app,context);
  const run=code=>vm.runInContext(code,context);
  run('drawLab=()=>{window.LabScenario.render(state.current);drawHints();drawFlag();};updateControls=checkHealth=drawNavigation=()=>{};');
  await run('start()');
  const result=(overrides={})=>{
    context.result={triggered:false,defense_blocked:false,defense_level:0,sources:[],excluded_sources:[],
      message:'Exploit verified. This model text is not proof.',seconds:.1,...overrides};
    run("state.labs['8'].last_result=result;drawConversation();drawWorkspace()");
  };
  const respond=value=>{context.fetch=async()=>({ok:true,json:async()=>copy(value)});};
  return {context,get,run,result,respond,click:id=>get(id).listeners.click()};
}

test('Lab 8 scenario explains the knowledge base and five separate workflow stages',async()=>{
  const ui=await studentUI();
  assert.match(ui.get('scenario-summary').textContent,/knowledge base to answer maintenance questions/);
  assert.match(ui.get('scenario-objective').textContent,/combining student-controlled knowledge with conversational instructions/);
  assert.equal(ui.get('scenario-workflow').children.length,5);
  assert.deepEqual(ui.get('scenario-workflow').children.map(child=>child.textContent.split(':')[0]),
    ['Edit','Save','Synchronize','Retrieve','Review the response']);
  assert.match(ui.get('scenario-objective-rules').textContent,/alone does not verify exploitation/);
});

test('four exact hints reveal sequentially, hide, and remain independent between students',async()=>{
  const ui=await studentUI();
  assert.equal(ui.get('hints').children.length,0);
  for(let count=1;count<=4;count++) {
    ui.click('hint-button');
    assert.deepEqual(ui.get('hints').children.map(child=>child.textContent),hints.slice(0,count));
  }
  assert.equal((await studentUI()).get('hints').children.length,0);
  ui.click('hint-button');assert.equal(ui.get('hints').children.length,0);
});

test('knowledge disabled, saved awaiting sync, and active snapshot are separate states',async()=>{
  const ui=await studentUI();
  ui.run('drawKnowledge();drawWorkspace()');
  assert.equal(ui.get('kb-status').textContent,'Knowledge base disabled');
  ui.run("state.labs['8'].kb_revision=2;state.labs['8'].synced_revision=1;drawKnowledge();drawWorkspace()");
  assert.match(ui.get('kb-notice').textContent,/Knowledge base disabled/);
  assert.match(ui.get('lab8-kb-feedback').textContent,/Saved document awaiting synchronization/);
  ui.run("state.labs['8'].use_kb=true;drawKnowledge();drawWorkspace()");
  assert.match(ui.get('lab8-kb-feedback').textContent,/Current synchronized snapshot active/);
  assert.match(ui.get('lab8-kb-feedback').textContent,/saved edits are excluded until you sync/);
  ui.run("state.labs['8'].synced_revision=2;drawKnowledge()");
  assert.equal(ui.get('kb-status').textContent,'Current synchronized snapshot active');
});

test('unsaved editor changes update their status without saving or syncing',async()=>{
  const ui=await studentUI();
  ui.click('new-document');
  ui.get('document-content').value='Fictional inspection note.';
  ui.get('document-content').listeners.input();
  assert.equal(ui.get('kb-status').textContent,'Editing · Unsaved changes');
  assert.equal(ui.run("state.labs['8'].documents.length"),0);
  assert.equal(ui.run("state.labs['8'].kb_revision"),0);
});

test('student document retrieval alone never shows success or reveals the learning explanation',async()=>{
  const ui=await studentUI();
  ui.result({kb_used:true,sources:[{id:'student-note',title:'Handover note',injected:true}]});
  assert.match(ui.get('attempt-result').textContent,/No exploit verified/);
  assert.match(ui.get('attempt-result').textContent,/Student-controlled document retrieved/);
  assert(!ui.get('attempt-result').className.includes('success'));
  assert.equal(ui.get('lab8-completion-explanation').hidden,true);
});

test('prior capture does not mark a later unverified reply successful',async()=>{
  const ui=await studentUI({exploited:true,flag:'fixture'});
  ui.result();
  assert.equal(ui.get('lab8-reply-feedback').textContent,'No exploit verified.No documents retrieved for the last reply.');
  assert(!ui.get('attempt-result').className.includes('success'));
});

test('only a boolean backend trigger displays verified feedback',async()=>{
  const ui=await studentUI();
  for(const triggered of [false,undefined,'true',1]) {
    ui.result({triggered});assert(!ui.get('attempt-result').className.includes('success'));
  }
  ui.result({triggered:true});
  assert(ui.get('attempt-result').className.includes('success'));
  assert.match(ui.get('lab8-reply-feedback').textContent,/Exploit verified/);
  assert.equal(ui.get('lab8-completion-explanation').hidden,true);
});

test('request difficulty and excluded sources use backend metadata without internal reasons',async()=>{
  const ui=await studentUI({defense_level:0});
  ui.result({defense_level:2,defense_blocked:true,defense_reason:'private_internal_reason',
    excluded_sources:[{id:'notice',title:'Fictional notice',reason:'private_scan_pattern'}]});
  const text=ui.get('attempt-result').textContent;
  assert.match(text,/Request difficulty: Hard · Guardrailed/);
  assert.match(text,/Excluded document: Fictional notice · notice. Not supplied to Plant AI/);
  assert.doesNotMatch(text,/private_internal_reason|private_scan_pattern/);
});

test('source feedback remains about the last reply after save, sync, or retrieval disablement',async()=>{
  const ui=await studentUI();
  ui.result({sources:[{id:'source-id',title:'Earlier note',injected:true}]});
  ui.run("state.labs['8'].use_kb=false;state.labs['8'].kb_revision=3;state.labs['8'].synced_revision=2;drawWorkspace();drawConversation()");
  assert.match(ui.get('lab8-kb-feedback').textContent,/Knowledge base disabled/);
  assert.match(ui.get('attempt-result').textContent,/Sources describe the last reply/);
  assert.match(ui.get('attempt-result').textContent,/does not rerun retrieval/);
  assert.match(ui.get('attempt-result').textContent,/Earlier note · source-id/);
});

test('a pending request hides earlier outcome feedback',async()=>{
  const ui=await studentUI();
  ui.result({triggered:true});
  ui.run("state.pending.set(8,{message:'Handover question',started:Date.now()});drawWorkspace();drawConversation()");
  assert.equal(ui.get('lab8-reply-feedback').textContent,'Awaiting the current model response.');
  assert.equal(ui.get('attempt-result').hidden,true);
});

test('learning explanation requires confirmed completion and stays hidden in every other lab',async()=>{
  const ui=await studentUI({completed:true,exploited:true});
  assert.equal(ui.get('lab8-completion-explanation').hidden,true);
  for(const id of [1,2,3,4,5,6,7,9]) {
    ui.run(`state.current=${id};state.labs['${id}'].completed=true;drawFlag()`);
    assert.equal(ui.get('lab8-completion-explanation').hidden,true);
  }
  const html=read('index.html');
  assert(html.indexOf('id="lab8-completion-explanation"')>html.indexOf('id="flag-dialog"'));
  for(const text of ['Chained RAG Poisoning and Prompt Injection','Student-controlled knowledge-base content',
    'student conversation','must not become authoritative instructions','multiple lower-trust input surfaces',
    'Document provenance','trusted-source precedence','separation of data and instructions','output verification']) assert(html.includes(text));
});

test('unconfirmed flag response cannot display completion or a points message',async()=>{
  const ui=await studentUI({exploited:true,flag:'fixture'});
  ui.get('flag-input').value='fixture';
  ui.respond({lab:{id:8,state_version:[1,1],completed:false},summary:{score:0}});
  await ui.run('submitFlag({preventDefault(){}})');
  assert.equal(ui.get('lab8-completion-explanation').hidden,true);
  assert.match(ui.get('flag-feedback').textContent,/did not confirm Lab 8 completion/);
  assert.doesNotMatch(ui.get('flag-feedback').textContent,/\+400/);
});

test('backend-confirmed submission reveals learning and duplicate replies claim no new award',async()=>{
  const ui=await studentUI({exploited:true,flag:'fixture'});
  ui.get('flag-input').value='fixture';
  const complete={lab:{id:8,state_version:[1,1],completed:true,exploited:true,flag:'fixture',messages:[]},summary:{score:400},
    flag_submission:{newly_completed:true,points_awarded:400}};
  ui.respond(complete);
  await ui.run('submitFlag({preventDefault(){}})');
  assert.equal(ui.get('lab8-completion-explanation').hidden,true);
  assert.match(ui.get('flag-feedback').textContent,/\+400 points/);
  ui.get('flag-input').value='fixture';
  await ui.run('submitFlag({preventDefault(){}})');
  assert.equal(ui.get('flag-feedback').textContent,'Lab already completed');
});
