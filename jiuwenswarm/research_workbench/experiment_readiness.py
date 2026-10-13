"""Offline experiment admission. It never calls a model or assumes pending reviews passed."""
import argparse,json,hashlib
from pathlib import Path


def inspect(plan, cases, known_development_groups):
    errors=[]
    if plan.get('status')!='frozen':errors.append('plan_not_frozen')
    if plan.get('runner_status')!='implemented_and_tested':errors.append('fulltext_runner_not_validated')
    if plan.get('model_and_price_checked') is not True:errors.append('model_price_recheck_pending')
    if len(cases)!=plan['expected_cases']:errors.append('case_count_mismatch')
    seen=set(); groups=set()
    for c in cases:
        cid=c['case_id']
        if cid in seen:errors.append(cid+':duplicate_case_id')
        seen.add(cid)
        group=c.get('source_group');groups.add(group)
        if not group or group in known_development_groups:errors.append(cid+':source_seen_or_unknown')
        if c.get('split')!='test':errors.append(cid+':not_test_split')
        if not c.get('source_sha256') or not c.get('question') or 'gold_answer' not in c:errors.append(cid+':missing_frozen_content')
        if type(c.get('answerable')) is not bool:errors.append(cid+':answerability_required')
        if c.get('answerable') is False and c.get('absence_review_confirmed') is not True:
            errors.append(cid+':absence_review_not_confirmed')
        reviews=c.get('reviews',[])
        people={r.get('reviewer_id') for r in reviews if r.get('kind')=='human' and r.get('confirmed') is True and r.get('checked_same_pdf_hash')==c.get('source_sha256') and r.get('independent') is True and r.get('source_record')}
        people.discard(None);people.discard('')
        if len(people)<2:errors.append(cid+':two_confirmed_independent_humans_required')
        if c.get('adjudication') not in ('agreement','resolved'):errors.append(cid+':unresolved_label')
        if c.get('answerable') is False and not c.get('absence_review_scope'):errors.append(cid+':absence_scope_required')
        if c.get('answerable') is True and not c.get('evidence_quotes'):errors.append(cid+':quotes_required')
    groups.discard(None)
    if len(groups)!=plan['expected_source_groups']:errors.append('source_group_count_mismatch')
    max_calls=plan['expected_cases']*plan['repetitions']*plan['max_calls_per_case']
    per_call=plan['max_input_tokens']*plan['input_cny_per_million']/1e6+plan['max_output_tokens']*plan['output_cny_per_million']/1e6
    cap=round(max_calls*per_call+plan['development_reserve_cny'],6)
    if cap+plan['previous_reserved_cny']>plan['authorization_cap_cny']:errors.append('authorization_budget_exceeded')
    return {'ready':not errors,'blockers':errors,'max_test_calls':max_calls,'max_new_reservation_cny':cap,'total_reservation_ceiling_cny':round(cap+plan['previous_reserved_cny'],6),'api_calls':0,'note':'This checks recorded declarations only; it cannot prove reviewer identity, independence, source authenticity or semantic support.'}


def public_questions(cases):
    # Explicit allowlist: labels, correct pages and supporting quotes stay local.
    return [{'case_id':c['case_id'],'question':c['question']} for c in cases]


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plan',required=True,type=Path);p.add_argument('--cases',required=True,type=Path)
    p.add_argument('--development-groups',required=True,type=Path);p.add_argument('--out',required=True,type=Path)
    args=p.parse_args()
    plan=json.loads(args.plan.read_text(encoding='utf-8'));cases=json.loads(args.cases.read_text(encoding='utf-8'))['cases']
    known={g['source_group'] for g in json.loads(args.development_groups.read_text(encoding='utf-8'))['groups']}
    report=inspect(plan,cases,known)
    report['input_hashes']={k:hashlib.sha256(v.read_bytes()).hexdigest() for k,v in [('plan',args.plan),('cases',args.cases),('development_groups',args.development_groups)]}
    args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    raise SystemExit(0 if report['ready'] else 2)
