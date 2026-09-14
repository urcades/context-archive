"""JSON CLI sharing retrieval implementations with the read-only MCP server."""
import argparse
from dataclasses import asdict
import inspect
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from . import adapters, core, query, runtime

READ_TOOLS=('status','sources','search','timeline','get_record','get_blob','decode_blob','describe_schema','query','app_usage_summary','domain_summary','projects','episodes','project_timeline')


def read_config(path):
    if not path.exists(): return {'sources':[],'exclusions':[]}
    config=json.loads(path.read_text())
    if not isinstance(config,dict) or not isinstance(config.get('sources',[]),list) or not isinstance(config.get('exclusions',[]),list):
        raise ValueError('Config requires sources and exclusions arrays')
    if not all(isinstance(x,str) for x in config.get('exclusions',[])): raise ValueError('Exclusions must be strings')
    return config


def invoke(name,archive,params):
    if name not in READ_TOOLS: raise ValueError('Unknown retrieval tool')
    if not isinstance(params,dict) or 'archive' in params: raise ValueError('Parameters must be an object without archive')
    function=getattr(query,name)
    inspect.signature(function).bind(archive,**params)
    return function(archive,**params)


def doctor(config):
    discovery=adapters.discover(config=config); checks=[]
    for source in discovery['sources']:
        check={**asdict(source),'path':str(source.path)}
        try:
            with core.snapshot(source.path,timeout=2) as c: c.execute('SELECT name FROM sqlite_schema LIMIT 1').fetchall()
            check['readable']=True
        except (OSError,sqlite3.Error,TimeoutError,ValueError) as exc:
            check.update(readable=False,error=str(exc))
        checks.append(check)
    return {'permission_executable':str(Path(sys.executable).resolve()),'runtime_executable':runtime.interpreter(),
            'sources':checks,'issues':discovery['issues'],'background':runtime.background_status(),
            'note':'Readability is measured in this process only; it does not establish background Full Disk Access or sync completeness.'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    common=argparse.ArgumentParser(add_help=False)
    common.add_argument('--archive',default=argparse.SUPPRESS,help='Archive SQLite path (default: CONTEXT_ARCHIVE_HOME/data/archive.sqlite3)')
    common.add_argument('--config',default=argparse.SUPPRESS,help='Discovery JSON (default: CONTEXT_ARCHIVE_HOME/config.json)')
    for action in common._actions:
        if action.dest!='help': parser._add_action(action)
    commands=parser.add_subparsers(dest='command',required=True)
    for name in READ_TOOLS:
        sub=commands.add_parser(name.replace('_','-'),aliases=[name] if '_' in name else [],parents=[common])
        sub.add_argument('--params',default='{}',help=f'JSON keyword arguments for {name}{inspect.signature(getattr(query,name))}; archive is supplied separately')
    for name in ('doctor','capture','reindex','uninstall','mcp'): commands.add_parser(name,parents=[common])
    for name in ('export','backup'):
        commands.add_parser(name,parents=[common]).add_argument('destination',type=Path)
    editor=commands.add_parser('put-project',parents=[common]); editor.add_argument('definition',type=Path)
    imp=commands.add_parser('import',parents=[common]); imp.add_argument('kind',choices=['knowledge','browser']); imp.add_argument('path',type=Path)
    install=commands.add_parser('install',parents=[common]); install.add_argument('--no-start',action='store_true'); install.add_argument('--codex',action='store_true')
    args=parser.parse_args(argv)
    try:
        root=runtime.home(); archive=Path(getattr(args,'archive',root/'data/archive.sqlite3')).expanduser().resolve()
        config_path=Path(getattr(args,'config',root/'config.json')).expanduser().resolve()
        name=args.command.replace('-','_')
        if name in READ_TOOLS: result=invoke(name,archive,json.loads(args.params))
        elif name=='put_project':
            from .correlation import put_project
            result=put_project(archive,json.loads(args.definition.read_text()))
        elif name=='doctor': result=doctor(read_config(config_path))
        elif name=='capture':
            discovered=adapters.discover(config=read_config(config_path))
            core.protect(archive,[s.path for s in discovered['sources']])
            result=core.collect(archive,discovered['sources'],discovered['issues'])
        elif name=='reindex': result=core.reindex(archive)
        elif name=='import':
            from . import importers
            result=getattr(importers,'import_'+args.kind)(args.path,archive)
        elif name in ('export','backup'):
            from . import transfer
            result=getattr(transfer,'export_archive' if name=='export' else name)(archive,args.destination)
        elif name=='install': result=runtime.install(archive,config_path,args.no_start,args.codex)
        elif name=='uninstall': result=runtime.uninstall()
        elif name=='mcp':
            from .mcpserver import create_server
            create_server(archive).run(); return 0
        print(json.dumps(result,ensure_ascii=True,default=str))
        return 1 if name=='capture' and result['failures'] else 0
    except (ValueError,TypeError,OSError,sqlite3.Error,TimeoutError,subprocess.SubprocessError,RuntimeError) as exc:
        print(json.dumps({'error':str(exc)}),file=sys.stderr); return 2


if __name__=='__main__': raise SystemExit(main())
