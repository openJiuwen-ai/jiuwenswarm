"""Validate a local submission directory without uploading or calling Reviewer."""
import argparse
import hashlib
import json
from pathlib import Path
import re

REQUIRED = ('paper/paper.pdf','AgenticReviewer/PaperReview-AccessToken.txt',
            'docs/architecture.md','docs/module_call.md','docs/innovation.md',
            'framework_contribution.md','resource_report.md','提交说明.md')


def check(folder):
    folder = Path(folder).resolve()
    blockers = [f'missing:{name}' for name in REQUIRED if not (folder/name).is_file() or not (folder/name).stat().st_size]
    if not (folder/'code').is_dir() or not any((folder/'code').rglob('*.py')):
        blockers.append('agent_source_missing')
    def load(name):
        p=folder/name
        return json.loads(p.read_text(encoding='utf-8')) if p.is_file() else {}
    metadata=load('release-metadata.json')
    team=metadata.get('team_name','')
    if not team or team in ('TEAM_NAME_PENDING','待填写') or folder.name!=team:
        blockers.append('team_name_or_root_directory_unconfirmed')
    if metadata.get('final_paper_approved') is not True:
        blockers.append('paper_is_not_final')
    statistics=load('evidence/statistics.json')
    if statistics.get('status')!='scored' or statistics.get('resolved_outputs')!=192:
        blockers.append('semantic_reviews_incomplete')
    paper=folder/'paper/paper.pdf'
    paper_hash=hashlib.sha256(paper.read_bytes()).hexdigest() if paper.is_file() else None
    if not paper.is_file() or not paper.read_bytes().startswith(b'%PDF-'):
        blockers.append('paper_not_a_pdf')
    receipt=load('AgenticReviewer/receipt.json')
    token=folder/'AgenticReviewer/PaperReview-AccessToken.txt'
    token_text=token.read_text(encoding='utf-8').strip() if token.is_file() else ''
    if not token_text or any(word in token_text.lower() for word in ('pending','todo','待填写')):
        blockers.append('reviewer_access_token_missing')
    if not paper_hash or receipt.get('paper_sha256')!=paper_hash or receipt.get('result_verified') is not True:
        blockers.append('reviewer_result_not_bound_to_final_pdf')
    if not token_text or receipt.get('token_sha256')!=hashlib.sha256(token_text.encode()).hexdigest():
        blockers.append('reviewer_token_not_bound_to_receipt')
    if not re.fullmatch(r'https://(?:github\.com|atomgit\.com|gitcode\.com)/[^\s]+/(?:pull|pulls|merge_requests)/\d+',metadata.get('contribution_pr_url') or ''):
        blockers.append('contribution_pr_link_missing')
    # A bounded allowlist package is still checked for obvious accidentally copied API keys.
    secret_files=[]
    for p in folder.rglob('*'):
        if not p.is_file() or p.suffix.lower() not in ('.py','.json','.md','.txt','.js','.html','.patch'): continue
        if p.name=='PaperReview-AccessToken.txt': continue
        if re.search(rb'\bsk-[A-Za-z0-9_-]{20,}',p.read_bytes()): secret_files.append(str(p.relative_to(folder)))
    if secret_files: blockers.append('possible_api_key_in_package')
    return {'ready':not blockers,'blockers':blockers,'paper_sha256':paper_hash,
            'possible_secret_files':secret_files,'rule_sources':['W02','I03','I05'],
            'note':'Local checks only; no upload, no model call, no verification of organizer acceptance.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder',type=Path)
    parser.add_argument('--out',type=Path)
    args=parser.parse_args()
    result=check(args.folder)
    text=json.dumps(result,ensure_ascii=False,indent=2)
    if args.out: args.out.write_text(text,encoding='utf-8')
    print(text)
    raise SystemExit(0 if result['ready'] else 2)
