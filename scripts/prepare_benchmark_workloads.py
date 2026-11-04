#!/usr/bin/env python3
"""Create a fixed 2,000-native-token synthetic document without generating text.

Run on the Docker host. A temporary existing Python image reaches the active
llama-server tokenizer through the Ollama container's network namespace. No
stored model or application configuration is changed.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess

WORKER = r'''
import hashlib, json, os, urllib.request
base = 'http://127.0.0.1:' + os.environ['TOKENIZER_PORT']
def api(path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)
def tokenize(text):
    return api('/tokenize', {'content':text, 'add_special':False})['tokens']
header = ''' + repr('''SYNTHETIC SECURITY OPERATIONS REVIEW
All organizations, hosts, accounts and events in this document are fictional.
Incident identifier: SOC-2026-041.
First alert timestamp: 2026-10-18 14:35 UTC.
Affected service: internal-invoice-indexer.
Containment delay: 17 minutes from first alert.
Recovery window: 45 minutes after containment.
Scope: a training tenant with invented support records and no live customer information.
The review distinguishes observations, approved actions, and follow-up checks.

''') + r'''
topics = ['authentication telemetry', 'network flow review', 'application audit trails', 'retrieval provenance', 'service account scope', 'change approval records', 'backup verification', 'outbound destination checks']
lines = []
for index in range(1, 301):
    topic = topics[(index - 1) % len(topics)]
    lines.append(f'Review item {index:03d}: Analysts checked {topic}. The training evidence was retained with its source identifier. Findings were compared with the approved service baseline. Unverified claims were recorded as observations rather than instructions.\n')
tokens = tokenize(header + ''.join(lines))
assert len(tokens) > 2000
document = api('/detokenize', {'tokens':tokens[:2000]})['content']
count = len(tokenize(document))
assert count == 2000, f'Round-trip document has {count} native tokens; refusing an approximate fixture.'
question = 'Identify the incident identifier, first alert timestamp, affected service, containment delay, and recovery window. Then summarize the document in one short paragraph.'
data = {
 'P1': {'prompt':'Explain what prompt injection is in two sentences.'},
 'P2': {'prompt':'Explain the difference between direct and indirect prompt injection, including three technical examples and the trust boundaries involved.'},
 'P3': {
   'document':document, 'document_tokens':count, 'document_token_count':count,
   'document_sha256':hashlib.sha256(document.encode()).hexdigest(),
   'prompt':'Read this synthetic security operations document.\n\n'+document+'\n\n'+question,
   'specified_details':{'incident_identifier':'SOC-2026-041','first_alert_timestamp':'2026-10-18 14:35 UTC','affected_service':'internal-invoice-indexer','containment_delay':'17 minutes','recovery_window':'45 minutes'},
   'tokenizer_evidence':{'endpoint':'native llama-server /tokenize with add_special=false; /detokenize first 2000 tokens; exact round-trip verification','exact_count':count,'model':'ministral-3:3b','document_only_excludes_chat_template':True},
 },
}
print(json.dumps(data,indent=2))
'''

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', default='aigoat-ollama-1')
    parser.add_argument('--python-image', default='python@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8')
    parser.add_argument('--output', type=Path, default=Path('performance/benchmark_workloads.json'))
    args = parser.parse_args()
    listing = subprocess.check_output(['docker','top',args.container,'-eo','pid,args'],text=True)
    matches = re.findall(r'llama-server\b[^\n]*?--port\s+(\d+)',listing)
    if len(matches) != 1:
        raise SystemExit('Exactly one loaded llama-server is required; preload the existing model before preparing this fixture.')
    command = ['docker','run','--rm','-i','--network','container:'+args.container,'-e','TOKENIZER_PORT='+matches[0],'--entrypoint','python',args.python_image,'-']
    result = subprocess.run(command,input=WORKER,text=True,capture_output=True,check=True)
    data = json.loads(result.stdout)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(data,indent=2)+'\n')
    print(json.dumps({'output':str(args.output),'document_tokens':data['P3']['document_tokens'],'document_sha256':data['P3']['document_sha256']}))

if __name__ == '__main__':
    main()
