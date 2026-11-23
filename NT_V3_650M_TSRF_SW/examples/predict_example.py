"""Run: python examples/predict_example.py INPUT OUTPUT (paths use caller CWD)."""
import argparse
import json
from incfinder import IncFinderError, predict


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input')
    parser.add_argument('output')
    parser.add_argument('--model-dir', help='External models directory; relative to caller CWD')
    args = parser.parse_args()
    try:
        summary = predict(input=args.input, output=args.output, model_dir=args.model_dir, device='cpu',
                          allow_encoder_drift=True)
    except IncFinderError as exc:
        parser.exit(2, f'{exc}\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
