# ruff: noqa: E401,E402,F401
# 手测用的一次性 mock skill（顶层可执行代码，非模块），不走正常 lint 规则。
import argparse, time, json, sys, os
parser = argparse.ArgumentParser()
parser.add_argument('--input-dir', required=True)
parser.add_argument('--output-dir', required=True)
parser.add_argument('--status-file', required=True)
parser.add_argument('--progress-file', required=True)
args = parser.parse_args()
sys.path.insert(0, r'D:/jiuwenswarm/jiuwenswarm/resources/agent/workspace/skills/planning')
from scripts._progress import write_progress_file, PHASE_ORDER
# 模拟 5 阶段，每个 0.3s
for i, phase in enumerate(PHASE_ORDER):
    write_progress_file(args.progress_file, current_phase=phase, index=i, total=len(PHASE_ORDER))
    time.sleep(0.3)
# 写 status.json 终态
import pathlib
status = {
    'status': 'complete',
    'artifacts': [str(pathlib.Path(args.output_dir) / 'mock.json')],
    'errors': [], 'warnings': [],
    'check_feasibility': {'passed': True, 'downgraded_to': None, 'issues': []},
    'method_rounds_used': 0, 'wall_time_seconds': 1.5, 'tier': 0,
    'checkpoint_status': {'method_design': 'passed', 'experiment_plan': 'passed', 'tier_downgrade': 'skipped', 'execution_config': 'passed'},
}
with open(args.status_file, 'w', encoding='utf-8') as f:
    json.dump(status, f, ensure_ascii=False)
# 模拟一个产物
import pathlib
pathlib.Path(args.output_dir, 'mock.json').write_text('{"mock": true}', encoding='utf-8')
sys.exit(0)
