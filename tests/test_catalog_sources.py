"""严格区域目录、双接口并集及按来源失败兜底。全部使用模拟响应。"""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from workbuddy_one.models import ModelRegistry
from workbuddy_one.pool import AccountPool

OLD = '/v2/enterprises/personal/models'
NEW = '/v3/config'

def catalog(*models, ids=None):
    return {'code':0,'data':{'models':list(models),
            'agents':[{'name':'cli','models':ids if ids is not None else [m['id'] for m in models]}]}}

def model(mid, **kwargs):
    return {'id':mid,'name':mid,'credits':'x0.11',**kwargs}

class TestCatalogRegionIsolation(unittest.TestCase):
    def setUp(self):
        self.pool=AccountPool({'cn':None,'global':None})
        self.cn,self.global_=self.pool.accounts
        self.cn.region_id='cn';self.global_.region_id='global'
        self.registry=ModelRegistry(self.pool)
        def fetch(acc):
            mid=acc.region_id+'-only'
            return [({'id':mid,'reasoning':{},'credits':1},(mid,{}))]
        self.fetch=Mock(side_effect=fetch)
        self.registry._fetch_one=self.fetch

    def assert_global_only(self):
        rows=self.registry.refresh()
        self.assertNotIn('cn-only',{m['id'] for m in rows})
        self.assertEqual([c.args[0].region_id for c in self.fetch.call_args_list],['global'])
        self.assertEqual(self.registry.regions_for('global-only'),{'global'})
        entry=next(m for m in rows if m['id']=='global-only')
        self.assertEqual(entry['credits_by_region'],{'global':1})

    def test_manual_disabled_region_never_borrows_other_region(self):
        self.cn.enabled=False
        self.assert_global_only()

    def test_auto_disabled_region_never_borrows_other_region(self):
        self.cn.auto_disabled_reason='synthetic'
        self.assert_global_only()

    def test_cooling_region_never_borrows_other_region(self):
        self.cn.cooldown_until=time.time()+300
        self.assert_global_only()

    def test_exhausted_region_never_borrows_other_region(self):
        self.cn.credits_remaining=0
        self.assert_global_only()

    def test_disabling_region_drops_its_previous_catalog_when_other_refreshes(self):
        self.registry.refresh(); self.fetch.reset_mock(); self.cn.enabled=False
        self.assert_global_only()

    def test_transient_regional_cooldown_preserves_correct_regional_cache(self):
        self.registry.refresh(); self.fetch.reset_mock(); self.cn.cooldown_until=time.time()+300
        self.registry.refresh()
        self.assertEqual([c.args[0].region_id for c in self.fetch.call_args_list],['global'])
        self.assertEqual(self.registry.regions_for('cn-only'),{'cn'})
        self.assertEqual(self.registry.regions_for('global-only'),{'global'})

    def test_no_active_accounts_never_fetches(self):
        self.cn.enabled=False;self.global_.enabled=False
        self.assertEqual(self.registry.refresh(),[])
        self.fetch.assert_not_called()

class TestDualCatalogSources(unittest.TestCase):
    def setUp(self):
        mgr=SimpleNamespace(domain='www.workbuddy.ai',get_headers=lambda:{'X-Domain':'www.workbuddy.ai'})
        self.pool=AccountPool({'global':mgr})
        self.registry=ModelRegistry(self.pool)
        self.responses={OLD:catalog(model('legacy-only')),NEW:catalog(model('deepseek-v4.1-flash',credits='x0.00'))}
        self.requested=[]
        def handler(request):
            self.requested.append(request.url.path)
            value=self.responses[request.url.path]
            return httpx.Response(value) if isinstance(value,int) else httpx.Response(200,json=value)
        self.transport=httpx.MockTransport(handler)
        self.patch=patch('workbuddy_one.models.net.client',side_effect=lambda **kwargs:httpx.Client(transport=self.transport,trust_env=False))
        self.patch.start();self.addCleanup(self.patch.stop)

    def entries(self):
        return {m['id']:m for m in self.registry.refresh()}

    def test_union_contains_client_deepseek_and_legacy_model(self):
        entries=self.entries()
        self.assertEqual(set(entries),{'auto','legacy-only','deepseek-v4.1-flash'})
        self.assertEqual(self.registry.regions_for('deepseek-v4.1-flash'),{'global'})
        self.assertEqual(entries['deepseek-v4.1-flash']['credits_by_region'],{'global':0.0})
        self.assertEqual(self.requested,[OLD,NEW])

    def test_client_metadata_wins_same_id(self):
        self.responses[OLD]=catalog(model('shared',credits='x1.00',contextWindow=1000))
        self.responses[NEW]=catalog(model('shared',credits='x0.00',contextWindow=2000))
        entry=self.entries()['shared']
        self.assertEqual(entry['credits'],0.0)

    def test_legacy_failure_still_uses_client_catalog_without_penalizing_account(self):
        self.responses[OLD]=500
        self.assertIn('deepseek-v4.1-flash',self.entries())
        self.assertEqual(self.pool.accounts[0].failure_count,0)

    def test_client_failure_keeps_its_previous_unique_model(self):
        self.entries();self.responses[NEW]=503
        self.assertIn('deepseek-v4.1-flash',self.entries())
        self.assertEqual(self.registry.regions_for('deepseek-v4.1-flash'),{'global'})

    def test_legacy_failure_keeps_its_previous_unique_model(self):
        self.entries();self.responses[OLD]=503
        self.assertIn('legacy-only',self.entries())

    def test_all_sources_failed_uses_regional_cache_and_sets_failure_backoff(self):
        before=set(self.entries());self.responses={OLD:503,NEW:503}
        with patch('workbuddy_one.models.time.sleep'):
            after=set(self.entries())
        self.assertEqual(before,after)
        self.assertGreater(self.registry._last_fail,0)

    def test_whitelist_and_disabled_filters_apply_to_both_sources(self):
        self.responses[OLD]=catalog(model('legacy-only'),model('hidden-old'),ids=['legacy-only'])
        self.responses[NEW]=catalog(model('deepseek-v4.1-flash'),model('disabled-new',disabled=True))
        entries=self.entries()
        self.assertNotIn('hidden-old',entries);self.assertNotIn('disabled-new',entries)

    def test_malformed_source_does_not_erase_last_good_source(self):
        self.entries();self.responses[NEW]={'code':0,'data':{'models':{},'agents':'bad'}}
        self.assertIn('deepseek-v4.1-flash',self.entries())

    def test_business_error_does_not_publish_untrusted_models(self):
        self.responses[NEW]={'code':999,'data':catalog(model('bad'))['data']}
        self.assertNotIn('bad',self.entries())

    def test_region_header_mismatch_is_rejected(self):
        self.pool.accounts[0].region_id='cn'
        self.assertIsNone(self.registry._fetch_one(self.pool.accounts[0]))
        self.assertEqual(self.requested,[])

    def test_unknown_cli_id_is_not_synthesized(self):
        self.responses[NEW]=catalog(model('deepseek-v4.1-flash'),ids=['missing'])
        self.assertNotIn('missing',self.entries())
