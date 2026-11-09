#!/usr/bin/env python3
"""Run an integration tool with locally saved private runtime paths, never secrets in argv."""
import json,os,pathlib,subprocess,sys
root=pathlib.Path(__file__).resolve().parents[1]
p=root/'.private/runtime.json'
if p.exists():os.environ.update(json.loads(p.read_text()))
raise SystemExit(subprocess.call([sys.executable,*sys.argv[1:]],cwd=root))
