"""Evidence-bound paper assembly. No model calls; no inferred or synthetic study scores."""
import argparse
import math
from pathlib import Path
import shutil

from .pipeline import put, read, run, sha, audit

METHODS = ('A00', 'A10', 'A01', 'A11')


def render_results(stats):
    """Render validated numeric results, or explicit pending text without numeric outcomes."""
    if stats.get('status') != 'scored':
        summary = ('Semantic assessment is incomplete; condition success rates and the primary '
                   'effect are not reported. No benefit is inferred from execution completion.')
        return {'summary': summary, 'section': summary + '\n', 'table': '% Semantic scores pending.\n',
                'markdown': '# Semantic results pending\n\n' + summary + '\n'}
    if (type(stats.get('planned_outputs')) is not int or stats['planned_outputs'] <= 0 or
            stats.get('resolved_outputs') != stats['planned_outputs'] or set(stats.get('methods', {})) != set(METHODS)):
        raise ValueError('Incomplete statistical grid cannot produce a results table')
    def number(value, low, high):
        if type(value) not in (float, int) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('Invalid statistical value')
        return float(value)
    rows = []
    for method in METHODS:
        row = stats['methods'][method]
        if type(row.get('denominator')) is not int or row['denominator'] * 4 != stats['planned_outputs']:
            raise ValueError('Condition denominator differs from the frozen grid')
        values = [number(row[k], 0, 1) for k in ('grounded_success','answerable','unanswerable')]
        rows.append((method, row['denominator'], values))
    effect = number(stats['primary_effect'], -1, 1)
    if not math.isclose(effect, rows[3][2][0] - rows[0][2][0], abs_tol=1e-12):
        raise ValueError('Primary effect disagrees with condition means')
    interval = stats.get('bootstrap_95_percentile')
    if not isinstance(interval, list) or len(interval) != 2:
        raise ValueError('Missing descriptive interval')
    lo, hi = [number(v, -1, 1) for v in interval]
    if lo > hi: raise ValueError('Reversed interval')
    summary = (f'The paired A11--A00 difference in grounded task success is {100*effect:+.2f} '
               f'percentage points; the descriptive source-cluster 95 percent bootstrap interval '
               f'is [{100*lo:+.2f}, {100*hi:+.2f}] percentage points. '
               'This small, partially masked study does not establish broad generalization or autonomous research capability.')
    proxy = stats.get('review_basis') == 'user_accepted_scoring'
    if proxy:
        summary += (' Ratings and proposed resolutions were accepted by the project user; ' 'individual reviewer attestations and independence remain unverified. ' 'These are descriptive results conditional on that accepted scoring set.')
    table = ['\\begin{tabular}{lrrrr}', '\\hline',
             'Condition & Slots & Overall & Answerable & Unanswerable \\\\', '\\hline']
    markdown = ['# '+('User-accepted scoring results' if proxy else 'Verified semantic results'), '', '| Condition | Slots | Overall | Answerable | Unanswerable |',
                '|---|---:|---:|---:|---:|']
    for method, denominator, values in rows:
        formatted = [f'{v*100:.2f}' for v in values]
        table.append(' & '.join([method,str(denominator),*formatted]) + r' \\')
        markdown.append('| '+' | '.join([method,str(denominator),*[x+'%' for x in formatted]])+' |')
    table += ['\\hline', '\\end{tabular}']
    section = ('\\begin{table}[t]\n\\centering\\small\n\\input{results-table.tex}\n'
               '\\caption{Human-adjudicated grounded task success (percent). Repetitions are averaged '
               'within question; rejected outputs remain in the denominator.}\n\\end{table}\n' + summary + '\n')
    if proxy:
        section = section.replace('Human-adjudicated grounded', 'User-accepted scoring: grounded')
    return {'summary':summary,'section':section,'table':'\n'.join(table)+'\n',
            'markdown':'\n'.join(markdown)+'\n\n'+summary+'\n'}


