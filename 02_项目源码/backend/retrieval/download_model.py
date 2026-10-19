"""Explicit reproducible model repair, never invoked during application startup.

Usage: python -m backend.retrieval.download_model --model-dir models/multilingual-e5-small
The included manifest pins all assets. No endpoint configuration or keys used.
"""
import argparse
import hashlib
import json
import urllib.request
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir',type=Path,required=True)
    args=parser.parse_args()
    manifest=json.loads((args.model_dir/'manifest.json').read_text())
    urls={'LICENSE':'https://raw.githubusercontent.com/microsoft/unilm/master/LICENSE',
          'UPSTREAM_MODEL_CARD.md':f"https://huggingface.co/{manifest['upstream']}/raw/{manifest['upstream_revision']}/README.md"}
    for name,expected in manifest['files'].items():
        target=args.model_dir/name
        if not target.resolve().is_relative_to(args.model_dir.resolve()):
            raise ValueError('Invalid model manifest path')
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==expected['sha256']:
            print(name,'verified')
            continue
        target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_suffix(target.suffix+'.part')
        url=urls.get(name,f"https://huggingface.co/{manifest['model']}/resolve/{manifest['revision']}/{name}")
        try:
            digest=hashlib.sha256();size=0
            with urllib.request.urlopen(url,timeout=120) as response, temporary.open('wb') as output:
                while block:=response.read(1024*1024):
                    output.write(block);digest.update(block);size+=len(block)
            if digest.hexdigest()!=expected['sha256'] or size!=expected['bytes']:
                raise ValueError('Downloaded asset checksum mismatch: '+name)
            temporary.replace(target)
            print(name,'verified')
        finally:
            temporary.unlink(missing_ok=True)


if __name__=='__main__':main()
