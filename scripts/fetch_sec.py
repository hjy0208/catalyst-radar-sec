#!/usr/bin/env python3
import json, os, re, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urljoin
import xml.etree.ElementTree as ET
from html import unescape

IDENTITY = os.environ.get('SEC_USER_AGENT', '').strip()
if not IDENTITY:
    raise SystemExit('SEC_USER_AGENT secret is required, e.g. Catalyst Radar your@email.com')

HEADERS = {
    'User-Agent': IDENTITY,
    'Accept': 'application/atom+xml,application/json,text/plain,*/*',
    'Accept-Encoding': 'identity',
}

ALLOWED = {'NASDAQ', 'NYSE', 'NYSE AMERICAN'}

def get_text(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=30) as r:
                return r.read().decode('utf-8', errors='replace')
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last

def normalize_exchange(x):
    s = (x or '').upper().strip()
    if 'NASDAQ' in s:
        return 'NASDAQ'
    if s == 'NYSE' or 'NEW YORK STOCK EXCHANGE' in s:
        return 'NYSE'
    if 'NYSE AMERICAN' in s or s == 'AMEX':
        return 'NYSE AMERICAN'
    return s

def company_map():
    raw = json.loads(get_text('https://www.sec.gov/files/company_tickers_exchange.json'))
    fields = raw['fields']
    pos = {name: fields.index(name) for name in ['cik','name','ticker','exchange']}
    out = {}
    for row in raw['data']:
        cik = str(int(row[pos['cik']]))
        out[cik] = {
            'name': row[pos['name']],
            'ticker': row[pos['ticker']],
            'exchange': normalize_exchange(row[pos['exchange']]),
        }
    return out

def parse_atom(xml_text, fallback_form):
    root = ET.fromstring(xml_text)
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    rows = []
    for e in root.findall('a:entry', ns):
        title = (e.findtext('a:title', default='', namespaces=ns) or '').strip()
        updated = (e.findtext('a:updated', default='', namespaces=ns) or '').strip()
        summary = (e.findtext('a:summary', default='', namespaces=ns) or '').strip()
        ident = (e.findtext('a:id', default='', namespaces=ns) or '').strip()
        link_el = e.find('a:link', ns)
        url = link_el.attrib.get('href','') if link_el is not None else ''
        cikm = re.search(r'\((\d{6,10})\)\s*\((?:Filer|Reporting)\)', title, re.I) or re.search(r'\((\d{6,10})\)', title)
        if not cikm:
            continue
        formm = re.match(r'^([^\s]+(?:\s+[^\s-]+)?)\s+-\s+', title)
        form = (formm.group(1) if formm else fallback_form).strip()
        company = re.sub(r'^.*?\s+-\s+', '', title)
        company = re.sub(r'\s*\(\d{6,10}\).*$', '', company).strip()
        am = re.search(r'accession-number=([0-9-]+)', ident, re.I)
        items = ''
        im = re.search(r'Items?[^<]*', summary, re.I)
        if im: items = im.group(0)
        rows.append({
            'cik': str(int(cikm.group(1))), 'company': company, 'form': form,
            'filing_date': updated[:10], 'title': title, 'summary': re.sub('<[^>]+>',' ',summary),
            'items': items, 'url': url, 'accession': am.group(1) if am else ident,
        })
    return rows


def strip_html(text):
    text = re.sub(r'(?is)<script.*?</script>|<style.*?</style>', ' ', text or '')
    text = re.sub(r'(?i)<br\\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>', '\\n', text)
    text = re.sub(r'(?s)<[^>]+>', ' ', text)
    text = unescape(text)
    return re.sub(r'[ \\t]+', ' ', text).replace('\\r','').strip()

KEY_RE = re.compile(r'(revenue|net income|operating income|earnings per share|EPS|guidance|contract|agreement|acquisition|merger|purchase price|consideration|order|customer|clinical|phase\\s*[123]|FDA|approval|trial|capacity|production|launch|expects?|forecast|outlook|\\$\\s?\\d|\\d+(?:\\.\\d+)?%)', re.I)
NOISE_RE = re.compile(r'(forward-looking statements|financial statements and exhibits|regulation fd disclosure|table of contents|signature)', re.I)

