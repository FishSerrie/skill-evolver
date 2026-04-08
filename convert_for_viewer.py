#!/usr/bin/env python3
"""Convert evolver iteration data into Creator eval-viewer format.

Creator viewer expects:
  workspace/iteration-N/<run_dir>/
    ├── eval_metadata.json   # {"prompt": ..., "eval_id": ...}
    ├── grading.json          # {"expectations": [{"text": ..., "passed": ..., "evidence": ...}], ...}
    └── outputs/              # files representing the skill output
        └── <output files>
"""
import json, shutil
from pathlib import Path

WORKSPACE = Path('plugin/skills/skill-evolver-workspace')
EVOLVE_DIR = WORKSPACE / 'evolve'

def convert():
    # Clean any previous Creator-format iteration dirs
    for d in WORKSPACE.iterdir():
        if d.is_dir() and d.name.startswith('iteration-') and d.name != 'iteration-0':
            if d.name not in ('evolve', 'evals', 'working-skill'):
                shutil.rmtree(d)
    # Also remove iteration-0 since we re-create
    if (WORKSPACE / 'iteration-0').exists():
        shutil.rmtree(WORKSPACE / 'iteration-0')

    # Convert each evolver iteration
    for iter_e in sorted(EVOLVE_DIR.glob('iteration-E*')):
        # Skip the holdout marker (iteration-E999)
        iter_num_str = iter_e.name.replace('iteration-E', '')
        try:
            iter_num = int(iter_num_str)
        except ValueError:
            continue

        target = WORKSPACE / f'iteration-{iter_num}'
        target.mkdir(exist_ok=True)

        grading_path = iter_e / 'grading.json'
        if not grading_path.exists():
            continue
        grading = json.loads(grading_path.read_text())

        for r in grading.get('results', []):
            case_id = r['case_id']
            run_dir = target / f'case_{case_id:02d}'
            run_dir.mkdir(exist_ok=True)
            outputs_dir = run_dir / 'outputs'
            outputs_dir.mkdir(exist_ok=True)

            # 1. eval_metadata.json — viewer reads this for prompt
            (run_dir / 'eval_metadata.json').write_text(json.dumps({
                'eval_id': case_id,
                'prompt': r['prompt'],
                'split': r.get('split', 'dev'),
            }, indent=2))

            # 2. grading.json — viewer reads this for expectations
            (run_dir / 'grading.json').write_text(json.dumps({
                'case_id': case_id,
                'split': r.get('split', 'dev'),
                'overall_pass': r['overall_pass'],
                'expectations': [
                    {
                        'text': a.get('description', a.get('type', '')),
                        'passed': a['passed'],
                        'evidence': f"{a['type']}: {a.get('value', '')}"
                    }
                    for a in r['assertions']
                ],
                'pass_rate': r.get('pass_rate', 0),
                'passed': r['passed'],
                'total': r['total'],
            }, indent=2))

            # 3. outputs/trace.md — what the skill produced (the trace)
            trace_src = iter_e / 'traces' / f'case-{case_id:03d}.trace.md'
            if trace_src.exists():
                (outputs_dir / 'trace.md').write_text(trace_src.read_text())

            # 4. transcript.md as a backup prompt source
            (run_dir / 'transcript.md').write_text(
                f"## Eval Prompt\n\n{r['prompt']}\n\n"
                f"## Result\n\n{'PASS' if r['overall_pass'] else 'FAIL'} "
                f"({r['passed']}/{r['total']})\n"
            )

        # iteration-level metadata
        (target / 'iteration_metadata.json').write_text(json.dumps({
            'iteration': iter_num,
            'pass_rate': grading.get('pass_rate', 0),
            'n_cases': len(grading.get('results', [])),
        }, indent=2))

    # Report
    print("Converted iterations:")
    for d in sorted(WORKSPACE.glob('iteration-*')):
        if d.is_dir():
            cases = list(d.glob('case_*'))
            print(f"  {d.name}: {len(cases)} cases")

if __name__ == '__main__':
    convert()
