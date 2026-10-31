"""Optional real-Word acceptance on owned files; never opens a user manuscript."""
import argparse
from datetime import datetime,timezone
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from document_review_ingest import ingest_bytes
from document_review_word import preserve_docx


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--prelaunch',action='store_true',help='Start an owned hidden Word process before COM activation')
    args=parser.parse_args(); directory=args.directory.resolve()
    if directory==ROOT/'dist' or not directory.is_relative_to(ROOT/'dist'):parser.error('Evidence must be a child directory inside repository dist')
    powershell=shutil.which('powershell.exe')
    if not powershell:parser.error('Native Windows Word is required')
    command=[powershell,'-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(ROOT/'scripts/verify_word_hyperlinks.ps1'),'-OutputDirectory',str(directory)]
    if args.prelaunch:command.append('-Prelaunch')
    def invoke(mode):
        attempt={'native_validation_completed':False,'stage':mode,'observed_at':datetime.now(timezone.utc).isoformat()}
        try:
            result=subprocess.run(command+['-Mode',mode],capture_output=True,timeout=90,creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            attempt.update(stage_succeeded=False,reason='Word automation timed out')
            (directory/'native-attempt.json').write_text(json.dumps(attempt,indent=2),encoding='utf-8')
            raise
        attempt.update(stage_succeeded=result.returncode==0,returncode=result.returncode)
        (directory/'native-attempt.json').write_text(json.dumps(attempt,indent=2),encoding='utf-8')
        if result.returncode:
            print((result.stdout+result.stderr).decode('utf-8',errors='replace'))
        result.check_returncode()
    invoke('fixtures')
    raw=(directory/'source.docx').read_bytes();document=ingest_bytes('source.docx',raw)
    cases=[{'id':'surrounding','text':'Better CITATION omega','label':'CITATION'},
           {'id':'label','text':'Alpha CITING omega','label':'CITING'},
           {'id':'boundary','text':'Alpha extra CITATION omega','label':'CITATION'}]
    for case in cases:
        revised=deepcopy(document);revised.blocks[0].text=case['text']
        for kind,tracked in [('clean',False),('tracked',True)]:
            data,_=preserve_docx(raw,document,revised,tracked=tracked)
            (directory/(case['id']+'-'+kind+'.docx')).write_bytes(data)
    (directory/'cases.json').write_text(json.dumps({'source_sha256':hashlib.sha256(raw).hexdigest(),'cases':cases},ensure_ascii=False),encoding='utf-8')
    invoke('verify')
    result=json.loads((directory/'native-verification.json').read_text(encoding='utf-8-sig'))
    assert result['passed'] and len(result['results'])==6
    assert (directory/'source.docx').read_bytes()==raw
    (directory/'native-attempt.json').write_text(json.dumps({'native_validation_completed':True,'passed':True,'observed_at':datetime.now(timezone.utc).isoformat()},indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'word_version':result['word_version'],'word_build':result['word_build'],'native_variants':len(result['results'])}))


if __name__=='__main__':main()