def meaningful_sentences(text, limit=3):
    plain = strip_html(text)
    parts = re.split(r'(?<=[.!?])\\s+|\\n+', plain)
    ranked=[]
    for sent in parts:
        sent=re.sub(r'\\s+',' ',sent).strip()
        if len(sent)<35 or len(sent)>650 or NOISE_RE.search(sent):
            continue
        score=0
        if KEY_RE.search(sent): score += 5
        if re.search(r'[\\$€£]|\\b\\d+(?:\\.\\d+)?%|\\b\\d+(?:\\.\\d+)?\\s*(?:million|billion|m|bn)\\b',sent,re.I): score += 6
        if re.search(r'expects?|will |planned|scheduled|guidance|forecast|target',sent,re.I): score += 2
        if score:
            ranked.append((score,sent))
    ranked.sort(key=lambda x:(-x[0], len(x[1])))
    out=[]
    for _,sent in ranked:
        if any(sent[:90].lower()==x[:90].lower() for x in out): continue
        out.append(sent)
        if len(out)>=limit: break
    return out

def filing_documents(index_url):
    try:
        html=get_text(index_url,retries=2)
    except Exception:
        return []
    rows=re.findall(r'(?is)<tr[^>]*>(.*?)</tr>',html)
    primary=[]; exhibits=[]
    for row in rows:
        rowtxt=re.sub(r'\\s+',' ',strip_html(row))
        links=re.findall(r'(?i)href=["\\\']([^"\\\']+)["\\\']',row)
        if not links: continue
        href=links[0]
        if not re.search(r'\\.(?:htm|html|txt)(?:$|\\?)',href,re.I): continue
        full=urljoin(index_url,href)
        if re.search(r'EX-99(?:\\.1)?|PRESS RELEASE|EARNINGS RELEASE',rowtxt,re.I): exhibits.append(full)
        elif re.search(r'\\b(?:8-K|6-K)\\b',rowtxt,re.I) and not re.search(r'XBRL|XML|GRAPHIC',rowtxt,re.I): primary.append(full)
    docs=[]
    for u in primary[:1]+exhibits[:1]:
        if u not in docs: docs.append(u)
    return docs

def enrich_filing(row):
    docs=filing_documents(row.get('url',''))
    chunks=[]
    for u in docs:
        try:
            chunks.append(get_text(u,retries=2))
            time.sleep(0.18)
        except Exception:
            continue
    if not chunks:
        return row
    keys=meaningful_sentences(' '.join(chunks),3)
    if keys:
        row['key_sentences']=keys
        row['body_excerpt']=' '.join(keys)[:1800]
    return row


def main():
    cmap = company_map()
    time.sleep(0.5)
    entries = []
    for form in ('8-K','6-K'):
        q = urlencode({'action':'getcurrent','type':form,'dateb':'','owner':'include','count':'100','output':'atom'})
        xml = get_text('https://www.sec.gov/cgi-bin/browse-edgar?' + q)
        entries.extend(parse_atom(xml, form))
        time.sleep(0.75)

    out, excluded = [], 0
    seen = set()
    for r in entries:
        m = cmap.get(r['cik'])
        if not m or m['exchange'] not in ALLOWED or not m['ticker']:
            excluded += 1
            continue
        key = r['accession'] or r['url']
        if key in seen: continue
        seen.add(key)
        out.append({**r, 'company': m['name'] or r['company'], 'ticker': m['ticker'], 'exchange': m['exchange']})

    # Apps Script가 실제로 사용하는 고우선 8-K/6-K만 원문/EX-99.1에서 핵심문장을 보강합니다.
    enriched=0
    for r in out:
        if enriched>=36: break
        items=(r.get('items') or '')
        if r.get('form','').upper().startswith('8-K') and not re.search(r'Item\s+(?:1\.01|1\.02|2\.01|2\.02|3\.01|3\.02|7\.01|8\.01)',items,re.I):
            continue
        enrich_filing(r); enriched += 1
        time.sleep(0.18)
    print(f'enriched {enriched} filings with primary/EX-99.1 text')

    payload = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source': 'SEC EDGAR latest 8-K/6-K via GitHub Actions',
        'excluded': excluded,
        'events': out[:150],
    }
    dest = Path('docs/sec_latest.json')
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'wrote {len(out[:150])} events, excluded {excluded}')

if __name__ == '__main__':
    main()
