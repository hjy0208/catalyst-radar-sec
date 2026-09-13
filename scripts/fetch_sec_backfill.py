#!/usr/bin/env python3
import concurrent.futures as cf
import json, os, re, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

IDENTITY=os.environ.get('SEC_USER_AGENT','').strip()
if not IDENTITY:
    raise SystemExit('SEC_USER_AGENT secret is required')
HEADERS={'User-Agent':IDENTITY,'Accept':'text/plain,text/html,application/json,*/*','Accept-Encoding':'identity'}
ALLOWED={'NASDAQ','NYSE','NYSE AMERICAN'}
FORMS={'8-K','8-K/A','6-K','6-K/A'}
FUTURE_RE=re.compile(r'(expected\s+to|scheduled\s+to|plans?\s+to|planned\s+to|intends?\s+to|targets?\s+to|aims?\s+to|anticipated\s+to|projected\s+to|slated\s+to|will\s+(?:announce|launch|commence|begin|start|submit|complete|report|release|present)|PDUFA|top[- ]?line|commercial\s+launch|phase\s*[123].{0,80}(?:data|results?)|20\d{2}\s*Q[1-4]|Q[1-4]\s*20\d{2}|H[12]\s*20\d{2}|20\d{2}\s*H[12])',re.I)
EVENT_RE=re.compile(r'(clinical|phase\s*[123]|FDA|approval|PDUFA|NDA|BLA|contract|agreement|order|production|launch|capacity|plant|investment|acquisition|merger|earnings|guidance|commercialization)',re.I)

def get_text(url,retries=3):
    last=None
    for i in range(retries):
        try:
            req=Request(url,headers=HEADERS)
            with urlopen(req,timeout=35) as r:
                return r.read().decode('utf-8',errors='replace')
        except Exception as e:
            last=e; time.sleep(1.0*(i+1))
    raise last

def normalize_exchange(x):
    s=(x or '').upper().strip()
    if 'NASDAQ' in s:return 'NASDAQ'
    if s=='NYSE' or 'NEW YORK STOCK EXCHANGE' in s:return 'NYSE'
    if 'NYSE AMERICAN' in s or s=='AMEX':return 'NYSE AMERICAN'
    return s

def common_ticker(ticker,name=''):
    t=(ticker or '').upper().strip(); c=(name or '').upper()
    if not t:return False
    if re.search(r'-(?:P[A-Z]?|PR[A-Z]?|WT|WS|WTS)$',t):return False
    if re.search(r'\.(?:P|PR)[A-Z]?$',t):return False
    if len(t)>=5 and re.search(r'(?:WW|WZ|WT|WS|W)$',t):return False
    if re.search(r'(ACQUISITION|BLANK CHECK|SPAC)',c) and t.endswith('U'):return False
    if re.search(r'\bWARRANTS?\b|\bPREFERRED\b|\bDEPOSITARY SHARE\b|\bUNITS?\b',c):return False
    return True

def company_map():
    raw=json.loads(get_text('https://www.sec.gov/files/company_tickers_exchange.json'))
    fields=raw['fields']; pos={n:fields.index(n) for n in ['cik','name','ticker','exchange']}; out={}
    for row in raw['data']:
        cik=str(int(row[pos['cik']])); ex=normalize_exchange(row[pos['exchange']]); ticker=row[pos['ticker']]
        if ex in ALLOWED and common_ticker(ticker,row[pos['name']]):
            out[cik]={'name':row[pos['name']],'ticker':ticker,'exchange':ex}
    return out

def qtr(dt): return (dt.month-1)//3+1

def fetch_daily_index(dt):
    url=f"https://www.sec.gov/Archives/edgar/daily-index/{dt.year}/QTR{qtr(dt)}/master.{dt:%Y%m%d}.idx"
    try: txt=get_text(url,1)
    except Exception: return []
    rows=[]; started=False
    for line in txt.splitlines():
        if not started:
            if line.startswith('-----'): started=True
            continue
        parts=line.split('|')
        if len(parts)!=5: continue
        cik,name,form,filed,filename=parts
        if form not in FORMS: continue
        rows.append({'cik':str(int(cik)),'company':name,'form':form,'filing_date':filed,'filename':filename})
    return rows

