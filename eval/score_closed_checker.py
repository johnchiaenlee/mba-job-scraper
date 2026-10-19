"""Score v1 (original) vs v2 (improved) closed-job checker on a 100-job gold set.

Gold set: closed_checker_gold_set_2026-10-18.csv — 24 jobs the checker closed on
2026-10-12 + 76 random jobs it kept Open. Label = what a person sees on the page
(job page with no closed banner = open; "No longer accepting applications" banner
or redirect to a generic search page = closed). 20 labels hand-verified in a browser.

Setup used for the 2026-10-18 run: page responses cached to cache/<job_id>_first.json
(first attempt) and cache/<job_id>_clean.json (after retrying 429s); the old and new
check_closed.py copied in as check_closed_v1.py / check_closed_v2.py.
Labels go stale as jobs close — re-label before re-running.

Both versions are run on the SAME cached responses, so the comparison is fair.
v1 sees only the first response (it never retries). v2 retries rate-limited
responses, which we replay from the cached retry result."""
import json, csv, re, sys, collections
gold = json.load(open('gold_raw.json'))
PHRASES = ["no longer accepting applications","this job is no longer available","job has expired",
           "this position has been filled","application deadline has passed","the job is no more available"]

def label(clean):                       # gold label = what a human sees
    if clean['status'] in (429, 999): return None
    if '/jobs/view/' not in clean['final']:
        return 'closed' if 'expired_jd_redirect' in clean['final'] or '/jobs/search' in clean['final'] else None
    return 'closed' if 'closed-job__flavor--closed' in clean['body'] else 'open'

import types, importlib, time as _time
class FakeResp:
    def __init__(self, d): self.status_code, self.url, self.text = d['status'], d['final'], d['body']
def replay(module, fn, first, clean):
    """Call the real checker function, feeding it cached responses instead of the network."""
    queue = [first] + [clean]*10
    module.requests.get = lambda *a, **k: FakeResp(queue.pop(0))
    module.time.sleep = lambda s: None
    out = getattr(module, fn)('https://www.linkedin.com/jobs/view/0')
    return out if isinstance(out, str) else (out[0] if isinstance(out[0], str) else ('closed' if out[0] else 'open'))
import sys; _gs = types.ModuleType('gspread'); _gs.Worksheet = object; sys.modules['gspread'] = _gs
g = types.ModuleType('google'); g.oauth2 = types.ModuleType('google.oauth2'); sa = types.ModuleType('google.oauth2.service_account'); sa.Credentials=None
sys.modules.update({'google':g,'google.oauth2':g.oauth2,'google.oauth2.service_account':sa})
M1 = importlib.import_module('check_closed_v1'); M2 = importlib.import_module('check_closed_v2')
def v1(f, c): return replay(M1, 'is_closed', f, c)
def v2(f, c): return replay(M2, 'check_status', f, c)

rows=[]
for g in gold:
    f = json.load(open(f"cache/{g['id']}_first.json")); c = json.load(open(f"cache/{g['id']}_clean.json"))
    t = re.search(r'<title>(.*?)</title>', c['body'], re.S)
    rows.append(dict(row=g['row'], url=g['url'], title=(t.group(1).strip()[:80] if t else ''),
                     first_status=f['status'], gold=label(c), v1=v1(f,c), v2=v2(f,c)))

def metrics(name):
    R=[r for r in rows if r['gold']]
    tp=sum(r[name]=='closed' and r['gold']=='closed' for r in R)
    fp=sum(r[name]=='closed' and r['gold']=='open' for r in R)
    fn=sum(r[name]!='closed' and r['gold']=='closed' for r in R)
    silent=sum(r[name]=='open' and r['gold']=='closed' for r in R)
    unk=sum(r[name]=='unknown' for r in R)
    acc=sum(r[name]==r['gold'] for r in R)
    P = tp/(tp+fp) if tp+fp else float('nan'); Rc = tp/(tp+fn) if tp+fn else float('nan')
    return dict(n=len(R),accuracy=acc/len(R),precision=P,recall=Rc,
                f1=2*P*Rc/(P+Rc) if P+Rc else 0,tp=tp,fp=fp,fn=fn,silently_wrong_open=silent,unknown=unk)

if __name__=='__main__':
    print('gold labels:', collections.Counter(r['gold'] for r in rows))
    print('first-attempt status:', collections.Counter(r['first_status'] for r in rows))
    for n in ('v1','v2'): print(n, {k:(round(v,3) if isinstance(v,float) else v) for k,v in metrics(n).items()})
    with open('gold_set.csv','w',newline='') as fh:
        w=csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print('\nDisagreements with gold:')
    for r in rows:
        if r['gold'] and (r['v1']!=r['gold'] or r['v2']!=r['gold']):
            print(f"  row {r['row']:>5} gold={r['gold']:6} v1={r['v1']:6} v2={r['v2']:7} first={r['first_status']} {r['title'][:50]}")
