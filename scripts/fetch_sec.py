#!/usr/bin/env python3
import json, os, re, time
from datetime import datetime, timezone, date
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
    'Accept': 'application/atom+xml,application/json,text/html,text/plain,*/*',
    'Accept-Encoding': 'identity',
}
ALLOWED = {'NASDAQ', 'NYSE', 'NYSE AMERICAN'}


def get_text(url, retries=3, timeout=30, max_bytes=None):
    last = None
    for i in range(retries):
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=timeout) as r:
                data = r.read(max_bytes) if max_bytes else r.read()
                return data.decode('utf-8', errors='replace')
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
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
    pos = {name: fields.index(name) for name in ['cik', 'name', 'ticker', 'exchange']}
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
        url = link_el.attrib.get('href', '') if link_el is not None else ''
        cikm = re.search(r'\((\d{6,10})\)\s*\((?:Filer|Reporting)\)', title, re.I) or re.search(r'\((\d{6,10})\)', title)
        if not cikm:
            continue
        formm = re.match(r'^([^\s]+(?:\s+[^\s-]+)?)\s+-\s+', title)
        form = (formm.group(1) if formm else fallback_form).strip()
        company = re.sub(r'^.*?\s+-\s+', '', title)
        company = re.sub(r'\s*\(\d{6,10}\).*$', '', company).strip()
        am = re.search(r'accession-number=([0-9-]+)', ident, re.I)
        clean_summary = re.sub(r'<[^>]+>', ' ', summary)
        clean_summary = re.sub(r'\s+', ' ', clean_summary).strip()
        items = ' '.join(re.findall(r'Item\s+\d+\.\d+:[^\n\r<]+', unescape(summary), re.I))
        rows.append({
            'cik': str(int(cikm.group(1))), 'company': company, 'form': form,
            'filing_date': updated[:10], 'title': title, 'summary': clean_summary,
            'items': items, 'url': url, 'accession': am.group(1) if am else ident,
        })
    return rows


