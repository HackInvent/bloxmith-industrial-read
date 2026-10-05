"""FB1/FB2/FB3/FB4/FB5: real protocol servers, wallet, both runtimes and all package origins."""

from contextlib import ExitStack
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT),str(ROOT/"tests"),str(Path(__file__).parent)]
from blocs.industrial_read.block import IndustrialReadBlock
from block_test_packages import install_test_package,prepare_release_run,surface_payload
from industrial_fixture import modbus_server,modbus_config,opcua_server,opcua_config
from ui_smoke_common import (isolated_server,http_json,graph_payload,text_node,data_edge,create_project_api,
    graph_storage_dir,create_run_api,wait_for_run_terminal,wait_for_run_predicate,stop_run_api)


def value(data,port):
    raw=data.get("output_values",{}).get("equipment:"+str(port),{}).get("value")
    return json.loads(raw) if raw else {}


def periodic_at_play(origin):
    """A listener with no data edges must start only at Play and respect its batch cap."""
    with modbus_server() as api,isolated_server() as server:
        cfg=modbus_config(api,start_mode='periodic',max_measurements_per_run=2,stale_after_ms=10000)
        node=IndustrialReadBlock().build_node_payload(node_id='equipment',position={'x':160,'y':160},config_overrides=cfg)
        if origin:
            model=install_test_package(server,'industrial_read',origin=origin);node['block_version']=model['version']
        graph=graph_payload('Periodic without data trigger',[node],[])
        project=create_project_api(server,document=graph)['project']
        rid=prepare_release_run(server,project['graph_id'],graph)['run_id']
        assert api.connections==0 and not api.requests
        try:
            http_json(server.base_url,f'/api/runs/{rid}/play',method='POST',payload={})
            data=wait_for_run_predicate(server,rid,lambda item:item.get('status')=='failed' or value(item,2).get('code')=='measurement_limit',
                'Periodic listener did not start at Play',timeout_sec=15)
            assert data['status']!='failed',data.get('logs')
            assert value(data,2)['measurements']==2 and value(data,1)['points'][0]['value']==118.4
            before=len(api.requests);time.sleep(.4);assert len(api.requests)==before==8
        finally:
            stop_run_api(server,rid);wait_for_run_terminal(server,rid,timeout_sec=15)
    print('[ok] Industrial autonomous Play/limit '+str(origin),flush=True)


