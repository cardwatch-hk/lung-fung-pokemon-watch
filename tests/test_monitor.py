import copy
import tempfile
import unittest
from pathlib import Path
from monitor import parse_product, parse_listing, evolve, empty_state, deliver, FileStore, GitHubStore, IssueNotifier
import json
import base64
from urllib.error import HTTPError

URL='https://eshop.lungfung.hk/product/abc/'
def page(stock='in-stock', price='<span class="amount">$388.00</span>'):
    return f'<div class="summary entry-summary"><h1 class="product_title">寶可夢 超級交響樂卡包 M1S</h1><p class="price">{price}</p><p class="stock {stock}">stock</p><button class="single_add_to_cart_button">buy</button></div>'
def product(price='388.00', stock='in_stock'):
    return dict(url=URL,name='寶可夢 超級交響樂卡包 M1S',price=price,stock=stock)

class ParsingTests(unittest.TestCase):
    def test_real_lungfung_m1s_summary(self):
        raw=(Path(__file__).parent/'fixtures/lungfung_m1s_summary.html').read_bytes()
        p=parse_product(raw,'https://eshop.lungfung.hk/product/msn260257/')
        self.assertEqual(p['price'],'388.00')
        self.assertEqual(p['stock'],'out_of_stock')
        self.assertIn('超級交響樂',p['name'])
    def test_sale_price_not_original_or_recommended_price(self):
        p=parse_product(page(price='<del><span class="amount">$500.00</span></del><ins><span class="amount">$388.00</span></ins>')+'<div class="related"><p class="stock out-of-stock">sold</p><p class="price">$1.00</p></div>',URL)
        self.assertEqual((p['price'],p['stock']),('388.00','in_stock'))
    def test_product_sold_out_overrules_button(self):
        self.assertEqual(parse_product(page('out-of-stock'),URL)['stock'],'out_of_stock')
    def test_missing_stock_and_ranged_price_are_unknown(self):
        p=parse_product(page(price='<span class="amount">$100</span> – <span class="amount">$200</span>').replace('class="stock in-stock"','class="other"'),URL)
        self.assertIsNone(p['price']); self.assertEqual(p['stock'],'unknown')
    def test_wrong_page_fails_closed(self):
        with self.assertRaises(ValueError): parse_product('<h1>Access denied</h1>',URL)
    def test_listing_follows_pagination_and_rejects_external(self):
        html=f'<ul class="products"><li class="product"><a href="{URL}">寶可夢卡包</a><a href="https://evil.example/product/x/">x</a></li></ul><nav class="woocommerce-pagination"><a href="https://eshop.lungfung.hk/brand/pokemon/page/2/">2</a></nav>'
        urls,pages=parse_listing(html,'https://eshop.lungfung.hk/brand/pokemon/')
        self.assertEqual(urls,{URL}); self.assertEqual(len(pages),1)
    def test_pokemon_merchandise_is_excluded_even_with_brand_category(self):
        raw=f'<ul class="products"><li class="product"><p>寶可夢卡牌</p><div class="woocommerce-loop-product__title"><a href="{URL}">KAO 蒸氣眼膜 (POKEMON SLEEP)</a></div></li></ul>'
        self.assertEqual(parse_listing(raw,'https://eshop.lungfung.hk/')[0],set())
        with self.assertRaises(ValueError):
            parse_product(page().replace('寶可夢 超級交響樂卡包 M1S','KAO 蒸氣眼膜 (POKEMON SLEEP)'),URL)

