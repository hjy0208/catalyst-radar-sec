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
MAX_DOC_FETCHES = 280
TIME_BUDGET_SEC = 8 * 60
WORKERS = 4

# Calendar 후보는 "미래 의도/시점"과 "투자 이벤트"가 한 문장 또는 인접 문장에 같이 있어야 합니다.
FUTURE_RE = re.compile(
    r'(expects?(?:\s+to|\s+that)|expected\s+to|scheduled(?:\s+to|\s+for)|plans?(?:\s+to|\s+for)|planned\s+to|'
    r'intends?(?:\s+to|\s+for)|targets?(?:\s+to|\s+for)|aims?(?:\s+to|\s+for)|anticipates?(?:\s+to|\s+that)|'
    r'projected\s+to|slated\s+to|on\s+track\s+to|set\s+to|due\s+(?:on|in|by)|'
    r'will\s+(?:announce|launch|commence|begin|start|submit|complete|report|release|present|publish|open|close|deliver|produce)|'
    r'PDUFA|top[- ]?line|readout|commercial\s+launch|'
    r'20\d{2}\s*Q[1-4]|Q[1-4]\s*20\d{2}|H[12]\s*20\d{2}|20\d{2}\s*H[12]|'
    r'(?:first|second|third|fourth)\s+quarter\s+(?:of\s+)?20\d{2}|'
    r'(?:first|second)\s+half\s+(?:of\s+)?20\d{2}|'
    r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+(?:\d{1,2}(?:st|nd|rd|th)?[,]?\s+)?20\d{2}|'
    r'by\s+(?:year[- ]?end|the\s+end\s+of)\s+20\d{2}|later\s+this\s+year|next\s+(?:quarter|year))', re.I)
EVENT_RE = re.compile(
    r'(clinical|trial|phase\s*[123]|FDA|EMA|PDUFA|NDA|BLA|approval|regulatory|submission|application|'
    r'top[- ]?line|readout|data|results?|contract|agreement|order|customer|production|manufactur|launch|capacity|'
    r'plant|facility|investment|acquisition|merger|transaction|closing|earnings|revenue|guidance|commercializ|'
    r'investor\s+day|conference|presentation)', re.I)
PAST_ONLY_RE = re.compile(
    r'\b(?:was|were|has been|have been)\s+(?:completed|launched|submitted|approved|announced|reported|released|closed)\b', re.I)
LOW_VALUE_RE = re.compile(
    r'(?:resign|resignation|retir(?:e|ement)|appoint(?:ed|ment)|board\s+of\s+directors|compensat(?:ion|ory)|employment\s+agreement|'
    r'indenture|senior\s+notes?|convertible\s+notes?|debentures?|redemption|redeem(?:ed|able)?|maturity\s+date|coupon|'
    r'cash\s+runway|support\s+(?:the\s+company.?s\s+)?operations\s+(?:into|through)|fund\s+(?:its\s+)?operations\s+(?:into|through)|liquidity\s+runway)', re.I)
HIGH_VALUE_RE = re.compile(
    r'(clinical|trial|phase\s*[123]|FDA|EMA|PDUFA|NDA|BLA|top[- ]?line|readout|regulatory\s+(?:submission|approval)|'
    r'contract|customer|order|production|manufactur|commercial\s+launch|capacity|plant|facility|guidance|earnings|revenue|'
    r'investor\s+day|conference|presentation|merger|acquisition|closing)', re.I)


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
    if re.search(r'\b(?:SPAC|BLANK CHECK)\b|\bACQUISITION\s+(?:CORP(?:ORATION)?|CO(?:MPANY)?|LTD)\b', c): return False
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


def abs_doc_url(href, row, nodash):
    h=(href or '').strip()
    if not h: return ''
    if h.startswith('/'): return 'https://www.sec.gov'+h
    if h.startswith('http'): return h
    return f"https://www.sec.gov/Archives/edgar/data/{int(row['cik'])}/{nodash}/"+h.split('/')[-1]


def parse_document_table(raw, row, nodash):
    """Parse SEC filing index 'Document Format Files' rows.
    SEC index pages expose Seq / Description / Document / Type / Size. We keep the actual 8-K/6-K and EX-99.x docs.
    """
    docs=[]
    for tr in re.findall(r'<tr[^>]*>([\s\S]*?)</tr>', raw, re.I):
        cells=re.findall(r'<td[^>]*>([\s\S]*?)</td>', tr, re.I)
        if len(cells)<4: continue
        vals=[strip_html(c) for c in cells]
        hrefm=re.search(r'href=["\']([^"\']+)["\']', cells[2], re.I)
        if not hrefm: continue
        typ=(vals[3] or '').upper().strip()
        desc=(vals[1] or '').upper().strip()
        url=abs_doc_url(hrefm.group(1), row, nodash)
        if not url: continue
        docs.append({'type':typ,'desc':desc,'url':url})
    return docs