def strip_html(text):
    text = re.sub(r'(?is)<script.*?</script>|<style.*?</style>', ' ', text or '')
    text = re.sub(r'(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>', '\n', text)
    text = re.sub(r'(?s)<[^>]+>', ' ', text)
    text = unescape(text)
    text = text.replace('\xa0', ' ').replace('\r', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def split_sentences(text):
    plain = strip_html(text)
    raw = re.split(r'(?<=[.!?])\s+|\n+', plain)
    out = []
    for s in raw:
        s = re.sub(r'\s+', ' ', s).strip(' \t-•|')
        if 30 <= len(s) <= 900:
            out.append(s)
    return out


NOISE_RE = re.compile(
    r'forward-looking statements|safe harbor|financial statements and exhibits|regulation fd disclosure|'
    r'table of contents|pursuant to the requirements|signature|commission file number|'
    r'not be deemed .* filed|incorporated by reference|exhibit index|press release furnished as exhibit',
    re.I
)
QUANT_RE = re.compile(
    r'[\$€£¥]|\b\d+(?:\.\d+)?%|\b\d+(?:\.\d+)?\s*(?:million|billion|thousand|m|bn)\b|'
    r'\bEPS\b|earnings per share|revenue|net income|operating income|adjusted EBITDA|free cash flow',
    re.I
)
FUTURE_RE = re.compile(
    r'expects?|anticipates?|plans?|planned|intends?|targets?|scheduled|guidance|forecast|outlook|'
    r'will\s+(?:launch|submit|complete|close|begin|commence|report|release|present|provide|increase|decrease)',
    re.I
)

ITEM_TERMS = {
    '1.01': re.compile(r'entered into|agreement|contract|purchase order|supply|customer|credit facility|loan|term|consideration|commitment', re.I),
    '1.02': re.compile(r'terminat|cancel|expire|expiration|ended|cease|effective|fee|penalty', re.I),
    '2.01': re.compile(r'acquisition|acquired|disposition|disposed|sale of|sold|purchase price|consideration|cash|shares|completed', re.I),
    '2.02': re.compile(r'revenue|sales|net income|operating income|EPS|earnings per share|EBITDA|margin|guidance|outlook|quarter|fiscal year', re.I),
    '3.01': re.compile(r'Nasdaq|NYSE|listing|delisting|deficien|compliance|bid price|cure|deadline|hearing', re.I),
    '3.02': re.compile(r'issued|sold|shares|warrant|convertible|purchase price|offering|proceeds|private placement|securities', re.I),
    '7.01': re.compile(r'guidance|clinical|FDA|approval|contract|agreement|acquisition|production|capacity|launch|results', re.I),
    '8.01': re.compile(r'clinical|FDA|approval|contract|agreement|acquisition|production|capacity|launch|results|guidance', re.I),
    '6-K': re.compile(r'clinical|FDA|approval|contract|agreement|acquisition|production|capacity|launch|results|guidance|revenue|EPS', re.I),
}


def primary_item(items, form):
    m = re.search(r'Item\s+(1\.01|1\.02|2\.01|2\.02|3\.01|3\.02|7\.01|8\.01)', items or '', re.I)
    if m:
        return m.group(1)
    if str(form).upper().startswith('6-K'):
        return '6-K'
    return ''


def sentence_score(sent, item):
    if NOISE_RE.search(sent):
        return -100
    score = 0
    term_re = ITEM_TERMS.get(item)
    if term_re and term_re.search(sent):
        score += 8
    if QUANT_RE.search(sent):
        score += 8
    if FUTURE_RE.search(sent):
        score += 3
    if re.search(r'\b(?:202[0-9]|203[0-9])\b|\bQ[1-4]\b|first quarter|second quarter|third quarter|fourth quarter', sent, re.I):
        score += 2
    if re.search(r'Item\s+\d+\.\d+|Form\s+8-K|Form\s+6-K|Accession|Filed:', sent, re.I):
        score -= 7
    # 아주 긴 법률 문구는 의미 밀도가 낮습니다.
    if len(sent) > 600:
        score -= 3
    return score


def pick_summary_sentences(text, item, limit=3):
    ranked = []
    for sent in split_sentences(text):
        sc = sentence_score(sent, item)
        if sc < 5:
            continue
        ranked.append((sc, sent))
    ranked.sort(key=lambda x: (-x[0], len(x[1])))
    out = []
    for _, sent in ranked:
        normalized = re.sub(r'[^a-z0-9]+', ' ', sent.lower())[:140]
        if any(normalized == re.sub(r'[^a-z0-9]+', ' ', x.lower())[:140] for x in out):
            continue
        out.append(sent)
        if len(out) >= limit:
            break
    return out


def filing_documents(index_url, item=''):
    try:
        html = get_text(index_url, retries=2, max_bytes=1800000)
    except Exception:
        return []
    rows = re.findall(r'(?is)<tr[^>]*>(.*?)</tr>', html)
    primary, ex991, other99, ex10, ex2 = [], [], [], [], []
    for row in rows:
        rowtxt = re.sub(r'\s+', ' ', strip_html(row))
        links = re.findall(r'(?i)href=["\']([^"\']+)["\']', row)
        if not links:
            continue
        href = next((x for x in links if re.search(r'\.(?:htm|html|txt)(?:$|\?)', x, re.I)), links[0])
        if not re.search(r'\.(?:htm|html|txt)(?:$|\?)', href, re.I):
            continue
        full = urljoin(index_url, href)
        if re.search(r'\bEX-99\.1\b|EARNINGS RELEASE|PRESS RELEASE', rowtxt, re.I):
            ex991.append({'url': full, 'kind': 'EX-99.1'})
        elif re.search(r'\bEX-99(?:\.|\b)', rowtxt, re.I):
            other99.append({'url': full, 'kind': 'EX-99'})
        elif re.search(r'\bEX-10(?:\.1|\.2|\b)', rowtxt, re.I):
            ex10.append({'url': full, 'kind': 'EX-10'})
        elif re.search(r'\bEX-2(?:\.1|\b)', rowtxt, re.I):
            ex2.append({'url': full, 'kind': 'EX-2'})
        elif re.search(r'\b(?:8-K|6-K)\b', rowtxt, re.I) and not re.search(r'XBRL|XML|GRAPHIC', rowtxt, re.I):
            primary.append({'url': full, 'kind': 'primary'})

    # v0.2.17: 현재 공시 본문/보도자료를 우선하고 계약서 전문은 보조 자료로만 사용합니다.
    if item == '1.01':
        candidates = primary[:1] + ex991[:1] + ex10[:1]
    elif item == '2.01':
        candidates = primary[:1] + ex991[:1] + ex2[:1]
    elif item == '2.02':
        candidates = ex991[:1] + primary[:1] + other99[:1]
    elif item in {'3.01', '3.02', '1.02'}:
        candidates = primary[:1] + ex991[:1] + other99[:1]
    else:
        candidates = primary[:1] + ex991[:1] + other99[:1]

    docs, seen = [], set()
    for d in candidates:
        if d['url'] not in seen:
            docs.append(d); seen.add(d['url'])
    return docs[:3]


MONEY_DETAIL_RE = re.compile(
    r'[\$€£¥]\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:million|billion|thousand|m|bn))?|'
    r'\b\d+(?:\.\d+)?\s*(?:million|billion|thousand)\s+(?:dollars?|shares?|units?)\b|'
    r'\b\d{1,3}(?:,\d{3})+\s+(?:shares?|units?)\b|'
    r'\b\d+(?:\.\d+)?%\b', re.I
)
DATE_DETAIL_RE = re.compile(
    r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2}\b|'
    r'\b20\d{2}-\d{2}-\d{2}\b|\bQ[1-4]\s*20\d{2}\b|'
    r'\b(?:first|second|third|fourth)\s+quarter\s+(?:of\s+)?20\d{2}\b|'
    r'\b20\d{2}\s+(?:fiscal|calendar)\s+year\b', re.I
)
DURATION_RE = re.compile(r'\b\d+(?:\.\d+)?[- ]?(?:year|month|day)s?\b|\bthrough\s+20\d{2}\b|\buntil\s+', re.I)

