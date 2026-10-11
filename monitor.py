"""Public-page observations, durable outbox, GitHub Issue email notifications."""
import argparse
import base64
import copy
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit, urlencode
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

BASE = 'https://eshop.lungfung.hk'
AGENT = 'LungFungPokemonWatch/1.0 (public stock and price observations)'
SOURCES = [BASE+'/brand/pokemon/', BASE+'/product-category/pokemon/',
           BASE+'/?'+urlencode({'s':'POKEMON','post_type':'product'}),
           BASE+'/?'+urlencode({'s':'寶可夢','post_type':'product'})]
POKEMON = re.compile(r'pok[eé]mon|寶可夢|精靈寶可夢|寵物小精靈', re.I)
TCG = re.compile(r'卡包|卡盒|卡牌|集換|擴充包|補充包|起始套組|套牌|牌組|\bTCG\b|card\s*game|booster|deck', re.I)

def is_tcg_name(name):
    return bool(POKEMON.search(name) and TCG.search(name))

def safe_url(url, base=BASE):
    p=urlsplit(urljoin(base,url))
    if p.scheme!='https' or p.netloc!='eshop.lungfung.hk' or p.username:
        raise ValueError('Unexpected shop URL')
    return urlunsplit((p.scheme,p.netloc,p.path,p.query,''))

def parse_listing(raw, url):
    soup=BeautifulSoup(raw,'html.parser')
    cards=soup.select('ul.products li.product')
    if not cards and not soup.select('.woocommerce-info, .woocommerce-no-products-found'):
        raise ValueError('Unrecognized listing; refusing empty discovery')
    products=set()
    for card in cards:
        title=card.select_one('.woocommerce-loop-product__title')
        if not is_tcg_name((title or card).get_text(' ',strip=True)):
            continue
        for a in card.select('a[href]'):
            try: target=safe_url(a['href'],url)
            except ValueError: continue
            if re.fullmatch(r'/product/[^/]+/',urlsplit(target).path):
                p=urlsplit(target)
                products.add(urlunsplit((p.scheme,p.netloc,p.path,'','')))
    pages=set()
    for a in soup.select('.woocommerce-pagination a[href]'):
        try: pages.add(safe_url(a['href'],url))
        except ValueError: continue
    return products,pages

def parse_product(raw, url):
    soup=BeautifulSoup(raw,'html.parser')
    summary=soup.select_one('.summary.entry-summary')
    title=summary.select_one('h1.product_title') if summary else None
    if not title or not is_tcg_name(title.get_text()):
        raise ValueError('Unrecognized Pokemon product page')
    name=title.get_text(' ',strip=True)
    price=None
    price_node=summary.find(['p','span'],class_='price',recursive=False)
    if price_node:
        sale=price_node.select_one('ins')
        amounts=(sale or price_node).select('.amount')
        amounts=[a for a in amounts if not a.find_parent('del')]
        if len(amounts)==1:
            symbols=[x.get_text(strip=True) for x in amounts[0].select('.woocommerce-Price-currencySymbol')]
            text=amounts[0].get_text('',strip=True)
            if not symbols or all(s in ('$', 'HK$', 'HKD') for s in symbols):
                number=re.sub(r'(HKD|HK\$|\$|,|\s)','',text)
                try:
                    value=Decimal(number)
                    if value.is_finite() and value>=0:
                        price=format(value.quantize(Decimal('.01')),'f')
                except InvalidOperation: pass
    stock='unknown'
    nodes=summary.find_all(class_='stock',recursive=False)
    classes={c for n in nodes for c in n.get('class',[])}
    if 'out-of-stock' in classes: stock='out_of_stock'
    elif 'in-stock' in classes and 'available-on-backorder' not in classes: stock='in_stock'
    elif 'available-on-backorder' in classes: stock='backorder'
    return dict(url=safe_url(url),name=name,price=price,stock=stock)

