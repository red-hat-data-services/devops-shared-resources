#!/usr/bin/env python3
"""Verify or publish a trusted adapter's version-1 validated change request."""
import argparse
import json
from pathlib import Path
import sys

# Avoid lib.__init__, which eagerly imports unrelated sync/HTTP dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from validated_pr_publisher import publish, verify


def main():
    """Keep verification opt-in publication separate and emit a JSON receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', type=Path, required=True)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--publish', action='store_true', help='Explicitly allow commits, pushes, and PR writes')
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    if args.publish:
        result = publish(args.path, request)
    else:
        verify(args.path, request)
        result = {'status': 'verified'}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