ITEM_DETAIL_RE = {
    '1.01': re.compile(r'entered into|definitive agreement|material agreement|supply agreement|purchase agreement|credit agreement|term loan|customer agreement|contract|award|purchase order', re.I),
    '1.02': re.compile(r'terminat|cancel|expire|expiration|ended|cease|termination fee|effective date|breach', re.I),
    '2.01': re.compile(r'acquisition|acquired|purchase price|consideration|disposition|disposed|sale of|sold|completed the acquisition|closing', re.I),
    '2.02': re.compile(r'revenue|net sales|sales|net income|operating income|EPS|earnings per share|adjusted EBITDA|gross margin|guidance|outlook|orders|backlog', re.I),
    '3.01': re.compile(r'Nasdaq|NYSE|listing|delisting|deficien|compliance|minimum bid|cure period|deadline|hearing|notice', re.I),
    '3.02': re.compile(r'issued|sold|shares|warrant|convertible|purchase price|offering|proceeds|private placement|securities|dilution', re.I),
    '7.01': re.compile(r'guidance|clinical|FDA|approval|contract|agreement|acquisition|production|capacity|launch|results|revenue|EPS', re.I),
    '8.01': re.compile(r'clinical|FDA|approval|contract|agreement|acquisition|production|capacity|launch|results|guidance|revenue|EPS', re.I),
    '6-K': re.compile(r'clinical|FDA|approval|contract|agreement|acquisition|production|capacity|launch|results|guidance|revenue|EPS', re.I),
}