class Shop:
    def __init__(self):
        self.started=time.monotonic()
        self.last_request=0
        self.robot=RobotFileParser()
        self.robot.parse(self.get(BASE+'/robots.txt',check=False).decode('utf-8').splitlines())
    def get(self,url,check=True):
        safe_url(url)
        if time.monotonic()-self.started>240: raise TimeoutError('Scan budget exceeded')
        if check and not self.robot.can_fetch(AGENT,url):
            raise ValueError('robots.txt disallows this URL')
        time.sleep(max(0,1-(time.monotonic()-self.last_request)))
        self.last_request=time.monotonic()
        for attempt in range(2):
            try:
                with urlopen(Request(url,headers={'User-Agent':AGENT,'Accept':'text/html'}),timeout=25) as r:
                    safe_url(r.url)
                    raw=r.read(3_000_001)
                if len(raw)>3_000_000: raise ValueError('Oversized shop response')
                return raw
            except HTTPError as e:
                if e.code not in (429,500,502,503,504) or attempt: raise
                time.sleep(3)
            except (URLError,TimeoutError):
                if attempt: raise
                time.sleep(2)
    def scan(self, known):
        queue=list(SOURCES); seen=set(); urls=set()
        while queue:
            url=queue.pop(0)
            if url in seen: continue
            if len(seen)>=20: raise ValueError('Pagination limit; discovery incomplete')
            seen.add(url)
            found,pages=parse_listing(self.get(url),url)
            urls.update(found); queue.extend(sorted(pages-seen))
        if not urls: raise ValueError('No products discovered; refusing to reset state')
        urls.update(url for url,p in known.items() if is_tcg_name(p['name']))
        if len(urls)>100: raise ValueError('Product limit; review before increasing traffic')
        # Retain delisted product history; a removed detail page is not a sold-out signal.
        observations=[]; missing=[]
        for url in sorted(urls):
            try: observations.append(parse_product(self.get(url),url))
            except HTTPError as e:
                if e.code in (404,410) and url in known: missing.append(url)
                else: raise
        return observations,dict(discovery_pages=len(seen),discovered=len(urls),removed=len(missing))

def empty_state():
    return dict(schema=1,initialized=False,sequence=0,products={},outbox=[],delivered=[],heartbeat=None)

def validate(state):
    if not isinstance(state,dict) or state.get('schema')!=1:
        raise ValueError('Invalid state schema; refusing silent reset')
    for key,kind in [('initialized',bool),('sequence',int),('products',dict),('outbox',list),('delivered',list)]:
        if not isinstance(state.get(key),kind): raise ValueError('Invalid state field: '+key)
    for url,p in state['products'].items():
        safe_url(url)
        if p.get('url')!=url or p.get('stock') not in ('in_stock','out_of_stock','backorder','unknown'):
            raise ValueError('Invalid product state')
        if p.get('price') is not None and not re.fullmatch(r'\d+\.\d{2}',p['price']):
            raise ValueError('Invalid price state')
    ids=[]
    for event in state['outbox']:
        if not all(k in event for k in ('id','kinds','before','after','observed_at')):
            raise ValueError('Invalid pending event')
        ids.append(event['id'])
    if len(ids)!=len(set(ids)): raise ValueError('Duplicate pending event IDs')
    return state

def evolve(state, observations, day):
    result=copy.deepcopy(validate(state))
    # Narrow old broad Pokemon searches without resetting TCG history or outbox.
    result['products']={url:p for url,p in result['products'].items() if is_tcg_name(p['name'])}
    for p in observations:
        url=p['url']; old=result['products'].get(url)
        kinds=[]
        if result['initialized']:
            if old is None: kinds=['new']
            else:
                if old['stock']=='out_of_stock' and p['stock']=='in_stock': kinds.append('restock')
                if old['price'] is not None and p['price'] is not None and old['price']!=p['price']:
                    kinds.append('price')
        if kinds:
            result['sequence']+=1
            digest=hashlib.sha256(json.dumps([result['sequence'],old,p,kinds],sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]
            result['outbox'].append(dict(id=digest,kinds=kinds,before=old,after=copy.deepcopy(p),observed_at=datetime.now(timezone.utc).isoformat()))
        merged=copy.deepcopy(p)
        if old:
            if p['price'] is None: merged['price']=old['price']
            if p['stock']=='unknown': merged['stock']=old['stock']
        result['products'][url]=merged
    result['initialized']=True
    result['heartbeat']=day
    return validate(result)

class FileStore:
    def __init__(self,path): self.path=Path(path)
    def load(self):
        if not self.path.exists(): return empty_state()
        return validate(json.loads(self.path.read_text(encoding='utf-8')))
    def save(self,state):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(validate(state),ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8')
        temp.replace(self.path)

class GitHub:
    def __init__(self,repo,token):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo): raise ValueError('Invalid repository')
        if not token: raise ValueError('GITHUB_TOKEN is missing')
        self.root='https://api.github.com/repos/'+repo
        self.token=token
    def request(self,method,path,payload=None):
        data=json.dumps(payload).encode() if payload is not None else None
        headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json',
                 'X-GitHub-Api-Version':'2022-11-28','User-Agent':AGENT,'Content-Type':'application/json'}
        # Never retry a mutation blindly: the next run reconciles the durable event ID.
        with urlopen(Request(self.root+path,data=data,headers=headers,method=method),timeout=30) as r:
            return json.load(r)

