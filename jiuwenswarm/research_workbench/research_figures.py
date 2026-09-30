"""Plot saved, accepted scores; never infer missing reviews or rerun models."""
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from .pipeline import put
from .review_scoring import statistics_report


def make_results_figure(stats, review, records, cases, out):
    if stats.get('status') != 'scored':
        raise ValueError('Scored, complete inputs are required for a results figure')
    # This is a diagnostic on saved raw answers, not a gate-removal experiment.
    raw = deepcopy(records)
    for row in raw:
        row['delivery_status'] = 'completed'
    semantic = statistics_report(raw, cases, review, bootstrap_samples=1)
    if semantic['status'] != 'scored':
        raise ValueError('Raw-answer diagnostic is incomplete')
    methods = ('A00', 'A10', 'A01', 'A11')
    bars = []
    for method in methods:
        row = stats['methods'][method]
        n = row['denominator']
        delivered = round(row['grounded_success'] * n)
        raw_success = round(semantic['methods'][method]['grounded_success'] * n)
        if not 0 <= delivered <= raw_success <= n:
            raise ValueError('Invalid accounting of delivered and rejected successes')
        bars.append({'method': method, 'denominator': n, 'delivered': delivered,
                     'semantic_success_rejected': raw_success - delivered,
                     'other_failures': n - raw_success})
    groups = defaultdict(list)
    for row in stats['case_means']:
        groups[row['source_group']].append(row['A11'] - row['A00'])
    differences = {g:sum(v)/len(v) for g,v in sorted(groups.items())}
    data = {'review_basis': stats['review_basis'], 'bars': bars,
            'source_group_differences': differences,
            'note': 'Post-hoc raw-answer accounting; fixed denominators; no new model calls or causal gate-removal claim.'}
    out = Path(out)
    put(out/'figure-data.json', data)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    with plt.rc_context({'font.family':'DejaVu Sans','font.size':9,'pdf.fonttype':42}):
        fig, (left, right) = plt.subplots(1, 2, figsize=(7.0, 3.1), layout='constrained')
        x = list(range(4))
        bottom = [0]*4
        for field,label,color,hatch in [
            ('delivered','Delivered success','#24765f',None),
            ('semantic_success_rejected','Semantic success, rejected','#dec68d','///'),
            ('other_failures','Other failures','#e1e5e6',None)]:
            values=[b[field] for b in bars]
            left.bar(x,values,bottom=bottom,label=label,color=color,width=.62,hatch=hatch,edgecolor='white',linewidth=.5)
            bottom=[a+b for a,b in zip(bottom,values)]
        for i,b in enumerate(bars):
            left.text(i,b['delivered']/2,str(b['delivered']),ha='center',va='center',color='white',fontweight='bold')
            if b['semantic_success_rejected']:
                left.text(i,b['delivered']+b['semantic_success_rejected']/2,str(b['semantic_success_rejected']),ha='center',va='center',fontsize=8)
        left.set(xticks=x,xticklabels=methods,ylabel=f"Outputs ({bars[0]['denominator']} per condition)",ylim=(0,max(bottom)+2),title='(a) Delivered vs. raw-answer accounting')
        left.legend(loc='upper center',bbox_to_anchor=(.5,-.15),frameon=False,fontsize=7,ncol=1)
        labels=list(differences);values=[100*differences[k] for k in labels]
        right.barh(labels,values,color=['#24765f' if v>=0 else '#a65849' for v in values],height=.62)
        right.axvline(0,color='#5b6568',linewidth=.7)
        right.set(xlabel='A11 minus A00 (percentage points)',xlim=(-55,55),title='(b) Paired differences by paper')
        right.invert_yaxis()
        for ax in (left,right):
            ax.spines[['top','right']].set_visible(False)
        fig.savefig(out/'results-figure.pdf',metadata={'CreationDate':None,'ModDate':None})
        fig.savefig(out/'results-figure.png',dpi=180)
        plt.close(fig)
    put(out/'results-figure.tex',r'''\begin{figure}[t]
\centering
\includegraphics[width=\linewidth]{results-figure.pdf}
\caption{User-accepted scoring. (a) Fixed-denominator outcome accounting: hatched portions are saved answers judged semantically successful but rejected by the delivery contract. This post-hoc diagnostic is not a new experiment with the gate removed. (b) Question-averaged A11--A00 differences within each fixed paper group; no per-paper uncertainty claim is made.}
\label{fig:outcomes}
\end{figure}
''')
    return data