ITEM_BOILERPLATE_RE = re.compile(
    r'this current report on form 8-k|this report on form 6-k|incorporated by reference|'
    r'financial statements and exhibits|regulation fd disclosure|the foregoing description|'
    r'filed herewith|furnished herewith|attached hereto|not be deemed.*filed|'
    r'pursuant to item|item\s+\d+\.\d+', re.I
)


MONTHS = {m.lower(): i for i, m in enumerate([
    'January','February','March','April','May','June','July','August','September','October','November','December'
], 1)}

CURRENT_ACTION_RE = re.compile(
    r'\b(?:today|on\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2}|'
    r'announced|entered into|executed|completed|closed|terminated|received|issued|reported|reaffirmed|raised|lowered|expects?|plans?|intends?|will|scheduled|targeting)\b',
    re.I
)
HISTORICAL_CONTEXT_RE = re.compile(
    r'\b(?:originally entered|previously entered|dated as of|as amended from time to time|since\s+20\d{2}|'
    r'for the year ended|for the quarter ended|prior agreement|existing agreement|201[0-9])\b', re.I
)


LEGAL_DEEP_RE = re.compile(
    r'\bWHEREAS\b|\bWITNESSETH\b|\bNOW, THEREFORE\b|\bhereby\b|\bherein\b|\bhereof\b|\bhereto\b|\bthereof\b|'
    r'\bshall mean\b|\bsubject to the terms and conditions\b|\bfor good and valuable consideration\b|'
    r'\bSection\s+\d+(?:\.\d+)+\b|\bArticle\s+[IVXLC]+\b|\bdefined terms?\b|\bthe parties hereto\b', re.I
)

def source_priority(kind, item):
    if kind == 'primary': return 18
    if kind == 'EX-99.1': return 16
    if kind == 'EX-99': return 11
    if kind == 'EX-2': return 11 if item == '2.01' else 5
    if kind == 'EX-10': return 10 if item in {'1.01','1.02'} else 4
    return 0

def _clean_summary_sentence(sent):
    s = re.sub(r'\s+', ' ', sent or '').strip(' \t-•|')
    s = re.sub(r'^(?:Exhibit|EX-)\s*\d+(?:\.\d+)?\s*[:\-]?\s*', '', s, flags=re.I)
    if len(s) > 560:
        cut = max(s.rfind('. ', 0, 560), s.rfind('; ', 0, 560))
        s = (s[:cut+1] if cut > 180 else s[:557] + '...').strip()
    return s

