"""Offline, hash-checked PDF text chunks. Does not approve labels or call an LLM."""
import argparse
import hashlib
import json
import re
from pathlib import Path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def chunks(text, limit=1600, overlap=200):
    if not 0 <= overlap < limit or limit < 4:
        raise ValueError('Invalid UTF-8 chunk limits')
    start=0
    while start < len(text):
        part=text[start:].encode()[:limit].decode('utf-8',errors='ignore')
        end=start+len(part)
        yield {'start_char':start,'end_char':end,'text':part,'bytes':len(part.encode()),'sha256':sha(part.encode())}
        if end==len(text):break
        back=len(part.encode()[-overlap:].decode('utf-8',errors='ignore')) if overlap else 0
        start=max(start+1,end-back)


def build(project, index):
    project=Path(project).resolve()
    records=json.loads(Path(index).read_text(encoding='utf-8'))
    documents=[]; all_chunks=[]
    for record in records:
        pdf=(project/record['pdf']).resolve()
        if not pdf.is_relative_to(project) or pdf.suffix.lower()!='.pdf':
            raise ValueError('Invalid source path')
        if sha(pdf.read_bytes())!=record['sha256']:raise ValueError('PDF hash mismatch')
        extracted=pdf.with_suffix('.extracted.txt')
        if not extracted.resolve().is_relative_to(project):raise ValueError('Invalid extracted text path')
        parts=re.split(r'\n=== PDF PAGE (\d+) ===\n',extracted.read_text(encoding='utf-8'))
        pages={int(parts[i]):parts[i+1] for i in range(1,len(parts),2)}
        if len(pages)!=(len(parts)-1)//2 or set(pages)!={p['page'] for p in record['pages']}:
            raise ValueError('Page set differs from frozen index')
        doc_id='D'+record['sha256'][:16]
        if any(d['id']==doc_id for d in documents):raise ValueError('Duplicate PDF identity; resolve before indexing')
        documents.append({'id':doc_id,'pdf':record['pdf'],'pdf_sha256':record['sha256'],
            'extracted_text_sha256':sha(extracted.read_bytes()),'pages':len(pages),
            'source_group':None,'source_group_status':'paper identity/version grouping requires review',
            'split':'development_only','license_status':record.get('redistribution_license','unknown')})
        for page in record['pages']:
            text=pages[page['page']]
            if sha(text.encode())!=page['text_sha256'] or len(text)!=page['chars']:
                raise ValueError('Extracted page hash mismatch')
            for n,chunk in enumerate(chunks(text),1):
                all_chunks.append(dict(chunk,id=f'{doc_id}-P{page["page"]:03}-C{n:03}',document_id=doc_id,
                    page_1based=page['page'],page_sha256=page['text_sha256']))
    return {'schema_version':1,'kind':'derived_fulltext_index_not_approved_dataset',
        'split':'development_only','chunk_utf8_bytes':1600,'overlap_utf8_bytes_max':200,
        'source_index_sha256':sha(Path(index).read_bytes()),'api_calls':0,
        'notes':'PDF physical pages; extraction artifacts and table order need human checking. No gold labels in this index.',
        'documents':documents,'chunks':all_chunks}


def search(index, question, top_k=5):
    # Baseline diagnostic only: vocabulary overlap, no embedding model or gold labels.
    words=set(re.findall(r'[a-z0-9]+',question.lower()))-{'the','a','an','is','are','of','in','to','and','does','what','with'}
    hits=[]
    for chunk in index['chunks']:
        if sha(chunk['text'].encode())!=chunk['sha256']:raise ValueError('Chunk hash mismatch')
        score=len(words & set(re.findall(r'[a-z0-9]+',chunk['text'].lower())))
        if score:hits.append({'id':chunk['id'],'page_1based':chunk['page_1based'],'document_id':chunk['document_id'],'overlap_score':score})
    return sorted(hits,key=lambda h:(-h['overlap_score'],h['id']))[:top_k]


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',required=True,type=Path)
    parser.add_argument('--source-index',required=True,type=Path)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args()
    result=build(args.project,args.source_index)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    # Never overwrite a frozen index. Identical rebuilds are accepted.
    encoded=json.dumps(result,ensure_ascii=False,indent=2)
    if args.out.exists() and args.out.read_text(encoding='utf-8')!=encoded:raise SystemExit('Output exists with different content; use a new version')
    args.out.write_text(encoded,encoding='utf-8')
    print(json.dumps({'documents':len(result['documents']),'pages':sum(d['pages'] for d in result['documents']),'chunks':len(result['chunks']),'api_calls':0}))
