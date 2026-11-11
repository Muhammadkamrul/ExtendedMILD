#!/usr/bin/env python3
"""
run_all.py -- one command to reproduce every result table of the MILD paper on
all three datasets, with resumable execution, CSV tables, a paper-vs-reproduced
comparison, paired significance tests and an auto-written REPORT.md.

Typical use (from the package folder):

  python prepare_data.py --microservices <zip> --sdn <zip>      # once
  python run_all.py                                              # everything
  python run_all.py --groups main                                # only Tables V-XI
  python run_all.py --datasets sdn --groups main,ablation        # subset
  python run_all.py --report_only                                # rebuild tables
  python run_all.py --list                                       # show the job plan

Groups:
  main                 MILD + MLP + LSTM + WKPI/Dist/LR on each dataset (Tables V-XI)
  ablation             ablation variants as launched in the notebooks (Tables XII-XIV)
  ablation_consistent  ablation variants around the main configuration
  sensitivity          alpha and w_c sweeps (Tables XV-XVII)
  opintel              multi-horizon urgency + SHAP explanations (Sec. V-E)
  seeds                main runs repeated with --extra_seeds (run-to-run variability)

Interrupt at any time (Ctrl+C). Re-running the same command resumes: finished
jobs (folder contains DONE) are skipped, and a job that was interrupted is
restarted from its first fold (or from the last finished fold with
--fold_resume, which can change the random-number stream of later folds).
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mild_repro import jobs as J  # noqa: E402
from mild_repro import paper_config as P  # noqa: E402

ALL_DATASETS = ['statistical', 'microservices', 'sdn']
DEFAULT_GROUPS = 'main,ablation,sensitivity,opintel'
ORDER = {'mild': 0, 'classic': 1, 'mlp': 2, 'lstm': 3, 'multihorizon': 4}


def log(msg, fh):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    fh.write(line + '\n')
    fh.flush()


def write_progress(out, plan, state):
    with open(os.path.join(out, 'progress.json'), 'w') as f:
        json.dump({'updated': time.strftime('%Y-%m-%d %H:%M:%S'), 'total_jobs': len(plan),
                   'done': sum(1 for s in state.values() if s == 'done'),
                   'failed': [k for k, s in state.items() if s.startswith('failed')],
                   'jobs': state}, f, indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=os.path.join(HERE, 'results'))
    ap.add_argument('--data_root', default=HERE)
    ap.add_argument('--datasets', default=','.join(ALL_DATASETS))
    ap.add_argument('--groups', default=DEFAULT_GROUPS)
    ap.add_argument('--extra_seeds', default='43,44,45,46', help='used only by the "seeds" group')
    ap.add_argument('--retries', type=int, default=1)
    ap.add_argument('--fold_resume', action='store_true')
    ap.add_argument('--report_only', action='store_true')
    ap.add_argument('--list', action='store_true')
    a = ap.parse_args()

    datasets = [d.strip() for d in a.datasets.split(',') if d.strip()]
    groups = [g.strip() for g in a.groups.split(',') if g.strip()]
    seeds = [42] + ([int(s) for s in a.extra_seeds.split(',')] if 'seeds' in groups else [])
    os.makedirs(a.out, exist_ok=True)
    plan = J.build_jobs(a.out, datasets, groups, seeds=seeds, fold_resume=a.fold_resume)
    plan.sort(key=lambda j: (ALL_DATASETS.index(j['dataset']), 0 if any(r.startswith('main') for r in j['roles']) else 1,
                             ORDER[j['method']]))
    with open(os.path.join(a.out, 'job_plan.json'), 'w') as f:
        json.dump(plan, f, indent=2)
    fh = open(os.path.join(a.out, 'orchestrator.log'), 'a')

    if a.list:
        for j in plan:
            done = os.path.exists(os.path.join(j['outdir'], 'DONE'))
            print(f"{'[done]' if done else '[todo]'} {j['id']:45s} roles={','.join(j['roles'])}")
        return

    # check datasets
    for ds in datasets:
        for k in ('csv', 'events'):
            p = os.path.join(a.data_root, P.DATASETS[ds][k])
            if not os.path.exists(p):
                log(f'ERROR: missing {p}. Run prepare_data.py first.', fh)
                sys.exit(2)

    state = {}
    if not a.report_only:
        log(f'Plan: {len(plan)} unique jobs for datasets={datasets} groups={groups}', fh)
        try:
            for n, j in enumerate(plan, 1):
                if os.path.exists(os.path.join(j['outdir'], 'DONE')):
                    state[j['id']] = 'done'
                    continue
                os.makedirs(j['outdir'], exist_ok=True)
                spec = dict(j, data_root=a.data_root)
                spec_path = os.path.join(j['outdir'], 'spec.json')
                with open(spec_path, 'w') as f:
                    json.dump(spec, f, indent=2)
                ok = False
                for attempt in range(1 + a.retries):
                    log(f'[{n}/{len(plan)}] START {j["id"]} ({",".join(j["roles"])}) attempt {attempt + 1}', fh)
                    t0 = time.time()
                    with open(os.path.join(j['outdir'], 'job.log'), 'a') as jl:
                        rc = subprocess.call([sys.executable, '-u', '-m', 'mild_repro.jobs', '--spec', spec_path],
                                             cwd=HERE, stdout=jl, stderr=subprocess.STDOUT,
                                             env=dict(os.environ, TF_CPP_MIN_LOG_LEVEL='3'))
                    dt = (time.time() - t0) / 60
                    if rc == 0 and os.path.exists(os.path.join(j['outdir'], 'DONE')):
                        log(f'[{n}/{len(plan)}] DONE  {j["id"]} in {dt:.1f} min', fh)
                        ok = True
                        break
                    log(f'[{n}/{len(plan)}] FAIL  {j["id"]} rc={rc} after {dt:.1f} min (see {j["outdir"]}/job.log)', fh)
                state[j['id']] = 'done' if ok else 'failed'
                write_progress(a.out, plan, state)
                # rebuild tables after every finished job, so partial results are always visible
                try:
                    from mild_repro import report
                    report.build(a.out, plan, datasets)
                except Exception as exc:
                    log(f'(report refresh skipped: {exc})', fh)
        except KeyboardInterrupt:
            log('Interrupted by user. Re-run the same command to resume.', fh)
            write_progress(a.out, plan, state)
            sys.exit(130)

    from mild_repro import report
    cells = report.build(a.out, plan, datasets)
    write_progress(a.out, plan, {j['id']: ('done' if os.path.exists(os.path.join(j['outdir'], 'DONE')) else 'todo')
                                 for j in plan})
    if cells is not None and not cells.empty:
        c = cells['status'].value_counts().to_dict()
        log(f'Comparison with the paper: {c}', fh)
    log(f'Tables: {os.path.join(a.out, "tables")}   Significance: {os.path.join(a.out, "significance")}', fh)
    log(f'Read {os.path.join(a.out, "REPORT.md")} for the interpretation.', fh)


if __name__ == '__main__':
    main()