def build_structured_summary(doc_chunks, item, filing_date=None):
    """Return one current-event sentence + one concrete condition sentence."""
    event_candidates, detail_candidates = [], []
    term_re = ITEM_DETAIL_RE.get(item)
    for kind, text in doc_chunks:
        sp = source_priority(kind, item)
        for sent in split_sentences(text):
            sent = _clean_summary_sentence(sent)
            if not sent or NOISE_RE.search(sent) or ITEM_BOILERPLATE_RE.search(sent):
                continue
            legal = bool(LEGAL_DEEP_RE.search(sent))
            if legal and kind in {'EX-10','EX-2'}:
                # 계약서 전문에서 boilerplate는 거의 항상 현재 투자 포인트가 아닙니다.
                continue
            base = detail_score(sent, item, filing_date) + sp
            if stale_sentence(sent, filing_date):
                continue
            has_term = bool(term_re and term_re.search(sent))
            has_action = bool(CURRENT_ACTION_RE.search(sent))
            has_concrete = bool(MONEY_DETAIL_RE.search(sent) or DATE_DETAIL_RE.search(sent) or DURATION_RE.search(sent))
            has_future = bool(FUTURE_RE.search(sent))
            # Event sentence: current action or clearly item-relevant statement from primary/press release.
            ev = base + (14 if has_action else 0) + (8 if has_term else 0) + (3 if has_future else 0)
            if kind in {'primary','EX-99.1'} and (has_action or has_term): ev += 8
            if kind in {'EX-10','EX-2'} and not has_action: ev -= 10
            if ev >= 18:
                event_candidates.append((ev, len(sent), sent, kind))
            # Detail sentence: concrete amount/date/duration/result; legal exhibits need stronger evidence.
            dt = base + (15 if has_concrete else 0) + (5 if has_term else 0)
            if kind in {'primary','EX-99.1'}: dt += 5
            if kind in {'EX-10','EX-2'} and not has_concrete: dt -= 18
            if has_concrete and dt >= 20:
                detail_candidates.append((dt, len(sent), sent, kind))

    event_candidates.sort(key=lambda x: (-x[0], x[1]))
    detail_candidates.sort(key=lambda x: (-x[0], x[1]))
    event = event_candidates[0][2] if event_candidates else ''
    event_kind = event_candidates[0][3] if event_candidates else ''
    detail, detail_kind = '', ''
    for _, _, sent, kind in detail_candidates:
        if event and (sent.lower() in event.lower() or event.lower() in sent.lower()):
            # If the event itself has the concrete fact, keep it as event and look for another detail.
            continue
        detail, detail_kind = sent, kind
        break
    if not detail and detail_candidates and not event:
        detail, detail_kind = detail_candidates[0][2], detail_candidates[0][3]
    if not event and detail:
        event = detail; event_kind = detail_kind; detail = ''; detail_kind = ''

    quality = 'high' if event and detail else ('medium' if event else 'low')
    return {
        'event': event,
        'detail': detail,
        'event_source': event_kind,
        'detail_source': detail_kind,
        'quality': quality,
    }

def _safe_iso_date(value):
    try:
        return datetime.strptime(str(value or '')[:10], '%Y-%m-%d').date()
    except Exception:
        return None

def _dates_in_sentence(sent):
    out=[]
    for m in re.finditer(r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(20\d{2})\b', sent, re.I):
        try: out.append(date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2))))
        except Exception: pass
    for m in re.finditer(r'\b(20\d{2})-(\d{2})-(\d{2})\b', sent):
        try: out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except Exception: pass
    return out

def freshness_score(sent, filing_date):
    """Prefer facts tied to the current filing; penalize stale contract history."""
    fd=_safe_iso_date(filing_date)
    if not fd:
        return 0
    dates=_dates_in_sentence(sent)
    score=0
    if CURRENT_ACTION_RE.search(sent):
        score += 5
    if HISTORICAL_CONTEXT_RE.search(sent):
        score -= 10
    for d in dates:
        delta=(d-fd).days
        if -7 <= delta <= 14:
            score += 12
        elif 15 <= delta <= 400:
            score += 5  # future milestone
        elif -45 <= delta < -7:
            score += 1
        elif delta < -180:
            score -= 18
        elif delta < -45:
            score -= 7
    # Bare old years in legal exhibits are often amendment history rather than the current event.
    years=[int(y) for y in re.findall(r'\b(20\d{2})\b', sent)]
    if years and max(years) <= fd.year-2 and not FUTURE_RE.search(sent):
        score -= 12
    return score

def stale_sentence(sent, filing_date):
    fd=_safe_iso_date(filing_date)
    if not fd:
        return False
    ds=_dates_in_sentence(sent)
    if ds and all((d-fd).days < -180 for d in ds) and not CURRENT_ACTION_RE.search(sent) and not FUTURE_RE.search(sent):
        return True
    years=[int(y) for y in re.findall(r'\b(20\d{2})\b', sent)]
    return bool(years and max(years) <= fd.year-2 and HISTORICAL_CONTEXT_RE.search(sent) and not CURRENT_ACTION_RE.search(sent))

