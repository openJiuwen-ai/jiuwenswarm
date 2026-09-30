"""Hash-bound human ratings. Missing/ambiguous ratings never become zero."""
import hashlib
import json
import random
import re
import statistics
from pathlib import Path

FIELDS = ('answer_correct', 'all_substantive_claims_supported',
          'all_emitted_citations_support_claims', 'input_relative_abstention_appropriate',
          'corpus_relative_task_success')


def rating_value(value):
    value = re.split(r'[（(；;。。，,：:\s]', value.strip(), 1)[0].strip()
    if value in ('是','全部','合理','正确','已核对','已本人核对','已确认'): return True
    if value in ('否','部分','无','不合理','不正确'): return False
    return None


def import_markdown(paths, template):
    """Read the issued v1.16 Markdown form; leave unfamiliar language unresolved."""
    originals = {r['response_id']: r for r in template['answers']}
    result, seen = [], set()
    labels = dict(zip(FIELDS, ['答案正确', '全部实质性主张有证据支持',
        '实际发出的引用支持所附主张', '若拒答，相对实际输入是否合理', '相对指定完整论文是否完成问题']))
    for path in paths:
        text = Path(path).read_text(encoding='utf-8-sig')
        # Combined paper packets may carry different reviewer declarations.
        for section in re.split(r'(?=^# 队友[^\n]* · T\d+ · )', text, flags=re.M):
            if not re.search(r'^## \d+ · [a-f0-9]+', section, re.M):
                continue
            header = section.split('\n## ', 1)[0]
            name = re.search(r'姓名：([^\n]*?)\s+日期：([^\n]*?)\s+AI辅助范围：', header)
            prior = re.search(r'是否已看过其他人的评分或这些模型输出：([^\n]*)', header)
            for match in re.finditer(r'^## \d+ · ([a-f0-9]+)\s*\n(.*?)(?=^## \d+ · |\Z)', section, re.M|re.S):
                rid, block = match.groups()
                if rid not in originals or rid in seen: raise ValueError('Unknown/duplicate Markdown response ID')
                seen.add(rid)
                row = dict(originals[rid])
                answer = re.search(r'模型原始输出（不改写）：\s*```json\s*(.*?)\s*```', block, re.S)
                checksum = re.search(r'回答SHA256：`([a-f0-9]+)`', block)
                if not answer or not checksum or checksum[1] != row['answer_sha256'] or digest(json.loads(answer[1])) != row['answer_sha256']:
                    raise ValueError('Markdown answer changed or hash missing')
                scores = block.rsplit('### 本人评分（请填写）', 1)
                if len(scores) != 2: raise ValueError('Missing review section')
                raw = dict(re.findall(r'^- ([^：\n]+)：([^\n]*)', scores[1], re.M))
                row.update({field: rating_value(raw.get(label,'')) for field,label in labels.items()})
                row.update(reviewer=name[1].strip() if name else None, date=name[2].strip() if name else None,
                    prior_exposure=prior[1].strip() if prior else None,
                    ai_assistance=(re.search(r'AI辅助范围：(.*?)本人实际核对范围：', header, re.S).group(1).strip()
                        if re.search(r'AI辅助范围：(.*?)本人实际核对范围：', header, re.S) else None),
                    declared_human_scope=(header.split('本人实际核对范围：', 1)[1].split('\n', 1)[0].strip()
                        if '本人实际核对范围：' in header else None),
                    imported_section=section.split('\n',1)[0],
                    rationale=raw.get('原文页码、短证据与理由'),
                    human_confirmed=rating_value(raw.get('我已本人核对上述评分','')),
                    imported_raw_fields=raw, imported_from=str(path),
                    imported_file_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest())
                result.append(row)
    if not result: raise ValueError('No issued Markdown records found')
    return {'status': 'imported_pending_validation', 'answers': result,
            'note': 'Exact yes/no/full/partial phrases normalized; ambiguous or blank values remain null. Raw fields retained.'}



