"""Compile a development draft with a local engine; offline by default."""
import argparse,hashlib,json,shutil,subprocess,tempfile
from pathlib import Path
root=Path(__file__).resolve().parent
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--allow-downloads',action='store_true',help='Allow Tectonic to download public TeX dependencies')
parser.add_argument('--research',type=Path,required=True)
args=parser.parse_args()
from jiuwenswarm.research_workbench.paper_artifacts import verify_inputs
check=verify_inputs(root,args.research)
if not check['valid']:raise SystemExit('Stale paper inputs: '+str(check['blockers']))
input_hash=hashlib.sha256((root/'paper-inputs.json').read_bytes()).hexdigest()
portable=root.parents[1]/'.tools/tectonic-0.17.0/tectonic.exe'
engine=str(portable) if portable.is_file() else shutil.which('tectonic')
if not engine:raise SystemExit('No Tectonic compiler. Source retained; no new PDF generated.')
sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.suffix in ('.tex','.sty','.bst','.bib','.pdf','.png') and p.name != 'paper.pdf'}
with tempfile.TemporaryDirectory(prefix='.build-',dir=root) as tmp:
    command=[engine,'--untrusted','--keep-logs','--outdir',tmp]
    if not args.allow_downloads:command+=['--only-cached']
    completed=subprocess.run(command+['paper.tex'],cwd=root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=600)
    log=completed.stdout.decode('utf-8',errors='replace')
    (root/'build-console.txt').write_text(log,encoding='utf-8')
    if completed.returncode:raise SystemExit('Build failed; prior PDF (if any) retained. See build-console.txt.')
    for name,digest in sources.items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest:raise SystemExit('Source changed during build; output not adopted.')
    pdf=Path(tmp)/'paper.pdf'
    if not pdf.is_file():raise SystemExit('No PDF produced')
    check=verify_inputs(root,args.research)
    if not check['valid'] or input_hash!=hashlib.sha256((root/'paper-inputs.json').read_bytes()).hexdigest():raise SystemExit('Paper inputs changed during build; output not adopted.')
    shutil.copy2(pdf,root/'paper.pdf')
    if (Path(tmp)/'paper.log').exists():shutil.copy2(Path(tmp)/'paper.log',root/'paper.log')
record={'status':'compiled_development_draft','pdf_sha256':hashlib.sha256((root/'paper.pdf').read_bytes()).hexdigest(),'sources':sources,'engine_sha256':hashlib.sha256(Path(engine).read_bytes()).hexdigest(),'offline':not args.allow_downloads,'not_submission_ready':True,'visual_review':'pending','paper_inputs_sha256':input_hash,'semantic_status':check['semantic_status']}
(root/'build-result.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print(json.dumps({'status':record['status'],'offline':record['offline'],'pdf_sha256':record['pdf_sha256']}))
