#!/usr/bin/env python3
"""Check actual lab credential values against public artifacts without printing them."""
import base64,json,pathlib,re,subprocess,sys
from lab import ROOT,get,guard,save

def main():
 guard();sensitive=[]
 for secret in (get('secrets') or {'items':[]})['items']:
  for key,val in secret.get('data',{}).items():
   if key in {'client-secret','password','token','tls.key'}:
    raw=base64.b64decode(val)
    if len(raw)>=12:sensitive.append(raw)
 files=[]
 for path in ROOT.rglob('*'):
  if not path.is_file() or any(p in {'.private','.git','__pycache__'} for p in path.relative_to(ROOT).parts):continue
  files.append(path)
 matches=[]
 for p in files:
  raw=p.read_bytes()
  if any(value in raw for value in sensitive):matches.append(str(p.relative_to(ROOT)))
 staged=subprocess.check_output(['git','diff','--cached','--binary'],cwd=ROOT)
 if any(value in staged for value in sensitive):matches.append('STAGED_DIFF')
 result={'status':'FAIL' if matches else 'PASS','checked_files':len(files),'checked_actual_credentials':len(sensitive),'matches':matches,'method':'exact sensitive values from owned namespace Secrets; no values stored','scope':'public repo artifacts and staged diff; runtime logs collected separately'}
 save('evidence/secret-scan.json',result);print(json.dumps(result));return 1 if matches else 0
if __name__=='__main__':sys.exit(main())