def import_text_review(path, template, source_url=None):
    """Import ID-keyed text-only reviews. Unmatched IDs stay quarantined, never fuzzy-joined."""
    text = Path(path).read_text(encoding='utf-8-sig')
    expected = {r['response_id']: r for r in template['answers']}
    labels = dict(zip(FIELDS, ['答案正确','全部实质性主张有证据支持',
        '实际发出的引用支持所附主张','若拒答，相对实际输入是否合理','相对指定完整论文是否完成问题']))
    rows, unknown, seen = [], [], set()
    for match in re.finditer(r'^\s*(\d+)\s*·\s*([a-f0-9]+)\s*\n(.*?)(?=^\s*\d+\s*·\s*[a-f0-9]+\s*\n|\Z)', text, re.M|re.S):
        number, rid, body = match.groups()
        raw = dict(re.findall(r'^- ([^：\n]+)：([^\n]*)', body, re.M))
        if rid not in expected:
            unknown.append({'received_id':rid,'ordinal':number,'raw_fields':raw})
            continue
        if rid in seen: raise ValueError('Duplicate text review response ID')
        seen.add(rid)
        row = dict(expected[rid])
        row.update({field:rating_value(raw.get(label,'')) for field,label in labels.items()})
        row.update(reviewer=None,date=None,prior_exposure=None,human_confirmed=None,
            rationale=raw.get('原文页码、短证据与理由'), imported_raw_fields=raw,
            imported_from=source_url or str(path),
            imported_file_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            binding_method='Exact response ID joined to issued frozen answer template; received text has no answer hash.')
        rows.append(row)
    if not rows and not unknown: raise ValueError('No text review records found')
    return {'status':'received_pending_human_declarations','answers':rows,'unmatched_records':unknown,
            'missing_ids':sorted(set(expected)-seen)}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_ratings(document, records):
    """Accept partial canonical forms, but require attributable, exact answer bindings."""
    expected = {r['response_id']: r for r in records}
    accepted, pending, seen = {}, [], set()
    for row in document.get('answers', []):
        rid = row.get('response_id')
        if rid not in expected or rid in seen:
            raise ValueError('Unknown or duplicated response ID')
        seen.add(rid)
        original = expected[rid]
        if row.get('answer_sha256') != original.get('answer_sha256'):
            raise ValueError('Rating answer hash mismatch: ' + rid)
        if 'answer' in row and digest(row['answer']) != original.get('answer_sha256'):
            raise ValueError('Changed answer in rating: ' + rid)
        for field in FIELDS:
            if row.get(field) is not None and type(row[field]) is not bool:
                raise ValueError('Use true/false/null, not inferred numeric/string ratings: ' + field)
        a, n = row.get('unsupported_claims'), row.get('total_substantive_claims')
        if (a is None) != (n is None) or (a is not None and
                (type(a) is not int or type(n) is not int or not 0 <= a <= n)):
            raise ValueError('Invalid claim counts')
        required = ('reviewer', 'date', 'prior_exposure', 'rationale')
        complete = row.get('human_confirmed') is True and all(
            isinstance(row.get(k), str) and row[k].strip() and '____' not in row[k] for k in required)
        # Explicitly record N/A as null; abstaining outputs need separate input/corpus judgments.
        if (original.get('answer') or {}).get('abstain') is True:
            complete &= all(type(row.get(k)) is bool for k in
                            ('input_relative_abstention_appropriate', 'corpus_relative_task_success'))
        else:
            complete &= all(type(row.get(k)) is bool for k in
                            ('answer_correct', 'all_substantive_claims_supported', 'corpus_relative_task_success'))
            if (original.get('answer') or {}).get('citations'):
                complete &= type(row.get('all_emitted_citations_support_claims')) is bool
        if complete:
            accepted[rid] = row
        else:
            pending.append(rid)
    return accepted, pending


def reconcile(records, first=None, second=None, adjudications=None):
    a, _ = validate_ratings(first or {}, records)
    b, _ = validate_ratings(second or {}, records)
    # Explicit user acceptance is a separate analysis basis, never a reviewer signature.
    if (adjudications or {}).get('approval_mode') == 'user_accepted_scoring':
        approval = adjudications.get('user_confirmation', {})
        rows = adjudications.get('answers', [])
        if (approval.get('confirmed') is not True or not approval.get('statement') or
                not approval.get('date') or approval.get('ratings_sha256') != digest(rows)):
            raise ValueError('Missing or stale user acceptance')
        received = [{r['response_id']: r for r in (form or {}).get('answers', [])}
                    for form in (first, second)]
        for row in rows:
            rid = row['response_id']
            if any(rid not in form for form in received) or row.get('review_hashes') != [
                    digest(form[rid]) for form in received]:
                raise ValueError('User-accepted score does not bind current received ratings')
            if row.get('confirmation_role') != 'user_acceptance_not_reviewer_attestation':
                raise ValueError('User acceptance must not impersonate a reviewer')
        final, pending = validate_ratings(adjudications, records)
        return {'first_complete': len(a), 'second_complete': len(b), 'resolved': len(final),
                'missing': [r['response_id'] for r in records if r['response_id'] not in final],
                'disputes': [], 'ratings': final, 'basis': 'user_accepted_scoring',
                'independent_review_verified': False,
                'unresolved_nonprimary_fields': adjudications.get('unresolved_nonprimary_fields', []),
                'limitation': 'User accepted AI-assisted teammate ratings and proposed resolutions; '
                    'reviewer identities, original review dates and output-review independence remain unverified. '
                    'This departs from the planned fully documented dual-review process.'}
    resolutions = {}
    for row in (adjudications or {}).get('answers', []):
        rid = row.get('response_id')
        if rid in resolutions or rid not in {r['response_id'] for r in records}:
            raise ValueError('Unknown/duplicate adjudication')
        resolutions[rid] = row
    final, disputes, missing = {}, [], []
    for record in records:
        rid = record['response_id']
        if rid not in a or rid not in b:
            missing.append(rid)
            continue
        if a[rid]['reviewer'].strip().casefold() == b[rid]['reviewer'].strip().casefold():
            raise ValueError('Two forms must represent different human reviewers')
        disagreements = [k for k in (*FIELDS, 'unsupported_claims', 'total_substantive_claims')
                         if a[rid].get(k) != b[rid].get(k)]
        if disagreements:
            resolution = resolutions.get(rid)
            if resolution:
                if resolution.get('review_hashes') != [digest(a[rid]), digest(b[rid])]:
                    raise ValueError('Adjudication does not bind the current two ratings')
                approved, _ = validate_ratings({'answers': [resolution]}, [record])
                if rid in approved:
                    final[rid] = approved[rid]
                    continue
            disputes.append({'response_id': rid, 'fields': disagreements,
                             'review_hashes': [digest(a[rid]), digest(b[rid])]})
        else:
            final[rid] = a[rid]
    return {'first_complete': len(a), 'second_complete': len(b), 'resolved': len(final),
            'missing': missing, 'disputes': disputes, 'ratings': final}