class StateTests(unittest.TestCase):
    def test_merchandise_cleanup_preserves_card_history_and_pending_alert(self):
        s=evolve(empty_state(),[product(stock='out_of_stock')],'2026-10-09')
        s=evolve(s,[product()],'2026-10-09')
        s['products'][URL+'eye/']=dict(product(),url=URL+'eye/',name='KAO 眼膜 (POKEMON SLEEP)')
        clean=evolve(s,[product()],'2026-10-09')
        self.assertEqual(len(clean['products']),1)
        self.assertEqual(clean['outbox'],s['outbox'])
    def test_baseline_is_silent_and_repeat_is_deduplicated(self):
        s=evolve(empty_state(),[product()],'2026-10-09')
        self.assertEqual(s['outbox'],[])
        self.assertEqual(evolve(s,[product()],'2026-10-09'),s)
    def test_new_restock_price_and_return_to_old_price(self):
        s=evolve(empty_state(),[product(stock='out_of_stock')],'2026-10-09')
        s=evolve(s,[product('400.00')],'2026-10-09')
        self.assertEqual(s['outbox'][0]['kinds'],['restock','price'])
        s=evolve(s,[product('388.00'),dict(product(),url=URL+'new/')],'2026-10-09')
        self.assertEqual(len(s['outbox']),3)
        self.assertEqual(len({e['id'] for e in s['outbox']}),3)
    def test_unknown_and_missing_do_not_erase_known_history(self):
        s=evolve(empty_state(),[product(stock='out_of_stock')],'2026-10-09')
        s=evolve(s,[product(None,'unknown')],'2026-10-09')
        self.assertEqual(s['products'][URL]['stock'],'out_of_stock')
        s=evolve(s,[],'2026-10-09')
        self.assertIn(URL,s['products'])
        s=evolve(s,[product()],'2026-10-09')
        self.assertEqual(s['outbox'][0]['kinds'],['restock'])
    def test_corrupt_state_is_not_reset(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.json';path.write_text('{broken')
            with self.assertRaises(ValueError): FileStore(path).load()
    def test_delivery_retries_and_ack_is_saved(self):
        with tempfile.TemporaryDirectory() as d:
            store=FileStore(Path(d)/'state.json')
            s=evolve(empty_state(),[product(stock='out_of_stock')],'2026-10-09')
            s=evolve(s,[product()],'2026-10-09');store.save(s)
            class Failing:
                def send(self,event): raise OSError('offline')
            with self.assertRaises(OSError): deliver(s,store,Failing())
            self.assertEqual(len(store.load()['outbox']),1)
            class Success:
                def send(self,event): return 'https://github.com/example/watch/issues/1'
            deliver(store.load(),store,Success())
            self.assertEqual(store.load()['outbox'],[])
            self.assertEqual(len(store.load()['delivered']),1)

class CloudTests(unittest.TestCase):
    def test_existing_closed_issue_acknowledges_crash_without_duplicate(self):
        event=dict(id='abc123',kinds=['restock'],before=product(stock='out_of_stock'),after=product(),observed_at='2026-10-09T12:00:00+00:00')
        class API:
            def request(self,method,path,payload=None):
                if method!='GET': raise AssertionError('Would create duplicate issue')
                return [dict(body='<!-- lung-fung-event:abc123 -->',state='closed',html_url='https://github.com/example/watch/issues/1')]
        self.assertEqual(IssueNotifier(API()).send(event),'https://github.com/example/watch/issues/1')
    def test_issue_lookup_paginates_before_creating_cantonese_alert(self):
        event=dict(id='abc123',kinds=['price'],before=product(),after=product('400.00'),observed_at='2026-10-09T12:00:00+00:00')
        calls=[]
        class API:
            def request(self,method,path,payload=None):
                calls.append((method,path,payload))
                if method=='GET': return [dict(body='unrelated')]*100 if '&page=1' == path[-7:] else []
                return dict(html_url='https://github.com/example/watch/issues/2')
        self.assertEqual(IssueNotifier(API()).send(event),'https://github.com/example/watch/issues/2')
        self.assertEqual(len(calls),3)
        self.assertIn('HK$388.00 → HK$400.00',calls[-1][2]['body'])
        self.assertIn('20:00:00 HKT',calls[-1][2]['body'])
    def test_private_repository_refused_before_state_or_notifications(self):
        class API:
            def request(self,method,path,payload=None):
                if path: raise AssertionError('Private repo used')
                return dict(private=True,visibility='private',default_branch='main')
        with self.assertRaises(ValueError): GitHubStore(API())
    def test_remote_state_missing_is_not_reinitialized(self):
        class API:
            def request(self,method,path,payload=None):
                if not path: return dict(private=False,visibility='public',default_branch='main')
                raise HTTPError('https://api.github.com',404,'Not Found',{},None)
        with self.assertRaises(HTTPError): GitHubStore(API()).load()
    def test_remote_state_noop_and_compare_and_swap(self):
        state=empty_state(); raw=json.dumps(state,ensure_ascii=False,sort_keys=True,indent=2)+'\n'
        writes=[]
        class API:
            def request(self,method,path,payload=None):
                if not path: return dict(private=False,visibility='public',default_branch='main')
                if method=='GET':return dict(sha='first',content=base64.b64encode(raw.encode()).decode())
                writes.append(payload);return dict(content=dict(sha='second'))
        store=GitHubStore(API()); loaded=store.load();store.save(loaded)
        self.assertEqual(writes,[])
        loaded['heartbeat']='2026-10-09';store.save(loaded)
        self.assertEqual(writes[0]['sha'],'first')
        self.assertEqual(store.sha,'second')

if __name__=='__main__':unittest.main()
