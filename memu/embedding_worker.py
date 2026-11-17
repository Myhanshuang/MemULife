"""Private, short-lived encoding worker. No network listener or task retries."""

import argparse
from contextlib import redirect_stdout
import json
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-path', required=True)
    parser.add_argument('--threads', type=int, default=2)
    args = parser.parse_args()
    model = None
    output = sys.stdout
    try:
        for line in sys.stdin:
            try:
                items = json.loads(line)['items']
                with redirect_stdout(sys.stderr):
                    if model is None:
                        from .gemma_embedding import LocalGemmaEmbedding
                        model = LocalGemmaEmbedding(args.model_path, threads=args.threads)
                    embeddings = model.embed(items)
                result = {'embeddings': embeddings}
            except Exception as error:
                result = {'error': type(error).__name__}
            output.write(json.dumps(result, ensure_ascii=False) + '\n')
            output.flush()
    finally:
        if model is not None:
            model.close()


if __name__ == '__main__':
    main()
