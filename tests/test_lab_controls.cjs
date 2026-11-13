const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {test}=require('node:test');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8').replace(/\nstart\(\);\s*$/,'');
const points=[100,100,150,100,250,200,300,400,500];
const flag='PANOPTIS{isolated-fixture}';

function studentUI(lab,{completed=false}={}){
  const elements=new Map();
  let focused;
  class Element{
    constructor(){this.value='';this.textContent='';this.hidden=true;this.open=false;this.children=[];this.attributes={};}
    append(...children){this.children.push(...children);}
    replaceChildren(...children){this.children=children;this.textContent=children.map(c=>c.textContent).join('');}
    setAttribute(name,value){this.attributes[name]=value;}
    showModal(){this.open=true;}
    close(){this.open=false;}
    focus(){focused=this;}
    querySelectorAll(){return [];}
  }
  const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const context=vm.createContext({document:{getElementById:get,createElement:()=>new Element(),
    createTextNode:text=>({textContent:text}),querySelector:()=>null},
    window:{},setTimeout:()=>0,clearTimeout(){},FlagPresentation:{render(){},reveal(){}}});
  vm.runInContext(source,context);
  const run=code=>vm.runInContext(code,context);
  context.initial={id:lab,attempt:1,state_version:[1,1],boiler_version:[1,1],messages:[],
    completed,exploited:true,flag,defense_level:0,last_result:null};
  context.catalog=points.map((p,i)=>({id:i+1,title:'Fixture lab '+(i+1),points:p,hints:['One','Two','Three','Four']}));
  run(`state.current=initial.id;state.catalog=catalog;state.labs[String(initial.id)]=initial;
    state.summary={score:initial.completed?catalog[initial.id-1].points:0,completed:initial.completed?[initial.id]:[]};
    if(initial.id===2)attachLab2Version({lab:initial},initial.completed?'1.1.1':'1.1.0');
    savedControls=updateControls;drawLab=drawFlag;drawNavigation=updateControls=drawLab2Progress=()=>{};drawFlag();`);
  const response=({attempt=1,complete=true,exploited=true}={})=>({
    lab:{id:lab,attempt,state_version:[attempt,2],boiler_version:[attempt,2],messages:[],
      completed:complete,exploited,flag:complete||exploited?flag:null,defense_level:0,last_result:null},
    summary:{score:complete?points[lab-1]:0,completed:complete?[lab]:[]},
    flag_submission:{newly_completed:complete,points_awarded:complete?points[lab-1]:0}});
  const respond=(body,ok=true)=>{context.fetch=async(url,options)=>{
    context.lastRequest={url,body:JSON.parse(options.body)};
    return {ok,headers:{get:()=>body.lab?`${body.lab.attempt}.2.${Number(body.lab.completed)}`:null},json:async()=>body};
  };};
  return {get,run,context,response,respond,focused:()=>focused};
}

test('every lab opens flag submission with a blank input and no repeated flag',()=>{
  for(let lab=1;lab<=9;lab++){
    const ui=studentUI(lab,{completed:true});
    ui.run("state.flagDrafts.set(state.current,'old draft');state.flagFeedback.set(state.current,{message:'old error'});openFlagSubmission()");
    assert.equal(ui.get('flag-dialog').open,true);
    assert.equal(ui.get('flag-input').value,'');
    assert.equal(ui.get('captured-panel').hidden,true);
    assert.equal(ui.get('captured-flag').textContent,'');
    assert.equal(ui.get('flag-form').hidden,false);
    assert.equal(ui.get('flag-feedback').hidden,true);
    assert.equal(ui.focused(),ui.get('flag-input'));
  }
});

test('manual flag entry stays editable without a capture and locks only while submitting',()=>{
  for(let lab=1;lab<=9;lab++){
    const ui=studentUI(lab);
    ui.run('currentLab().exploited=false;currentLab().flag=null;savedControls()');
    assert.equal(ui.get('flag-input').disabled,false);
    assert.equal(ui.get('submit-flag').disabled,false);
    ui.run('state.flagBusy.add(state.current);savedControls()');
    assert.equal(ui.get('flag-input').disabled,true);
    assert.equal(ui.get('submit-flag').disabled,true);
  }
});

test('a successful flag submission closes the modal and updates each lab score',async()=>{
  for(let lab=1;lab<=9;lab++){
    const ui=studentUI(lab);ui.run('openFlagSubmission()');ui.get('flag-input').value=flag;
    ui.respond(ui.response());await ui.run('submitFlag({preventDefault(){}})');
    assert.equal(ui.get('flag-dialog').open,false);
    assert.equal(ui.get('flag-input').value,'');
    assert.equal(ui.run('state.summary.score'),points[lab-1]);
    assert.equal(ui.run('currentLab().completed'),true);
    assert.equal(ui.focused(),ui.get('flag-button'));
    assert.equal(ui.context.lastRequest.url,`/api/labs/${lab}/flag`);
    assert.deepEqual(ui.context.lastRequest.body,{flag});
  }
});

test('invalid flags leave the modal open with input and an error',async()=>{
  for(let lab=1;lab<=9;lab++){
    const ui=studentUI(lab);ui.run('openFlagSubmission()');ui.get('flag-input').value='PANOPTIS{wrong}';
    ui.respond({error:'That flag does not match this lab and attempt.'},false);
    await ui.run('submitFlag({preventDefault(){}})');
    assert.equal(ui.get('flag-dialog').open,true);
    assert.equal(ui.get('flag-input').value,'PANOPTIS{wrong}');
    assert.match(ui.get('flag-feedback').textContent,/does not match/);
    assert.equal(ui.get('flag-feedback').hidden,false);
    assert.equal(ui.run('state.summary.score'),0);
  }
});

test('restart accepts revoked awards and late old responses cannot restore them in any lab',async()=>{
  for(let lab=1;lab<=9;lab++){
    const ui=studentUI(lab,{completed:true}),old=ui.response();
    ui.respond(ui.response({attempt:2,complete:false,exploited:false}));
    await ui.run('restartLab()');
    assert.equal(ui.run('currentLab().completed'),false);
    assert.equal(ui.run('currentLab().flag'),null);
    assert.equal(ui.run('state.summary.score'),0);
    ui.context.old=old;ui.context.lab=lab;
    assert.equal(ui.run('acceptResponse(lab,old)'),false);
    assert.equal(ui.run('state.summary.score'),0);
    assert.equal(ui.run('currentLab().flag'),null);
  }
});

test('the solution placeholder unlocks only with all hints and hides when hints close',()=>{
  for(let lab=1;lab<=9;lab++){
    const ui=studentUI(lab);
    ui.run('drawHints();showSolution()');
    assert.equal(ui.get('solution-button').hidden,true);
    assert.equal(ui.get('solution-dialog').open,false);
    for(let count=1;count<=4;count++){
      ui.context.count=count;ui.run('state.hintCounts.set(state.current,count);drawHints()');
      assert.equal(ui.get('solution-button').hidden,count<4);
    }
    assert.match(ui.get('hint-button').textContent,/Close all hints/);
    ui.run('showSolution()');assert.equal(ui.get('solution-dialog').open,true);
    assert.match(ui.get('solution-heading').textContent,new RegExp('Lab '+lab+':'));
    ui.run('state.hintCounts.set(state.current,0);drawHints()');
    assert.equal(ui.get('solution-button').hidden,true);
    ui.run('state.current=state.current===9?1:state.current+1;drawHints()');
    assert.equal(ui.get('solution-button').hidden,true);
  }
});