def main():
    for protocol in ("modbus_tcp","opcua"):
        for mode in ("centralized","zeromq_active"):
            for origin in (None,"managed","linked"):
                with ExitStack() as stack:
                    root=Path(stack.enter_context(TemporaryDirectory(prefix="industrial-runtime-")))
                    api=stack.enter_context(modbus_server() if protocol=="modbus_tcp" else opcua_server(root))
                    server=stack.enter_context(isolated_server())
                    cfg=(modbus_config(api) if protocol=="modbus_tcp" else opcua_config(api))
                    node=IndustrialReadBlock().build_node_payload(node_id="equipment",position={"x":430,"y":160},config_overrides=cfg)
                    node["inputs"].reverse();node["outputs"].reverse()
                    if origin:
                        model=install_test_package(server,"industrial_read",origin=origin)
                        node["block_version"]=model["version"]
                        for surface in ("modal","inspector_panel","node_card"):
                            rendered=surface_payload(server,model,node,surface)["html"]
                            assert "{{" not in rendered and "fixture-password" not in rendered
                    if protocol=="opcua":
                        http_json(server.base_url,"/api/application/secrets/init",method="POST",payload={"password":"temporary-industrial-test"})
                        http_json(server.base_url,"/api/application/secrets",method="POST",payload={"name":"industrial_fixture","value":api.secret})
                    graph=graph_payload("Read-only industrial fixture",[text_node("command","Commands","",60,160),node],
                        [data_edge("command","command",1,"equipment",1)])
                    project=create_project_api(server,document=graph)["project"]
                    expected=118.4 if protocol=="modbus_tcp" else 23.5
                    def calls():
                        return len(api.requests if protocol=="modbus_tcp" else api.reads)
                    before=calls()
                    if mode=="centralized":
                        def command(obj):
                            graph["nodes"][0]["outputs"][0]["text"]=json.dumps(obj)
                            (graph_storage_dir(server,project)/"graph.json").write_text(json.dumps({**graph,"graph_id":project["graph_id"]}),encoding="utf-8")
                            http_json(server.base_url,f"/api/projects/{project['graph_id']}/graph/reload",method="POST",payload={})
                            run=create_run_api(server,graph,project_id=project["graph_id"],runtime_mode=mode)
                            data=wait_for_run_terminal(server,run["run_id"],timeout_sec=25)
                            assert data["status"]=="success",data.get("logs")
                            assert "fixture-password" not in json.dumps(data)
                            return data
                        assert value(command({"action":"status"}),2)["code"]=="preview"
                        assert calls()==before
                        first=value(command({"action":"read","request_id":"one","points":["temperature"]}),1)
                        assert first["request_id"]=="one" and len(first["points"])==1,first
                        assert first["points"][0]["value"]==expected and first["points"][0]["quality"]["valid"],first
                        before=calls()
                        bad=value(command({"action":"write","value":1}),2)
                        assert bad["code"]=="command_rejected" and calls()==before,bad
                    else:
                        rid=prepare_release_run(server,project["graph_id"],graph)["run_id"]
                        assert calls()==before,"Preparation connected to equipment"
                        http_json(server.base_url,f"/api/runs/{rid}/play",method="POST",payload={})
                        def send(obj):
                            http_json(server.base_url,f"/api/runs/{rid}/active/control",method="POST",payload={"action":"publish_output",
                                "node_id":"command","port_id":1,"value":json.dumps(obj),"content_type":"application/json"})
                        def wait(predicate):
                            data=wait_for_run_predicate(server,rid,lambda item:item.get("status")=="failed" or predicate(item),
                                "Industrial command did not complete",timeout_sec=25)
                            assert data["status"]!="failed",data.get("logs")
                            assert "fixture-password" not in json.dumps(data)
                            return data
                        stopped=False
                        try:
                            send({"action":"status","request_id":"initial"})
                            wait(lambda data:value(data,2).get("request_id")=="initial")
                            assert calls()==before,"On-command Play performed a read"
                            send({"action":"read","request_id":"one","points":["temperature"]})
                            first=value(wait(lambda data:value(data,1).get("request_id")=="one"),1)
                            assert first["points"][0]["value"]==expected,first
                            before=calls()
                            send({"action":"read","request_id":"one","points":["temperature"]})
                            wait(lambda data:value(data,2).get("code")=="duplicate")
                            assert calls()==before
                            expired=value(wait(lambda data:value(data,1).get("event")=="invalidated"),1)
                            assert all(p["value"] is None and not p["quality"]["valid"] for p in expired["points"])
                            send({"action":"start"})
                            wait(lambda data:value(data,2).get("measurements",0)>=3)
                            send({"action":"stop","request_id":"stop-acquisition"})
                            wait(lambda data:value(data,2).get("request_id")=="stop-acquisition")
                            before=calls();time.sleep(.4);assert calls()==before
                            if protocol=="opcua":
                                http_json(server.base_url,"/api/application/secrets/lock",method="POST",payload={})
                                send({"action":"read","request_id":"locked"})
                                wait(lambda data:value(data,2).get("reason")=="credentials")
                                assert calls()==before and not api.writes
                            else:
                                # Stop an actual Run while a server withholds its response.
                                api.mode="hold";api.release.clear()
                                send({"action":"read","request_id":"held"})
                                deadline=time.monotonic()+8
                                while calls()==before and time.monotonic()<deadline:
                                    time.sleep(.02)
                                assert calls()>before
                            started=time.monotonic();stop_run_api(server,rid)
                            ended=wait_for_run_terminal(server,rid,timeout_sec=15);stopped=True
                            assert time.monotonic()-started<8,ended.get("logs")
                            assert "shutdown_timeout" not in str(ended.get("logs"))
                            if protocol=="modbus_tcp":
                                before=calls();api.release.set()
                                deadline=time.monotonic()+3
                                while api.closed<api.connections and time.monotonic()<deadline:
                                    time.sleep(.02)
                                assert api.closed==api.connections and calls()==before,"Connection survived actual Run Stop"
                        finally:
                            if protocol=="modbus_tcp":
                                api.release.set()
                            if not stopped:
                                stop_run_api(server,rid)
                                wait_for_run_terminal(server,rid,timeout_sec=15)
                    if protocol=="opcua":
                        assert not api.writes,"OPC UA business Write was sent"
                print("[ok] Industrial real protocol "+protocol+" "+mode+" "+str(origin),flush=True)
    for origin in (None,'managed','linked'):
        periodic_at_play(origin)


if __name__=="__main__":
    main()
