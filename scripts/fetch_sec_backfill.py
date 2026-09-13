#!/usr/bin/env python3
import concurrent.futures as cf
import html
import json, os, re, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

IDENTITY = os.environ.get('SEC_USER_AGENT', '').strip()
if not IDENTITY:
    raise SystemExit('SEC_USER_AGENT secret is required')

HEADERS = {
    'User-Agent': IDENTITY,
    'Accept': 'text/html,text/plain,application/json,*/*',
    'Accept-Encoding': 'identity',
}
ALLOWED = {'NASDAQ','NYSE','NYSE AMERICAN'}
FORMS = {'8-K','8-K/A','6-K','6-K/A'}
PRIORITY_ITEMS = {'1.01','1.02','2.01','2.02','7.01','8.01'}
MAX_8K_INDEX = 500
MAX_6K_INDEX = 100
MAX_DOC_FETCHES = 240
TIME_BUDGET_SEC = 8 * 60
WORKERS = 4

FUTURE_RE = re.compile(
    r'(expected\s+to|scheduled\s+to|plans?\s+to|planned\s+to|intends?\s+to|targets?\s+to|aims?\s+to|'
    r'anticipated\s+to|projected\s+to|slated\s+to|will\s+(?:announce|launch|commence|begin|start|submit|complete|report|release|present)|'
    r'PDUFA|top[- ]?line|commercial\s+launch|phase\s*[123].{0,100}(?:data|results?)|'
    r'20\d{2}\s*Q[1-4]|Q[1-4]\s*20\d{2}|H[12]\s*20\d{2}|20\d{2}\s*H[12])', re.I)
EVENT_RE = re.compile(
    r'(clinical|phase\s*[123]|FDA|approval|PDUFA|NDA|BLA|contract|agreement|order|production|launch|capacity|plant|investment|'
    r'acquisition|merger|earnings|guidance|commercialization|trial|data|results?)', re.I)


def get_text(url, retries=2, timeout=18):
    last = None
    for i in range(retries):
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=timeout) as r:
                return r.read().decode('utf-8', errors='replace')
        except Exception as e:
            last = e
            time.sleep(0.7 * (i + 1))
    raise last


def normalize_exchange(x):
    s=(x or '').upper().strip()
    if 'NASDAQ' in s: return 'NASDAQ'
    if s == 'NYSE' or 'NEW YORK STOCK EXCHANGE' in s: return 'NYSE'
    if 'NYSE AMERICAN' in s or s == 'AMEX': return 'NYSE AMERICAN'
    return s


def common_ticker(ticker, name=''):
    t=(ticker or '').upper().strip(); c=(name or '').upper()
    if not t: return False
    if re.search(r'-(?:P[A-Z]?|PR[A-Z]?|WT|WS|WTS)$', t): return False
    if re.search(r'\.(?:P|PR)[A-Z]?$', t): return False
    if len(t) >= 5 and re.search(r'(?:WW|WZ|WT|WS|W)$', t): return False
    if re.search(r'(ACQUISITION|BLANK CHECK|SPAC)', c) and t.endswith('U'): return False
    if re.search(r'\bWARRANTS?\b|\bPREFERRED\b|\bDEPOSITARY SHARE\b|\bUNITS?\b', c): return False
    return True


def company_map():
    raw=json.loads(get_text('https://www.sec.gov/files/company_tickers_exchange.json', timeout=25))
    fields=raw['fields']; pos={n:fields.index(n) for n in ['cik','name','ticker','exchange']}; out={}
    for row in raw['data']:
        cik=str(int(row[pos['cik']])); ex=normalize_exchange(row[pos['exchange']]); ticker=row[pos['ticker']]
        if ex in ALLOWED and common_ticker(ticker,row[pos['name']]):
            out[cik]={'name':row[pos['name']],'ticker':ticker,'exchange':ex}
    return out


def qtr(dt): return (dt.month-1)//3+1


def fetch_daily_index(dt):
    url=f"https://www.sec.gov/Archives/edgar/daily-index/{dt.year}/QTR{qtr(dt)}/master.{dt:%Y%m%d}.idx"
    try: txt=get_text(url,1,10)
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


def strip_html(raw):
    s=re.sub(r'<script[\s\S]*?</script>',' ',raw,flags=re.I)
    s=re.sub(r'<style[\s\S]*?</style>',' ',s,flags=re.I)
    s=re.sub(r'<[^>]+>',' ',s)
    s=html.unescape(s)
    return re.sub(r'\s+',' ',s).strip()


def filing_index_url(row):
    accession = Path(row['filename']).name.replace('.txt','')
    nodash = accession.replace('-','')
    return f"https://www.sec.gov/Archives/edgar/data/{int(row['cik'])}/{nodash}/{accession}-index.htm", accession, nodash