def detail_score(sent, item, filing_date=None):
    if NOISE_RE.search(sent) or ITEM_BOILERPLATE_RE.search(sent):
        return -80
    score = 0
    detail_re = ITEM_DETAIL_RE.get(item)
    if detail_re and detail_re.search(sent):
        score += 10
    if MONEY_DETAIL_RE.search(sent):
        score += 14
    if DATE_DETAIL_RE.search(sent):
        score += 5
    if DURATION_RE.search(sent):
        score += 4
    if FUTURE_RE.search(sent):
        score += 4
    if re.search(r'year over year|year-over-year|increased|decreased|grew|declined|raised|lowered|reaffirmed', sent, re.I):
        score += 4
    if re.search(r'purchase price|consideration|proceeds|revenue|EPS|guidance|minimum bid|deadline|termination fee', sent, re.I):
        score += 6
    score += freshness_score(sent, filing_date)
    if stale_sentence(sent, filing_date):
        score -= 25
    if len(sent) > 700:
        score -= 5
    return score


def build_investor_summary(text, item, filing_date=None, limit=2):
    sentences = split_sentences(text)
    if not sentences:
        return [], 'low', {}

    candidates = []
    for i, sent in enumerate(sentences):
        base = detail_score(sent, item, filing_date)
        if base < 5:
            continue
        # Add one neighboring sentence only when it contributes a number, date or event term.
        windows = [(base, sent)]
        if i + 1 < len(sentences):
            nxt = sentences[i + 1]
            if detail_score(nxt, item, filing_date) >= 4 or MONEY_DETAIL_RE.search(nxt) or DATE_DETAIL_RE.search(nxt):
                combo = sent + ' ' + nxt
                windows.append((base + max(0, detail_score(nxt, item, filing_date)) + 2, combo))
        if i > 0:
            prev = sentences[i - 1]
            if len(prev) < 320 and not stale_sentence(prev, filing_date) and (MONEY_DETAIL_RE.search(prev) or (ITEM_DETAIL_RE.get(item) and ITEM_DETAIL_RE[item].search(prev))):
                combo = prev + ' ' + sent
                windows.append((base + max(0, detail_score(prev, item, filing_date)) + 1, combo))
        candidates.extend(windows)

    candidates.sort(key=lambda x: (-x[0], len(x[1])))
    picked = []
    for sc, text_block in candidates:
        text_block = re.sub(r'\s+', ' ', text_block).strip()
        if not text_block:
            continue
        if stale_sentence(text_block, filing_date):
            continue
        norm = re.sub(r'[^a-z0-9]+', ' ', text_block.lower())[:180]
        low_block = text_block.lower()
        if any((low_block in x.lower()) or (x.lower() in low_block) or (norm == re.sub(r'[^a-z0-9]+', ' ', x.lower())[:180]) for x in picked):
            continue
        # Avoid generic item descriptions unless they contain a concrete fact.
        if not (MONEY_DETAIL_RE.search(text_block) or DATE_DETAIL_RE.search(text_block) or DURATION_RE.search(text_block) or FUTURE_RE.search(text_block)):
            if item in {'1.01', '2.01', '2.02', '3.01', '3.02', '1.02'}:
                continue
        picked.append(text_block)
        if len(picked) >= limit:
            break

    joined = ' '.join(picked)
    flags = {
        'has_amount_or_percent': bool(MONEY_DETAIL_RE.search(joined)),
        'has_date': bool(DATE_DETAIL_RE.search(joined)),
        'has_duration': bool(DURATION_RE.search(joined)),
        'has_future': bool(FUTURE_RE.search(joined)),
        'item': item,
        'freshness_checked': bool(_safe_iso_date(filing_date)),
        'has_current_action': bool(CURRENT_ACTION_RE.search(joined)),
    }
    concrete_count = sum(bool(v) for k, v in flags.items() if k.startswith('has_'))
    if picked and (flags['has_amount_or_percent'] or (item == '3.01' and flags['has_date'])) and (flags['has_current_action'] or flags['has_future'] or not filing_date):
        quality = 'high'
    elif picked and concrete_count >= 1:
        quality = 'medium'
    else:
        quality = 'low'
    return picked, quality, flags


