import unittest
import numpy as np
from .qasper_dense import normalize,dense_rank,fuse_rankings
from .qasper_dense_eval import paired_cluster_interval


class DenseTests(unittest.TestCase):
    def test_norm_ranking_and_bad_vectors(self):
        rows=[{'id':'b'},{'id':'a'},{'id':'c'}]
        v=np.array([[3,0],[3,0],[0,4]])
        self.assertTrue(np.allclose(np.linalg.norm(normalize(v),axis=1),1))
        self.assertEqual([r['id'] for r in dense_rank(rows,v,[1,0])],['a','b','c'])
        for bad in ([[0,0]],[[float('nan'),1]]):
            with self.assertRaises(ValueError):normalize(bad)
        with self.assertRaises(ValueError):dense_rank(rows[:1],v,[1,0])
        with self.assertRaises(ValueError):dense_rank(rows,v,[1,0,0])

    def test_fusion_is_order_symmetric_and_deterministic(self):
        a=[{'id':'a'},{'id':'b'}];b=[{'id':'b'},{'id':'c'}]
        self.assertEqual(fuse_rankings(a,b),fuse_rankings(b,a))
        self.assertEqual(fuse_rankings(a,b)[0]['id'],'b')
        with self.assertRaises(ValueError):fuse_rankings(a+a,b)

    def test_cluster_estimator_weights_questions_not_paper_means(self):
        rows=[{'paper_id':pid,'bm25':{'m':0.0},'bge':{'m':value}}
              for pid,value in [('a',1.),('b',0.),('b',0.),('b',0.)]]
        result=paired_cluster_interval(rows,'bge','m',draws=1000)
        self.assertEqual(result['delta'],.25)
        self.assertEqual(result['questions'],4)
        self.assertEqual(result['papers'],2)
        self.assertEqual(result,paired_cluster_interval(rows,'bge','m',draws=1000))


if __name__=='__main__':unittest.main()