class GitHubStore:
    def __init__(self,api):
        self.api=api; self.sha=None; self.serialized=None
        repo=api.request('GET','')
        if repo.get('private') or repo.get('visibility')!='public':
            raise ValueError('Zero-cost guard: monitoring requires a public repository')
        self.branch=repo['default_branch']
    def load(self):
        # Missing remote state is an error, not an invitation to forget alert history.
        data=self.api.request('GET','/contents/data/state.json?'+urlencode({'ref':self.branch}))
        self.sha=data['sha']
        self.serialized=base64.b64decode(data['content']).decode('utf-8')
        return validate(json.loads(self.serialized))
    def save(self,state):
        raw=json.dumps(validate(state),ensure_ascii=False,sort_keys=True,indent=2)+'\n'
        if raw==self.serialized: return
        data=self.api.request('PUT','/contents/data/state.json',dict(message='monitor: persist state and notification outbox',content=base64.b64encode(raw.encode()).decode(),sha=self.sha,branch=self.branch))
        self.sha=data['content']['sha']; self.serialized=raw

def alert_text(event):
    p=event['after']; before=event['before']
    labels={'new':'新發現產品','restock':'由售完轉為有貨','price':'價格變動','test':'TEST 測試通知'}
    kinds=' / '.join(labels[k] for k in event['kinds'])
    name=html.escape(re.sub(r'\s*\(下單.*','',p['name'])).replace('@','＠')
    title=f'龍豐 {kinds}｜{name[:100]} [LF:{event["id"]}]'
    stocks={'in_stock':'有貨','out_of_stock':'已售完','backorder':'接受預訂／缺貨待訂','unknown':'未能確認'}
    price='HK$'+p['price'] if p['price'] is not None else '未能確認'
    lines=[f'**{kinds}**', '',f'產品：{name}',f'價格：{price}',f'庫存：{stocks[p["stock"]]}']
    if 'price' in event['kinds']: lines.append(f'價格變動：HK${before["price"]} → HK${p["price"]}')
    stamp=datetime.fromisoformat(event['observed_at']).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S HKT')
    lines += [f'發現時間：{stamp}', f'官方產品頁：{p["url"]}', '',
              '「新發現」只代表首次被此監察記錄，唔代表剛上架。庫存以網站檢查時顯示為準。',
              f'<!-- lung-fung-event:{event["id"]} -->']
    return title,'\n\n'.join(lines)

class IssueNotifier:
    def __init__(self,api): self.api=api
    def send(self,event):
        marker='<!-- lung-fung-event:'+event['id']+' -->'
        # Search listing rather than eventually consistent search index; includes closed issues.
        for page in range(1,101):
            issues=self.api.request('GET',f'/issues?state=all&sort=created&direction=desc&per_page=100&page={page}')
            for issue in issues:
                if marker in (issue.get('body') or '') and not issue.get('pull_request'):
                    return issue['html_url']
            if len(issues)<100: break
        else: raise ValueError('Issue reconciliation limit exceeded; refusing possible duplicate')
        title,body=alert_text(event)
        return self.api.request('POST','/issues',dict(title=title,body=body))['html_url']

def deliver(state,store,notifier):
    while state['outbox']:
        event=state['outbox'][0]
        url=notifier.send(event)
        state['outbox'].pop(0)
        state['delivered'].append(dict(id=event['id'],issue=url))
        state['delivered']=state['delivered'][-500:]
        store.save(state)
    return state

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--state',default='local-state.json')
    parser.add_argument('--local',action='store_true')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--test-alert',action='store_true')
    args=parser.parse_args()
    api=None
    if args.local or args.dry_run: store=FileStore(args.state)
    else:
        api=GitHub(os.environ.get('GITHUB_REPOSITORY',''),os.environ.get('GITHUB_TOKEN',''))
        store=GitHubStore(api)
    state=store.load()
    if args.test_alert:
        event_id='setup-test-v1'
        if not any(e['id']==event_id for e in state['outbox']+state['delivered']):
            state['outbox'].append(dict(id=event_id,kinds=['test'],before=None,after=dict(url=BASE+'/brand/pokemon/',name='Email 測試（唔係補貨通知）',price=None,stock='unknown'),observed_at=datetime.now(timezone.utc).isoformat()))
    else:
        observations,stats=Shop().scan(state['products'])
        state=evolve(state,observations,datetime.now(timezone.utc).date().isoformat())
        print(json.dumps(dict(stats,observations=observations,pending=len(state['outbox'])),ensure_ascii=False,indent=2))
    if args.dry_run:
        print('DRY RUN: no state writes or notifications');return
    store.save(state) # Outbox checkpoint MUST succeed before any external notification.
    if api: deliver(state,store,IssueNotifier(api))
    else: print('LOCAL: state saved; cloud/email not active')

if __name__=='__main__':
    try: main()
    except Exception as e:
        # Do not dump request headers, tokens, secrets, or API response bodies to public logs.
        detail=f' HTTP {e.code}' if isinstance(e,HTTPError) else ''
        print('Monitor failed: '+type(e).__name__+detail,file=sys.stderr)
        sys.exit(1)
