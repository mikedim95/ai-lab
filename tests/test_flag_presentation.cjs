const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {test}=require('node:test');
const source=fs.readFileSync(path.join(__dirname,'../static/flag-ui.js'),'utf8');
const flag='PANOPTIS{0123456789ABCDEF0123456789ABCDEF}';

function presentation({clipboard=true,rejectClipboard=false,fallback=true}={}){
  const elements=new Map(),copies=[];
  let selected='';
  class Element{
    constructor(tag='div'){this.tagName=tag;this.textContent='';this.hidden=false;this.open=false;
      this.disabled=false;this.children=[];this.style={};this.listeners={};}
    append(child){this.children.push(child);child.parent=this;}
    remove(){if(this.parent)this.parent.children=this.parent.children.filter(item=>item!==this);}
    setAttribute(){}
    showModal(){this.open=true;}
    close(){this.open=false;}
    focus(){}
    select(){selected=this.value;}
    addEventListener(name,handler){this.listeners[name]=handler;}
  }
  for(const id of ['captured-flag-banner','visible-captured-flag','flag-capture-dialog','flag-capture-value','copy-captured-flag'])elements.set(id,new Element());
  elements.get('captured-flag-banner').hidden=true;
  const context={document:{getElementById:id=>elements.get(id),createElement:tag=>new Element(tag),body:new Element('body'),
    execCommand:command=>{assert.equal(command,'copy');if(fallback)copies.push(selected);return fallback;}},
    navigator:clipboard?{clipboard:{writeText:async value=>{if(rejectClipboard)throw new Error('Denied');copies.push(value);}}}:{},
    fetch:()=>{throw new Error('Copying must never make an API request');}};
  context.window=context;vm.createContext(context);vm.runInContext(source,context);
  return {ui:context.FlagPresentation,get:id=>elements.get(id),copies,context};
}

test('capture popup contains only a selectable flag and copy button',()=>{
  const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
  const popup=html.match(/<dialog id="flag-capture-dialog"[\s\S]*?<\/dialog>/)[0];
  assert.equal([...popup.matchAll(/<button\b/g)].length,1);
  assert.match(popup,/Copy to clipboard/);
  assert.doesNotMatch(popup,/<form|<input|award-points|completion-explanation/);
});

test('a captured flag is rendered at the top and revealed in its own popup',()=>{
  const view=presentation();view.ui.reveal(flag);
  assert.equal(view.get('captured-flag-banner').hidden,false);
  assert.equal(view.get('visible-captured-flag').textContent,flag);
  assert.equal(view.get('flag-capture-value').textContent,flag);
  assert.equal(view.get('flag-capture-dialog').open,true);
});

test('successful clipboard copy closes the popup while retaining the top flag',async()=>{
  const view=presentation();view.ui.reveal(flag);
  assert.equal(await view.ui.copyAndClose(),true);
  assert.deepEqual(view.copies,[flag]);
  assert.equal(view.get('flag-capture-dialog').open,false);
  assert.equal(view.get('captured-flag-banner').hidden,false);
  assert.equal(view.get('visible-captured-flag').textContent,flag);
});

test('plain HTTP uses a selection-copy fallback without submitting anything',async()=>{
  const view=presentation({clipboard:false});view.ui.reveal(flag);
  assert.equal(await view.ui.copyAndClose(),true);
  assert.deepEqual(view.copies,[flag]);
  assert.equal(view.get('flag-capture-dialog').children.length,0);
  assert.equal(view.get('flag-capture-dialog').open,false);
});

test('clipboard permission failure falls back to selection copying',async()=>{
  const view=presentation({rejectClipboard:true});view.ui.reveal(flag);
  assert.equal(await view.ui.copyAndClose(),true);
  assert.deepEqual(view.copies,[flag]);
});

test('a failed copy keeps the flag available and does not falsely close the popup',async()=>{
  const view=presentation({clipboard:false,fallback:false});view.ui.reveal(flag);
  assert.equal(await view.ui.copyAndClose(),false);
  assert.equal(view.get('flag-capture-dialog').open,true);
  assert.match(view.get('copy-captured-flag').textContent,/Copy failed/);
  assert.equal(view.get('visible-captured-flag').textContent,flag);
});

test('reopening a saved flag only updates the banner without auto-opening a popup',()=>{
  const view=presentation();view.ui.render(flag);
  assert.equal(view.get('flag-capture-dialog').open,false);
  assert.equal(view.get('captured-flag-banner').hidden,false);
});

test('switching labs replaces or clears the displayed flag',()=>{
  const view=presentation();view.ui.reveal(flag);
  const other='PANOPTIS{FEDCBA9876543210FEDCBA9876543210}';
  view.ui.render(other);
  assert.equal(view.get('visible-captured-flag').textContent,other);
  assert.equal(view.get('flag-capture-value').textContent,other);
  view.ui.render(null);
  assert.equal(view.get('captured-flag-banner').hidden,true);
  assert.equal(view.get('flag-capture-dialog').open,false);
  assert.equal(view.get('flag-capture-value').textContent,'');
});