def choose_docs(row, items, docs):
    """Choose documents that are most likely to contain catalyst language.
    Important fix: an 8-K primary document often only says the press release is furnished as EX-99.1.
    We therefore fetch EX-99.1 as well as the primary form document.
    """
    form_base='8-K' if row['form'].startswith('8-K') else '6-K'
    primary=[d for d in docs if d['type']==form_base]
    ex991=[d for d in docs if re.match(r'EX-99(?:\.1|\.01)?$', d['type']) or 'EX-99.1' in d['desc']]
    ex99other=[d for d in docs if d['type'].startswith('EX-99') and d not in ex991]

    # Press releases/presentations are especially important for results, Reg FD, other events, and foreign issuers.
    prefer_exhibit = row['form'].startswith('6-K') or bool(set(items) & {'2.02','7.01','8.01'})
    ordered=(ex991+primary+ex99other) if prefer_exhibit else (primary+ex991+ex99other)
    seen=set(); out=[]
    for d in ordered:
        if d['url'] in seen: continue
        seen.add(d['url']); out.append(d['url'])
        if len(out)>=3: break
    return out


def parse_index(row):
    index_url, accession, nodash = filing_index_url(row)
    raw = get_text(index_url, 2, 15)
    text = strip_html(raw)
    items = []
    for x in re.findall(r'Item\s+(1\.01|1\.02|2\.01|2\.02|3\.01|3\.02|7\.01|8\.01)', text, re.I):
        if x not in items: items.append(x)

    docs=parse_document_table(raw,row,nodash)
    doc_urls=choose_docs(row,items,docs)

    # Fallback for unusual index markup.
    if not doc_urls:
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', raw, re.I)
        fallback=[]
        for h in hrefs:
            if not h.lower().endswith(('.htm','.html','.txt')): continue
            u=abs_doc_url(h,row,nodash)
            if u and ('-index' not in u.lower()) and ('-headers' not in u.lower()): fallback.append(u)
        doc_urls=list(dict.fromkeys(fallback))[:3]
    return index_url, accession, items, doc_urls


def sentence_list(text):
    # SEC HTML flattening can remove line breaks. Split on normal punctuation and common bullet separators.
    text=re.sub(r'\s+',' ',text).strip()
    parts=re.split(r'(?<=[.!?])\s+|\s*[•▪◦]\s*|\s{2,}', text)
    return [p.strip() for p in parts if 20 <= len(p.strip()) <= 2200]


def schedule_snippets(text):
    sents=sentence_list(text)
    out=[]
    for i,s in enumerate(sents):
        # Use a 3-sentence window so date/plan and event can be adjacent instead of identical sentence.
        lo=max(0,i-1); hi=min(len(sents),i+2)
        window=' '.join(sents[lo:hi])
        if LOW_VALUE_RE.search(window) and not HIGH_VALUE_RE.search(window): continue
        if not FUTURE_RE.search(window): continue
        if not EVENT_RE.search(window): continue
        # Don't keep a window that only describes a completed past event unless another future marker is present.
        if PAST_ONLY_RE.search(s) and not FUTURE_RE.search(s):
            continue
        cleaned=re.sub(r'\s+',' ',window).strip()
        if cleaned and cleaned not in out:
            out.append(cleaned[:1800])
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

    # 8-K: skip low-priority Items before downloading actual filing documents.
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
        found=schedule_snippets(text)
        if found:
            snips.extend(found)
            # Keep checking up to one more doc when available, because the primary filing and EX-99.1 can contain complementary dates.
            if len(snips)>=4: break
    if not snips: return None

    return {
      'cik':row['cik'],'company':m['name'] or row['company'],'ticker':m['ticker'],'exchange':m['exchange'],
      'form':row['form'],'filing_date':row['filing_date'],'title':f"{row['form']} - {m['name']}",
      'items':','.join(items[:5]),'text':' '.join(snips),'summary':' '.join(snips[:3]),'url':index_url,'accession':accession
    }


def main():
    start_ts=time.time(); now=datetime.now(timezone.utc)
    print('step 1/4: load listed company map', flush=True)
    cmap=company_map(); time.sleep(.4)

    print('step 2/4: scan SEC daily indexes (90 days)', flush=True)
    rows=[]; d=(now-timedelta(days=90)).date(); scanned_days=0
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
                print(f'  processed {processed}/{len(selected)}; docs fetched {counters["docs"]}; catalysts {len(out)}; elapsed {int(time.time()-start_ts)}s', flush=True)
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