def statistics_report(records, cases, review, repetitions=2, bootstrap_samples=10000):
    """Fixed case/repetition grid; source-cluster bootstrap is descriptive at n=8."""
    methods = ('A00', 'A10', 'A01', 'A11')
    case_map = {c['case_id']: c for c in cases}
    expected = {(c, r, m) for c in case_map for r in range(repetitions) for m in methods}
    slots = {(r['case_id'], r['repetition'], r['method']): r for r in records}
    if len(slots) != len(records) or set(slots) - expected:
        raise ValueError('Invalid experimental grid')
    report = {'status': 'waiting_for_human_reviews', 'planned_outputs': len(expected),
              'resolved_outputs': review['resolved'], 'primary_effect': None, 'methods': None,
              'uncertainty': 'Source-cluster percentile bootstrap; exploratory, eight groups; not a significance claim.'}
    report['review_basis'] = review.get('basis', 'documented_dual_review')
    report['review_limitation'] = review.get('limitation')
    if set(slots) != expected or review['resolved'] != len(expected):
        return report
    scores = {}
    for key, record in slots.items():
        row = review['ratings'][record['response_id']]
        answer = record.get('answer') or {}
        if record['delivery_status'] != 'completed':
            score = False
        elif case_map[key[0]]['answerable']:
            score = (answer.get('abstain') is not True and row['answer_correct'] is True and
                     row['all_substantive_claims_supported'] is True and
                     (not answer.get('citations') or row['all_emitted_citations_support_claims'] is True))
        else:
            score = (answer.get('abstain') is True and row['input_relative_abstention_appropriate'] is True
                     and row['corpus_relative_task_success'] is True)
        scores[key] = int(score)
    means = {(c, m): statistics.mean(scores[c, r, m] for r in range(repetitions))
             for c in case_map for m in methods}
    summary = {}
    for method in methods:
        summary[method] = {'grounded_success': statistics.mean(means[c, method] for c in case_map),
                           'denominator': len(case_map) * repetitions}
        for label, flag in [('answerable', True), ('unanswerable', False)]:
            subset = [c for c in case_map if case_map[c]['answerable'] is flag]
            summary[method][label] = statistics.mean(means[c, method] for c in subset) if subset else None
    groups = sorted({c['source_group'] for c in cases})
    differences = {g: [means[c, 'A11'] - means[c, 'A00'] for c in case_map
                       if case_map[c]['source_group'] == g] for g in groups}
    rng = random.Random(20260928)
    samples = []
    for _ in range(bootstrap_samples):
        draws = [v for group in rng.choices(groups, k=len(groups)) for v in differences[group]]
        samples.append(statistics.mean(draws))
    samples.sort()
    report.update(status='scored', methods=summary,
                  primary_effect=summary['A11']['grounded_success']-summary['A00']['grounded_success'],
                  paired_differences={m: summary[m]['grounded_success']-summary['A00']['grounded_success']
                                      for m in methods[1:]},
                  bootstrap_95_percentile=[samples[int(.025*(len(samples)-1))], samples[int(.975*(len(samples)-1))]],
                  bootstrap_seed=20260928, bootstrap_samples=bootstrap_samples,
                  case_means=[{'case_id': c, 'source_group': case_map[c]['source_group'],
                               **{m: means[c, m] for m in methods}} for c in case_map])
    return report


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Import the issued Markdown rating packet without inventing judgments.')
    parser.add_argument('--markdown', type=Path, nargs='+', required=True)
    parser.add_argument('--template', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    template = json.loads(args.template.read_text(encoding='utf-8-sig'))
    value = import_markdown(args.markdown, template)
    accepted, pending = validate_ratings(value, template['answers'])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'imported':len(value['answers']),'complete':len(accepted),'pending':len(pending)}))
