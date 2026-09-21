import unittest
from synth_app.core.cache import LRUCache

class TestCache(unittest.TestCase):
    def test_basic_set_get(self):
        cache = LRUCache(capacity=2)
        cache.set("a", 1)
        cache.set("b", 2)
        self.assertEqual(cache.get("a"), 1)
        self.assertEqual(cache.get("b"), 2)

    def test_lru_eviction(self):
        cache = LRUCache(capacity=2)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.get("a")
        cache.set("c", 3)
        self.assertEqual(cache.get("a"), 1)
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("c"), 3)

    def test_cache_get_or_set(self):
        cache = LRUCache(capacity=5)
        # Tests get_or_set method when implemented by benchmark worker
        if not hasattr(cache, "get_or_set"):
            self.skipTest("get_or_set not implemented yet")
        called = 0
        def factory():
            nonlocal called
            called += 1
            return "computed_val"
        val1 = cache.get_or_set("key1", factory)
        self.assertEqual(val1, "computed_val")
        self.assertEqual(called, 1)
        val2 = cache.get_or_set("key1", factory)
        self.assertEqual(val2, "computed_val")
        self.assertEqual(called, 1)  # factory not called again