def strip_submission(raw):
    s=re.sub(r'<script[\s\S]*?</script>',' ',raw,flags=re.I)
    s=re.sub(r'<style[\s\S]*?</style>',' ',s,flags=re.I)
    s=re.sub(r'<[^>]+>',' ',s)
    s=s.replace('&nbsp;',' ').replace('&amp;','&').replace('&quot;','"').replace('&#39;',"'")
    return re.sub(r'\s+',' ',s)

def extract_items(text):
    found=[]
    for x in re.findall(r'Item\s+(1\.01|1\.02|2\.01|2\.02|3\.01|3\.02|7\.01|8\.01)',text,re.I):
        if x not in found:found.append(x)
    return ','.join(found[:5])

def schedule_snippets(text):
    # split generously around punctuation; keep future + event sentences only
    chunks=re.split(r'(?<=[.!?])\s+|\n+',text)
    out=[]
    for c in chunks:
        c=re.sub(r'\s+',' ',c).strip()
        if len(c)<20 or len(c)>1800: continue
        if FUTURE_RE.search(c) and EVENT_RE.search(c):
            out.append(c[:1600])
            if len(out)>=8: break
    return out

def enrich(row,cmap):
    m=cmap.get(row['cik'])
    if not m:return None
    url='https://www.sec.gov/Archives/'+row['filename']
    try: raw=get_text(url,2)
    except Exception:return None
    text=strip_submission(raw)
    snips=schedule_snippets(text)
    if not snips:return None
    accession=Path(row['filename']).name.replace('.txt','')
    nodash=accession.replace('-','')
    index_url=f"https://www.sec.gov/Archives/edgar/data/{int(row['cik'])}/{nodash}/{accession}-index.htm"
    return {
      'cik':row['cik'],'company':m['name'] or row['company'],'ticker':m['ticker'],'exchange':m['exchange'],
      'form':row['form'],'filing_date':row['filing_date'],'title':f"{row['form']} - {m['name']}",
      'items':extract_items(text),'text':' '.join(snips),'summary':' '.join(snips[:3]),'url':index_url,'accession':accession
    }

def main():
    now=datetime.now(timezone.utc)
    start=now-timedelta(days=90)
    cmap=company_map(); time.sleep(.5)
    rows=[]
    d=start.date()
    while d<=now.date():
        if d.weekday()<5:
            for r in fetch_daily_index(datetime(d.year,d.month,d.day)):
                if r['cik'] in cmap: rows.append(r)
            time.sleep(.12)
        d+=timedelta(days=1)
    # newest first; dedupe by filename
    uniq={r['filename']:r for r in rows}
    rows=sorted(uniq.values(),key=lambda r:(r['filing_date'],r['filename']),reverse=True)
    print(f'candidate filings: {len(rows)}')
    out=[]
    # SEC asks automated users to stay <=10 req/sec. 3 workers keeps this conservative.
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        futs=[]
        for r in rows:
            futs.append(ex.submit(enrich,r,cmap))
            time.sleep(.08)
        for i,f in enumerate(cf.as_completed(futs),1):
            try:
                x=f.result()
                if x: out.append(x)
            except Exception: pass
            if i%250==0: print('processed',i,'found',len(out))
    out.sort(key=lambda x:(x['filing_date'],x['accession']),reverse=True)
    payload={'generated_at':datetime.now(timezone.utc).isoformat(),'source':'SEC EDGAR 90-day calendar backfill via GitHub Actions','days':90,'candidate_filings':len(rows),'events':out}
    dest=Path('docs/sec_backfill_90d.json'); dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print('wrote',len(out),'calendar candidates')

if __name__=='__main__': main()
