#!/usr/bin/env python3
"""Explicitly erase an idle llama-server slot's transient KV cache, not its model.

This helper changes no stored configuration, weights or volumes. It is used
between measured runs to compare identical uncached prompts with a resident
model. --clear is required; otherwise only tokenizer/slot availability is read.
"""
import argparse
import json
import re
import subprocess

WORKER = r'''
import json, os, urllib.request
base='http://127.0.0.1:'+os.environ['RUNNER_PORT']
def api(path, body=None):
    request=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=30) as response:
        return json.load(response)
slots=api('/slots')
if isinstance(slots,dict): slots=slots.get('slots',[])
assert isinstance(slots,list) and slots, 'Native slot API returned no slots'
assert all(not slot.get('is_processing',False) for slot in slots), 'Refusing to erase a busy slot'
cleared=[]
if os.environ['CLEAR_SLOT']=='1':
    for slot in slots:
        result=api('/slots/'+str(slot['id'])+'?action=erase',{})
        assert 'n_erased' in result, 'Native server did not confirm erased token count: '+str(result)
        cleared.append({'slot_id':slot['id'],'n_erased':result['n_erased']})
print(json.dumps({'cleared':os.environ['CLEAR_SLOT']=='1','slots':cleared,'available_slots':len(slots),'runner_port':int(os.environ['RUNNER_PORT']),'method':'native llama-server /slots/{id}?action=erase'}))
'''

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--container',default='aigoat-ollama-1')
    p.add_argument('--python-image',default='python@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8')
    p.add_argument('--clear',action='store_true')
    a=p.parse_args()
    listing=subprocess.check_output(['docker','top',a.container,'-eo','pid,args'],text=True)
    ports=re.findall(r'llama-server\b[^\n]*?--port\s+(\d+)',listing)
    if len(ports)!=1:raise SystemExit('Exactly one loaded model runner is required')
    cmd=['docker','run','--rm','-i','--network','container:'+a.container,'-e','RUNNER_PORT='+ports[0],'-e','CLEAR_SLOT='+('1' if a.clear else '0'),'--entrypoint','python',a.python_image,'-']
    result=subprocess.run(cmd,input=WORKER,text=True,capture_output=True)
    if result.returncode:
        raise SystemExit(result.stderr or result.stdout)
    data=json.loads(result.stdout)
    if a.clear and not data.get('cleared'):raise SystemExit('Cache was not cleared')
    print(json.dumps(data))

if __name__=='__main__':main()