def verify_inputs(folder, research=None, require_pdf=False):
    """Reject stale assembled sources, evidence, code, or build outputs without overwriting them."""
    folder = Path(folder).resolve()
    record = read(folder/'paper-inputs.json')
    blockers = []
    for name, checksum in record['files'].items():
        path = (folder/name).resolve()
        if not path.is_relative_to(folder) or not path.is_file() or sha(path) != checksum:
            blockers.append('changed_or_missing:' + name)
    if research is not None:
        research = Path(research).resolve()
        for name, checksum in record['live_inputs'].items():
            path = (research/name).resolve()
            if not path.is_relative_to(research) or not path.is_file() or sha(path) != checksum:
                blockers.append('live_input_changed:' + name)
        # Adding the previously absent other rater or adjudication also invalidates old output.
        current = sorted(str(p.relative_to(research)).replace('\\','/') for p in
                         (research/'evaluations/formal-final').glob('*.json')
                         if p.name in ('reviewer-A.json','reviewer-B.json','adjudications.json'))
        if current != record['review_paths']:
            blockers.append('review_file_set_changed')
    for name, checksum in record['code'].items():
        path = Path(__file__).with_name(name)
        if not path.is_file() or sha(path) != checksum: blockers.append('analysis_code_changed:' + name)
    if require_pdf:
        build = read(folder/'build-result.json') if (folder/'build-result.json').is_file() else {}
        pdf = folder/'paper.pdf'
        if not pdf.is_file() or build.get('pdf_sha256') != sha(pdf): blockers.append('pdf_not_bound_to_build')
        if build.get('paper_inputs_sha256') != sha(folder/'paper-inputs.json'):
            blockers.append('build_not_bound_to_inputs')
        for name, checksum in build.get('sources', {}).items():
            path = (folder/name).resolve()
            if not path.is_relative_to(folder) or not path.is_file() or sha(path) != checksum:
                blockers.append('build_source_changed:' + name)
    return {'valid':not blockers, 'blockers':blockers, 'semantic_status':record['semantic_status'],
            'new_api_calls':0, 'submission_ready':False}


def assemble(research, template, out):
    research, template, out = (Path(p).resolve() for p in (research, template, out))
    # Never mutate the prior manuscript, frozen runs, or an existing delivery.
    if out.exists(): raise ValueError('Output must be a new directory; existing drafts are preserved')
    out.mkdir(parents=True)
    report = run(research, out=out/'evidence')
    rendered = render_results(report['statistics'])
    text = (template/'paper.tex').read_text(encoding='utf-8')
    if text.count('%% RESEARCH_SUMMARY %%') != 1 or text.count('%% RESEARCH_RESULTS %%') != 1:
        raise ValueError('Paper template must contain the two explicit result markers exactly once')
    for p in template.iterdir():
        if p.is_file() and p.suffix in ('.sty','.bst','.bib'):
            shutil.copy2(p,out/p.name)
    put(out/'paper.tex',text.replace('%% RESEARCH_SUMMARY %%',rendered['summary']).replace(
        '%% RESEARCH_RESULTS %%',r'\input{results-section.tex}'))
    if report['statistics']['status'] == 'scored':
        from .research_figures import make_results_figure
        checked = audit(research)
        make_results_figure(report['statistics'], read(out/'evidence/review-resolution.private.json'),
                            checked['records'], checked['cases'], out)
        rendered['section'] += '\\input{results-figure.tex}\n'
    put(out/'results-section.tex',rendered['section'])
    put(out/'results-table.tex',rendered['table'])
    put(out/'results.en.md',rendered['markdown'])
    shutil.copy2(out/'evidence/execution-table.tex',out/'execution-table.tex')
    shutil.copy2(template/'build.py',out/'build.py')
    provenance = report['provenance']
    live = {'workspace.json':provenance['workspace_sha256'],
            'experiments/fulltext-confirmatory-v1/freeze.json':provenance['freeze_sha256']}
    for name, checksum in provenance['review_files'].items():
        live[str(Path(name).resolve().relative_to(research)).replace('\\','/')] = checksum
    record = {'status':'assembled_development_manuscript','semantic_status':report['statistics']['status'],
        'template_sha256':sha(template/'paper.tex'), 'protocol_hash':report['protocol_hash'],
        'live_inputs':live, 'review_paths':sorted(n for n in live if n.startswith('evaluations/')),
        'code':{n:sha(Path(__file__).with_name(n)) for n in ('pipeline.py','review_scoring.py','paper_artifacts.py','research_figures.py')},
        'files':{str(p.relative_to(out)).replace('\\','/'):sha(p) for p in out.rglob('*') if p.is_file()},
        'new_api_calls':0,'submission_ready':False}
    put(out/'paper-inputs.json',record)
    return verify_inputs(out,research)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','verify'])
    parser.add_argument('--research',type=Path,required=True)
    parser.add_argument('--paper',type=Path,required=True)
    parser.add_argument('--template',type=Path)
    parser.add_argument('--require-pdf',action='store_true')
    args=parser.parse_args()
    if args.action=='prepare' and args.template is None:parser.error('--template is required for prepare')
    result=(assemble(args.research,args.template,args.paper) if args.action=='prepare' else
            verify_inputs(args.paper,args.research,args.require_pdf))
    import json
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(0 if result['valid'] else 2)
