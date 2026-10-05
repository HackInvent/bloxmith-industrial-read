"""FB1/FB2/FB3/FB4: actual local Modbus/secure OPC UA, calibration, expiry, failures and Stop."""

from pathlib import Path
import json
import selectors
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT),str(ROOT/"tests"),str(Path(__file__).parent)]
from blocs.industrial_read import logic,engine
from industrial_fixture import modbus_server,modbus_config,opcua_server,opcua_config,certificate


def refused(action):
    try:
        action()
    except logic.IndustrialError:
        return
    raise AssertionError("Unsafe request/configuration was accepted")


def wait(listener,events,predicate,timeout=8):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        listener.tick(time.monotonic())
        if any(predicate(event) for event in events):
            return next(event for event in reversed(events) if predicate(event))
        time.sleep(.01)
    raise AssertionError(events)


def read(listener,events,ident,points=None):
    while time.monotonic()<listener.min_next:
        listener.tick(time.monotonic());time.sleep(.01)
    item={"action":"read","request_id":ident}
    if points:
        item["points"]=points
    listener.command(logic.command(item,listener.cfg))
    return wait(listener,events,lambda event:event.get("measurements",{}).get("request_id")==ident)["measurements"]


def parent_death(api):
    """The helper must also die when a package host is killed before finally can run."""
    script='''import json,sys,time,types
from pathlib import Path
package=types.ModuleType("owned_test");package.__path__=[sys.argv[1]];sys.modules["owned_test"]=package
from owned_test import engine,logic
cfg=logic.configuration(json.loads(sys.argv[2]));child=engine.Connection(cfg,None)
while True:
    for event in child.poll():
        if event.get("type")=="ready":
            child.send({"action":"read"});print(child.process.pid,flush=True)
    time.sleep(.01)
'''
    cfg=modbus_config(api);api.mode="hold";api.release.clear();before=len(api.requests)
    parent=subprocess.Popen([sys.executable,"-B","-c",script,str(Path(__file__).parents[1]),json.dumps(cfg)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    selector=selectors.DefaultSelector();selector.register(parent.stdout,selectors.EVENT_READ)
    try:
        assert selector.select(timeout=8),"Owned parent did not create its helper"
        child=int(parent.stdout.readline())
        deadline=time.monotonic()+3
        while len(api.requests)==before and time.monotonic()<deadline:
            time.sleep(.01)
        assert len(api.requests)>before
        parent.kill();parent.wait(timeout=2)
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            status=Path('/proc')/str(child)/'status'
            if not status.exists() or 'State:\tZ' in status.read_text():
                break
            time.sleep(.01)
        else:
            raise AssertionError("Connection helper survived abrupt parent death")
    finally:
        selector.close()
        if parent.poll() is None:
            parent.kill();parent.wait(timeout=2)
        parent.stdout.close();parent.stderr.close();api.release.set();api.mode="ok"


def main():
    for raw in ({"write":True},{"enabled":True},{"points":[{"id":"x","area":"holding","address":1,"type":"bool"}]},
        {"endpoint":"tcp://user:password@localhost:502"},{"interval_ms":1},{"unit_id":0},
        {"credential_ref":"password"},{"protocol":"opcua","opcua_security":"none","credential_ref":"secret://workspace/private"}):
        refused(lambda:logic.configuration(raw))
    cfg=logic.configuration({})
    for raw in ({"action":"write"},{"action":"read","endpoint":"tcp://other:502"},{"action":"read","points":["unknown"]}):
        refused(lambda:logic.command(raw,cfg))
    refused(lambda:logic.document('{"action":"read","action":"stop"}'))
    with modbus_server() as api:
        cfg=logic.configuration(modbus_config(api));events=[];listener=engine.Listener(cfg,events.append)
        try:
            assert api.connections==0
            result=read(listener,events,"first")
            values={p["id"]:p for p in result["points"]}
            assert values["temperature"]["value"]==118.4 and values["pressure"]["value"]==12.5,values
            assert values["running"]["value"] is True and values["ready"]["value"] is False
            assert all(v["quality"]["valid"] and not v["source_age_known"] for v in values.values())
            assert {entry[0] for entry in api.requests}=={1,2,3,4}
            before=len(api.requests);listener.command({"action":"read","request_id":"first"})
            assert events[-1]["status"]["code"]=="duplicate" and len(api.requests)==before
            wait(listener,events,lambda event:event.get("measurements",{}).get("event")=="invalidated")
            assert events[-1]["status"]["code"]=="stale"
            for mode in ("exception","nonfinite"):
                api.mode=mode
                data=read(listener,events,mode,["pressure"])
                assert data["points"][0]["value"] is None and not data["points"][0]["quality"]["valid"]
            api.mode="wrong_tid"
            while time.monotonic()<listener.min_next:
                time.sleep(.01)
            listener.command({"action":"read","request_id":"protocol"})
            wait(listener,events,lambda event:event.get("status",{}).get("reason")=="protocol_error")
            assert listener.child is None and not listener.periodic
            api.mode="ok";read(listener,events,"recovered")
            api.mode="hold";api.release.clear()
            while time.monotonic()<listener.min_next:
                time.sleep(.01)
            listener.command({"action":"read","request_id":"held"});listener.tick(time.monotonic())
            process=listener.child.process
            started=time.monotonic();listener.command({"action":"stop"})
            assert time.monotonic()-started<1 and process.poll() is not None
            assert listener.child is None and not listener.fresh
        finally:
            listener.close();api.release.set()
        # An unreachable target consumes the configured finite reconnect budget.
        api.mode="drop";events=[]
        listener=engine.Listener(logic.configuration(modbus_config(api,start_mode="periodic",reconnect_delay_ms=250,reconnect_attempts=2)),events.append)
        try:
            wait(listener,events,lambda event:event.get("status",{}).get("retry_scheduled") is False)
            assert listener.retries==2 and not listener.periodic and listener.child is None
        finally:
            listener.close()
        # All declared register widths, signedness and the four byte/word layouts.
        api.mode="ok";points=[];expected={};address=100
        values={"int16":-32768,"uint16":65535,"int32":-2147483648,"uint32":4294967295,
            "int64":-9223372036854775808,"uint64":18446744073709551615,"float32":12.5,"float64":-123.25}
        for kind,value in values.items():
            for byte_order in ("big","little"):
                for word_order in ("big","little"):
                    ident=kind+'-'+byte_order+'-'+word_order
                    raw=struct.pack('>'+logic.TYPES[kind][0],value);words=[raw[i:i+2] for i in range(0,len(raw),2)]
                    if byte_order=='little':
                        words=[word[::-1] for word in words]
                    if word_order=='little':
                        words.reverse()
                    for index,word in enumerate(words):
                        api.values[address+index]=int.from_bytes(word,'big')
                    points.append({"id":ident,"area":"holding","address":address,"type":kind,
                        "byte_order":byte_order,"word_order":word_order})
                    expected[ident]=value;address+=4
        events=[];listener=engine.Listener(logic.configuration(modbus_config(api,points=points)),events.append)
        try:
            measured=read(listener,events,'all-formats')
            assert {p['id']:p['value'] for p in measured['points']}==expected,measured
        finally:
            listener.close()
        events=[];listener=engine.Listener(logic.configuration(modbus_config(api,start_mode='periodic',max_measurements_per_run=2)),events.append)
        try:
            wait(listener,events,lambda event:event.get('status',{}).get('code')=='measurement_limit')
            before=len(api.requests);listener.tick(time.monotonic()+10)
            assert listener.count==2 and not listener.periodic and listener.child is None and len(api.requests)==before
        finally:
            listener.close()
        api.mode='hold';api.release.clear();events=[]
        listener=engine.Listener(logic.configuration(modbus_config(api,read_timeout_ms=100)),events.append)
        try:
            listener.command({'action':'read','request_id':'timeout'})
            wait(listener,events,lambda event:event.get('status',{}).get('reason')=='timeout')
            assert listener.child is None and not listener.fresh
            invalid=[e['measurements'] for e in events if 'measurements' in e]
            assert invalid and all(p['value'] is None for p in invalid[-1]['points'])
        finally:
            listener.close();api.release.set();api.mode='ok'
        parent_death(api)
    with TemporaryDirectory(prefix="industrial-opcua-") as temporary,opcua_server(Path(temporary)) as api:
        cfg=logic.configuration(opcua_config(api));events=[]
        listener=engine.Listener(cfg,events.append,lambda ref:api.secret)
        try:
            result=read(listener,events,"secure")
            assert result["points"][0]["value"]==23.5 and result["points"][0]["source_age_known"],result
            assert result["points"][1]["value"] is True and not api.writes
            for mode in ("stale","future","missing_time","bad","uncertain","array"):
                api.mode=mode
                data=read(listener,events,mode)
                assert all(p["value"] is None and not p["quality"]["valid"] for p in data["points"]),data
            assert not api.writes
        finally:
            listener.close()
        api.mode="ok";before=len(api.reads)
        wrong,_=certificate("urn:fixture:other-server",True)
        events=[];listener=engine.Listener({**cfg,"server_certificate_pem":wrong},events.append,lambda ref:api.secret)
        try:
            listener.command({"action":"read","request_id":"wrong-cert"})
            wait(listener,events,lambda event:event.get("status",{}).get("code")=="read_error",timeout=12)
            assert len(api.reads)==before and not any("fixture-password" in str(item) for item in events)
        finally:
            listener.close()
    with TemporaryDirectory(prefix="industrial-anonymous-") as temporary,opcua_server(Path(temporary),secure=False) as api:
        cfg=logic.configuration(opcua_config(api,opcua_security='none',credential_ref='',allow_insecure=True))
        events=[];listener=engine.Listener(cfg,events.append)
        try:
            data=read(listener,events,'explicit-anonymous')
            assert data['points'][0]['value']==23.5 and not api.writes,data
        finally:
            listener.close()
    print("[ok] Real read-only Modbus and encrypted OPC UA, exact certificate pinning, quality, stale invalidation, retry bounds and Stop")


if __name__=="__main__":
    main()