def parse_index(row):
    index_url, accession, nodash = filing_index_url(row)
    raw = get_text(index_url, 2, 15)
    text = strip_html(raw)
    items = []
    for x in re.findall(r'Item\s+(1\.01|1\.02|2\.01|2\.02|3\.01|3\.02|7\.01|8\.01)', text, re.I):
        if x not in items: items.append(x)

    # Prefer the primary 8-K/6-K doc; for 6-K, EX-99.1 is often where catalyst details live.
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', raw, re.I)
    docs=[]
    for h in hrefs:
        if not h.lower().endswith(('.htm','.html','.txt')): continue
        if h.startswith('/'): full='https://www.sec.gov'+h
        elif h.startswith('http'): full=h
        else: full=f"https://www.sec.gov/Archives/edgar/data/{int(row['cik'])}/{nodash}/"+h.split('/')[-1]
        docs.append(full)
    # dedupe preserving order
    docs=list(dict.fromkeys(docs))
    if row['form'].startswith('8-K'):
        priority=[u for u in docs if re.search(r'8k|8-k',u,re.I)]
    else:
        priority=[u for u in docs if re.search(r'ex99|99-?1|6k|6-k',u,re.I)]
    return index_url, accession, items, (priority or docs)[:2]


def schedule_snippets(text):
    chunks=re.split(r'(?<=[.!?])\s+|\n+', text)
    out=[]
    for c in chunks:
        c=re.sub(r'\s+',' ',c).strip()
        if len(c)<30 or len(c)>1800: continue
        if FUTURE_RE.search(c) and EVENT_RE.search(c):
            out.append(c[:1600])
            if len(out)>=6: break
    return out


def enrich(row,cmap,counters,start_ts):
    if time.time()-start_ts > TIME_BUDGET_SEC: return None
    m=cmap.get(row['cik'])
    if not m: return None
    try:
        index_url, accession, items, doc_urls = parse_index(row)
    except Exception:
        return None

    # 8-K: skip low-priority Items before downloading the actual filing document.
    if row['form'].startswith('8-K') and not (set(items) & PRIORITY_ITEMS):
        return None
    if not doc_urls: return None

    snips=[]
    for u in doc_urls:
        with counters['lock']:
            if counters['docs'] >= MAX_DOC_FETCHES: break
            counters['docs'] += 1
        try: raw=get_text(u,1,15)
        except Exception: continue
        text=strip_html(raw)
        snips.extend(schedule_snippets(text))
        if snips: break
    if not snips: return None

    return {
      'cik':row['cik'],'company':m['name'] or row['company'],'ticker':m['ticker'],'exchange':m['exchange'],
      'form':row['form'],'filing_date':row['filing_date'],'title':f"{row['form']} - {m['name']}",
      'items':','.join(items[:5]),'text':' '.join(snips),'summary':' '.join(snips[:3]),'url':index_url,'accession':accession
    }


def main():
    start_ts=time.time(); now=datetime.now(timezone.utc); start=now-timedelta(days=90)
    print('step 1/4: load listed company map', flush=True)
    cmap=company_map(); time.sleep(.4)

    print('step 2/4: scan SEC daily indexes (90 days)', flush=True)
    rows=[]; d=start.date(); scanned_days=0
    while d<=now.date():
        if d.weekday()<5:
            dayrows=fetch_daily_index(datetime(d.year,d.month,d.day))
            rows.extend(r for r in dayrows if r['cik'] in cmap)
            scanned_days+=1
            if scanned_days%10==0: print(f'  index days {scanned_days}, filings {len(rows)}', flush=True)
            time.sleep(.08)
        d+=timedelta(days=1)

    uniq={r['filename']:r for r in rows}
    rows=sorted(uniq.values(),key=lambda r:(r['filing_date'],r['filename']),reverse=True)
    rows8=[r for r in rows if r['form'].startswith('8-K')][:MAX_8K_INDEX]
    rows6=[r for r in rows if r['form'].startswith('6-K')][:MAX_6K_INDEX]
    selected=rows8+rows6
    print(f'step 3/4: lightweight candidate scan. total={len(rows)}, selected={len(selected)} (8-K {len(rows8)}, 6-K {len(rows6)})', flush=True)

    import threading
    counters={'docs':0,'lock':threading.Lock()}
    out=[]; processed=0
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs=[ex.submit(enrich,r,cmap,counters,start_ts) for r in selected]
        for f in cf.as_completed(futs):
            processed+=1
            try:
                x=f.result()
                if x: out.append(x)
            except Exception:
                pass
            if processed%25==0:
                print(f'  processed {processed}/{len(selected)}; primary docs fetched {counters["docs"]}; catalysts {len(out)}; elapsed {int(time.time()-start_ts)}s', flush=True)
            if time.time()-start_ts > TIME_BUDGET_SEC:
                print('  time budget reached; stopping with partial but usable result', flush=True)
                break

    out.sort(key=lambda x:(x['filing_date'],x['accession']),reverse=True)
    payload={
      'generated_at':datetime.now(timezone.utc).isoformat(),
      'source':'SEC EDGAR lightweight 90-day calendar backfill via GitHub Actions',
      'days':90,'all_candidate_filings':len(rows),'selected_filings':len(selected),
      'primary_docs_fetched':counters['docs'],'time_budget_sec':TIME_BUDGET_SEC,
      'events':out
    }
    dest=Path('docs/sec_backfill_90d.json'); dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'step 4/4: wrote {len(out)} calendar candidates in {int(time.time()-start_ts)}s', flush=True)

if __name__=='__main__': main()
