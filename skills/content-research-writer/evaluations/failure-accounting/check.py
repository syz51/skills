"""Synthetic executable comparison; no claim about original private protocol outcomes."""
import argparse
import json

parser = argparse.ArgumentParser()
parser.add_argument('--variant', choices=('baseline', 'candidate', 'contrast'), required=True)
args = parser.parse_args()
# Eligibility was fixed before observing outcomes. Eligible failed attempts
# belong to this estimand; ineligible cases belong to neither denominator.
cases = [
    {'eligible': True, 'completed': True, 'success': True},
    {'eligible': True, 'completed': True, 'success': True},
    {'eligible': True, 'completed': False, 'success': False},
    {'eligible': True, 'completed': True, 'success': False},
    {'eligible': False, 'completed': False, 'success': False},
]
eligible = [case for case in cases if case['eligible']]
analyzed = ([case for case in eligible if case['completed']]
            if args.variant == 'baseline' else eligible)
rate = sum(case['success'] for case in analyzed) / len(analyzed)
checks = [
    {'name': 'declared eligible denominator is preserved', 'passed': len(analyzed) == 4},
    {'name': 'failed eligible attempt affects the estimate', 'passed': rate == 0.5},
    {'name': 'prespecified ineligible exclusion remains excluded', 'passed': all(case['eligible'] for case in analyzed)},
]
if args.variant == 'contrast':
    # A legitimate exclusion is preserved even when that case would improve
    # the result if opportunistically included after its outcome was known.
    changed = [{**case, 'success': True} if not case['eligible'] else case for case in cases]
    selected = [case for case in changed if case['eligible']]
    checks.append({'name': 'ineligible outcome cannot change the declared estimate',
                   'passed': sum(case['success'] for case in selected) / len(selected) == rate})
result = {'variant': args.variant, 'analyzed': len(analyzed), 'success_rate': rate,
          'checks': checks, 'passed': all(check['passed'] for check in checks),
          'scope': 'Synthetic calculation for a declared estimand; not a universal inclusion policy or proof of guidance benefit.'}
print(json.dumps(result))
raise SystemExit(0 if result['passed'] else 1)
