"""Install our local research package, then exercise the actual native adapter offline."""
import asyncio, hashlib,json,os,subprocess,sys,time
from pathlib import Path
import argparse
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--research',type=Path,required=True)
parser.add_argument('--demo',action='store_true',help='Also assemble and compile an offline demonstration paper; no model calls')
args=parser.parse_args()
root=args.research.resolve().parent
os.environ['JIUWENSWARM_HOME']=str(root/'runtime-home')
os.environ['JIUWENSWARM_DATA_DIR']=str(root/'runtime-home/.jiuwenswarm')
os.environ['RESEARCH_WORKSPACE']=str(root/'research')
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness import DeepAgent,DeepAgentConfig
from openjiuwen.harness.rails import SkillUseRail
from jiuwenswarm.server.runtime import extension_package_manager as catalog
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.research_workbench.pipeline import sha,put,read,run

async def main():
    started=time.monotonic()
    demonstration=None
    module=Path(__file__).parent
    source=module/'native_plugin';pid='research-evidence-offline'
    before=sha(root/'research/workspace.json')
    existing=catalog.show_plugin_package(pid)
    if not existing:
        item=catalog.import_plugin_package({'path':str(source)})
        catalog.install_plugin_package(item)
    else:
        local=catalog.resolve_plugin_dir(pid)
        for p in source.rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts:
                assert sha(p)==sha(local/p.relative_to(source)),'Installed plugin differs; do not overwrite'
    assert catalog.is_plugin_allowed(pid)
    manager=SkillManager(workspace_dir=str(get_agent_workspace_dir()))
    dest=get_agent_workspace_dir()/'skills/research-evidence-workflow/SKILL.md'
    if not dest.exists():
        imported=await manager.handle_skills_import_local({'path':str(module/'native_skill')})
        assert imported['success'],imported
    assert sha(dest)==sha(module/'native_skill/SKILL.md')
    adapter=object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance=DeepAgent(AgentCard(name='competition-native-offline')).configure(
        DeepAgentConfig(enable_task_loop=False,rails=[SkillUseRail(skills_dir=[],include_tools=False)]))
    adapter._loaded_plugins={}
    await adapter._load_plugins_for_request({'plugin_names':[pid]})
    record=adapter._loaded_plugins[pid][0]
    ref=next(r for r in record.refs if r.kind.value=='tool')
    tool=Runner.resource_mgr.get_tool(ref.identity)
    assert tool is not None
    try:
        audit=await tool.invoke({'action':'audit'})
        prepared=await tool.invoke({'action':'prepare'})
        assert audit['resources']['verified_artifacts']==1440
        assert prepared['resources']['requests']==384
        expected = run(root/'research')
        assert prepared['statistics'] == expected['statistics']
        manifest=read(Path(prepared['manifest_path']))
        for name,h in manifest.items():assert sha(Path(prepared['report_path']).parent/name)==h
        invalid=False
        try:await tool.invoke({'action':'run_experiment'})
        except ValueError:invalid=True
        assert invalid
        if args.demo:
            draft=await tool.invoke({'action':'draft'})
            paper=Path(draft['paper_source']).parent
            compiled=subprocess.run([sys.executable,'-X','utf8',str(paper/'build.py'),'--research',str(root/'research')],cwd=paper,capture_output=True,text=True,timeout=660)
            put(paper/'demo-compile.log',compiled.stdout+compiled.stderr)
            if compiled.returncode:
                raise RuntimeError('Offline PDF compilation failed; see '+str(paper/'demo-compile.log'))
            from jiuwenswarm.research_workbench.paper_artifacts import verify_inputs
            verified=verify_inputs(paper,root/'research',True)
            assert verified['valid']
            demonstration={'paper_directory':str(paper),'paper_sha256':sha(paper/'paper.pdf'),'paper_verification':verified,
                'stages':['native plugin load','audit frozen experiment','prepare statistics','assemble manuscript and figures','offline compile','verify PDF binding'],
                'reuses_historical_experiment':True,'new_model_calls':0,'elapsed_seconds':round(time.monotonic()-started,3),
                'human_steps':['research question and protocol design','source checks and user acceptance of scoring','author review and external Reviewer evaluation']}
            put(paper/'demo-verification.json',demonstration)
    finally:
        await adapter._instance.unload_extension(record)
    assert Runner.resource_mgr.get_tool(ref.identity) is None
    assert before==sha(root/'research/workspace.json')
    report={'native_catalog_installed':True,'native_skill_installed':True,
        'unmodified_JiuWenSwarmDeepAdapter_loaded_plugin':True,'real_DeepAgent_tool_invoked':True,
        'tool_unloaded':True,'paid_action_rejected':True,'workspace_unchanged':True,
        'new_api_calls':0,'verified_artifacts':1440,'manifest_files_checked':len(manifest),
        'output_report':prepared['report_path'],'plugin_dir':str(catalog.resolve_plugin_dir(pid)),
        'skill_path':str(dest),'human_reviews':prepared['human_reviews'],
        'review_basis':prepared['statistics']['review_basis'],
        'primary_effect':prepared['statistics']['primary_effect'],
        'limits':'Native tool dispatch and deterministic material generation verified. No LLM planning, Team autonomous research, fresh experiments, or final paper generation validated.'}
    if demonstration:
        report['demonstration']=demonstration
        put(root/'research/demo-latest.json',report)
    else:
        put(root/'research/native-verification.json',report)
    print(json.dumps(report,ensure_ascii=False))

asyncio.run(main())
