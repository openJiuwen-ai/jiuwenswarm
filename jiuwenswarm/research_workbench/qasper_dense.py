"""Pinned local BGE ONNX baseline; no network, model training or gold access."""
import hashlib
import json
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

PREFIX='Represent this sentence for searching relevant passages: '


def normalize(vectors):
    vectors=np.asarray(vectors,dtype=np.float32)
    if vectors.ndim!=2 or not np.isfinite(vectors).all():raise ValueError('Invalid embeddings')
    norms=np.linalg.norm(vectors,axis=1,keepdims=True)
    if np.any(norms==0):raise ValueError('Zero embedding')
    return vectors/norms


class LocalBGE:
    def __init__(self,directory):
        directory=Path(directory);self.manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
        for name in ('onnx/model.onnx','tokenizer.json'):
            if hashlib.sha256((directory/name).read_bytes()).hexdigest()!=self.manifest['files'][name]['sha256']:
                raise ValueError('Model/tokenizer checksum mismatch')
        self.tokenizer=Tokenizer.from_file(str(directory/'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=512,direction='right')
        self.tokenizer.enable_padding(pad_id=0,pad_token='[PAD]')
        options=ort.SessionOptions();options.intra_op_num_threads=4;options.inter_op_num_threads=1
        self.session=ort.InferenceSession(str(directory/'onnx/model.onnx'),sess_options=options,providers=['CPUExecutionProvider'])
        self.inputs={x.name for x in self.session.get_inputs()}
        if not self.inputs<={'input_ids','attention_mask','token_type_ids'}:raise ValueError('Unexpected ONNX inputs')
        self.truncated=0

    def encode(self,texts,query=False):
        if not texts or any(not isinstance(t,str) for t in texts):raise ValueError('Need text batch')
        vectors=[]
        for start in range(0,len(texts),16):
            encoded=self.tokenizer.encode_batch([(PREFIX+t if query else t) for t in texts[start:start+16]])
            self.truncated+=sum(bool(e.overflowing) for e in encoded)
            data={'input_ids':np.asarray([e.ids for e in encoded],dtype=np.int64),
                  'attention_mask':np.asarray([e.attention_mask for e in encoded],dtype=np.int64),
                  'token_type_ids':np.asarray([e.type_ids for e in encoded],dtype=np.int64)}
            output=self.session.run(None,{k:v for k,v in data.items() if k in self.inputs})[0]
            if output.ndim!=3 or output.shape[2]!=384:raise ValueError('Unexpected token embedding shape')
            vectors.append(normalize(output[:,0,:]))
        return np.concatenate(vectors)


def dense_rank(paragraphs,embeddings,query_embedding):
    if len(paragraphs)!=len(embeddings) or len({p['id'] for p in paragraphs})!=len(paragraphs):raise ValueError('Index identity mismatch')
    doc=normalize(embeddings);query=normalize(np.asarray(query_embedding).reshape(1,-1))
    if query.shape[1]!=doc.shape[1]:raise ValueError('Embedding dimension mismatch')
    scores=doc@query[0]
    return sorted([{'id':p['id'],'score':float(s)} for p,s in zip(paragraphs,scores)],key=lambda r:(-r['score'],r['id']))


def fuse_rankings(first,second):
    scores={}
    for ranking in (first,second):
        ids=[r['id'] for r in ranking]
        if len(ids)!=len(set(ids)):raise ValueError('Duplicate ranked IDs')
        for rank,pid in enumerate(ids,1):scores[pid]=scores.get(pid,0)+1/(60+rank)
    return [{'id':pid,'score':score} for pid,score in sorted(scores.items(),key=lambda p:(-p[1],p[0]))]
