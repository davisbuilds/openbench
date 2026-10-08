#!/usr/bin/env python3
"""Build a task-free browser variant from an explicit immutable developer image."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid

ROOT=Path(__file__).resolve().parents[2]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-image',required=True)
    parser.add_argument('--receipt',required=True,type=Path)
    parser.add_argument('--tag',default='openbench-local/browser-runtime:dev-v1')
    args=parser.parse_args()
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',args.base_image): parser.error('base must be a local immutable image ID')
    if args.receipt.exists(): parser.error('receipt already exists; preserve prior evidence')
    inspected=json.loads(subprocess.check_output(['docker','image','inspect',args.base_image]))[0]
    if inspected['Id']!=args.base_image or inspected['Config'].get('Labels',{}).get('org.openbench.developer-environment')!='3':
        parser.error('base must be developer runtime v3')
    tag='openbench-local/browser-build-base:'+uuid.uuid4().hex
    subprocess.run(['docker','tag',args.base_image,tag],check=True)
    try:
        with tempfile.TemporaryDirectory(prefix='obench-browser-build-') as temp:
            root=Path(temp)
            for name in ('Dockerfile','package.json','package-lock.json'):
                shutil.copyfile(ROOT/'docker/browser-runtime'/name,root/name)
            shutil.copyfile(ROOT/'obench/sandbox_gateway.py',root/'sandbox_gateway.py')
            for p in root.iterdir(): p.chmod(0o644)
            hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}
            iid=root/'image-id.txt'
            subprocess.run(['docker','build','--build-arg','BASE_IMAGE='+tag,'--tag',args.tag,'--iidfile',str(iid),str(root)],check=True)
            image=iid.read_text().strip()
            args.receipt.parent.mkdir(parents=True,exist_ok=True)
            with args.receipt.open('x') as output:
                json.dump({'image_id':image,'base_image_id':args.base_image,'inputs':hashes,
                           'status':'built; browser and harness qualification required'},output,indent=2)
            print(image)
    finally:
        subprocess.run(['docker','image','rm',tag],check=True,stdout=subprocess.DEVNULL)


if __name__=='__main__':main()