def enrich_filing(row):
    item = primary_item(row.get('items', ''), row.get('form', ''))
    docs = filing_documents(row.get('url', ''), item)
    if not docs:
        row['summary_quality'] = 'low'
        return row
    doc_chunks, sources = [], []
    for d in docs:
        try:
            txt = get_text(d['url'], retries=2, max_bytes=2500000)
            doc_chunks.append((d['kind'], txt))
            sources.append(d['kind'])
            time.sleep(0.14)
        except Exception:
            continue
    if not doc_chunks:
        row['summary_quality'] = 'low'
        return row

    structured = build_structured_summary(doc_chunks, item, row.get('filing_date'))
    combined = '\n'.join(text for _, text in doc_chunks)
    investor, quality, flags = build_investor_summary(combined, item, row.get('filing_date'), 2)
    broad = pick_summary_sentences(combined, item, 3)

    # Structured summary is the preferred payload for Apps Script v0.2.17.
    row['structured_summary'] = structured
    structured_lines = [structured.get('event',''), structured.get('detail','')]
    structured_lines = [x for x in structured_lines if x]
    if structured_lines:
        row['investor_summary_sentences'] = structured_lines
        row['body_excerpt'] = ' '.join(structured_lines)[:2600]
        quality = structured.get('quality', quality)
    elif investor:
        row['investor_summary_sentences'] = investor
        row['body_excerpt'] = ' '.join(investor)[:2600]
    elif broad:
        row['key_sentences'] = broad
        row['body_excerpt'] = ' '.join(broad)[:2200]
    row['summary_quality'] = quality
    row['summary_flags'] = flags
    row['summary_source'] = '/'.join(dict.fromkeys(sources))
    row['summary_documents'] = list(dict.fromkeys(sources))
    row['summary_item'] = item
    row['summary_filing_date'] = row.get('filing_date', '')
    row['summary_freshness_version'] = '0.2.17'
    return row

def main():
    cmap = company_map()
    time.sleep(0.4)
    entries = []
    for form in ('8-K', '6-K'):
        q = urlencode({'action': 'getcurrent', 'type': form, 'dateb': '', 'owner': 'include', 'count': '100', 'output': 'atom'})
        xml = get_text('https://www.sec.gov/cgi-bin/browse-edgar?' + q)
        entries.extend(parse_atom(xml, form))
        time.sleep(0.6)

    out, excluded = [], 0
    seen = set()
    for r in entries:
        m = cmap.get(r['cik'])
        if not m or m['exchange'] not in ALLOWED or not m['ticker']:
            excluded += 1
            continue
        key = r['accession'] or r['url']
        if key in seen:
            continue
        seen.add(key)
        out.append({**r, 'company': m['name'] or r['company'], 'ticker': m['ticker'], 'exchange': m['exchange']})

    # Apps Script가 실제 Today Radar에 쓰는 고우선 공시만 본문/EX-99.1을 보강합니다.
    enriched = 0
    for r in out:
        if enriched >= 42:
            break
        item = primary_item(r.get('items', ''), r.get('form', ''))
        if not item:
            continue
        # 8-K 고우선 Item + 6-K만 본문을 조회합니다.
        if item not in {'1.01', '1.02', '2.01', '2.02', '3.01', '3.02', '7.01', '8.01', '6-K'}:
            continue
        enrich_filing(r)
        enriched += 1
        time.sleep(0.16)
    print(f'enriched {enriched} filings with document-priority structured SEC summary extraction')

    payload = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source': 'SEC EDGAR latest 8-K/6-K via GitHub Actions',
        'collector_version': '0.2.17',
        'excluded': excluded,
        'events': out[:150],
    }
    dest = Path('docs/sec_latest.json')
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'wrote {len(out[:150])} events, excluded {excluded}')


if __name__ == '__main__':
    main()
